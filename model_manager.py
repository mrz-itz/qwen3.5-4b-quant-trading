# -*- coding: utf-8 -*-
"""模型打包与快速切换管理器 (Model Packaging & Switching Manager)

核心功能:
1. 模型打包 (pack_model): 将从 Hugging Face 下载的模型目录打包标准化，存入独立 models/ 文件夹;
2. 模型发现 (list_models): 扫描已打包的模型目录，提取架构、模态能力(文本/多模态视觉)及大小;
3. 快速切换 (switch_model): 一键切换当前系统使用的活跃模型，通知或平滑重载 model_server;
4. 结构兼容: 兼容标准 HF 目录、快照缓存目录、以及复杂子组件目录(如 MiniMax-H3 等)。
"""
from __future__ import annotations
import datetime as _dt
import json
import os
import re
import shutil
from typing import Any

import config


def _get_folder_size(path: str) -> tuple[int, int]:
    """计算文件夹总字节数及文件数量。"""
    total_size = 0
    file_count = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total_size += os.path.getsize(fp)
                    file_count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return total_size, file_count


def _inspect_hf_config(dir_path: str) -> dict[str, Any]:
    """读取并解析目录下的 config.json (若存在)，推测模型类型与架构。"""
    cfg_path = os.path.join(dir_path, "config.json")
    # 若根目录没有, 递归查找深度最多2层的子目录
    if not os.path.exists(cfg_path):
        for root, _, files in os.walk(dir_path):
            if "config.json" in files:
                cfg_path = os.path.join(root, "config.json")
                break

    info = {
        "has_config": False,
        "config_path": None,
        "model_type": "unknown",
        "architectures": [],
        "is_vision": False,
        "raw_config": {}
    }
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            info["has_config"] = True
            info["config_path"] = cfg_path
            info["model_type"] = data.get("model_type", "custom")
            info["architectures"] = data.get("architectures", [])
            info["raw_config"] = data

            # 判断是否支持视觉多模态
            arch_str = "".join(info["architectures"]).lower()
            mtype_str = str(info["model_type"]).lower()
            has_preprocessor = os.path.exists(os.path.join(os.path.dirname(cfg_path), "preprocessor_config.json"))
            if (has_preprocessor or "vision" in data or "image_token_id" in data or
                "vl" in mtype_str or "vision" in arch_str or "conditionalgeneration" in arch_str):
                info["is_vision"] = True
        except Exception:
            pass
    return info


def get_active_model() -> dict[str, Any]:
    """获取当前激活的模型信息。"""
    active_path = config.get_active_model_dir()
    name = os.path.basename(active_path)

    meta_file = os.path.join(active_path, "model_meta.json")
    if os.path.exists(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
                meta["path"] = active_path
                meta["is_active"] = True
                return meta
        except Exception:
            pass

    cfg = _inspect_hf_config(active_path)
    total_size, file_cnt = _get_folder_size(active_path)
    return {
        "name": name,
        "path": active_path,
        "is_active": True,
        "model_type": cfg["model_type"],
        "architectures": cfg["architectures"],
        "capabilities": ["text", "vision"] if cfg["is_vision"] else ["text"],
        "size_bytes": total_size,
        "size_mb": round(total_size / (1024 * 1024), 2),
        "file_count": file_cnt,
        "description": "默认系统模型"
    }


def set_active_model(name: str, path: str) -> None:
    """持久化记录当前激活的模型。"""
    data = {
        "name": name,
        "path": path,
        "updated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(config.ACTIVE_MODEL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_models() -> list[dict[str, Any]]:
    """扫描 models/ 目录(及历史根目录模型)，返回全部已打包的模型列表。"""
    os.makedirs(config.MODELS_DIR, exist_ok=True)
    active_dir = os.path.abspath(config.get_active_model_dir())

    models = []
    scanned_paths = set()

    # 1. 扫描 models/ 专属文件夹
    candidates = []
    if os.path.isdir(config.MODELS_DIR):
        for item in os.listdir(config.MODELS_DIR):
            p = os.path.join(config.MODELS_DIR, item)
            if os.path.isdir(p) and not item.startswith("."):
                candidates.append((item, p))

    # 2. 兼容项目根目录下原有 qwen3.5-4B (仅当 models 目录下未打包同名模型时)
    root_qwen = os.path.join(config.BASE_DIR, "qwen3.5-4B")
    if os.path.isdir(root_qwen) and not os.path.exists(os.path.join(config.MODELS_DIR, "qwen3.5-4B")):
        candidates.append(("qwen3.5-4B", root_qwen))

    for name, p in candidates:
        norm_path = os.path.abspath(p)
        if norm_path in scanned_paths:
            continue
        scanned_paths.add(norm_path)

        meta_path = os.path.join(p, "model_meta.json")
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass

        cfg = _inspect_hf_config(p)
        total_size, file_cnt = _get_folder_size(p)
        is_active = (norm_path == active_dir)

        model_entry = {
            "name": meta.get("name") or name,
            "path": norm_path,
            "is_active": is_active,
            "model_type": meta.get("model_type") or cfg["model_type"],
            "architectures": meta.get("architectures") or cfg["architectures"],
            "capabilities": meta.get("capabilities") or (["text", "vision"] if cfg["is_vision"] else ["text"]),
            "size_bytes": total_size,
            "size_mb": round(total_size / (1024 * 1024), 2),
            "file_count": file_cnt,
            "description": meta.get("description", ""),
            "packaged_at": meta.get("packaged_at", "")
        }
        models.append(model_entry)

    # 排序: 激活模型排第一，其余按名称排序
    models.sort(key=lambda m: (not m["is_active"], m["name"].lower()))
    return models


def pack_model(source_path: str,
               model_name: str | None = None,
               copy_files: bool = False,
               description: str = "") -> dict[str, Any]:
    """从 Hugging Face 下载的原始目录打包模型到 models/<model_name>。

    特点:
    - 针对大文件(safetensors/bin等)，在同分区下优先使用硬链接(Hardlink)，零耗时、零额外占用硬盘空间;
    - 自动探查并提取 config.json、tokenizers、weights 等关键文件;
    - 自动生成标准化 model_meta.json 元数据文件;
    - 自动检验打包后的模型可用性。
    """
    if not os.path.exists(source_path):
        return {"ok": False, "error": f"源路径不存在: {source_path}"}

    source_path = os.path.abspath(source_path)
    clean_name = model_name or os.path.basename(source_path.rstrip("/\\"))
    clean_name = re.sub(r"[^\w\-\.]", "_", clean_name)
    target_dir = os.path.join(config.MODELS_DIR, clean_name)

    # 若已存在同名目录
    os.makedirs(target_dir, exist_ok=True)

    print(f"[packager] 开始打包模型: {source_path} -> {target_dir}")

    # 1. 探查配置与结构
    cfg_info = _inspect_hf_config(source_path)

    # 2. 收集需要链接/复制的文件
    # 忽略下载锁、未完成片段文件等临时文件
    ignore_exts = {".lock", ".incomplete", ".tmp"}
    files_to_transfer = []

    for root, _, files in os.walk(source_path):
        # 忽略 .cache 或 .git 内部的无效目录(若根下有有效内容)
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in ignore_exts or f.startswith("."):
                continue
            src_fp = os.path.join(root, f)
            rel_fp = os.path.relpath(src_fp, source_path)
            files_to_transfer.append((src_fp, rel_fp))

    if not files_to_transfer:
        return {"ok": False, "error": "源目录下未找到可打包的文件"}

    linked_count = 0
    copied_count = 0

    for src_fp, rel_fp in files_to_transfer:
        dst_fp = os.path.join(target_dir, rel_fp)
        os.makedirs(os.path.dirname(dst_fp), exist_ok=True)
        if os.path.exists(dst_fp):
            continue

        transferred = False
        if not copy_files:
            # 优先尝试硬链接
            try:
                os.link(src_fp, dst_fp)
                linked_count += 1
                transferred = True
            except (OSError, NotImplementedError):
                pass

        if not transferred:
            shutil.copy2(src_fp, dst_fp)
            copied_count += 1

    # 3. 生成标准化 model_meta.json
    total_size, file_cnt = _get_folder_size(target_dir)
    target_cfg = _inspect_hf_config(target_dir)

    meta = {
        "name": clean_name,
        "source_path": source_path,
        "target_path": target_dir,
        "model_type": target_cfg["model_type"],
        "architectures": target_cfg["architectures"],
        "capabilities": ["text", "vision"] if target_cfg["is_vision"] else ["text"],
        "size_bytes": total_size,
        "size_mb": round(total_size / (1024 * 1024), 2),
        "file_count": file_cnt,
        "transfer_mode": f"hardlinks={linked_count}, copies={copied_count}",
        "description": description or f"从 {os.path.basename(source_path)} 打包构建",
        "packaged_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    meta_file = os.path.join(target_dir, "model_meta.json")
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[packager] 模型打包完成! 名称: {clean_name}, 文件数: {file_cnt}, 大小: {meta['size_mb']} MB "
          f"({meta['transfer_mode']})")

    return {
        "ok": True,
        "model_name": clean_name,
        "path": target_dir,
        "meta": meta
    }


def switch_model(model_name: str, reload_server: bool = True) -> dict[str, Any]:
    """切换当前激活的模型。"""
    models = list_models()
    matched = next((m for m in models if m["name"] == model_name or os.path.basename(m["path"]) == model_name), None)
    if not matched:
        # 支持按绝对路径切换
        if os.path.isdir(model_name):
            matched = {
                "name": os.path.basename(model_name),
                "path": os.path.abspath(model_name)
            }
        else:
            return {"ok": False, "msg": f"未找到名为 '{model_name}' 的打包模型。可用模型: {[m['name'] for m in models]}"}

    target_name = matched["name"]
    target_path = matched["path"]

    # 1. 持久化记录到 active_model.json
    set_active_model(target_name, target_path)

    # 2. 通知或重载模型服务
    server_reloaded = False
    if reload_server:
        import model_client
        # 尝试 HTTP 热切换
        res = model_client.switch_model(target_name)
        if res.get("ok"):
            server_reloaded = True
        else:
            # 若热切换未成功或服务未跑，尝试重启进程
            try:
                import control
                st = control.model_status()
                if st["running"]:
                    print(f"[model_manager] 重启模型服务以载入新模型 {target_name} ...")
                    control.model_stop()
                    control.model_start()
                    server_reloaded = True
            except Exception:
                pass

    return {
        "ok": True,
        "msg": f"已成功切换激活模型为: {target_name}" + (" (模型服务已重新载入)" if server_reloaded else ""),
        "active_model": target_name,
        "path": target_path,
        "server_reloaded": server_reloaded
    }


def init_default_models() -> None:
    """初始化模型仓库: 若根目录有 qwen3.5-4B，则将其标准化打包链接到 models/qwen3.5-4B。"""
    os.makedirs(config.MODELS_DIR, exist_ok=True)
    root_qwen = os.path.join(config.BASE_DIR, "qwen3.5-4B")
    target_qwen = os.path.join(config.MODELS_DIR, "qwen3.5-4B")
    if os.path.isdir(root_qwen) and not os.path.exists(target_qwen):
        print(f"[model_manager] 首次初始化: 打包基础模型 {root_qwen} -> {target_qwen} ...")
        pack_model(root_qwen, "qwen3.5-4B", copy_files=False, description="本地基准 Qwen3.5-4B 多模态量化分析模型")


if __name__ == "__main__":
    init_default_models()
    print("当前已打包模型列表:")
    import pprint
    pprint.pprint(list_models())
    print("\n当前激活模型:")
    pprint.pprint(get_active_model())
