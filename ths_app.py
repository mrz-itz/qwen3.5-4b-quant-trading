# -*- coding: utf-8 -*-
"""同花顺 App 驱动(模拟炒股)。

⚠️ 校准说明:同花顺版本/布局会变。定位优先走 selectors.json(文本锚点),
probe 命令可把当前屏元素导出来帮你补锚点。页面若与默认锚点不符,函数会打印
“找不到 X”并返回失败,不会盲目乱点。

已实现/可校验:包探测、启动到前台、搜索并打开个股、读持仓(尽力)。
下单路径结构已搭好,锚点需用真实 App 校准后启用。
"""
from __future__ import annotations
import json
import os
import re
import time

import config

SEL_PATH = os.path.join(config.BASE_DIR, "selectors.json")

_PKG_CANDIDATES = ["com.hexin.plat.android", "com.hexin.plat.android.hexin",
                   "com.hexin.plat.hexin", "com.thsi.thsi"]


def _load_sel() -> dict:
    if os.path.exists(SEL_PATH):
        try:
            with open(SEL_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


_SEL = _load_sel()


def _anchor(key: str) -> dict | None:
    """取一个锚点:{by:text|desc, text:...};缺失返回 None。"""
    return _SEL.get(key)


def _tap_anchor(e, key: str, fallbacks: tuple[str, ...] = ()) -> bool:
    a = _anchor(key)
    if a:
        if e.tap_by_text(a.get("text", ""), field=a.get("by", "text")):
            return True
    for fb in fallbacks:
        if e.tap_by_text(fb):
            return True
    print(f"[ths] 找不到锚点: {key} {fallbacks}")
    return False


# ---------- 包 & 启动 ----------
def detect_package(e) -> str | None:
    out = e.shell("pm list packages")
    for cand in _PKG_CANDIDATES:
        if cand in out:
            return cand
    hits = re.findall(r"package:(com\.\w*hexin\w*\.?\w*)", out)
    if hits:
        return hits[0]
    return None


def launcher_activity(e, pkg: str) -> str | None:
    out = e.shell(f"cmd package resolve-activity --brief {pkg}").strip()
    m = re.search(r"(\S+/\S+)", out)
    return m.group(1) if m else None


def bring_app(e) -> bool:
    pkg = detect_package(e)
    if not pkg:
        print("[ths] 未检测到同花顺(请先安装并至少打开过一次)。可选包:", _PKG_CANDIDATES)
        return False
    act = launcher_activity(e, pkg)
    if act:
        e.shell(f"am start -n {act}")
    else:
        e.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    time.sleep(6)
    print(f"[ths] 已请求启动 {pkg}")
    return True


# ---------- 导航 ----------
def goto_stock(e, code: str) -> bool:
    """通过顶部搜索进入个股行情页。"""
    # 1) 尝试点首页搜索入口(可能在顶部,可能是输入框)
    a = _anchor("search_entry")
    if a and e.tap_by_text(a.get("text", ""), field=a.get("by", "text")):
        time.sleep(2)
    elif not e.tap_by_text("搜索"):
        print("[ths] 未能打开搜索页")
        return False
    # 2) 输入代码
    time.sleep(1)
    e.input_text(code)
    time.sleep(3)
    # 3) 在候选/联想里点第一个包含代码的条目
    for n in e.dump_nodes():
        txt = n["text"]
        if code in txt:
            e.tap(n["cx"], n["cy"])
            time.sleep(3)
            return True
    print(f"[ths] 搜索 {code} 后未找到匹配条目")
    return False


def _read_holdings_from_text(vtext: str, cash_line: str = "") -> tuple[dict, float, float]:
    """尽力从持仓页文本反推 {code:{qty,cost,name}},可用资金与总资产。返回(None,..)表示未识别。"""
    holdings: dict[str, dict] = {}
    cash = asset = None
    mm = re.search(r"可用\s*资金?[:：]?\s*([\d,，.]+)", vtext)
    if not mm:
        mm = re.search(r"可用\s*([\d,，.]+)", vtext)
    if mm:
        cash = float(mm.group(1).replace(",", "").replace("，", ""))
    am = re.search(r"总资产[:：]?\s*([\d,，.]+)|资产[:：]?\s*([\d,，.]+)", vtext)
    if am:
        asset = float((am.group(1) or am.group(2)).replace(",", "").replace("，", ""))
    # 持仓行模式: 名称 6位代码 数量 ...
    for m in re.finditer(r"([一-龥A-Za-z*]{1,12})\s*(\d{6})\s*(\d{1,6})\s", vtext):
        name, code, qty = m.group(1), m.group(2), int(m.group(3))
        holdings[code] = {"code": code, "name": name, "qty": qty,
                          "available": qty, "cost": None, "price": None}
    return holdings, cash, asset


def get_portfolio(e, save_to_db: bool = True) -> tuple[dict, float | None, float | None]:
    """读取模拟盘持仓/资金。
    路径: 底部『交易』→ 模拟账户/模拟炒股 → 『持仓』
    1. 截图并以时间标准命名: YYYYMMDD_HHMMSS_portfolio.png
    2. 多模态识图 (vision.recognize_portfolio) + UI文本解析双保险
    3. 识别内容(现有股票、持仓数量、手中资金数量)存储到 MySQL 数据库中
    """
    import db_mysql
    import vision

    _dismiss_dialogs(e, max_rounds=2)
    if not _tap_anchor(e, "trade_tab", fallbacks=("交易",)):
        print("[ths] 找不到『交易』入口")
        return {}, None, None
    time.sleep(3)
    # 若弹出券商/模拟选择,选“模拟”
    if not _tap_anchor(e, "sim_entry", fallbacks=("模拟", "模拟炒股")):
        pass
    time.sleep(3)
    _tap_anchor(e, "positions_tab", fallbacks=("持仓",))
    time.sleep(3)

    # 1. 截图 (时间命名)
    ts = time.strftime("%Y%m%d_%H%M%S")
    shot_path = os.path.join(config.SHOT_DIR, f"{ts}_portfolio.png")
    e.screencap(shot_path)
    print(f"[ths] 持仓页面截图已保存: {shot_path}")

    # 2. 识别持仓: 优先大模型多模态识图，UI文本解析兜底补充
    holdings: dict[str, dict] = {}
    cash = asset = None

    # 方式一: 大模型图像识别
    rec = {}
    try:
        rec = vision.recognize_portfolio(shot_path)
    except Exception as ex:
        print(f"[ths] 大模型持仓识图调用异常: {ex}")

    if rec.get("ok"):
        cash = rec.get("cash")
        asset = rec.get("total_asset")
        for s in rec.get("stocks") or []:
            code = s.get("code")
            if code:
                holdings[code] = {
                    "code": code,
                    "name": s.get("name", ""),
                    "qty": int(s.get("qty", 0)),
                    "available": int(s.get("available", s.get("qty", 0))),
                    "cost": s.get("cost"),
                    "price": s.get("price"),
                    "market_value": s.get("market_value"),
                }
        print(f"[ths] 视觉大模型持仓识别成功: 股票 {len(holdings)} 只, 资金 {cash}, 资产 {asset}")

    # 方式二: UI层级文本解析兜底与核对
    vtext = e.visible_text()
    ui_hold, ui_cash, ui_asset = _read_holdings_from_text(vtext)

    # 若图像未能提取出可用资金，采用UI文本结果
    if cash is None and ui_cash is not None:
        cash = ui_cash
    if asset is None and ui_asset is not None:
        asset = ui_asset
    # 合并UI文本提取出的股票 (若模型漏提)
    for code, h in ui_hold.items():
        if code not in holdings:
            holdings[code] = h
        else:
            if not holdings[code].get("name") and h.get("name"):
                holdings[code]["name"] = h["name"]

    if not holdings and cash is None:
        print("[ths] 未能从当前页解析出持仓或资金。当前页文本:")
        print("  ", vtext[:300])
        return {}, None, None

    print(f"[ths] 持仓识别汇总: 现有股票 {len(holdings)} 只, 手中可用资金 {cash}, 总资产 {asset}")

    # 3. 存储到 MySQL 数据库
    if save_to_db:
        try:
            stocks_list = list(holdings.values())
            bid = db_mysql.save_portfolio(
                cash=cash or 0.0,
                total_asset=asset,
                stocks=stocks_list,
                shot_path=shot_path,
                source="vision"
            )
            print(f"[ths] 持仓识别结果已存入 MySQL (balance_id={bid})")
        except Exception as ex:
            print(f"[ths] 持仓结果存入 MySQL 失败: {ex}")

    return holdings, cash, asset


# ---------- 下单(同花顺模拟盘:交易 → 模拟账户控制台填单) ----------
# 实测(雷电/同花顺当前版本):
#   1) 模拟买卖走「模拟炒股」控制台的填单页;个股行情页的「下单」进的是实盘券商 → 严禁触碰。
#   2) 填单页会记住上次输入(tab 切换不清空);且「行情/五档」面板可展开把表单顶下屏,
#      故坐标不可靠 → 一律按控件:三个 EditText 按从上到下顺序定位,输入前先 DEL 清空。
# 铁律:提交前必须看到含“(模拟炒股)”的按钮,否则中止,绝不碰实盘。
def _bottom_nav(e, label: str) -> bool:
    """点底部主导航(只匹配底部导航条 y≈2356~2560 的文本,避免误点内容区)。"""
    for n in e.dump_nodes():
        if n["text"] == label and 2300 < n["cy"] < 2600:
            e.tap(n["cx"], n["cy"]); time.sleep(2)
            return True
    print(f"[ths] 底部导航找不到: {label}")
    return False


_ACTION_WORDS = {"买入", "卖出", "撤单", "持仓", "查询"}


def _tap_in(e, text: str, y1: int = 0, y2: int = 2600) -> bool:
    """在 y 纵向带内点首个含 text 的节点(text 优先,desc 兜底)。"""
    for n in e.dump_nodes():
        v = n["text"] or n["desc"]
        if text in v and y1 <= n["cy"] <= y2:
            e.tap(n["cx"], n["cy"]); time.sleep(1.5)
            return True
    return False


def _open_sim_buy_sell(e, side_cn: str) -> bool:
    """确保位于 模拟炒股 控制台并切到 买入/卖出 填单页。

    结尾做防呆:页面标题必须是“模拟炒股”或提交键含“(模拟炒股)”,否则中止。
    """
    nodes = e.dump_nodes()
    if not (_ACTION_WORDS <= {n["text"] for n in nodes}):
        if not _bottom_nav(e, "交易"):
            return False
        time.sleep(3)
        nodes = e.dump_nodes()
        if not (_ACTION_WORDS <= {n["text"] for n in nodes}):
            if not _tap_in(e, "模拟", 100, 300):
                print("[ths] 交易页未见模拟动作组,疑似停在实盘账户,需手动切到模拟盘。")
                return False
            time.sleep(3)
    # 切到目标 买入/卖出(顶部工具栏或模拟首页动作格)
    if not _tap_in(e, side_cn, 250, 1600):
        print(f"[ths] 找不到 {side_cn} 入口(需校准)")
        return False
    time.sleep(3)
    # 防呆:必须是模拟页
    texts = {n["text"] for n in e.dump_nodes()}
    if "模拟炒股" not in texts and not any("模拟炒股)" in s for s in texts):
        print("[ths] 当前不是模拟炒股填单页(可能误入实盘),已中止。")
        return False
    return True


def _collapse_panel(e) -> None:
    """行情/五档面板展开时先“收起”,否则下方表单可能被顶出可视区。"""
    for n in e.dump_nodes():
        if n["text"] == "收起":
            e.tap(n["cx"], n["cy"]); time.sleep(2)
            return


def _edits(e, y0: int = 200, y1: int = 1700):
    """表单区的 EditText(代码/价格/数量),按 y 从上到下排序。"""
    return sorted((n for n in e.dump_nodes()
                   if n.get("cls") == "EditText" and y0 <= n["cy"] <= y1),
                  key=lambda n: n["cy"])


def _set_edit(e, idx: int, value, fallback_xy) -> bool:
    """点第 idx 个 EditText → DEL 清空旧值 → 输入 value;找不到用坐标兜底。"""
    fields = _edits(e)
    if idx < len(fields):
        bx = fields[idx]
        e.tap(bx["cx"], bx["cy"])
    else:
        e.tap(*fallback_xy)
    time.sleep(0.4)
    # 连按删除键清掉记忆值(每次 adb 调用有开销;8 下已够清 6 位代码/价格/数量)
    for _ in range(8):
        e.shell("input keyevent 67")
    time.sleep(0.1)
    e.input_text(str(value))
    return True


def _submit_sim(e) -> bool:
    """只提交含“(模拟炒股)”的按钮;找不到说明误入实盘表单,直接中止。"""
    for n in e.dump_nodes():
        if "模拟炒股)" in (n["text"] or "") and n["cy"] < 1900:
            e.tap(n["cx"], n["cy"])
            return True
    print("[ths] 未找到(模拟炒股)提交按钮 —— 疑似实盘表单,已中止,不会下实盘单。")
    return False


_AFFIRM_KW = ("确认", "确定", "买入", "卖出")
_OK_WORDS = ("确定", "完成", "知道了", "好的", "好", "OK")
# 系统提示窗的成功 / 失败语义词(以此判是否真已报单,不依赖截图模型)
_SUB_OK = ("委托已提交", "合同号", "已报", "委托成功", "提交成功", "成交")
_SUB_BAD = ("余额不足", "失败", "数量不正确", "价格超过", "超出", "不可用",
            "暂停", "废单", "无效", "资金不足")


def _act_dialog(nodes):
    """若当前是确认/系统提示弹窗,返回待点按钮 (kind, cx, cy);否则 None。

    成对窗(取消/确认买入)→ 点右侧确认键;原生系统单键窗(确定)→ 点该键。
    只认对话框语义按钮,绝不碰实盘表单。
    """
    cancel = next((n for n in nodes
                   if (n.get("text") or n.get("desc")) == "取消"), None)
    if cancel is not None:
        band = [n for n in nodes
                if n is not cancel and abs(n["cy"] - cancel["cy"]) <= 90
                and n["cx"] > cancel["cx"]]
        aff = next((n for n in band
                    if any(k in (n.get("text") or "") + (n.get("desc") or "")
                           for k in _AFFIRM_KW)), None)
        if aff is None and band:
            aff = max(band, key=lambda n: n["cx"])   # 兜底:同排最右按钮
        if aff is not None:
            return ("ok", aff["cx"], aff["cy"])
        return None                                  # 认不出确认键 → 不冒险
    okb = next((n for n in nodes
                if n.get("cls") == "Button"
                and (n.get("text") or "").strip() in _OK_WORDS), None)
    if okb is not None:
        return ("info", okb["cx"], okb["cy"])
    return None


def _dismiss_dialogs(e, max_rounds: int = 6) -> bool:
    """把屏上残留的确认/系统提示弹窗逐个点掉,直到回到可导航态(下单前兜底用)。"""
    quiet = 0
    for _ in range(max_rounds):
        act = _act_dialog(e.dump_nodes())
        if act:
            e.tap(act[1], act[2])
            quiet = 0
            time.sleep(1.0)
            continue
        quiet += 1
        if quiet >= 2:           # 连续两轮无弹窗才算清完(避免动画期间误判)
            return True
        time.sleep(0.6)
    return True


def _settle_after_submit(e, max_rounds: int = 10) -> str:
    """提交后连点可能连弹的两层弹窗,并据弹窗文本判读是否真已报单。

    实测同花顺模拟盘提交一次会连弹两层,任一残留都会压住界面、把后续下单挡死
    在导航外(日志反复出现“底部导航找不到:交易”):
      ①「委托买入确认」→ 取消 / 确认买入(成对)
      ②「系统信息 委托已提交，合同号为…」→ 单键 确定(原生提示窗,出现略迟)
    返回: 'ok' = 见到 委托已提交/合同号 类并清完弹窗(已报单,可记账);
          'reject' = 见到 余额不足/失败 类(未成交,不记账);
          'unknown' = 未等到明确弹窗(交截图模型备援判读)。
    """
    saw_ok = saw_bad = False
    quiet = 0
    for _ in range(max_rounds):
        nodes = e.dump_nodes()
        joined = "|".join((n.get("text") or "") for n in nodes)
        if any(k in joined for k in _SUB_OK):
            saw_ok = True
        if any(k in joined for k in _SUB_BAD):
            saw_bad = True
        act = _act_dialog(nodes)
        if act:
            e.tap(act[1], act[2])
            quiet = 0
            time.sleep(1.0)
            continue
        quiet += 1
        if quiet >= 2:
            break
        time.sleep(0.5)
    if saw_bad and not saw_ok:
        return "reject"
    return "ok" if saw_ok else "unknown"


def _clear_leftover_dialog(e) -> bool:
    """下单前兜底:清掉上次遗留的确认/系统提示弹窗(成对窗或单键窗都可能残留)。"""
    return _dismiss_dialogs(e, max_rounds=3)


def place_order(e, side: str, code: str, name: str, shares: int,
                price=None) -> bool:
    """在 模拟盘 控制台对指定股票下单。返回是否成功提交。

    实测路径:交易 → 模拟账户控制台 → 买入/卖出 填单页
      表单从上到下三个 EditText: 代码 → 价格(可自动带出) → 数量
      提交按钮「买 入(模拟炒股) / 卖 出(模拟炒股)」
    """
    side_cn = "买入" if side == "buy" else "卖出"
    print(f"[ths] 模拟盘 {side_cn} {name}({code}) {shares}股 ...")
    if shares <= 0:
        return False
    # 0) 兜底:清掉上次遗留的确认弹窗,避免挡住本次导航
    if _clear_leftover_dialog(e):
        print("[ths] 已清掉残留确认弹窗")
    # 1) 到模拟控制台并切目标 tab
    if not _open_sim_buy_sell(e, side_cn):
        return False
    _collapse_panel(e)
    # 2) 代码(表单区第 1 个 EditText),输入后等解析自动带出价格/可买
    if not _set_edit(e, 0, code, (500, 560)):
        return False
    time.sleep(2)
    # 3) 价格:第 2 个 EditText。若未自动带价(占位仍是“价格”或空)才补填 row 价
    fields = _edits(e)
    if price and len(fields) > 1 and fields[1]["text"] in ("", "价格"):
        _set_edit(e, 1, f"{price:.2f}", (500, 780))
        time.sleep(0.3)
    # 4) 数量:第 3 个 EditText
    if not _set_edit(e, 2, str(shares), (500, 1053)):
        return False
    time.sleep(0.3)
    # 5) 提交(只认 (模拟炒股))
    if not _submit_sim(e):
        return False
    # 6) 连点两层弹窗,并按弹窗文本判是否已报单(权威);截图模型仅弹窗不明时兜底
    time.sleep(1.2)
    outcome = _settle_after_submit(e)
    # 7) 截图存档;必要时交模型备援判读
    from vision import order_succeeded
    ts = time.strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(config.SHOT_DIR, f"{ts}_order_{side}_{code}.png")
    e.screencap(shot)
    if outcome == "unknown":
        verdict = order_succeeded(shot, side)
        print(f"[ths] 弹窗未明确,交模型判读: {'成功' if verdict else '未确认'} -> {shot}")
    else:
        verdict = outcome == "ok"
        print(f"[ths] 弹窗判读: {'已报单(成功)' if verdict else '未成交(失败)'} [{outcome}] -> {shot}")
    return verdict
