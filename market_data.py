# -*- coding: utf-8 -*-
"""行情数据:实时行情 + 日K + 本地指标(MA/RSI/MACD),生成喂给模型的文本特征。

源选择(本机实测):
- 实时行情:东财 push2delay(UTF-8、fltt=2 直接浮点价;主站 push2 会掐 python 连接)
- 日K/前复权:腾讯 web.ifzq.gtimg.cn fqkline(UTF-8 JSON)
- 兜底:腾讯 qt.gtimg.cn(GBK)
失败返回 None,调用方本轮跳过该股。
"""
from __future__ import annotations
import json
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _http(url: str, referer: str = "https://quote.eastmoney.com/", enc: str = "utf-8") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode(enc, errors="replace")


def classify(code: str):
    """不同接口的前缀:东财市场号 / 腾讯标记 / 新浪标记。"""
    if code.startswith(("4", "8", "920")):      # 北交所(含新 920 段)
        return "0", "bj", "bj"
    if code.startswith(("5", "6", "9")):        # 上交所股票/基金
        return "1", "sh", "sh"
    return "0", "sz", "sz"                       # 深交所股票/基金


def fetch_quote(code: str) -> dict | None:
    """东财实时行情(fltt=2 已是浮点价)。"""
    m, _, _ = classify(code)
    for secid in (f"{m}.{code}", f"1.{code}" if code.startswith(("4", "8", "920")) else None):
        if secid is None:
            continue
        p = urllib.parse.urlencode(dict(secid=secid, fltt="2", invt="2",
                                        fields="f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f169,f170,f171"))
        try:
            j = json.loads(_http(f"https://push2delay.eastmoney.com/api/qt/stock/get?{p}"))
        except Exception:
            continue
        if (j or {}).get("data"):
            return j["data"]
    return None


def fetch_kline(code: str, days: int = 160) -> list[list] | None:
    """腾讯前复权日K;返回 [[date,open,close,high,low,vol(手)], ...]。"""
    cache = _cache_kline.get(code)
    if cache and time.time() - cache["ts"] < 3600:
        return cache["data"]
    _, tencent, _ = classify(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent}{code},day,,,{days},qfq"
    try:
        txt = _http(url, referer="https://gu.qq.com/")
        j = json.loads(txt)
        d = j["data"][f"{tencent}{code}"]
        rows = d.get("qfqday") or d.get("day") or []
    except Exception:
        return None
    if rows:
        _cache_kline[code] = {"ts": time.time(), "data": rows}
    return rows or None


# ---------- 兜底:腾讯快照(GBK) ----------
def fetch_quote_fallback(code: str) -> dict | None:
    _, tencent, _ = classify(code)
    try:
        raw = _http(f"https://qt.gtimg.cn/q={tencent}{code}",
                    referer="https://gu.qq.com/", enc="gbk")
    except Exception:
        return None
    if "=" not in raw:
        return None
    f = raw.split("=", 1)[1].strip().strip('"').split("~")
    if len(f) < 40 or not f[1]:
        return None

    def g(i):
        try:
            return float(f[i])
        except (ValueError, IndexError):
            return None

    prev = g(4); price = g(3)
    return {"f57": code, "f58": f[1], "f43": price, "f46": g(5), "f44": g(33), "f45": g(34),
            "f60": prev, "f169": price - prev if prev else None, "f170": g(32),
            "f47": g(6), "f48": None, "f168": None, "f50": None}


# ---------- 本地指标(纯 python) ----------
def _ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def compute_indicators(closes: list[float]) -> dict:
    out = {}
    for n in (5, 10, 20):
        if len(closes) >= n:
            out[f"ma{n}"] = sum(closes[-n:]) / n
    if len(closes) >= 60:
        out["ma60"] = sum(closes[-60:]) / 60
    if len(closes) >= 16:  # RSI14(Wilder)
        gains, losses = [], []
        for i in range(1, len(closes)):
            ch = closes[i] - closes[i - 1]
            gains.append(max(ch, 0.0)); losses.append(max(-ch, 0.0))
        g, l = sum(gains[:14]) / 14, sum(losses[:14]) / 14
        for i in range(14, len(gains)):
            g = (g * 13 + gains[i]) / 14
            l = (l * 13 + losses[i]) / 14
        out["rsi14"] = 100 - 100 / (1 + (g / l if l else 1e9))
    if len(closes) >= 35:  # MACD(12,26,9)
        dif = []
        for i in range(26, len(closes)):
            dif.append(_ema(closes[i - 11:i + 1], 12) - _ema(closes[i - 25:i + 1], 26))
        dea = _ema(dif, 9)
        out["dif"], out["dea"] = dif[-1], dea
        out["macd_hist"] = 2 * (dif[-1] - dea)
    if len(closes) >= 6:
        out["pct5"] = (closes[-1] / closes[-6] - 1) * 100
    if len(closes) >= 21:
        out["pct20"] = (closes[-1] / closes[-21] - 1) * 100
    return out


_cache_kline: dict[str, dict] = {}


# ---------- 组装 ----------
def _fmt(v, nd=2):
    return "None" if v is None else f"{v:.{nd}f}"


def quote_row(code: str, name: str) -> dict | None:
    d = fetch_quote(code)
    if not d:
        d = fetch_quote_fallback(code)
        if not d:
            return None
        src = "tencent-fb"
    else:
        src = "eastmoney"

    ind = {}
    kl = fetch_kline(code)
    if kl:
        closes = [float(r[2]) for r in kl]
        ind = compute_indicators(closes)

    row = dict(code=code, name=name, price=d.get("f43"), pct=d.get("f170"),
               chg=d.get("f169"), open=d.get("f46"), high=d.get("f44"),
               low=d.get("f45"), prev_close=d.get("f60"), volume=d.get("f47"),
               amount=d.get("f48"), turnover=d.get("f168"), volume_ratio=d.get("f50"),
               ind=ind, src=src, ts=time.strftime("%Y-%m-%d %H:%M:%S"))
    row["text"] = _to_text(row)
    return row


def _to_text(r: dict) -> str:
    ind = r["ind"]
    side = [f"MA5 {_fmt(ind.get('ma5'))}", f"MA10 {_fmt(ind.get('ma10'))}",
            f"MA20 {_fmt(ind.get('ma20'))}"]
    if ind.get("ma60") is not None:
        side.append(f"MA60 {_fmt(ind['ma60'])}")
    if ind.get("rsi14") is not None:
        side.append(f"RSI14 {_fmt(ind['rsi14'])}")
    if ind.get("macd_hist") is not None:
        side.append(f"MACD柱 {_fmt(ind['macd_hist'])}")
    if ind.get("pct5") is not None:
        side.append(f"近5日{_fmt(ind['pct5'])}% 近20日{_fmt(ind['pct20'])}%")
    p = r["price"]; pc = r["pct"]
    chg_s = f"+{pc}%" if (pc or 0) >= 0 else f"{pc}%"
    amt = (f"成交额 {r['amount']/1e8:.2f}亿" if r["amount"] else "")
    tn = (f"换手 {_fmt(r['turnover'])}%" if r["turnover"] is not None else "")
    vr = (f"量比 {_fmt(r['volume_ratio'])}" if r["volume_ratio"] is not None else "")
    return (f"{r['name']}({r['code']}) 现价 {_fmt(p)} {chg_s} "
            f"开 {_fmt(r['open'])} 高 {_fmt(r['high'])} 低 {_fmt(r['low'])} 昨收 {_fmt(r['prev_close'])} "
            f"{amt} {tn} {vr} | {' '.join(side)}").strip()


def refresh_all(stocks) -> dict[str, dict]:
    out = {}
    for code, name in stocks:
        r = quote_row(code, name)
        if r:
            out[code] = r
        time.sleep(0.12)
    return out


if __name__ == "__main__":
    for code, name in [("300190", "维尔利"), ("920045", "蘅东光"), ("515050", "通信ETF华夏"),
                       ("002241", "歌尔股份"), ("600522", "中天科技")]:
        r = quote_row(code, name)
        print((r["text"] if r else "FETCH FAIL"))
