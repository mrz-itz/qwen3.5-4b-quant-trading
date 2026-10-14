# -*- coding: utf-8 -*-
"""股票池来源 -> [(code, name), ...]

来源分两级:
  1) data/stocks.json —— 权威、可写来源(可视化平台增删的就是它)。
     每次调用都重读(文件很小),增删即刻对盯盘生效。
  2) 不存在时,从 股票信息.doc 解析并播种生成 stocks.json(Word COM 优先,
     COM 失败退回字节扫描)。.doc 只作为一次性种子,之后不再依赖 Word。
"""
from __future__ import annotations
import json
import os
import re

import config


def _parse_segment(seg: str):
    """把一行「300190维尔利」拆成 (code, name)。容忍开头空格/换行/中文全角。"""
    seg = seg.strip().strip("﻿")
    m = re.match(r"(\d{6})\s*(\S*)", seg)
    if not m:
        return None
    code = m.group(1)
    name = m.group(2).strip()
    if not name:  # 名称缺失时用代码占位
        name = code
    return (code, name)


def load_stocks_via_word():
    """用 Word COM 读取 .doc 全文并按段落拆行。"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx("Word.Application")
    app.Visible = False
    app.DisplayAlerts = 0
    stocks = []
    try:
        doc = app.Documents.Open(config.DOC_PATH, ReadOnly=True)
        text = doc.Content.Text  # 段落以 \r 分隔
        doc.Close(False)
    finally:
        app.Quit()
    for seg in re.split(r"[\r\n\a]+", text):
        p = _parse_segment(seg)
        if p:
            stocks.append(p)
    # 去重保序
    seen, out = set(), []
    for s in stocks:
        if s[0] not in seen:
            seen.add(s[0]); out.append(s)
    return out


def load_stocks_fallback():
    """Word COM 不可用时的二进制兜底:去掉不可打印字符后按数字切分。"""
    with open(config.DOC_PATH, "rb") as f:
        raw = f.read()
    # Word 二进制里文本多为 UTF-16LE(带 BOM 时),否则按 latin-1 保留可打印段
    try:
        txt = raw.decode("utf-16")
    except UnicodeDecodeError:
        txt = raw.decode("gbk", errors="ignore")
    found = re.findall(r"(\d{6})([一-龥A-Za-z*·]{1,12})", txt)
    seen, out = set(), []
    for code, name in found:
        if code not in seen:
            seen.add(code); out.append((code, name))
    return out


def _load_json_file() -> list[tuple[str, str]] | None:
    """读 data/stocks.json;不存在/损坏返回 None。"""
    if not os.path.exists(config.STOCKS_FILE):
        return None
    try:
        with open(config.STOCKS_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        out = []
        for r in rows:
            code = str(r.get("code", "")).strip()
            name = str(r.get("name", "")).strip()
            if code and code.isdigit():
                out.append((code, name or code))
        return out or None
    except Exception:
        return None


def _seed_from_doc() -> list[tuple[str, str]]:
    """从 .doc 解析(一次性种子)并落盘到 stocks.json。"""
    stocks = []
    try:
        stocks = load_stocks_via_word()
    except Exception as e:  # COM 失败(Word 占用/未装)
        print(f"[stocks] Word COM 失败({e}),使用二进制兜底解析")
        stocks = load_stocks_fallback()
    if not stocks:
        raise RuntimeError("未能从股票信息.doc 解析出任何股票,请检查文件内容")
    save_stocks([dict(code=c, name=n) for c, n in stocks])
    return stocks


def load_stocks() -> list[tuple[str, str]]:
    """返回股票池。优先 data/stocks.json;首次从 .doc 播种并写 json。"""
    rows = _load_json_file()
    if rows is not None:
        return rows
    return _seed_from_doc()


# ---------- 可视化平台增删接口 ----------
def save_stocks(items: list[dict]) -> list[tuple[str, str]]:
    """整表写回 stocks.json。items: [{"code":..,"name":..}, ...];去重保序。"""
    seen, clean = set(), []
    for it in items:
        code = str(it.get("code", "")).strip()
        name = str(it.get("name", "")).strip()
        if code.isdigit() and code not in seen:
            seen.add(code)
            clean.append({"code": code, "name": name or code})
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=1)
    return [(d["code"], d["name"]) for d in clean]


def current_list() -> list[dict]:
    """dashboard 展示用:返回 [{"code":..,"name":..}]。"""
    return [dict(code=c, name=n) for c, n in load_stocks()]


def _as_dicts(rows) -> list[dict]:
    return [dict(code=c, name=n) for c, n in rows]


def add_stock(code: str, name: str = "") -> dict:
    """追加单只;返回 {ok, msg, stocks:[{code,name}...]}。重复 code 报错。"""
    code = code.strip()
    if not (code.isdigit() and len(code) == 6):
        return {"ok": False, "msg": f"代码应为 6 位数字,收到: {code!r}", "stocks": current_list()}
    items = current_list()
    if any(d["code"] == code for d in items):
        return {"ok": False, "msg": f"{code} 已在股票池", "stocks": items}
    if not name.strip():
        name = _lookup_name(code)
    items.append({"code": code, "name": name.strip() or code})
    rows = save_stocks(items)
    return {"ok": True, "msg": f"已添加 {name or code}({code})", "stocks": _as_dicts(rows)}


def del_stock(code: str) -> dict:
    """删除单只;返回 {ok, msg, stocks}。"""
    code = code.strip()
    items = current_list()
    keep = [d for d in items if d["code"] != code]
    if len(keep) == len(items):
        return {"ok": False, "msg": f"{code} 不在股票池", "stocks": items}
    rows = save_stocks(keep)
    return {"ok": True, "msg": f"已删除 {code}", "stocks": _as_dicts(rows)}


def _lookup_name(code: str) -> str:
    """尽力从实时行情取名称;失败返回空。"""
    try:
        import market_data
        d = market_data.fetch_quote(code)
        if d and d.get("f58"):
            return str(d["f58"]).strip()
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    for c, n in load_stocks():
        print(c, n)
