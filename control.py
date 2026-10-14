# -*- coding: utf-8 -*-
r"""可视化平台的服务/进程控制层:检测、启动、停止 模型服务与盯盘进程、模拟器。

设计要点:
- 目标进程都经 .venv python(redirector)启动,一个逻辑进程常表现为
  「venv 垫片 + Anaconda worker」两条。因此:
    * 检测 = 扫描 cmdline 含关键子串的所有 PID(宁可多列,去重展示)。
    * 停止 = 对每个命中的 PID 做 taskkill /T(杀整棵子树),幂等。
- 只在本机 127.0.0.1 使用;停止用的是进程名+命令行匹配,不会误伤
  非 python 进程。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time

import config

_PS_QUERY = ("Get-CimInstance Win32_Process -Filter \"name='python.exe'\" "
             "| ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")


def list_python() -> list[dict]:
    """返回 [{pid, cmd}...] 本机全部 python 进程(worker 与垫片都会出现)。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_QUERY],
            capture_output=True, timeout=20,
            creationflags=config.no_window_flags())
        txt = out.stdout.decode("utf-8", errors="replace")
        if not txt.strip():
            txt = out.stdout.decode("gbk", errors="replace")
    except Exception:
        return []
    rows = []
    for ln in txt.splitlines():
        ln = ln.strip()
        if "|" not in ln:
            continue
        pid, _, cmd = ln.partition("|")
        if pid.isdigit():
            rows.append({"pid": int(pid), "cmd": cmd.strip()})
    return rows


def find_pids(substr: str) -> list[int]:
    """cmdline 含 substr 的 python PID(排序去重)。"""
    hits = sorted({p["pid"] for p in list_python() if substr in p["cmd"]})
    return hits


def kill_tree(pid: int) -> bool:
    """taskkill /T 杀整棵子树;进程已不存在视为成功。"""
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30,
                           creationflags=config.no_window_flags())
        return True
    except Exception:
        return False


def stop_matching(substr: str) -> list[int]:
    """终止所有 cmdline 含 substr 的 python 进程树,返回被终止的 PID。"""
    killed = []
    for pid in find_pids(substr):
        kill_tree(pid)
        killed.append(pid)
        time.sleep(0.3)
    return killed


def _spawn(args: list[str], log_path: str, live: bool | None = None) -> int:
    """后台起一个 venv python 子进程,输出追加到 log_path。返回 PID。"""
    py = config.get_python()
    logf = open(log_path, "ab")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.Popen([py, *args], cwd=config.BASE_DIR, env=env,
                         stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=config.no_window_flags())
    return p.pid


# ---------- 模型服务 ----------
def model_status() -> dict:
    try:
        import model_client
        h = model_client.health()
    except Exception:
        h = {}
    pids = find_pids("model_server.py")
    running = bool(pids) or h.get("ok") is True
    return {"running": running, "pids": pids, "health": h}


def model_start() -> dict:
    st = model_status()
    if st["health"].get("ok") or st["pids"]:
        return {"ok": False, "msg": "模型服务已在运行", "pids": st["pids"]}
    pid = _spawn(["-u", "model_server.py"], config.MODEL_LOG)
    # 等 /health(最多 300s,权重加载慢)
    for _ in range(60):
        try:
            import model_client
            if model_client.health().get("ok"):
                return {"ok": True, "msg": "模型服务已启动", "pid": pid}
        except Exception:
            pass
        time.sleep(5)
    return {"ok": False, "msg": "模型服务启动超时,见日志", "pid": pid}


def model_stop() -> dict:
    killed = stop_matching("model_server.py")
    return {"ok": True, "msg": f"已停止模型服务(pid={killed or '无'})", "killed": killed}


# ---------- 盯盘 loop ----------
def loop_status() -> dict:
    pids = find_pids("run.py loop")
    mode = "unknown"
    if pids:
        # 取任一命中 cmdline 判断 live/dry
        for p in list_python():
            if p["pid"] in pids:
                if "--live" in p["cmd"]:
                    mode = "live"
                elif "--dry" in p["cmd"]:
                    mode = "dry"
                else:
                    mode = "live" if not _loop_default_dry() else "dry"
                break
    return {"running": bool(pids), "pids": pids, "mode": mode}


def _loop_default_dry() -> bool:
    """run.py loop 不带参默认为 dry(见 run.py _is_dry)。"""
    return True


def loop_start(live: bool) -> dict:
    st = loop_status()
    if st["pids"]:
        return {"ok": False, "msg": f"盯盘已在运行(mode={st['mode']}),先停止再启动",
                "pids": st["pids"]}
    args = ["-u", "run.py", "loop", "--live"] if live else ["-u", "run.py", "loop"]
    pid = _spawn(args, config.LOOP_LOG)
    time.sleep(8)
    nxt = loop_status()
    ok = bool(nxt["pids"])
    return {"ok": ok, "msg": ("盯盘已启动(live 模拟盘真实下单)"
                              if live else "盯盘已启动(dry 演练,不下单)") + ("" if ok else " 但未检测到进程"),
            "pid": pid, "pids": nxt["pids"]}


def loop_stop() -> dict:
    killed = stop_matching("run.py loop")
    return {"ok": True, "msg": f"已停止盯盘(pid={killed or '无'})", "killed": killed}


# ---------- 模拟器 ----------
def emu_status() -> dict:
    try:
        from emulator import Emulator
        e = Emulator()
        ok = e.connect_adb(quiet=True)
        return {"adb": ok, "screen": e.screen_size() if ok else None}
    except Exception as ex:
        return {"adb": False, "error": repr(ex)}


def emu_start() -> dict:
    try:
        from emulator import Emulator
        e = Emulator()
        if e.start(wait_boot=True):
            return {"ok": True, "msg": "模拟器已启动,adb 在线"}
        return {"ok": False, "msg": "模拟器启动超时"}
    except Exception as ex:
        return {"ok": False, "msg": f"模拟器启动失败: {ex!r}"}


def emu_stop() -> dict:
    try:
        from emulator import Emulator
        e = Emulator()
        e.stop()
        return {"ok": True, "msg": "已发送模拟器关闭指令"}
    except Exception as ex:
        return {"ok": False, "msg": f"模拟器关闭失败: {ex!r}"}


# ---------- 急停 STOP ----------
def kill_flag(on: bool) -> dict:
    if on:
        with open(config.KILL_FILE, "w", encoding="utf-8") as f:
            f.write(f"set by dashboard {time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        try:
            os.remove(config.KILL_FILE)
        except FileNotFoundError:
            pass
    return {"ok": True, "msg": "急停已开启:盯盘暂停下单(删除 data\\STOP 恢复)" if on
            else "急停已关闭:恢复正常盯盘", "kill": os.path.exists(config.KILL_FILE)}


def portfolio_refresh() -> dict:
    """在模拟器中主动触发一次持仓与资金识别, 并落库 MySQL。"""
    try:
        from emulator import Emulator
        from ths_app import get_portfolio
        e = Emulator()
        if not e.connect_adb(quiet=True):
            return {"ok": False, "msg": "模拟器未连接, 请先启动雷电模拟器并进入同花顺持仓页"}
        hold, cash, asset = get_portfolio(e, save_to_db=True)
        return {"ok": True, "msg": f"持仓识别完成: 现有股票 {len(hold)} 只, 可用资金 {cash:,.2f} 元",
                "cash": cash, "total_asset": asset, "stocks_count": len(hold)}
    except Exception as ex:
        return {"ok": False, "msg": f"持仓识别异常: {ex!r}"}


def system_clean() -> dict:
    """手动执行启动清理逻辑: 超过1个月的历史图片、MySQL识别记录和日志。"""
    try:
        import cleaner
        res = cleaner.cleanup_system()
        shots_cnt = res.get("shots", {}).get("deleted_count", 0)
        logs_lines = res.get("logs", {}).get("removed_lines", 0)
        return {"ok": True, "msg": f"清理完成: 删除了 {shots_cnt} 张过期图片, {logs_lines} 行过期日志",
                "detail": res}
    except Exception as ex:
        return {"ok": False, "msg": f"系统清理异常: {ex!r}"}


def dispatch(action: str, **kw) -> dict:
    """dashboard POST /api/control 的统一入口。"""
    act = (action or "").strip().lower()
    if act == "model_start":
        return model_start()
    if act == "model_stop":
        return model_stop()
    if act == "loop_start":
        return loop_start(bool(kw.get("mode") == "live"))
    if act == "loop_stop":
        return loop_stop()
    if act == "emu_start":
        return emu_start()
    if act == "emu_stop":
        return emu_stop()
    if act == "kill_on":
        return kill_flag(True)
    if act == "kill_off":
        return kill_flag(False)
    if act == "portfolio_refresh":
        return portfolio_refresh()
    if act == "system_clean":
        return system_clean()
    if act == "model_switch":
        import model_manager
        return model_manager.switch_model(kw.get("name") or kw.get("model") or "")
    if act == "model_pack":
        import model_manager
        return model_manager.pack_model(kw.get("source") or "", model_name=kw.get("name"),
                                          copy_files=bool(kw.get("copy")), description=kw.get("desc") or "")
    return {"ok": False, "msg": f"未知动作: {action}"}


if __name__ == "__main__":
    for probe in ("model", "loop", "emu"):
        fn = {"model": model_status, "loop": loop_status, "emu": emu_status}[probe]
        print(probe, json.dumps(fn(), ensure_ascii=False))
