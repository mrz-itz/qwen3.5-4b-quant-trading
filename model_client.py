# -*- coding: utf-8 -*-
"""model_server 的 HTTP 客户端(进程内共用)。"""
from __future__ import annotations
import json
import urllib.request

import config


def _post(path: str, payload: dict, timeout: int = 300):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(config.MODEL_URL + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def health() -> dict:
    try:
        return _post("/health", {}, timeout=8)
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def chat(system: str | None, user: str, max_tokens: int = 300) -> str:
    j = _post("/analyze_text", {"system": system, "user": user,
                                "max_tokens": max_tokens})
    if not j.get("ok"):
        raise RuntimeError(f"model_server /analyze_text error: {j.get('error')}")
    return j.get("text", "")


def ask_image(user: str, image_path: str, system: str | None = None,
              max_tokens: int = 400) -> str:
    j = _post("/analyze_image", {"system": system, "user": user,
                                 "image": image_path, "max_tokens": max_tokens})
    if not j.get("ok"):
        raise RuntimeError(f"model_server /analyze_image error: {j.get('error')}")
    return j.get("text", "")


def switch_model(model_name: str) -> dict:
    """向运行中的 model_server 发送热切换模型请求。"""
    try:
        return _post("/switch_model", {"model": model_name}, timeout=120)
    except Exception as e:
        return {"ok": False, "error": repr(e)}


if __name__ == "__main__":
    import sys
    sys.stdout.write("health: ")
    sys.stdout.flush()
    import pprint
    pprint.pprint(health())
