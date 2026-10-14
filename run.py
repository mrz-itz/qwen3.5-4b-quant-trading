# -*- coding: utf-8 -*-
"""命令行入口。

用法(统一用 venv python):
  python run.py env             环境自检(GPU/模型/接口)
  python run.py serve           前台启动模型服务
  python run.py emu [start|status]  雷电模拟器开关机/状态
  python run.py probe           校准:截当前屏+导出 UI 文本(需模拟器+同花顺在前台)
  python run.py once [--dry|--live] [--force]  跑一轮分析
  python run.py loop [--dry|--live] [--force]  全自动盯盘
"""
from __future__ import annotations
import argparse
import os
import sys

import config


def cmd_env(args=None):
    import importlib.util
    print("== 环境自检 ==")
    print(f"模型目录存在: {os.path.isdir(config.MODEL_DIR)} -> {config.MODEL_DIR}")
    try:
        import torch
        print(f"torch {torch.__version__} | CUDA可用: {torch.cuda.is_available()}"
              + (f" | {torch.cuda.get_device_name(0)}" if torch.cuda.is_available() else ""))
    except Exception as e:
        print("torch 导入失败:", e)
    for m in ("transformers", "PIL"):
        spec = importlib.util.find_spec(m)
        print(f"{m}: {'有' if spec else '缺'}")
    try:
        import stocks_loader
        print(f"股票池: {len(stocks_loader.load_stocks())} 只")
    except Exception as e:
        print("股票池解析失败:", e)
    import model_client
    h = model_client.health()
    print("model_server 健康:", h)
    if not h.get("ok"):
        print("  (服务未启动。启动命令: python run.py serve;或先运行 loop 会自动拉起)")


def cmd_serve(args=None):
    import model_server
    # 由 run.py serve 进入时 sys.argv 含多余子命令,剥离后再交给 model_server
    import sys
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a == "--port"] or []
    model_server.main()


def cmd_emu(args):
    from emulator import Emulator
    e = Emulator()
    if args.mode == "status":
        print("实例:", e.list_instances())
        ok = e.connect_adb(quiet=True)
        print("adb 在线:", ok)
        if ok:
            print("屏幕:", e.screen_size())
    elif args.mode == "start":
        if e.start(wait_boot=True):
            print("模拟器已启动,adb 在线")
        else:
            print("启动超时/失败")
    elif args.mode == "stop":
        print("ldconsole quit:", e.ldconsole("quit", "--name", e.name).strip())


def cmd_probe(args):
    """把当前屏幕 dump 到 shots/probe-<ts>.png + UI 文本,供编写 selectors。"""
    import json
    import time as _t
    from emulator import Emulator
    e = Emulator()
    if not e.connect_adb(quiet=True):
        print("请先运行 python run.py emu start 并让同花顺显示在前台")
        return
    ts = _t.strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(config.SHOT_DIR, f"probe_{ts}.png")
    e.screencap(shot)
    nodes = e.dump_nodes()
    print(f"截图已存: {shot}")
    print("屏幕可见文本:")
    for n in nodes:
        tag = n["text"] or n["desc"]
        if tag:
            print(f"  [{n['cx']},{n['cy']}] {tag[:60]}")
    with open(os.path.join(config.DATA_DIR, f"probe_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump([n for n in nodes if (n["text"] or n["desc"])], f, ensure_ascii=False, indent=1)
    print("节点已存 data/probe_*.json")


def _is_dry(args) -> bool:
    return not (args.live and not args.dry)   # 默认 dry;--live 才真实下单


def cmd_once(args):
    import main_loop
    main_loop.ensure_model_server()
    from ledger import positions_from_ledger
    holdings = positions_from_ledger()
    dry = _is_dry(args)
    st = main_loop.run_cycle(dry=dry, holdings=holdings, cash=None, total_asset=None,
                             executor=None if dry else main_loop._make_executor(False))
    print("本轮统计:", st)


def cmd_loop(args):
    import main_loop
    main_loop.monitor(dry=_is_dry(args), force=args.force)


def main():
    ap = argparse.ArgumentParser(description="大模型量化交易(同花顺模拟盘)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("env").set_defaults(func=cmd_env)
    sub.add_parser("serve").set_defaults(func=cmd_serve)

    e = sub.add_parser("emu"); e.add_argument("mode", choices=["status", "start", "stop"])
    e.set_defaults(func=cmd_emu)

    sub.add_parser("probe").set_defaults(func=cmd_probe)

    for c in ("once", "loop"):
        p = sub.add_parser(c)
        p.add_argument("--dry", action="store_true", help="只分析不下单(默认)")
        p.add_argument("--live", action="store_true", help="在模拟盘真实下单")
        p.add_argument("--force", action="store_true", help="无视交易时段立即执行")
        p.set_defaults(func=cmd_once if c == "once" else cmd_loop)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
