# -*- coding: utf-8 -*-
"""MySQL 数据存储与管理模块:
1. 账户资金与股票持仓数据持久化 (现有股票、持仓数量、可用资金、总资产)
2. 图片识别历史记录与分析结果持久化
3. 历史数据清理功能 (启动时清理超1个月的数据)
"""
from __future__ import annotations
import datetime as _dt
import json
import os
from typing import Any

import pymysql

import config


def get_connection(use_db: bool = True):
    """获取 MySQL 数据库连接。若指定数据库不存在则自动创建。"""
    kwargs = {
        "host": config.MYSQL_HOST,
        "port": config.MYSQL_PORT,
        "user": config.MYSQL_USER,
        "password": config.MYSQL_PASSWORD,
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if use_db:
        try:
            kwargs["database"] = config.MYSQL_DB
            return pymysql.connect(**kwargs)
        except pymysql.err.OperationalError as e:
            # 1049: Unknown database
            if e.args[0] == 1049:
                _create_database_if_not_exists()
                kwargs["database"] = config.MYSQL_DB
                return pymysql.connect(**kwargs)
            raise
    return pymysql.connect(**kwargs)


def _create_database_if_not_exists():
    """创建目标数据库(若不存在)。"""
    conn = get_connection(use_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.MYSQL_DB}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            )
        conn.commit()
    finally:
        conn.close()


def init_db() -> bool:
    """初始化 MySQL 表结构(幂等操作)。"""
    try:
        _create_database_if_not_exists()
        conn = get_connection(use_db=True)
        try:
            with conn.cursor() as cur:
                # 1. 账户资产表
                cur.execute("""
                CREATE TABLE IF NOT EXISTS `account_balance` (
                    `id` INT AUTO_INCREMENT PRIMARY KEY,
                    `record_time` DATETIME NOT NULL COMMENT '识别/记录时间',
                    `cash` DECIMAL(14, 2) NOT NULL DEFAULT 0.00 COMMENT '手中可用资金数量',
                    `total_asset` DECIMAL(14, 2) DEFAULT 0.00 COMMENT '总资产',
                    `stock_count` INT NOT NULL DEFAULT 0 COMMENT '持仓股票种类数',
                    `screenshot_path` VARCHAR(255) DEFAULT '' COMMENT '关联持仓截图路径',
                    `source` VARCHAR(32) DEFAULT 'vision' COMMENT '来源: vision / uiautomator / ledger',
                    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX `idx_record_time` (`record_time`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账户资金及资产识别快照';
                """)

                # 2. 股票持仓明细表
                cur.execute("""
                CREATE TABLE IF NOT EXISTS `stock_positions` (
                    `id` INT AUTO_INCREMENT PRIMARY KEY,
                    `balance_id` INT DEFAULT NULL COMMENT '关联 account_balance.id',
                    `record_time` DATETIME NOT NULL COMMENT '识别/记录时间',
                    `stock_code` VARCHAR(16) NOT NULL COMMENT '股票代码',
                    `stock_name` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '股票名称',
                    `shares` INT NOT NULL DEFAULT 0 COMMENT '持仓数量',
                    `available_shares` INT NOT NULL DEFAULT 0 COMMENT '可用持仓数量',
                    `cost_price` DECIMAL(10, 3) DEFAULT NULL COMMENT '成本价',
                    `current_price` DECIMAL(10, 3) DEFAULT NULL COMMENT '现价',
                    `market_value` DECIMAL(14, 2) DEFAULT NULL COMMENT '持仓市值',
                    `source` VARCHAR(32) DEFAULT 'vision' COMMENT '来源',
                    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX `idx_record_time` (`record_time`),
                    INDEX `idx_stock_code` (`stock_code`),
                    INDEX `idx_balance_id` (`balance_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票持仓明细识别表';
                """)

                # 3. 图片识别记录表
                cur.execute("""
                CREATE TABLE IF NOT EXISTS `image_recognitions` (
                    `id` INT AUTO_INCREMENT PRIMARY KEY,
                    `record_time` DATETIME NOT NULL COMMENT '识别时间',
                    `image_path` VARCHAR(255) NOT NULL COMMENT '图片存储绝对/相对路径',
                    `recognition_type` VARCHAR(64) NOT NULL COMMENT '识别类型: portfolio/order_result/probe 等',
                    `prompt` TEXT COMMENT '识别提示词或提问',
                    `raw_result` MEDIUMTEXT COMMENT '多模态模型返回原始内容',
                    `parsed_json` MEDIUMTEXT COMMENT '结构化解析后的JSON字符串',
                    `status` VARCHAR(32) DEFAULT 'success' COMMENT '识别状态: success / failed',
                    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX `idx_record_time` (`record_time`),
                    INDEX `idx_type` (`recognition_type`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='图片识别记录及模型解析日志';
                """)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"[mysql] 初始化数据库失败: {e}")
        return False


def save_portfolio(cash: float,
                   total_asset: float | None,
                   stocks: list[dict[str, Any]],
                   shot_path: str = "",
                   source: str = "vision",
                   record_time: _dt.datetime | None = None) -> int:
    """将持仓识别结果(现有股票列表、持仓数量、手中资金数量)存储到 MySQL 中。

    stocks 格式:
    [
        {
            "code": "002156",
            "name": "通富微电",
            "qty": 1000,           # 持仓数量
            "available": 1000,     # 可用数量(选填,默认同qty)
            "cost": 21.50,         # 成本价(选填)
            "price": 22.10,        # 现价(选填)
            "market_value": 22100  # 市值(选填)
        }, ...
    ]
    返回生成的 account_balance id。
    """
    init_db()
    now = record_time or _dt.datetime.now()
    cash_val = float(cash or 0.0)
    asset_val = float(total_asset) if total_asset is not None else None

    # 计算预估总资产(若未传入)
    if asset_val is None:
        stock_val = sum(float(s.get("price") or s.get("cost") or 0.0) * int(s.get("qty", 0)) for s in stocks)
        asset_val = cash_val + stock_val

    conn = get_connection(use_db=True)
    try:
        with conn.cursor() as cur:
            # 1. 插入账户资金快照
            sql_balance = """
            INSERT INTO `account_balance`
                (`record_time`, `cash`, `total_asset`, `stock_count`, `screenshot_path`, `source`)
            VALUES (%s, %s, %s, %s, %s, %s)
            """
            cur.execute(sql_balance, (now, cash_val, asset_val, len(stocks), shot_path, source))
            balance_id = cur.lastrowid

            # 2. 插入持仓明细
            if stocks:
                sql_stock = """
                INSERT INTO `stock_positions`
                    (`balance_id`, `record_time`, `stock_code`, `stock_name`,
                     `shares`, `available_shares`, `cost_price`, `current_price`,
                     `market_value`, `source`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                rows = []
                for s in stocks:
                    code = str(s.get("code") or "").strip()
                    name = str(s.get("name") or "").strip()
                    qty = int(s.get("qty") or s.get("shares") or 0)
                    avail = int(s.get("available") or s.get("available_shares") or qty)
                    cost = float(s["cost"]) if s.get("cost") is not None else None
                    price = float(s["price"]) if s.get("price") is not None else None
                    mv = float(s["market_value"]) if s.get("market_value") is not None else (
                        (price or cost or 0.0) * qty if (price or cost) else None
                    )
                    rows.append((balance_id, now, code, name, qty, avail, cost, price, mv, source))
                cur.executemany(sql_stock, rows)

        conn.commit()
        return balance_id
    except Exception as e:
        conn.rollback()
        print(f"[mysql] 保存持仓识别数据失败: {e}")
        raise
    finally:
        conn.close()


def save_image_recognition(image_path: str,
                           recognition_type: str,
                           prompt: str,
                           raw_result: str,
                           parsed_data: dict | list | None = None,
                           status: str = "success",
                           record_time: _dt.datetime | None = None) -> int:
    """保存图片识别结果到 MySQL。"""
    init_db()
    now = record_time or _dt.datetime.now()
    parsed_json = json.dumps(parsed_data, ensure_ascii=False) if parsed_data is not None else None

    conn = get_connection(use_db=True)
    try:
        with conn.cursor() as cur:
            sql = """
            INSERT INTO `image_recognitions`
                (`record_time`, `image_path`, `recognition_type`, `prompt`, `raw_result`, `parsed_json`, `status`)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            cur.execute(sql, (now, image_path, recognition_type, prompt, raw_result, parsed_json, status))
            rec_id = cur.lastrowid
        conn.commit()
        return rec_id
    except Exception as e:
        conn.rollback()
        print(f"[mysql] 保存图片识别日志失败: {e}")
        return -1
    finally:
        conn.close()


def get_latest_portfolio() -> dict[str, Any] | None:
    """获取最新一次识别记录的持仓和资金状况。"""
    init_db()
    conn = get_connection(use_db=True)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""
            SELECT id, record_time, cash, total_asset, stock_count, screenshot_path, source
            FROM `account_balance`
            ORDER BY record_time DESC, id DESC
            LIMIT 1
            """)
            bal = cur.fetchone()
            if not bal:
                return None
            bal["cash"] = float(bal["cash"])
            bal["total_asset"] = float(bal["total_asset"]) if bal["total_asset"] is not None else None
            bal["record_time"] = bal["record_time"].strftime("%Y-%m-%d %H:%M:%S")

            cur.execute("""
            SELECT stock_code, stock_name, shares, available_shares,
                   cost_price, current_price, market_value
            FROM `stock_positions`
            WHERE balance_id = %s
            ORDER BY stock_code ASC
            """, (bal["id"],))
            stocks = cur.fetchall()
            for s in stocks:
                if s["cost_price"] is not None:
                    s["cost_price"] = float(s["cost_price"])
                if s["current_price"] is not None:
                    s["current_price"] = float(s["current_price"])
                if s["market_value"] is not None:
                    s["market_value"] = float(s["market_value"])
            bal["stocks"] = stocks
            return bal
    finally:
        conn.close()


def clean_old_records(cutoff_time: _dt.datetime) -> dict[str, int]:
    """清理早于 cutoff_time (如超过1个月)的持仓识别和图片识别记录。"""
    init_db()
    deleted = {"stock_positions": 0, "account_balance": 0, "image_recognitions": 0}
    conn = get_connection(use_db=True)
    try:
        with conn.cursor() as cur:
            # 1. 清理早于 cutoff_time 的持仓明细
            cur.execute("DELETE FROM `stock_positions` WHERE `record_time` < %s", (cutoff_time,))
            deleted["stock_positions"] = cur.rowcount

            # 2. 清理早于 cutoff_time 的账户资金快照
            cur.execute("DELETE FROM `account_balance` WHERE `record_time` < %s", (cutoff_time,))
            deleted["account_balance"] = cur.rowcount

            # 3. 清理早于 cutoff_time 的图片识别记录
            cur.execute("DELETE FROM `image_recognitions` WHERE `record_time` < %s", (cutoff_time,))
            deleted["image_recognitions"] = cur.rowcount

        conn.commit()
        return deleted
    except Exception as e:
        conn.rollback()
        print(f"[mysql] 清理历史识别数据失败: {e}")
        return deleted
    finally:
        conn.close()


if __name__ == "__main__":
    print("MySQL 初始化测试 ...")
    ok = init_db()
    print("init_db ok:", ok)
    if ok:
        print("写入测试持仓 ...")
        bid = save_portfolio(
            cash=188888.50,
            total_asset=250000.00,
            stocks=[
                {"code": "002156", "name": "通富微电", "qty": 1000, "cost": 21.5, "price": 22.0},
                {"code": "600189", "name": "泉阳泉", "qty": 2000, "cost": 9.1, "price": 9.8},
            ],
            shot_path="data/shots/test_portfolio.png",
            source="test"
        )
        print("生成 balance_id:", bid)
        latest = get_latest_portfolio()
        print("读取最新持仓:", latest)
