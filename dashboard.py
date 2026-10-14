# -*- coding: utf-8 -*-
"""本地可视化控制台(进程/日志/股票池管理)。

用法:  .venv\\Scripts\\python.exe dashboard.py [--port 8800]
打开浏览器访问 http://127.0.0.1:8800

API:
  GET  /                仪表盘页面(dashboard.html)
  GET  /api/status      整体状态(模型/盯盘/模拟器/时段/急停/股票池/今日统计)
  GET  /api/logs?file=loop|model&after=<字节偏移>   增量日志 tail
  GET  /api/trades?n=30 最近交易流水
  POST /api/control     {action: model_start|model_stop|loop_start|loop_stop|
                                 emu_start|emu_stop|kill_on|kill_off, mode?: live|dry}
  POST /api/stocks      {op: add|del, code, name?}
只监听 127.0.0.1,请勿暴露到外网。
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import control

_HTML_PATH = os.path.join(config.BASE_DIR, "dashboard.html")
_cache = {"ts": 0.0, "val": None}
_cache_lock = threading.Lock()
# 各探测项独立短缓存(避免每轮都新起 powershell/adb 子进程)
_slow = {"model": {"ts": 0.0, "val": None}, "loop": {"ts": 0.0, "val": None},
         "emu": {"ts": 0.0, "val": None}}


def _cached(key: str, fn, ttl: float):
    with _cache_lock:
        ent = _slow[key]
        if ent["val"] is not None and time.time() - ent["ts"] < ttl:
            return ent["val"]
    val = fn()
    with _cache_lock:
        ent = _slow[key]
        ent.update(ts=time.time(), val=val)
    return val


# ---------- 状态聚合(带短缓存,避免每轮都跑 powershell/adb) ----------
def _status() -> dict:
    with _cache_lock:
        if time.time() - _cache["ts"] < 2.0 and _cache["val"] is not None:
            return _cache["val"]
    now = time.localtime()
    try:
        import main_loop
        in_session = main_loop.is_trading_time()
    except Exception:
        in_session = False
    import ledger
    import stocks_loader

    model = _cached("model", control.model_status, 8)     # 模型健康 http 探测较贵
    loop = _cached("loop", control.loop_status, 6)         # powershell 扫描
    emu = _cached("emu", control.emu_status, 20)           # adb 连接最重,20s 一次即可
    kill = os.path.exists(config.KILL_FILE)

    today_orders = ledger.count_orders_today()
    today_decisions = _count_today(exec_status="decided")

    holdings = ledger.positions_from_ledger()
    portfolio_data = None
    try:
        import db_mysql
        portfolio_data = db_mysql.get_latest_portfolio()
    except Exception:
        pass

    import model_manager
    active_m = model_manager.get_active_model()
    models_list = model_manager.list_models()

    st = {
        "now": time.strftime("%Y-%m-%d %H:%M:%S", now),
        "weekday": time.strftime("%A", now),
        "in_session": in_session,
        "sessions": config.TRADE_SESSIONS,
        "poll_seconds": config.POLL_SECONDS,
        "kill": kill,
        "model": {"running": model["running"], "pids": model["pids"],
                  "loaded": bool(model["health"].get("model_loaded")),
                  "cuda": bool(model["health"].get("cuda")),
                  "device": model["health"].get("device"),
                  "error": model["health"].get("error"),
                  "active": active_m.get("name")},
        "models": models_list,
        "active_model": active_m,
        "loop": {"running": loop["running"], "pids": loop["pids"], "mode": loop["mode"]},
        "emu": emu,
        "today": {"orders": today_orders, "decisions": today_decisions},
        "holdings": len(holdings),
        "portfolio": portfolio_data,
        "stocks": stocks_loader.current_list(),
        "risk": {"max_orders_day": config.MAX_ORDERS_PER_DAY,
                 "cash_pct": config.DEFAULT_CASH_PERCENT,
                 "pos_pct": config.MAX_POSITION_PERCENT,
                 "min_lot": config.MIN_LOT},
    }
    with _cache_lock:
        _cache.update(ts=time.time(), val=st)
    return st


def _count_today(exec_status: str) -> int:
    p = os.path.join(config.DATA_DIR, "trades.csv")
    if not os.path.exists(p):
        return 0
    today = time.strftime("%Y-%m-%d")
    with open(p, encoding="utf-8") as f:
        return sum(1 for r in csv.DictReader(f)
                   if r.get("date") == today and r.get("exec_status") == exec_status)


def _tail_file(path: str, after: int) -> dict:
    if not os.path.exists(path):
        return {"text": "", "after": 0, "size": 0}
    size = os.path.getsize(path)
    if after is None or after < 0 or after > size:   # 文件轮转/截断 → 从头尾
        after = 0
    with open(path, "rb") as f:
        if after == 0:
            start = max(0, size - 64 * 1024)          # 首屏只取末尾 64KB
            f.seek(start)
            data = f.read()
            new_after = size
        else:
            f.seek(after)
            data = f.read()
            new_after = size
    return {"text": data.decode("utf-8", errors="replace"), "after": new_after, "size": size}


# ---------- HTTP ----------
class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _serve_html(self):
        try:
            with open(_HTML_PATH, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            body = b"dashboard.html missing"
        # 用 dashboard.html 的修改时间戳替换构建占位符;页面据此检测"文件已更新"并自动重载
        body = body.replace(b"__BUILD__", b"%.3f" % os.path.getmtime(_HTML_PATH))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if u.path == "/api/ping":
                return self._send({"ok": True, "service": "quant-dashboard",
                                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            if u.path == "/api/htmlver":
                try:
                    v = "%.3f" % os.path.getmtime(_HTML_PATH)
                except OSError:
                    v = "0"
                return self._send({"v": v})
            if u.path == "/" or u.path == "/index.html":
                return self._serve_html()
            if u.path == "/api/status":
                return self._send(_status())
            if u.path == "/api/portfolio":
                import db_mysql
                p = db_mysql.get_latest_portfolio()
                return self._send({"ok": True, "portfolio": p})
            if u.path == "/api/models":
                import model_manager
                return self._send({"ok": True, "models": model_manager.list_models(),
                                   "active": model_manager.get_active_model()})
            if u.path == "/api/trades":
                import ledger
                q = parse_qs(u.query)
                n = int((q.get("n") or ["30"])[0])
                rows = ledger.load_recent(n)
                # 倒序(最新在前)方便展示
                return self._send({"rows": list(reversed(rows))})
            if u.path == "/api/logs":
                q = parse_qs(u.query)
                fname = (q.get("file") or ["loop"])[0]
                after = int((q.get("after") or ["0"])[0])
                path = config.LOOP_LOG if fname == "loop" else config.MODEL_LOG
                r = _tail_file(path, after)
                r["file"] = fname
                return self._send(r)
            return self._send({"ok": False, "msg": f"no route {u.path}"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._send({"ok": False, "msg": repr(e)}, 500)

    def do_POST(self):
        try:
            from urllib.parse import urlparse
            u = urlparse(self.path)
            req = self._read_json()
            if u.path == "/api/control":
                with _cache_lock:
                    _cache["val"] = None                 # 强制下次刷新状态
                    for _e in _slow.values():            # 及各项慢探测缓存
                        _e["val"] = None
                r = control.dispatch(req.get("action", ""), mode=req.get("mode"), **req)
                return self._send(r)
            if u.path == "/api/models":
                import model_manager
                op = req.get("op")
                if op == "switch":
                    r = model_manager.switch_model(req.get("name") or "")
                elif op == "pack":
                    r = model_manager.pack_model(req.get("source") or "", model_name=req.get("name"),
                                                 copy_files=bool(req.get("copy")), description=req.get("desc") or "")
                else:
                    return self._send({"ok": False, "msg": f"未知 op {op}"}, 400)
                return self._send(r)
            if u.path == "/api/stocks":
                import stocks_loader
                op = req.get("op")
                if op == "add":
                    r = stocks_loader.add_stock(req.get("code", ""), req.get("name", ""))
                elif op == "del":
                    r = stocks_loader.del_stock(req.get("code", ""))
                else:
                    return self._send({"ok": False, "msg": f"未知 op {op}"}, 400)
                return self._send({"ok": r["ok"], "msg": r["msg"],
                                   "stocks": r.get("stocks") or []})
            return self._send({"ok": False, "msg": f"no route {u.path}"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._send({"ok": False, "msg": repr(e)}, 500)


def make_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    if getattr(config, "AUTO_CLEANUP_ON_START", True):
        try:
            import cleaner
            cleaner.cleanup_system()
        except Exception as e:
            print(f"[dashboard] 启动清理异常: {e}")
    return ThreadingHTTPServer((host or config.DASHBOARD_HOST, port or config.DASHBOARD_PORT), _H)


def ping(url: str | None = None, timeout: float = 2.0) -> bool:
    """探测 127.0.0.1 上是否已有本服务在跑(供桌面 App 复用端口)。"""
    import urllib.request
    base = url or f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"
    try:
        with urllib.request.urlopen(base + "/api/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=config.DASHBOARD_HOST)
    ap.add_argument("--port", type=int, default=config.DASHBOARD_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = make_server(args.host, args.port)
    print(f"[dashboard] 可视化平台: http://{args.host}:{args.port}", flush=True)
    if not args.no_browser:
        try:
            import webbrowser
            threading.Timer(0.8, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
        except Exception:
            pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
