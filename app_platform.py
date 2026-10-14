# -*- coding: utf-8 -*-
"""大模型量化交易 · 可视化控制台 —— 桌面 App 外壳。

原生窗口(pywebview/WebView2)内嵌控制台界面,不用开浏览器。
  - 若 127.0.0.1:8800 已有控制台服务在跑(旧实例/网页版),直接复用;
  - 否则本进程在后台线程拉起该服务。
窗口关闭 = 退出本 App;不影响独立运行的 盯盘/模型服务(它们照常后台跑)。

启动:双击 start_app.bat(用 pythonw,无黑色控制台窗口)。
"""
from __future__ import annotations
import os
import sys
import threading
import time

import config
import dashboard

_LOG = os.path.join(config.LOG_DIR, "app.log")
_URL = f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"
_MUTEX_NAME = "Local\\QuantTradeDashboardApp"   # 命名互斥体:保证单实例
_mutex_handle = None


def _log(msg: str) -> None:
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _shutdown_services() -> None:
    """关控制台时级联关闭:盯盘 loop → 模型服务 → 雷电模拟器(用户要求,2026-09-09)。"""
    import control
    for name, fn in (("loop", control.loop_stop),
                     ("model", control.model_stop),
                     ("emu", control.emu_stop)):
        try:
            r = fn() or {}
            _log(f"关窗级联关闭 {name}: ok={bool(r.get('ok'))} {r.get('msg') or ''}")
        except Exception as e:
            _log(f"关窗级联关闭 {name} 异常: {e!r}")


def _port_busy(host: str, port: int, timeout: float = 0.5) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _ensure_service():
    """复用或在本进程拉起控制台服务,返回 (server_or_None, reused: bool)。

    铁律:只要 8800 已被占用(无论是否响应),就【不】再 bind ——
    Windows 下 SO_REUSEADDR 允许双绑,会造成请求路由到两个服务。
    """
    busy = _port_busy(config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    if busy:
        try:
            if dashboard.ping(_URL):
                _log(f"复用已在运行的控制台服务: {_URL}")
                return None, True
            _log(f"端口 {config.DASHBOARD_PORT} 已被占用但未响应,按复用处理(页面可能空白)")
            return None, True
        except Exception as e:
            _log(f"复用检测异常: {e!r}")
            return None, True
    try:
        server = dashboard.make_server()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for _ in range(20):
            if dashboard.ping(_URL):
                break
            time.sleep(0.2)
        _log(f"已在本进程启动控制台服务: {_URL}")
        return server, False
    except Exception as e:  # 端口被占等情况
        _log(f"本进程启动控制台服务失败: {e!r}")
        return None, False


def _acquire_single_instance() -> bool:
    """获得命名互斥体则返回 True;已有实例在跑(GetLastError==183)返回 False。

    句柄需保持到进程结束才自动释放,故存模块全局。
    """
    global _mutex_handle
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return True   # 拿不到句柄(极端),不阻塞启动
    _mutex_handle = handle
    return kernel32.GetLastError() != 183


def main():
    import webview

    if not _acquire_single_instance():
        _log("检测到已有 App 实例在运行,本次启动退出(避免重复开窗)")
        return
    if getattr(config, "AUTO_CLEANUP_ON_START", True):
        try:
            import cleaner
            cleaner.cleanup_system()
            _log("启动系统自动清理完成")
        except Exception as e:
            _log(f"启动自动清理异常: {e!r}")
    server, reused = _ensure_service()
    _log("打开桌面窗口 ...")
    try:
        webview.create_window(
            "大模型量化交易 · 模拟盘控制台",
            _URL,
            width=1280, height=820, min_size=(1024, 700),
            background_color="#0d1117",
        )
        webview.start()
    except Exception as e:
        # 原生窗口不可用(如缺 WebView2 Runtime)→ 退回默认浏览器
        _log(f"原生窗口打开失败: {e!r};退回浏览器打开 {_URL}")
        import webbrowser
        webbrowser.open(_URL)
        if server is not None and not reused:
            server.serve_forever()     # 由本进程维持控制面
        return
    finally:
        if server is not None and not reused:
            try:
                server.shutdown()
            except Exception:
                pass
        _shutdown_services()   # 关窗级联停掉 盯盘/模型/模拟器
        _log("App 已退出")


if __name__ == "__main__":
    main()
