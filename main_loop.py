# -*- coding: utf-8 -*-
"""全自动盯盘调度:交易时段循环 -> 行情 -> (触发才)调模型 -> 下单/记帐。"""
from __future__ import annotations
import datetime as _dt
import os
import subprocess
import sys
import time

import config
import ledger
import market_data
import model_client
import strategy

_last_model_call: dict[str, float] = {}


# ---------- 交易时段 ----------
def is_trading_time(now: _dt.datetime | None = None) -> bool:
    now = now or _dt.datetime.now()
    if now.weekday() >= 5:                       # 六日休市
        return False
    hm = now.strftime("%H:%M")
    for a, b in config.TRADE_SESSIONS:
        if a <= hm < b:
            return True
    return False


def is_kill():
    return os.path.exists(config.KILL_FILE)


# ---------- 模型服务管理 ----------
def ensure_model_server(timeout: int = 300) -> bool:
    if model_client.health().get("ok"):
        return True
    print("[loop] 启动 model_server ...")
    logf = open(os.path.join(config.LOG_DIR, "model_server.log"), "ab")
    subprocess.Popen([sys.executable, os.path.join(config.BASE_DIR, "model_server.py")],
                     stdout=logf, stderr=subprocess.STDOUT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    t0 = time.time()
    while time.time() - t0 < timeout:
        if model_client.health().get("ok"):
            print("[loop] model_server 就绪")
            return True
        time.sleep(3)
    print("[loop] model_server 启动超时,请单独运行: python run.py serve")
    return False


# ---------- 触发判断(避免无谓调模型) ----------
def need_analysis(row: dict, is_held: bool) -> bool:
    pct = row.get("pct") or 0
    rsi = row.get("ind", {}).get("rsi14")
    vr = row.get("volume_ratio") or 0
    if is_held:
        return abs(pct) >= 0.6 or (rsi is not None and (rsi >= 70 or rsi <= 30))
    return abs(pct) >= 2.0 or vr >= 2.2 or (rsi is not None and (rsi >= 75 or rsi <= 25))


def throttled(code: str) -> bool:
    last = _last_model_call.get(code, 0)
    if time.time() - last < config.MODEL_CALL_MIN_INTERVAL:
        return True
    return False


# ---------- 单轮 ----------
def run_cycle(dry: bool, holdings: dict[str, dict], cash: float | None,
              total_asset: float | None, executor=None) -> dict:
    """跑一轮:刷新行情 → (全量/触发)调模型 → 记录/下单。返回本轮统计。"""
    stocks = _stocks()
    quotes = market_data.refresh_all(stocks)
    summary = {"checked": len(quotes), "decisions": 0, "orders": 0}
    # 可用资金/总资产:能传就传;未传时按账本实时估算(dry/live 一致口径)
    if cash is None:
        cash = ledger.cash_estimate()
    if total_asset is None and cash is not None:
        pos_val = sum(int(h["qty"]) * (quotes.get(c, {}).get("price") or h.get("cost") or 0)
                      for c, h in holdings.items())
        total_asset = cash + pos_val
    orders_today = ledger.count_orders_today()
    if config.MAX_ORDERS_PER_DAY > 0 and orders_today >= config.MAX_ORDERS_PER_DAY:
        print(f"[loop] 已达当日下单上限 {config.MAX_ORDERS_PER_DAY},本日不再交易")
    for code, name in stocks:
        row = quotes.get(code)
        if not row:
            print(f"[loop] {name}({code}) 行情抓取失败,跳过")
            continue
        is_held = code in holdings
        dec = None
        src = "model"
        # 规则卖出优先:确定性止盈止损破位,持仓且今日可卖才可能触发(A股 T+1)
        if is_held:
            dec = strategy.rule_sell(row, holdings)
            if dec:
                src = "rule"
                print(f"[loop] 卖出规则触发 {name}: {dec['reason']}")
        if dec is None:
            # 火力档:ANALYZE_ALL 后不再等指标触发,每轮每只都过模型(节流间隔已调低)
            analyze = config.ANALYZE_ALL or need_analysis(row, is_held)
            if not analyze or throttled(code):
                continue
            _last_model_call[code] = time.time()
            try:
                dec = strategy.decide(row, holdings, cash, total_asset)
            except Exception as e:  # 模型服务超时/解析等
                print(f"[loop] {code} 决策异常: {e}")
                continue
        if not dec:
            continue
        summary["decisions"] += 1
        ledger.log_decision(code, name, dec["action"], dec["shares"],
                            dec["confidence"], dec["reason"],
                            price=row.get("price"), exec_status="decided", src=src)
        if dec["action"] == "hold":
            print(f"[loop] {name} {dec['action']} conf={dec['confidence']:.2f} {dec['reason']}")
            continue
        # 下单额度检查(0=不限)
        if config.MAX_ORDERS_PER_DAY > 0 and orders_today >= config.MAX_ORDERS_PER_DAY:
            print(f"[loop] {name}:今日下单额度已用完,仅记录决策")
            continue
        amount = (row.get("price") or 0) * dec["shares"]
        if dry or executor is None:
            print(f"[loop][DRY] {name} {dec['action']} {dec['shares']}股 "
                  f"conf={dec['confidence']:.2f} {dec['reason']}")
            summary["orders"] += 1
        else:
            ok = executor(dec, row)
            if ok:
                orders_today += 1
                summary["orders"] += 1
                ledger.log_trade(code, name, dec["action"], row.get("price"), dec["shares"],
                                 amount, dec["confidence"], dec["reason"], src="ths")
        # 本笔生效后即时更新可用资金,避免同一轮后续股票按旧现金重复超买
        if cash is not None:
            if dec["action"] == "buy":
                cash = max(0.0, cash - amount)
            elif dec["action"] == "sell":
                cash += amount
        time.sleep(1.0)          # 逐股下单后的落定间隔(曾 2s)
    ledger.snapshot({"summary": summary, "holdings": {k: v.get("qty") for k, v in holdings.items()},
                     "cash": cash, "dry": dry, "kill": is_kill()})
    return summary


# ---------- 循环 ----------
def monitor(dry: bool, force: bool = False, interval: int | None = None) -> None:
    if not dry:
        from emulator import Emulator
        e = Emulator()
        e.connect_adb(quiet=True)
        if not e.adb_ok:
            print("[loop][警告] 模拟器未连接;live 模式下暂不真实下单,先做决策演练。"
                  "请先启动雷电并登录同花顺模拟盘后再用 --live")
    ensure_model_server()
    interval = interval or config.POLL_SECONDS
    print(f"[loop] 开始盯盘(dry={dry}, force={force}, 间隔 {interval}s)。"
          f"交易时段 {config.TRADE_SESSIONS}。急停文件: {config.KILL_FILE}")
    last_beat = 0
    while True:
        try:
            if not force and not is_trading_time():
                now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if time.time() - last_beat > 120:
                    print(f"[loop] {now} 非交易时段,等待中(Ctrl-C 退出)")
                    last_beat = time.time()
                time.sleep(15)
                continue
            if is_kill():
                print("[loop] 检测到急停文件 STOP,暂停下单(删除该文件可恢复)。本轮回合只分析不执行。")
                time.sleep(30)
                continue
            holdings = ledger.positions_from_ledger()
            cash, asset = None, None
            run_cycle(dry, holdings, cash, asset, executor=_make_executor(dry))
            print(f"[loop] 完成一轮,{time.strftime('%H:%M:%S')},睡 {interval}s ...")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[loop] 用户中断,退出。")
            break
        except Exception as e:  # 网络/单轮错误不致命
            print(f"[loop] 轮询异常: {e!r},继续")
            time.sleep(interval)


def _make_executor(dry: bool):
    if dry:
        return None

    def executor(dec: dict, row: dict) -> bool:
        from emulator import Emulator
        from ths_app import place_order
        e = Emulator()
        e.connect_adb(quiet=True)
        if not e.adb_ok:
            print(f"[loop][跳过] 模拟器离线,未下单 {dec['name']} {dec['action']}")
            return False
        return place_order(e, dec["action"], dec["code"], dec["name"],
                           dec["shares"], price=row.get("price"))
    return executor


def _stocks():
    # 每次重读:data/stocks.json 很小,读取极快;可视化平台增删即刻生效。
    import stocks_loader
    return stocks_loader.load_stocks()
