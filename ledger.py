# -*- coding: utf-8 -*-
"""账本:记录每次决策/下单/资产快照,供回看与风控计数。"""
from __future__ import annotations
import csv
import json
import os
import time

import config

TRADES_CSV = os.path.join(config.DATA_DIR, "trades.csv")
EQUITY_JSONL = os.path.join(config.DATA_DIR, "equity.jsonl")
STATUS_JSON = os.path.join(config.DATA_DIR, "status.json")

_FIELDS = ["ts", "date", "code", "name", "action", "side", "price", "shares",
           "amount", "confidence", "exec_status", "reason", "src"]

if not os.path.exists(TRADES_CSV):
    with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=_FIELDS).writeheader()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def log_row(row: dict) -> None:
    row = dict(row)
    row.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    row.setdefault("date", _today())
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        w.writerow({k: row.get(k, "") for k in _FIELDS})


def log_decision(code, name, action, shares, confidence, reason, src="model",
                 price=None, exec_status="decided", amount=None):
    log_row(dict(code=code, name=name, action=action, side=action,
                 shares=shares, confidence=confidence, reason=reason,
                 price=price, amount=amount, exec_status=exec_status, src=src))


def log_trade(code, name, side, price, shares, amount, confidence, reason, src="ths"):
    log_row(dict(code=code, name=name, action=side, side=side, price=price,
                 shares=shares, amount=amount, confidence=confidence,
                 reason=reason, exec_status="applied", src=src))


def snapshot(status: dict) -> None:
    status.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    with open(EQUITY_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(status, ensure_ascii=False) + "\n")
    with open(STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def count_orders_today() -> int:
    if not os.path.exists(TRADES_CSV):
        return 0
    with open(TRADES_CSV, encoding="utf-8") as f:
        return sum(1 for r in csv.DictReader(f)
                   if r.get("date") == _today() and r.get("exec_status") == "applied")


def positions_from_ledger() -> dict[str, dict]:
    """从账本反推持仓(仅统计 applied 的买卖),供无 App 时 dry-run 使用。

    返回项含 sellable(当日可卖股数,A股 T+1:今天买入的今天不可卖)。
    """
    pos: dict[str, dict] = {}
    if not os.path.exists(TRADES_CSV):
        return pos
    today = _today()
    with open(TRADES_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("exec_status") != "applied":
                continue
            code = r["code"]
            p = pos.setdefault(code, {"code": code, "name": r.get("name", ""),
                                      "qty": 0, "cost": 0.0, "sellable": 0,
                                      "today_qty": 0})
            qty = int(float(r["shares"] or 0))
            px = float(r["price"] or 0)
            if r["side"] == "buy":
                tot = p["qty"] * p["cost"] + qty * px
                p["qty"] += qty
                p["cost"] = tot / p["qty"] if p["qty"] else 0.0
                if r.get("date") >= today:          # 今天买入 → 暂不可卖
                    p["today_qty"] += qty
            elif r["side"] == "sell":
                p["qty"] = max(0, p["qty"] - qty)
                p["today_qty"] = max(0, p["today_qty"] - qty)
    out = {}
    for c, p in pos.items():
        if p["qty"] <= 0:
            continue
        p["sellable"] = max(0, p["qty"] - p["today_qty"])
        out[c] = p
    return out


def cash_estimate(initial: float | None = None) -> float:
    """按账本推算剩余可用资金(未接 App 时的近似)。

    初始资金 − 累计买入金额 + 累计卖出回款;卖出当日即可再买(与模拟盘一致)。
    """
    if not os.path.exists(TRADES_CSV):
        return initial if initial is not None else float(config.ASSUMED_CASH)
    cash = initial if initial is not None else float(config.ASSUMED_CASH)
    with open(TRADES_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("exec_status") != "applied":
                continue
            amt = float(r.get("amount") or 0)
            if r["side"] == "buy":
                cash -= amt
            elif r["side"] == "sell":
                cash += amt
    return max(0.0, cash)


def load_recent(n: int = 30) -> list[dict]:
    if not os.path.exists(TRADES_CSV):
        return []
    with open(TRADES_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-n:]
