# -*- coding: utf-8 -*-
"""全局配置:路径、股票池来源、交易时段、风控与账本目录。"""
from __future__ import annotations
import os

# 项目根目录(本文件所在目录)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- 关键路径 ----
DATA_DIR    = os.path.join(BASE_DIR, "data")             # 运行期产物(截图/日志/账本)
MODELS_DIR  = os.path.join(BASE_DIR, "models")            # 打包模型专属存储目录
ACTIVE_MODEL_FILE = os.path.join(DATA_DIR, "active_model.json") # 当前激活模型配置文件


def get_active_model_dir() -> str:
    """获取当前激活模型的实际目录路径。优先读 active_model.json, 未配置时使用 qwen3.5-4B。"""
    if os.path.exists(ACTIVE_MODEL_FILE):
        try:
            import json
            with open(ACTIVE_MODEL_FILE, "r", encoding="utf-8") as f:
                info = json.load(f)
                p = info.get("path")
                if p and os.path.isdir(p):
                    return p
                name = info.get("name")
                if name:
                    cand = os.path.join(MODELS_DIR, name)
                    if os.path.isdir(cand):
                        return cand
        except Exception:
            pass
    # 默认兜底: 优先 models/qwen3.5-4B, 其次根目录 qwen3.5-4B
    cand_models = os.path.join(MODELS_DIR, "qwen3.5-4B")
    if os.path.isdir(cand_models):
        return cand_models
    return os.path.join(BASE_DIR, "qwen3.5-4B")


MODEL_DIR   = get_active_model_dir()                      # 当前激活模型目录
DOC_PATH    = os.path.join(BASE_DIR, "股票信息.doc")      # 股票代码/名称来源
KILL_FILE   = os.path.join(DATA_DIR, "STOP")             # 急停:存在该文件则暂停下单
CACHE_FILE  = os.path.join(DATA_DIR, "quote_cache.json") # 行情短缓存
TRADES_CSV  = os.path.join(DATA_DIR, "trades.csv")       # 交易流水
EQUITY_JSONL= os.path.join(DATA_DIR, "equity.jsonl")     # 资产快照流水
STATUS_JSON = os.path.join(DATA_DIR, "status.json")       # 状态快照

# ---- 模型服务 ----
MODEL_HOST = "127.0.0.1"
MODEL_PORT = 8710
MODEL_URL  = f"http://{MODEL_HOST}:{MODEL_PORT}"

# ---- 可视化平台(dashboard) ----
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8800
# 起子进程用的解释器:优先 venv(含 CUDA torch);缺则退回当前解释器
_venv_py = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")


def get_python() -> str:
    if os.path.exists(_venv_py):
        return _venv_py
    import sys
    return sys.executable


def no_window_flags() -> int:
    """控制台子进程(powershell/adb/taskkill/python)的创建标志。

    父进程若为 pythonw/无控制台,每次 subprocess 起控制台程序都会新弹黑窗;
    统一加 CREATE_NO_WINDOW 可从源头杜绝。
    """
    import subprocess
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# 服务运行日志(盯盘/模型服务;dashboard 增量 tail 用)
LOOP_LOG    = os.path.join(DATA_DIR, "logs", "loop_live.log")
MODEL_LOG   = os.path.join(DATA_DIR, "logs", "model_server.out")
# 股票池权威来源(dashboard 增删写此文件;不存在时由 stocks_loader 从 .doc 播种)
STOCKS_FILE = os.path.join(DATA_DIR, "stocks.json")

# ---- 雷电模拟器 / adb ----
LD_DIR    = r"H:\leidian\LDPlayer14"
ADB       = os.path.join(LD_DIR, "adb.exe")
LDCONSOLE = os.path.join(LD_DIR, "ldconsole.exe")
ADB_HOST  = "127.0.0.1"
ADB_PORT  = 5555

# 模拟器分辨率(leidian0: 1440x2560@640dpi);改分辨率会影响 selectors 坐标,需重校准
SCREEN_W, SCREEN_H = 1440, 2560

# ---- A股交易时段(周一至五) ----
TRADE_SESSIONS = [("09:30", "11:30"), ("13:00", "15:00")]
# 节流:间隔多久跑一轮全池分析(秒);盘口数字每轮都刷新,但只有指标触发才调模型
POLL_SECONDS = int(os.environ.get("QTPOLL", "15"))   # 用户要求 45s→15s(激进档)

# ---- 火力档位(2026-09-08 用户选定:分散满仓,取消决策限制) ----
# 不再限每日单数、每轮全量分析、交易时段全力操作,追求收益最大化(全部针对模拟盘)。
DEFAULT_CASH_PERCENT = 1 / 3  # 单笔买入占「可用资金」上限(分散:每笔 ≤ 1/3)
ASSUMED_CASH = 200_000        # 读不到 App 资金时的兜底可用资金(实测你的模拟盘初始 20 万)
MAX_POSITION_PERCENT = 1 / 3  # 单只股票市值占资产上限(分散:单只 ≤ 1/3)
MIN_LOT = 100                 # A股一手
MAX_ORDERS_PER_DAY  = int(os.environ.get("QTMAXORD", "0"))  # 0=不限(此前 8);>0 才生效
MAX_ORDERS_PER_MIN  = 5       # 每分钟下单宽松上限(防同股误连点,整轮批量时也不卡火力)
MODEL_CALL_MIN_INTERVAL = 2   # 同一只股票两次调模型最短间隔(秒);调低以加快决策
ANALYZE_ALL = True            # 每轮全量分析每只股票,不再等「指标触发」

# ---- 自动卖出规则(确定性,持仓每轮先于 LLM 触发;激进落袋档 2026-09-09 用户选定) ----
SELL_STOP_LOSS_PCT   = 0.06   # 现价 ≤ 成本×(1-6%)  → 清仓止损
SELL_TP_HALF_PCT     = 0.06   # 现价 ≥ 成本×(1+6%)  → 减半止盈(不足一手则清仓)
SELL_TP_ALL_PCT      = 0.10   # 现价 ≥ 成本×(1+10%) → 清仓止盈
SELL_BREAK_MA        = True   # 收盘 <MA20 且 MA5<MA10 → 清仓(破位走坏)
SELL_OVERBOUGHT_RSI  = 80.0   # RSI≥80 且当日翻绿 → 减半止盈

# ---- 视觉/截图 ----
SHOT_DIR = os.path.join(DATA_DIR, "shots")
# 发往模型的截图最长边(缩放用,控制 token/显存)
VISION_MAX_EDGE = 1024

# ---- MySQL 数据库配置 ----
MYSQL_HOST     = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT     = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER     = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "111111")
MYSQL_DB       = os.environ.get("MYSQL_DB", "quant_trade")

# ---- 清理与留存配置(系统再次启动时超1个月删除) ----
CLEANUP_RETENTION_DAYS = int(os.environ.get("CLEANUP_RETENTION_DAYS", "30")) # 默认30天(1个月)
AUTO_CLEANUP_ON_START  = True                                               # 系统启动时自动触发清理

# ---- 让日志落盘 UTF-8 ----
LOG_DIR = os.path.join(DATA_DIR, "logs")

for _d in (DATA_DIR, SHOT_DIR, LOG_DIR, MODELS_DIR):
    os.makedirs(_d, exist_ok=True)
