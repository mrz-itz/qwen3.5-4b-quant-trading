# -*- coding: utf-8 -*-
"""视觉确认:把截图交给 Qwen3.5-4B「看」,用于验证界面状态 / 图表情绪。

用途示例:
- 点完「买入/卖出」后问:这笔下单成功了吗?确认按钮文案是什么?
- uiautomator 拿不到图表的区域时,让它描述画面(涨/跌/形态)。
"""
from __future__ import annotations
import os
import time

import config
import model_client

_VQA_SYSTEM = "你是手机截图阅读助手。用简体中文简洁回答与截图内容有关的问题,只陈述你能看到的客观事实。"


def ask(question: str, image_path: str) -> str:
    if not os.path.exists(image_path):
        return f"[vision] 截图不存在: {image_path}"
    for attempt in range(2):
        try:
            return model_client.ask_image(question, image_path, _VQA_SYSTEM, max_tokens=300)
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            return f"[vision] 读图失败: {e}"
    return "[vision] 读图失败"


def describe(image_path: str) -> str:
    return ask("请描述这个屏幕画面:当前是什么页面/在显示什么?关键数字和文字尽量念出来。", image_path)


def order_succeeded(image_path: str, side: str) -> bool:
    """读下单结果弹窗,粗判成功与否。"""
    t = ask(f"这是刚才{'买入' if side == 'buy' else '卖出'}操作后的截图。是否显示下单/委托成功、已提交等字样?"
            f"还是出现了错误提示或需要再次确认?请直接回答:成功/失败/需确认,并给一句话依据。", image_path)
    low = t.lower()
    return any(k in t for k in ("成功", "已提交", "已报", "委托成功", "成交")) and not any(
        k in t for k in ("失败", "错误", "不可用", "余额不足", "数量不正确"))


def confirm_page(image_path: str, expected: str) -> bool:
    """确认当前是否是我们预期的页面。"""
    t = ask(f"截图里是否能看到“{expected}”相关内容?回答 是/否 并简述。", image_path)
    return "是" in t and "否" not in t.split("是", 1)[0][-3:]
