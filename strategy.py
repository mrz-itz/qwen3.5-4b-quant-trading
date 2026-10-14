# -*- coding: utf-8 -*-
"""决策:把实时行情文本 + 账户状态喂给 Qwen3.5-4B,要求返回严格 JSON。

JSON 契约:
  {"action":"buy|sell|hold","shares":N,"confidence":0~1,"reason":"一句话中文理由"}
  - buy 的 shares 为本笔拟买股数(100 整数倍)
  - sell 的 shares 为拟卖股数(≤当前持仓)
  - hold 时 shares=0
"""
from __future__ import annotations
import json
import re

import config
import model_client

SYSTEM = (
    "你是一名激进而理性的 A 股短线交易助手,管理同花顺「模拟炒股」账户(虚拟资金,请像实盘一样认真,"
    "让策略结果有参考价值)。输入:某只股票的实时行情与技术指标文本,以及你当前持仓。"
    "目标:在 A 股规则内【收益最大化】——让资金尽量处于增值状态,别让现金长期空转。"
    "操作取向:①方向(相对均线位置、MACD/RSI、量能、动量)与账户约束都支持时,果断出手,不必等十全十美;"
    "②对强势票顺势持有,对走弱/破位/超买回落/止盈到位的果断减仓或清仓,及时轮动;"
    "③不追已涨停封板或临近涨停的票(买不进),也不在跌停封死时强行卖出(卖不出),其余按你的判断大胆执行。"
    "仓位纪律:④buy 单笔金额不超过可用资金的 {cash_pct}%,且该股买入后总市值不超过资产 {pos_pct}%,"
    "不要重仓押注单只;⑤sell 只卖你当前可卖的持仓(A股 T+1:今日买入今日不可卖);"
    "⑥shares 必须是 100 的整数倍且 >0(hold 则为 0);"
    "⑦出现明确止盈/止损信号时不要犹豫。"
    "必须且只能输出一个 JSON 对象,不要输出任何解释、不要用 Markdown 代码块,不要出现 JSON 以外的文字。"
)


def _fmt(v):
    return "None" if v is None else f"{v:.2f}"


def account_text(holdings: dict[str, dict], cash: float | None,
                 total_asset: float | None) -> str:
    lines = []
    if cash is not None:
        lines.append(f"可用资金 {cash:,.2f} 元")
    if total_asset is not None:
        lines.append(f"账户总资产 {total_asset:,.2f} 元")
    if holdings:
        for c, p in holdings.items():
            sell = int(p.get('sellable', p.get('qty', 0)))
            lines.append(f"持仓 {p.get('name', c)}({c}) {int(p['qty'])}股"
                         f"(可卖{sell}股) "
                         f"成本{_fmt(p.get('cost'))} 现价{_fmt(p.get('price'))} "
                         f"市值≈{p['qty']*(p.get('price') or p.get('cost') or 0):,.0f}元")
    else:
        lines.append("当前无持仓(空仓)")
    return "\n".join(lines)


def build_user_prompt(row: dict, holdings: dict[str, dict], cash: float | None,
                      total_asset: float | None) -> str:
    sys = SYSTEM.replace("{cash_pct}", f"{config.DEFAULT_CASH_PERCENT*100:.0f}")\
                .replace("{pos_pct}", f"{config.MAX_POSITION_PERCENT*100:.0f}")
    return (f"【当前账户】\n{account_text(holdings, cash, total_asset)}\n\n"
            f"【本只股票实时数据】\n{row['text']}\n\n"
            f"请对 {row['name']}({row['code']}) 给出决策 JSON,严格按照字段契约输出。"), sys


def parse_decision(raw: str) -> dict | None:
    """从模型输出中健壮地抽出 JSON。"""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"```(?:json)?", "", text).replace("```", "")
    # 尝试整段/大括号取 JSON
    for m in re.finditer(r"\{.*\}", text, re.S):
        cand = m.group(0)
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            # 截半/多余尾巴,尝试逐字符截断再解析
            for cut in range(len(cand), 1, -1):
                try:
                    o = json.loads(cand[:cut])
                    if isinstance(o, dict):
                        return o
                except json.JSONDecodeError:
                    continue
    # 最后兜底:正则抽字段
    fields = {}
    for key in ("action", "shares", "confidence", "reason"):
        m = re.search(rf'"{key}"\s*:\s*("([^"]*)"|[\d.]+)', text)
        if m:
            v = m.group(2) if m.group(2) is not None else m.group(1)
            fields[key] = v
    return fields or None


def sanitize(decision: dict, row: dict, holdings: dict[str, dict],
             cash: float | None, total_asset: float | None = None) -> dict:
    """把模型输出规整到契约内(类型/取值/clamp)。

    资金/仓位上限均以「模拟盘」估算为准:
      - buy: 单笔 ≤ 可用资金 × DEFAULT_CASH_PERCENT,且该股总市值 ≤ 资产 × MAX_POSITION_PERCENT;
      - sell: 只卖「可卖」股数(T+1,今日买入不可卖)。
    """
    action = str(decision.get("action", "hold")).strip().lower()
    if action not in ("buy", "sell", "hold"):
        action = "hold"
    try:
        shares = int(float(decision.get("shares", 0)))
    except (TypeError, ValueError):
        shares = 0
    try:
        conf = min(1.0, max(0.0, float(decision.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(decision.get("reason", ""))[:120]

    code = row["code"]
    price = row.get("price") or 0
    if action == "buy":
        shares = max(0, int(shares // 100) * 100)   # 对齐一手
        if not cash or cash <= 0:
            cash = config.ASSUMED_CASH                # 未接 App 时兜底资金
        if cash > 0 and price > 0:
            max_by_cash = int(cash * config.DEFAULT_CASH_PERCENT // price // 100) * 100
            cap = [max_by_cash]
            if total_asset and total_asset > 0:       # 单只仓位上限
                cur_val = holdings.get(code, {}).get("qty", 0) * price
                room = total_asset * config.MAX_POSITION_PERCENT - cur_val
                cap.append(int(room // price // 100) * 100 if room > 0 else 0)
            max_shares = min(cap)
            shares = min(shares, max_shares) if shares else max_shares
            if shares <= 0:
                action, shares = "hold", 0            # 现金/仓位不足 → 不买
    elif action == "sell":
        held_total = holdings.get(code, {}).get("qty", 0)
        sellable = holdings.get(code, {}).get("sellable", held_total)
        shares = max(0, int(shares // 100) * 100)
        shares = min(shares, sellable, held_total)
        if shares <= 0:          # 没有可卖持仓却要卖 => 改 hold
            action, shares = "hold", 0
    else:
        shares = 0
    return dict(code=code, name=row["name"], action=action, shares=shares,
                confidence=conf, reason=reason, raw=raw_or_empty(decision))


def _lot(n: int) -> int:
    """向下取整到 100 股一手。"""
    return max(0, int(n // 100) * 100)


def rule_sell(row: dict, holdings: dict[str, dict]) -> dict | None:
    """确定性自动卖出(激进落袋档,2026-09-09 用户选定)。不吃 LLM。

    仅对持仓中「今日可卖」(sellable>0,A股 T+1)触发,优先级:
      1) 止损:现价 ≤ 成本×(1-STOP_LOSS)        → 清仓
      2) 止盈到位:现价 ≥ 成本×(1+TP_ALL)       → 清仓
      3) 止盈减半:现价 ≥ 成本×(1+TP_HALF)      → 减半(不足一手则清仓,避免留碎股)
      4) 破位:收盘价 <MA20 且 MA5<MA10         → 清仓
      5) 超买回落:RSI≥OVERBOUGHT 且当日翻绿     → 减半(不足一手则清仓)
    命中返回与 decide 同构的 dict(带 src='rule');否则 None。
    """
    code = row.get("code")
    pos = (holdings or {}).get(code)
    if not pos:
        return None
    sellable = int(pos.get("sellable", pos.get("qty", 0)))
    if sellable <= 0:                    # T+1:今日买入不可卖
        return None
    price = row.get("price")
    cost = pos.get("cost")
    if not price or not cost or price <= 0 or cost <= 0:
        return None
    ind = row.get("ind") or {}

    half = lambda: (_lot(sellable // 2) or sellable)   # 减半;不足一手 → 清仓
    down = cost * (1 - config.SELL_STOP_LOSS_PCT)
    up_half = cost * (1 + config.SELL_TP_HALF_PCT)
    up_all = cost * (1 + config.SELL_TP_ALL_PCT)

    shares, conf, reason = 0, 0.0, ""
    if price <= down:
        shares, conf = sellable, 0.96
        reason = (f"止损(现价{price:.2f}≤成本{cost:.2f}×(1-{config.SELL_STOP_LOSS_PCT*100:.0f}%)),清仓")
    elif price >= up_all:
        shares, conf = sellable, 0.96
        reason = (f"止盈到位(现价{price:.2f}≥成本×1+{config.SELL_TP_ALL_PCT*100:.0f}%),清仓落袋")
    elif price >= up_half:
        shares = half()
        conf = 0.90
        reason = (f"止盈(现价{price:.2f}≥成本×1+{config.SELL_TP_HALF_PCT*100:.0f}%),"
                  f"减半{'清仓' if shares == sellable else '卖出'}")
    elif config.SELL_BREAK_MA and None not in (ind.get("ma5"), ind.get("ma10"), ind.get("ma20")) \
            and price < ind["ma20"] and ind["ma5"] < ind["ma10"]:
        shares, conf = sellable, 0.92
        reason = (f"破位走坏(现价{price:.2f}<MA20 {ind['ma20']:.2f} 且 MA5<MA10),清仓")
    elif ind.get("rsi14") is not None and ind["rsi14"] >= config.SELL_OVERBOUGHT_RSI \
            and (row.get("pct") or 0) < 0:
        shares = half()
        conf = 0.88
        reason = (f"超买回落(RSI {ind['rsi14']:.1f}≥{config.SELL_OVERBOUGHT_RSI:.0f} 且今日翻绿),"
                  f"减半{'清仓' if shares == sellable else '卖出'}")
    if shares <= 0:
        return None
    shares = min(shares, sellable)
    return dict(code=code, name=row.get("name"), action="sell", shares=shares,
                confidence=conf, reason=reason, src="rule")


def raw_or_empty(d):
    try:
        return json.dumps(d, ensure_ascii=False)[:200]
    except Exception:
        return ""


def decide(row: dict, holdings: dict[str, dict], cash: float | None,
           total_asset: float | None) -> dict | None:
    """对单只股票跑一次完整决策,返回规范化 dict,失败返回 None。"""
    prompt, sys_prompt = build_user_prompt(row, holdings, cash, total_asset)
    raw = model_client.chat(sys_prompt, prompt)
    parsed = parse_decision(raw)
    if not parsed:
        print(f"[strategy] 解析失败 code={row['code']} raw={raw[:150]}")
        return None
    return sanitize(parsed, row, holdings, cash, total_asset)
