# -*- coding: utf-8 -*-
"""Qwen3.5-4B 常驻模型服务(本机 HTTP)。

POST /health          -> {ok, model_loaded, cuda, device}
POST /analyze_text    {system?, user}                  -> {ok, text}
POST /analyze_image   {system?, user, image:<本地绝对路径>} -> {ok, text}

运行须用带 CUDA torch 的 venv:  .venv/Scripts/python.exe model_server.py
权重 bf16、safetensors mmap 直进显存,避免吃满本机仅 ~2G 的空闲内存。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch

import config

_MODEL = None
_PROC = None
_LOCK = threading.Lock()


def _strip_thinking(text: str) -> str:
    """去掉 Qwen 思考段(若开启)。"""
    text = re.sub(r"<\|startofthink\|>.*?<\|endofthink\|>", "", text, flags=re.S)
    text = re.sub(r"<\|im_start\|>think.*?<\|im_end\|>", "", text, flags=re.S)
    return text.strip()


def _load():
    global _MODEL, _PROC
    import torch
    from transformers import AutoProcessor, Qwen3_5ForConditionalGeneration

    use_cuda = torch.cuda.is_available()
    dtype = torch.bfloat16
    load_kw = dict(torch_dtype=dtype)
    try:
        from accelerate import dispatch_model  # noqa: F401 仅探测
        load_kw["device_map"] = {"": 0} if use_cuda else None
    except ImportError:
        load_kw["device_map"] = None

    _MODEL = Qwen3_5ForConditionalGeneration.from_pretrained(config.MODEL_DIR, **load_kw)
    if not use_cuda or (load_kw.get("device_map") is None and not _MODEL.device.type.startswith("cuda")):
        _MODEL.to("cuda" if use_cuda else "cpu")
    _MODEL.eval()
    _PROC = AutoProcessor.from_pretrained(config.MODEL_DIR)
    print(f"[model_server] loaded device={next(_MODEL.parameters()).device} "
          f"cuda={use_cuda} dtype={dtype}", flush=True)


def _generate(system: str | None, user: str, image_path: str | None,
              max_new: int) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    content = []
    image = None
    if image_path:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        # 等比缩到最长边,控制图像 token
        w, h = img.size
        scale = min(1.0, config.VISION_MAX_EDGE / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        image = img
        content.append({"type": "image", "image": image})
    content.append({"type": "text", "text": user})
    messages.append({"role": "user", "content": content})

    prompt = _PROC.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    kwargs = {"text": [prompt], "return_tensors": "pt"}
    if image is not None:
        kwargs["images"] = [image]
    inputs = _PROC(**kwargs).to(_MODEL.device)
    with torch.inference_mode():
        out = _MODEL.generate(
            **inputs, max_new_tokens=max_new, do_sample=False,
            pad_token_id=_PROC.tokenizer.pad_token_id or _PROC.tokenizer.eos_token_id)
    new = out[:, inputs["input_ids"].shape[1]:]
    text = _PROC.batch_decode(new, skip_special_tokens=True)[0]
    return _strip_thinking(text)


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_POST(self):
        import torch
        try:
            req = self._read_json()
            t0 = time.time()
            if self.path == "/analyze_text":
                if _MODEL is None:
                    return self._send({"ok": False, "error": "model not loaded"}, 503)
                with _LOCK:
                    text = _generate(req.get("system"), req.get("user", ""), None,
                                     int(req.get("max_tokens", 300)))
                self._send({"ok": True, "text": text, "sec": round(time.time() - t0, 2)})
            elif self.path == "/analyze_image":
                if _MODEL is None:
                    return self._send({"ok": False, "error": "model not loaded"}, 503)
                img = req.get("image")
                if not img or not os.path.exists(img):
                    return self._send({"ok": False, "error": "image path invalid"}, 400)
                with _LOCK:
                    text = _generate(req.get("system"), req.get("user", ""), img,
                                     int(req.get("max_tokens", 400)))
                self._send({"ok": True, "text": text, "sec": round(time.time() - t0, 2)})
            elif self.path == "/health":
                dev = None
                if _MODEL is not None:
                    dev = str(next(_MODEL.parameters()).device)
                self._send({"ok": True, "model_loaded": _MODEL is not None,
                            "cuda": torch.cuda.is_available(), "device": dev})
            else:
                self._send({"ok": False, "error": f"no route {self.path}"}, 404)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send({"ok": False, "error": repr(e)}, 500)


def _already_serving() -> bool:
    """8710 上已有健康服务(健康检查)时返回 True,避免 Windows 同端口双绑。"""
    import urllib.request
    try:
        with urllib.request.urlopen(config.MODEL_URL + "/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=config.MODEL_PORT)
    ap.add_argument("--preload", action="store_true", default=True)
    args = ap.parse_args()
    if _already_serving():
        print(f"[model_server] 检测到已有服务在 {config.MODEL_URL},退出本进程(防双绑)", flush=True)
        return
    if args.preload:
        _load()
    try:
        srv = ThreadingHTTPServer((config.MODEL_HOST, args.port), _H)
    except OSError as e:
        print(f"[model_server] 绑定 {config.MODEL_HOST}:{args.port} 失败: {e}", flush=True)
        return
    print(f"[model_server] listening on {config.MODEL_HOST}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    import os
    main()
