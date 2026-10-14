# -*- coding: utf-8 -*-
"""视觉确认与图片识别:
把截图交给 Qwen3.5-4B 读图分析, 用于:
1. 持仓与资产识别: 提取现有股票、持仓数量、可用资金数量、总资产等;
2. 下单结果弹窗判读: 验证买入/卖出委托是否成功;
3. 界面状态确认与异常检测;
4. 所有识别结果自动记录到 MySQL 数据库中。
"""
from __future__ import annotations
import json
import os
import re
import time
from typing import Any

import config
import db_mysql
import model_client

_VQA_SYSTEM = "你是手机证券交易与截图分析助手。请准确识别图像内容，提取客观事实与关键数据。"


def _extract_json(text: str) -> dict | list | None:
    """从大模型返回的文本中提取结构化 JSON 数据。"""
    if not text:
        return None
    text_clean = text.strip()
    # 1. 直接 json.loads
    try:
        return json.loads(text_clean)
    except Exception:
        pass
    # 2. 匹配 ```json ... ```
    m = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", text_clean, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3. 匹配最外层的 { ... } 或 [ ... ]
    m = re.search(r"([\{\[].*[\}\]])", text_clean, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def ask(question: str, image_path: str, rec_type: str = "general", max_tokens: int = 400) -> str:
    """基础图像问答，并将识别结果持久化到 MySQL。"""
    if not os.path.exists(image_path):
        return f"[vision] 截图不存在: {image_path}"

    ans = "[vision] 读图失败"
    status = "failed"
    for attempt in range(2):
        try:
            ans = model_client.ask_image(question, image_path, _VQA_SYSTEM, max_tokens=max_tokens)
            status = "success"
            break
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            ans = f"[vision] 读图失败: {e}"
            status = "error"

    # 持久化记录到 MySQL
    try:
        db_mysql.save_image_recognition(
            image_path=image_path,
            recognition_type=rec_type,
            prompt=question,
            raw_result=ans,
            status=status
        )
    except Exception:
        pass

    return ans


def describe(image_path: str) -> str:
    """描述屏幕画面及关键信息。"""
    return ask("请描述这个屏幕画面:当前是什么页面/在显示什么?关键数字和文字尽量念出来。",
               image_path, rec_type="describe")


def order_succeeded(image_path: str, side: str) -> bool:
    """读下单结果弹窗, 粗判成功与否并落库。"""
    prompt = (
        f"这是刚才{'买入' if side == 'buy' else '卖出'}操作后的截图。是否显示下单/委托成功、已提交等字样?"
        f"还是出现了错误提示或需要再次确认?请直接回答:成功/失败/需确认,并给一句话依据。"
    )
    t = ask(prompt, image_path, rec_type="order_result")
    low = t.lower()
    success = any(k in t for k in ("成功", "已提交", "已报", "委托成功", "成交")) and not any(
        k in t for k in ("失败", "错误", "不可用", "余额不足", "数量不正确", "资金不足"))
    return success


def confirm_page(image_path: str, expected: str) -> bool:
    """确认当前是否是我们预期的页面。"""
    prompt = f"截图里是否能看到“{expected}”相关内容?回答 是/否 并简述。"
    t = ask(prompt, image_path, rec_type="confirm_page")
    return "是" in t and "否" not in t.split("是", 1)[0][-3:]


def recognize_portfolio(image_path: str) -> dict[str, Any]:
    """通过大模型识别持仓截图，提取现有股票、持仓数量、可用资金、总资产。

    返回字典结构:
    {
        "ok": bool,
        "cash": float | None,          # 手中可用资金
        "total_asset": float | None,   # 总资产
        "stocks": [                    # 现有股票列表
            {
                "code": "002156",
                "name": "通富微电",
                "qty": 1000,
                "available": 1000,
                "cost": 21.5,
                "price": 22.0,
                "market_value": 22000.0
            }, ...
        ],
        "raw": str
    }
    """
    if not os.path.exists(image_path):
        return {"ok": False, "error": f"截图不存在: {image_path}", "stocks": [], "cash": None, "total_asset": None}

    prompt = (
        "你是专业的股票软件截图阅读助手。请仔细识别这张同花顺模拟炒股持仓/资产截图中的数据，"
        "准确提取出：\n"
        "1. 可用资金（手中可用现金金额，数字）；\n"
        "2. 总资产（数字）；\n"
        "3. 当前持仓的全部现有股票列表，包含每只股票的：\n"
        "   - code: 6位股票代码（如 002156, 600522 等）\n"
        "   - name: 股票名称（如 通富微电 等）\n"
        "   - qty: 持仓数量/股数（整数）\n"
        "   - available: 可用数量/可卖股数（整数，若未明确写出则等于qty）\n"
        "   - cost: 成本价（浮点数，若无填 null）\n"
        "   - price: 当前价/现价（浮点数，若无填 null）\n"
        "   - market_value: 市值（浮点数，若无填 null）\n\n"
        "请严格只输出一个符合以下结构的标准 JSON，不要有任何多余文字或markdown代码块外解释：\n"
        "{\n"
        '  "cash": 150000.00,\n'
        '  "total_asset": 200000.00,\n'
        '  "stocks": [\n'
        '    {"code": "002156", "name": "通富微电", "qty": 1000, "available": 1000, "cost": 21.50, "price": 22.00, "market_value": 22000.00}\n'
        "  ]\n"
        "}\n"
        "如果没有任何持仓股票，stocks 填 []。如果读不到某项资金数字则填 null。"
    )

    raw_output = ask(prompt, image_path, rec_type="portfolio", max_tokens=600)
    parsed = _extract_json(raw_output)

    result = {
        "ok": False,
        "cash": None,
        "total_asset": None,
        "stocks": [],
        "raw": raw_output
    }

    if isinstance(parsed, dict):
        try:
            if parsed.get("cash") is not None:
                result["cash"] = float(str(parsed["cash"]).replace(",", ""))
            if parsed.get("total_asset") is not None:
                result["total_asset"] = float(str(parsed["total_asset"]).replace(",", ""))
            stocks_raw = parsed.get("stocks") or []
            clean_stocks = []
            for s in stocks_raw:
                if not isinstance(s, dict):
                    continue
                code = str(s.get("code") or "").strip()
                name = str(s.get("name") or "").strip()
                qty = int(float(str(s.get("qty") or s.get("shares") or 0).replace(",", "")))
                avail = int(float(str(s.get("available") or s.get("available_shares") or qty).replace(",", "")))
                cost = float(str(s["cost"]).replace(",", "")) if s.get("cost") is not None else None
                price = float(str(s["price"]).replace(",", "")) if s.get("price") is not None else None
                mv = float(str(s["market_value"]).replace(",", "")) if s.get("market_value") is not None else None
                if code or name:
                    clean_stocks.append({
                        "code": code,
                        "name": name,
                        "qty": qty,
                        "available": avail,
                        "cost": cost,
                        "price": price,
                        "market_value": mv
                    })
            result["stocks"] = clean_stocks
            result["ok"] = True
        except Exception as e:
            print(f"[vision] 解析持仓 JSON 异常: {e}")

    # 将结构化结果更新进 MySQL 记录
    try:
        db_mysql.save_image_recognition(
            image_path=image_path,
            recognition_type="portfolio_parsed",
            prompt=prompt,
            raw_result=raw_output,
            parsed_data=result,
            status="success" if result["ok"] else "partial"
        )
    except Exception:
        pass

    return result
