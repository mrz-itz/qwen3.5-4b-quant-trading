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
    try:
        import db_mysql
        db_ok = db_mysql.init_db()
        print(f"MySQL 数据库: {'连通且表结构正常' if db_ok else '连接失败'} (库: {config.MYSQL_DB})")
    except Exception as e:
        print("MySQL 检查失败:", e)
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
    """把当前屏幕 dump 到 shots/<ts>_probe.png + UI 文本,供编写 selectors。"""
    import json
    import time as _t
    from emulator import Emulator
    e = Emulator()
    if not e.connect_adb(quiet=True):
        print("请先运行 python run.py emu start 并让同花顺显示在前台")
        return
    ts = _t.strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(config.SHOT_DIR, f"{ts}_probe.png")
    e.screencap(shot)
    nodes = e.dump_nodes()
    print(f"截图已存: {shot}")
    print("屏幕可见文本:")
    for n in nodes:
        tag = n["text"] or n["desc"]
        if tag:
            print(f"  [{n['cx']},{n['cy']}] {tag[:60]}")
    probe_json = os.path.join(config.DATA_DIR, f"{ts}_probe.json")
    with open(probe_json, "w", encoding="utf-8") as f:
        json.dump([n for n in nodes if (n["text"] or n["desc"])], f, ensure_ascii=False, indent=1)
    print(f"节点已存 {probe_json}")


def cmd_portfolio(args):
    """持仓识别: 支持模拟器当前屏幕、指定已有图片、或查看数据库最新持仓。"""
    import db_mysql
    if getattr(config, "AUTO_CLEANUP_ON_START", True):
        import cleaner
        cleaner.cleanup_system()

    # 1) 若传入了具体图片路径，则直接识别该图片
    if getattr(args, "image", None):
        import vision
        img_path = os.path.abspath(args.image)
        print(f"正在识别持仓图片: {img_path} ...")
        res = vision.recognize_portfolio(img_path)
        print("模型识别原始返回:")
        print(res.get("raw"))
        if res.get("ok"):
            bid = db_mysql.save_portfolio(
                cash=res.get("cash") or 0.0,
                total_asset=res.get("total_asset"),
                stocks=res.get("stocks") or [],
                shot_path=img_path,
                source="image_vision"
            )
            print(f"-> 识别成功并存入 MySQL (balance_id={bid})")
        return

    # 2) 若指定连接模拟器获取
    if getattr(args, "emu", False):
        from emulator import Emulator
        from ths_app import get_portfolio
        e = Emulator()
        if not e.connect_adb(quiet=True):
            print("模拟器未连接，请先运行 python run.py emu start 并打开持仓页")
            return
        print("正在通过模拟器同花顺进行持仓识别 ...")
        holdings, cash, asset = get_portfolio(e, save_to_db=True)
        return

    # 3) 默认查看 MySQL 中最新一次记录的持仓
    latest = db_mysql.get_latest_portfolio()
    if latest:
        print(f"== MySQL 最新持仓快照 (记录时间: {latest['record_time']}, 来源: {latest['source']}) ==")
        print(f"手中可用资金: {latest['cash']:,.2f} 元 | 预估总资产: {latest.get('total_asset', 0.0) or 0.0:,.2f} 元")
        print(f"关联截图: {latest.get('screenshot_path') or '无'}")
        stocks = latest.get("stocks") or []
        print(f"持仓股票 ({len(stocks)} 只):")
        for s in stocks:
            print(f"  [{s['stock_code']}] {s['stock_name']:<8} "
                  f"持仓: {s['shares']:>6} 股 (可用: {s['available_shares']:>6} 股) | "
                  f"成本: {s['cost_price'] or 0.0:>7.2f} | 现价: {s['current_price'] or 0.0:>7.2f} | "
                  f"市值: {s['market_value'] or 0.0:>9.2f}")
    else:
        print("MySQL 中暂无持仓识别记录。可使用 --emu 在模拟器中识别，或 --image <图片路径> 进行识别。")


def cmd_clean(args):
    """手动执行系统清理: 清理早于指定天数(默认30天/1个月)的图片、MySQL识别记录和日志。"""
    import cleaner
    days = getattr(args, "days", None) or config.CLEANUP_RETENTION_DAYS
    cleaner.cleanup_system(days=days)


def _is_dry(args) -> bool:
    return not (args.live and not args.dry)   # 默认 dry;--live 才真实下单


def cmd_once(args):
    if getattr(config, "AUTO_CLEANUP_ON_START", True):
        import cleaner
        cleaner.cleanup_system()
    import main_loop
    main_loop.ensure_model_server()
    from ledger import positions_from_ledger
    holdings = positions_from_ledger()
    dry = _is_dry(args)
    st = main_loop.run_cycle(dry=dry, holdings=holdings, cash=None, total_asset=None,
                             executor=None if dry else main_loop._make_executor(False))
    print("本轮统计:", st)


def cmd_model(args):
    """模型打包、查看与切换指令。"""
    import model_manager
    sub = args.model_sub
    if sub == "list":
        models = model_manager.list_models()
        print(f"== 已打包模型库 (共 {len(models)} 个) [目录: {config.MODELS_DIR}] ==")
        for i, m in enumerate(models, 1):
            act_tag = " [当前激活 ★]" if m["is_active"] else ""
            caps = "/".join(m.get("capabilities", [])) or "text"
            print(f" {i}. {m['name']:<18} ({caps}) {m.get('size_mb', 0):>8.1f} MB{act_tag}")
            print(f"    路径: {m['path']}")
            if m.get("description"):
                print(f"    说明: {m['description']}")
    elif sub == "switch":
        if not args.name:
            print("请指定要切换的模型名称，例如: python run.py model switch qwen3.5-4B")
            return
        res = model_manager.switch_model(args.name)
        print(res.get("msg") or res)
    elif sub == "pack":
        if not args.source:
            print("请指定 Hugging Face 下载的模型目录，例如: python run.py model pack C:\\path\\to\\model --name MyModel")
            return
        res = model_manager.pack_model(
            source_path=args.source,
            model_name=args.name,
            copy_files=args.copy,
            description=args.desc or ""
        )
        if res.get("ok"):
            print(f"打包成功! 已存入模型专有目录: {res['path']}")
            print(f"可运行: python run.py model switch {res['model_name']} 进行激活切换。")
        else:
            print(f"打包失败: {res.get('error')}")
    elif sub == "current":
        cur = model_manager.get_active_model()
        print(f"当前激活模型: {cur['name']} ({cur['path']})")
        print(f"模态能力: {cur.get('capabilities')} | 架构: {cur.get('architectures')}")


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

    # 模型打包与快速切换指令
    m_cmd = sub.add_parser("model", help="模型打包、管理与快速切换")
    m_sub = m_cmd.add_subparsers(dest="model_sub", required=True)
    m_sub.add_parser("list", help="列出所有已打包模型").set_defaults(model_sub="list")
    m_sub.add_parser("current", help="查看当前激活模型").set_defaults(model_sub="current")

    p_sw = m_sub.add_parser("switch", help="快速切换当前激活模型")
    p_sw.add_argument("name", type=str, help="目标模型名称")
    p_sw.set_defaults(model_sub="switch")

    p_pk = m_sub.add_parser("pack", help="从 Hugging Face 目录打包模型到独立 models/ 文件夹")
    p_pk.add_argument("source", type=str, help="Hugging Face 下载的模型目录路径")
    p_pk.add_argument("--name", type=str, default=None, help="打包后模型名称(默认用源文件夹名)")
    p_pk.add_argument("--copy", action="store_true", help="强制完整复制文件(默认同分区硬链接)")
    p_pk.add_argument("--desc", type=str, default="", help="模型描述信息")
    p_pk.set_defaults(model_sub="pack")
    m_cmd.set_defaults(func=cmd_model)

    # 持仓识别指令
    p_cmd = sub.add_parser("portfolio", help="持仓识别与查询")
    p_cmd.add_argument("--emu", action="store_true", help="连接雷电模拟器截图并识别最新持仓")
    p_cmd.add_argument("--image", type=str, default=None, help="识别指定持仓截图路径")
    p_cmd.set_defaults(func=cmd_portfolio)

    # 清理指令
    c_cmd = sub.add_parser("clean", help="清理超过1个月的历史图片、识别记录与日志")
    c_cmd.add_argument("--days", type=int, default=30, help="清理保留天数, 默认 30 天(1个月)")
    c_cmd.set_defaults(func=cmd_clean)

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
