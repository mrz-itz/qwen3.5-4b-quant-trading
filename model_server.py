# -*- coding: utf-8 -*-
"""模型服务 (本机 HTTP): 支持动态载入与多模型快速切换。

接口:
POST /health          -> {ok, model_loaded, active_model, is_multimodal, cuda, device}
POST /analyze_text    {system?, user}                      -> {ok, text}
POST /analyze_image   {system?, user, image:<本地绝对路径>} -> {ok, text}
POST /switch_model    {model:<模型名或路径>}               -> {ok, model, path}

运行须用带 CUDA torch 的 venv:  .venv/Scripts/python.exe model_server.py
权重 bf16、safetensors mmap 直进显存, 切换模型时自动清空 GPU 显存。
"""
from __future__ import annotations
import argparse
import gc
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch

import config
import model_manager

_MODEL = None
_PROC = None
_CURRENT_MODEL_NAME = ""
_CURRENT_MODEL_PATH = ""
_IS_MULTIMODAL = True
_LOCK = threading.Lock()


def _strip_thinking(text: str) -> str:
    """去掉 Qwen / DeepSeek 思考段(若开启)。"""
    text = re.sub(r"<\|startofthink\|>.*?<\|endofthink\|>", "", text, flags=re.S)
    text = re.sub(r"<\|im_start\|>think.*?<\|im_end\|>", "", text, flags=re.S)
    return text.strip()


def _unload_current_model():
    """彻底释放旧模型显存。"""
    global _MODEL, _PROC
    _MODEL = None
    _PROC = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _load(model_path: str | None = None):
    """动态载入目标模型, 自动适配多模态视觉模型与纯语言模型。"""
    global _MODEL, _PROC, _CURRENT_MODEL_NAME, _CURRENT_MODEL_PATH, _IS_MULTIMODAL

    target_dir = model_path or config.get_active_model_dir()
    target_dir = os.path.abspath(target_dir)
    if not os.path.isdir(target_dir):
        # 兜底到根目录 qwen3.5-4B
        target_dir = os.path.abspath(os.path.join(config.BASE_DIR, "qwen3.5-4B"))

    _unload_current_model()

    use_cuda = torch.cuda.is_available()
    dtype = torch.bfloat16
    load_kw = dict(torch_dtype=dtype)
    try:
        from accelerate import dispatch_model  # noqa: F401 仅探测
        load_kw["device_map"] = {"": 0} if use_cuda else None
    except ImportError:
        load_kw["device_map"] = None

    print(f"[model_server] 正在载入模型: {target_dir} ...", flush=True)

    # 1. 检测配置与架构
    from transformers import AutoConfig, AutoTokenizer, AutoProcessor

    cfg = model_manager._inspect_hf_config(target_dir)
    is_vision = cfg["is_vision"]
    archs = cfg["architectures"]

    loaded_model = None
    loaded_proc = None

    # 2. 模型实例化
    # 2.1 针对 Qwen3.5 特化处理 (速度与兼容最佳)
    if any("Qwen3_5" in a for a in archs):
        try:
            from transformers import Qwen3_5ForConditionalGeneration
            loaded_model = Qwen3_5ForConditionalGeneration.from_pretrained(target_dir, **load_kw)
            loaded_proc = AutoProcessor.from_pretrained(target_dir)
            is_vision = True
        except Exception as ex:
            print(f"[model_server] Qwen3_5 专用类载入重试: {ex}")

    # 2.2 通用多模态 Vision2Seq / ImageTextToText 尝试
    if loaded_model is None and is_vision:
        for loader_cls_name in ("AutoModelForImageTextToText", "AutoModelForVision2Seq"):
            loader_cls = getattr(__import__("transformers", fromlist=[loader_cls_name]), loader_cls_name, None)
            if loader_cls is not None:
                try:
                    loaded_model = loader_cls.from_pretrained(target_dir, trust_remote_code=True, **load_kw)
                    loaded_proc = AutoProcessor.from_pretrained(target_dir, trust_remote_code=True)
                    break
                except Exception:
                    pass

    # 2.3 通用 CausalLM 尝试 (纯文本或兼容 CausalLM 架构的多模态)
    if loaded_model is None:
        from transformers import AutoModelForCausalLM
        try:
            loaded_model = AutoModelForCausalLM.from_pretrained(target_dir, trust_remote_code=True, **load_kw)
            try:
                loaded_proc = AutoProcessor.from_pretrained(target_dir, trust_remote_code=True)
            except Exception:
                loaded_proc = AutoTokenizer.from_pretrained(target_dir, trust_remote_code=True)
                is_vision = False
        except Exception as e:
            raise RuntimeError(f"无法载入模型 {target_dir}: {e}")

    if not use_cuda or (load_kw.get("device_map") is None and not loaded_model.device.type.startswith("cuda")):
        loaded_model.to("cuda" if use_cuda else "cpu")
    loaded_model.eval()

    _MODEL = loaded_model
    _PROC = loaded_proc
    _IS_MULTIMODAL = is_vision
    _CURRENT_MODEL_PATH = target_dir
    _CURRENT_MODEL_NAME = os.path.basename(target_dir)

    print(f"[model_server] 载入成功! 模型={_CURRENT_MODEL_NAME} 多模态={_IS_MULTIMODAL} "
          f"device={next(_MODEL.parameters()).device} cuda={use_cuda}", flush=True)


def _generate(system: str | None, user: str, image_path: str | None,
              max_new: int) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})

    content = []
    image = None
    if image_path and os.path.exists(image_path) and _IS_MULTIMODAL:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        w, h = img.size
        scale = min(1.0, config.VISION_MAX_EDGE / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        image = img
        content.append({"type": "image", "image": image})
    elif image_path and not _IS_MULTIMODAL:
        user = f"[{os.path.basename(image_path)}] {user}"

    content.append({"type": "text", "text": user})
    messages.append({"role": "user", "content": content})

    if hasattr(_PROC, "apply_chat_template"):
        prompt = _PROC.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    else:
        # 兼容简单 tokenizer
        prompt = f"System: {system or ''}\nUser: {user}\nAssistant:"

    kwargs = {"text": [prompt], "return_tensors": "pt"}
    if image is not None:
        kwargs["images"] = [image]

    inputs = _PROC(**kwargs).to(_MODEL.device)
    pad_id = getattr(_PROC, "tokenizer", _PROC).pad_token_id or getattr(_PROC, "tokenizer", _PROC).eos_token_id

    with torch.inference_mode():
        out = _MODEL.generate(
            **inputs, max_new_tokens=max_new, do_sample=False, pad_token_id=pad_id
        )
    new = out[:, inputs["input_ids"].shape[1]:]
    tokenizer = getattr(_PROC, "tokenizer", _PROC)
    text = tokenizer.batch_decode(new, skip_special_tokens=True)[0]
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
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_POST(self):
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

            elif self.path == "/switch_model":
                target_model = req.get("model")
                if not target_model:
                    return self._send({"ok": False, "error": "missing model parameter"}, 400)
                models = model_manager.list_models()
                matched = next((m for m in models if m["name"] == target_model or os.path.basename(m["path"]) == target_model), None)
                if not matched and os.path.isdir(target_model):
                    matched = {"name": os.path.basename(target_model), "path": os.path.abspath(target_model)}

                if not matched:
                    return self._send({"ok": False, "error": f"model '{target_model}' not found"}, 404)

                with _LOCK:
                    _load(matched["path"])
                    model_manager.set_active_model(matched["name"], matched["path"])

                self._send({"ok": True, "active_model": _CURRENT_MODEL_NAME, "is_multimodal": _IS_MULTIMODAL,
                            "path": _CURRENT_MODEL_PATH})

            elif self.path == "/health":
                dev = None
                if _MODEL is not None:
                    dev = str(next(_MODEL.parameters()).device)
                self._send({
                    "ok": True,
                    "model_loaded": _MODEL is not None,
                    "active_model": _CURRENT_MODEL_NAME,
                    "is_multimodal": _IS_MULTIMODAL,
                    "cuda": torch.cuda.is_available(),
                    "device": dev
                })
            else:
                self._send({"ok": False, "error": f"no route {self.path}"}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send({"ok": False, "error": repr(e)}, 500)


def _already_serving() -> bool:
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
        print(f"[model_server] 检测到已有服务在 {config.MODEL_URL}, 退出本进程(防双绑)", flush=True)
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
    main()
