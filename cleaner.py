# -*- coding: utf-8 -*-
"""系统启动清理模块:
以再次启动系统的时间为基准, 清理超过1个月(默认30天)的:
1. 截图图片文件 (data/shots)
2. MySQL 中的图片识别与持仓历史记录 (image_recognitions / stock_positions / account_balance)
3. 历史日志文件及日志内容 (data/logs/*.log, equity.jsonl, trades.csv)
"""
from __future__ import annotations
import csv
import datetime as _dt
import os
import re
import time

import config
import db_mysql

_DATE_PATTERNS = [
    re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),   # 20261014_143000
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2}):(\d{2}):(\d{2})"), # 2026-10-14 14:30:00
    re.compile(r"(\d{4})(\d{2})(\d{2})"),                           # 20261014
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),                         # 2026-10-14
]


def _parse_time_from_name(name: str) -> _dt.datetime | None:
    """尝试从文件名提取时间信息。"""
    for pat in _DATE_PATTERNS:
        m = pat.search(name)
        if m:
            g = m.groups()
            try:
                if len(g) >= 6:
                    return _dt.datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]), int(g[5]))
                elif len(g) >= 3:
                    return _dt.datetime(int(g[0]), int(g[1]), int(g[2]))
            except ValueError:
                pass
    return None


def clean_shots(cutoff_time: _dt.datetime) -> dict[str, int]:
    """清理 shots 目录下超过1个月的截图。"""
    res = {"deleted_count": 0, "bytes_freed": 0}
    if not os.path.exists(config.SHOT_DIR):
        return res

    for root, _, files in os.walk(config.SHOT_DIR):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
                continue
            fpath = os.path.join(root, fname)
            try:
                # 优先解析文件名中的时间
                dt = _parse_time_from_name(fname)
                mtime = _dt.datetime.fromtimestamp(os.path.getmtime(fpath))

                # 若文件名中有早于 cutoff 的日期，或者修改时间早于 cutoff
                is_expired = False
                if dt is not None and dt < cutoff_time:
                    is_expired = True
                elif mtime < cutoff_time:
                    is_expired = True

                if is_expired:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    res["deleted_count"] += 1
                    res["bytes_freed"] += size
            except Exception as e:
                print(f"[cleaner] 删除过期截图失败 {fpath}: {e}")
    return res


def clean_mysql_records(cutoff_time: _dt.datetime) -> dict[str, int]:
    """清理 MySQL 数据库中超过1个月的持仓和图片识别数据。"""
    try:
        return db_mysql.clean_old_records(cutoff_time)
    except Exception as e:
        print(f"[cleaner] 清理 MySQL 记录异常: {e}")
        return {"stock_positions": 0, "account_balance": 0, "image_recognitions": 0}


def clean_log_file_lines(fpath: str, cutoff_time: _dt.datetime) -> tuple[int, int]:
    """清理文本日志文件中早于 cutoff_time 的日志行，保留最近1个月的内容。"""
    if not os.path.exists(fpath):
        return 0, 0
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return 0, 0

    if not lines:
        return 0, 0

    kept_lines = []
    removed_count = 0
    # 状态保留: 当某行没有时间戳时, 归属于上一行的判定
    current_line_keep = True

    for line in lines:
        dt = _parse_time_from_name(line[:80]) # 日志开头通常包含时间戳
        if dt is not None:
            current_line_keep = (dt >= cutoff_time)

        if current_line_keep:
            kept_lines.append(line)
        else:
            removed_count += 1

    if removed_count > 0:
        try:
            with open(fpath, "w", encoding="utf-8", errors="replace") as f:
                f.writelines(kept_lines)
        except Exception as e:
            print(f"[cleaner] 重写日志文件失败 {fpath}: {e}")

    return removed_count, len(kept_lines)


def clean_jsonl_records(fpath: str, cutoff_time: _dt.datetime) -> int:
    """清理 jsonl (如 equity.jsonl) 中早于 cutoff_time 的行。"""
    if not os.path.exists(fpath):
        return 0
    import json
    kept = []
    removed = 0
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    obj = json.loads(line_str)
                    ts_str = obj.get("ts") or ""
                    dt = _parse_time_from_name(ts_str)
                    if dt and dt < cutoff_time:
                        removed += 1
                        continue
                except Exception:
                    pass
                kept.append(line)
        if removed > 0:
            with open(fpath, "w", encoding="utf-8", errors="replace") as f:
                f.writelines(kept)
    except Exception as e:
        print(f"[cleaner] 清理 jsonl 失败 {fpath}: {e}")
    return removed


def clean_csv_records(fpath: str, cutoff_time: _dt.datetime) -> int:
    """清理 trades.csv 中超过1个月的流水记录, 保留表头。"""
    if not os.path.exists(fpath):
        return 0
    cutoff_date_str = cutoff_time.strftime("%Y-%m-%d")
    kept_rows = []
    removed = 0
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                row_date = row.get("date") or ""
                if row_date and row_date < cutoff_date_str:
                    removed += 1
                else:
                    kept_rows.append(row)
        if removed > 0 and fieldnames:
            with open(fpath, "w", encoding="utf-8", errors="replace", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(kept_rows)
    except Exception as e:
        print(f"[cleaner] 清理 CSV 记录失败 {fpath}: {e}")
    return removed


def clean_logs(cutoff_time: _dt.datetime) -> dict[str, int]:
    """清理 data/logs/ 目录下的过期日志内容。"""
    res = {"removed_lines": 0, "deleted_files": 0, "equity_removed": 0, "trades_removed": 0}
    if not os.path.exists(config.LOG_DIR):
        return res

    for fname in os.listdir(config.LOG_DIR):
        fpath = os.path.join(config.LOG_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            mtime = _dt.datetime.fromtimestamp(os.path.getmtime(fpath))
            # 若整个文件最后修改时间都早于 cutoff，且非活跃核心日志，则直接删除
            is_main_log = fname in ("loop_live.log", "model_server.log", "model_server.out", "app.log")
            if not is_main_log and mtime < cutoff_time:
                os.remove(fpath)
                res["deleted_files"] += 1
                continue

            # 核心日志文件逐行清理过期内容
            rm, _ = clean_log_file_lines(fpath, cutoff_time)
            res["removed_lines"] += rm
        except Exception as e:
            print(f"[cleaner] 清理日志文件失败 {fpath}: {e}")

    # 清理 equity.jsonl 和 trades.csv
    res["equity_removed"] = clean_jsonl_records(config.EQUITY_JSONL, cutoff_time)
    res["trades_removed"] = clean_csv_records(config.TRADES_CSV, cutoff_time)

    return res


def cleanup_system(startup_time: _dt.datetime | None = None, days: int | None = None) -> dict:
    """系统清理主入口:
    以系统再次启动的时间为基准, 计算1个月前的 cutoff 阈值,
    清理早于该阈值的图片截图、MySQL 识别记录及日志内容。
    """
    now = startup_time or _dt.datetime.now()
    retention_days = days if days is not None else config.CLEANUP_RETENTION_DAYS
    cutoff_time = now - _dt.timedelta(days=retention_days)

    print(f"[cleaner] 启动系统自动清理: 当前启动时间={now.strftime('%Y-%m-%d %H:%M:%S')}, "
          f"留存期={retention_days}天, 清理早于 {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')} 的数据...")

    # 1. 清理图片
    shots_stat = clean_shots(cutoff_time)

    # 2. 清理 MySQL 识别记录
    db_stat = clean_mysql_records(cutoff_time)

    # 3. 清理日志文件与内容
    logs_stat = clean_logs(cutoff_time)

    summary = {
        "startup_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "cutoff_time": cutoff_time.strftime("%Y-%m-%d %H:%M:%S"),
        "retention_days": retention_days,
        "shots": shots_stat,
        "mysql": db_stat,
        "logs": logs_stat,
    }

    print(f"[cleaner] 清理完成! 详情: "
          f"删除图片 {shots_stat['deleted_count']} 张 (释放 {shots_stat['bytes_freed'] / 1024 / 1024:.2f} MB), "
          f"清理 MySQL: 股票持仓明细={db_stat.get('stock_positions', 0)}, "
          f"资金快照={db_stat.get('account_balance', 0)}, "
          f"图片识别记录={db_stat.get('image_recognitions', 0)}, "
          f"清理日志行数 {logs_stat['removed_lines']} 行, 删除过期日志文件 {logs_stat['deleted_files']} 个, "
          f"流水记录={logs_stat['trades_removed']} 条, 资产快照={logs_stat['equity_removed']} 条")

    return summary


if __name__ == "__main__":
    import pprint
    res = cleanup_system()
    pprint.pprint(res)
