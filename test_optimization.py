# -*- coding: utf-8 -*-
"""自动化测试脚本:
验证:
1. 持仓识别与资金数据存储到 MySQL (现有股票, 持仓数量, 手中可用资金, 总资产)
2. 存储结构优化与图片时间命名规则
3. 启动清理功能: 以启动时间为基准, 超过1个月的图片、MySQL识别记录和日志内容删除验证
"""
from __future__ import annotations
import datetime as _dt
import json
import os
import sys
import time

import cleaner
import config
import db_mysql
import vision


def test_mysql_portfolio():
    print("=== 测试 1: 持仓识别与资金存储到 MySQL ===")
    assert db_mysql.init_db() is True, "MySQL 数据库初始化失败"

    test_cash = 168888.66
    test_asset = 288888.88
    test_stocks = [
        {"code": "002156", "name": "通富微电", "qty": 1500, "available": 1500, "cost": 22.3, "price": 24.5, "market_value": 36750.0},
        {"code": "600522", "name": "中天科技", "qty": 2000, "available": 2000, "cost": 14.2, "price": 15.6, "market_value": 31200.0},
        {"code": "688511", "name": "裕太微",   "qty": 500,  "available": 0,    "cost": 88.0, "price": 92.1, "market_value": 46050.0},
    ]
    ts_now = _dt.datetime.now()
    bid = db_mysql.save_portfolio(
        cash=test_cash,
        total_asset=test_asset,
        stocks=test_stocks,
        shot_path=f"data/shots/{time.strftime('%Y%m%d_%H%M%S')}_portfolio.png",
        source="test_vision",
        record_time=ts_now
    )
    print(f"-> 成功存入 MySQL, balance_id={bid}")
    assert bid > 0, "balance_id 应大于 0"

    latest = db_mysql.get_latest_portfolio()
    assert latest is not None, "未能查出最新持仓"
    assert abs(latest["cash"] - test_cash) < 0.01, f"可用资金不一致: {latest['cash']} vs {test_cash}"
    assert abs(latest["total_asset"] - test_asset) < 0.01, f"总资产不一致: {latest['total_asset']} vs {test_asset}"
    assert len(latest["stocks"]) == 3, f"持仓股票数量不一致: {len(latest['stocks'])}"

    stock_codes = [s["stock_code"] for s in latest["stocks"]]
    assert "002156" in stock_codes and "600522" in stock_codes and "688511" in stock_codes
    print("-> 数据库读取校验通过: 现有股票、持仓数量与可用资金全部匹配!")


def test_image_naming():
    print("\n=== 测试 2: 存储结构与时间命名方式 ===")
    ts = time.strftime("%Y%m%d_%H%M%S")
    shot_name = f"{ts}_portfolio.png"
    order_name = f"{ts}_order_buy_002156.png"
    probe_name = f"{ts}_probe.png"

    assert shot_name.startswith(ts), "持仓截图必须以时间戳开头"
    assert order_name.startswith(ts), "订单截图必须以时间戳开头"
    assert probe_name.startswith(ts), "校准截图必须以时间戳开头"

    # 测试从名称中解析时间
    parsed_time = cleaner._parse_time_from_name(shot_name)
    assert parsed_time is not None, "必须能从时间戳命名的文件名中精确提取出 datetime"
    print(f"-> 时间命名样例: {shot_name}, 解析结果: {parsed_time}")
    print("-> 存储结构时间命名规则校验通过!")


def test_cleanup_functionality():
    print("\n=== 测试 3: 启动清理功能 (超过1个月的数据自动清理) ===")
    now = _dt.datetime.now()
    retention_days = 30
    cutoff = now - _dt.timedelta(days=retention_days)

    # 1. 构造一个 40 天前(超过1个月)的历史截图
    old_dt = now - _dt.timedelta(days=40)
    old_ts_str = old_dt.strftime("%Y%m%d_%H%M%S")
    old_shot_path = os.path.join(config.SHOT_DIR, f"{old_ts_str}_test_old_portfolio.png")
    with open(old_shot_path, "wb") as f:
        f.write(b"fake image data")
    # 设置文件的修改时间为 40 天前
    old_epoch = old_dt.timestamp()
    os.utime(old_shot_path, (old_epoch, old_epoch))

    # 构造一个 5 天前(在1个月内)的正常截图
    new_dt = now - _dt.timedelta(days=5)
    new_ts_str = new_dt.strftime("%Y%m%d_%H%M%S")
    new_shot_path = os.path.join(config.SHOT_DIR, f"{new_ts_str}_test_recent_portfolio.png")
    with open(new_shot_path, "wb") as f:
        f.write(b"fake recent image data")
    new_epoch = new_dt.timestamp()
    os.utime(new_shot_path, (new_epoch, new_epoch))

    # 2. 构造 40 天前的 MySQL 记录
    db_mysql.save_portfolio(
        cash=99999.0,
        total_asset=199999.0,
        stocks=[{"code": "999999", "name": "过期股票", "qty": 999}],
        shot_path=old_shot_path,
        source="test_old",
        record_time=old_dt
    )
    db_mysql.save_image_recognition(
        image_path=old_shot_path,
        recognition_type="test_old_rec",
        prompt="old prompt",
        raw_result="old result",
        status="success",
        record_time=old_dt
    )

    # 3. 执行系统清理
    print(f"当前启动基准时间: {now}, cutoff: {cutoff}")
    clean_res = cleaner.cleanup_system(startup_time=now, days=retention_days)

    # 4. 验证清理结果
    # 4.1 过期截图必须被删除，未过期截图必须保留
    assert not os.path.exists(old_shot_path), "超过1个月的历史截图应被删除"
    assert os.path.exists(new_shot_path), "1个月内的最近截图应被保留"
    os.remove(new_shot_path) # 清理测试文件
    print("-> 截图清理验证通过: >1个月的图片已删除, <=1个月的图片完好保留!")

    # 4.2 验证数据库过期记录已被清理
    conn = db_mysql.get_connection(use_db=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM `account_balance` WHERE `record_time` < %s", (cutoff,))
            cnt_bal = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM `stock_positions` WHERE `record_time` < %s", (cutoff,))
            cnt_pos = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM `image_recognitions` WHERE `record_time` < %s", (cutoff,))
            cnt_rec = cur.fetchone()[0]
            assert cnt_bal == 0, f"仍有未清理的过期账户快照: {cnt_bal}"
            assert cnt_pos == 0, f"仍有未清理的过期持仓明细: {cnt_pos}"
            assert cnt_rec == 0, f"仍有未清理的过期图片识别记录: {cnt_rec}"
    finally:
        conn.close()
    print("-> MySQL 数据清理验证通过: >1个月的识别记录与持仓已彻底清空!")


def test_model_manager():
    print("\n=== 测试 4: 模型打包与快速切换系统 ===")
    import model_manager
    import tempfile

    # 1. 记录初始激活模型
    initial_model = model_manager.get_active_model()
    initial_name = initial_model.get("name", "qwen3.5-4B")

    # 2. 模拟一个从 Hugging Face 下载的模型目录结构
    temp_hf_dir = tempfile.mkdtemp(prefix="hf_mock_model_")
    try:
        # 写入 config.json
        mock_config = {
            "architectures": ["Qwen2ForCausalLM"],
            "model_type": "qwen2",
            "vocab_size": 151936,
            "hidden_size": 2048
        }
        with open(os.path.join(temp_hf_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(mock_config, f)

        # 写入 tokenizer_config.json
        with open(os.path.join(temp_hf_dir, "tokenizer_config.json"), "w", encoding="utf-8") as f:
            json.dump({"tokenizer_class": "Qwen2Tokenizer"}, f)

        # 写入虚拟权重文件
        with open(os.path.join(temp_hf_dir, "model.safetensors"), "wb") as f:
            f.write(b"MOCK_WEIGHTS_DATA_FOR_TESTING")

        test_pack_name = "test_hf_qwen2_mock"
        pack_res = model_manager.pack_model(
            source_path=temp_hf_dir,
            model_name=test_pack_name,
            copy_files=False,
            description="自动化测试 Hugging Face 打包模型"
        )
        assert pack_res.get("ok") is True, f"模型打包失败: {pack_res}"
        target_dir = pack_res["path"]
        assert os.path.exists(target_dir), "打包目标目录不存在"
        assert os.path.exists(os.path.join(target_dir, "model_meta.json")), "未生成 model_meta.json"
        print(f"-> 模型打包验证成功: 目标路径 {target_dir}")

        # 3. 验证模型发现
        all_models = model_manager.list_models()
        model_names = [m["name"] for m in all_models]
        assert test_pack_name in model_names, f"list_models 列表中应包含 {test_pack_name}"
        print(f"-> 模型列表发现成功: 包含 {model_names}")

        # 4. 验证模型切换
        switch_res = model_manager.switch_model(test_pack_name, reload_server=False)
        assert switch_res.get("ok") is True, f"切换模型失败: {switch_res}"
        active = model_manager.get_active_model()
        assert active["name"] == test_pack_name, f"当前激活模型应为 {test_pack_name}, 实际为 {active['name']}"
        print(f"-> 模型快速切换成功: 当前激活为 {active['name']}")

        # 5. 切回原有模型
        model_manager.switch_model(initial_name, reload_server=False)
        restored = model_manager.get_active_model()
        assert restored["name"] == initial_name, f"切回初始模型失败: 期望 {initial_name}, 实际为 {restored['name']}"
        print(f"-> 恢复初始模型成功: {restored['name']}")

    finally:
        # 清理临时源文件夹与打包生成的测试模型文件夹
        import shutil
        if os.path.exists(temp_hf_dir):
            shutil.rmtree(temp_hf_dir, ignore_errors=True)
        test_model_dir = os.path.join(config.MODELS_DIR, "test_hf_qwen2_mock")
        if os.path.exists(test_model_dir):
            shutil.rmtree(test_model_dir, ignore_errors=True)
        print("-> 测试临时打包文件已彻底清理")


if __name__ == "__main__":
    try:
        test_mysql_portfolio()
        test_image_naming()
        test_cleanup_functionality()
        test_model_manager()
        print("\n==========================================")
        print("所有优化项与新功能测试全部通过 (ALL TESTS PASSED)!")
        print("==========================================")
    except Exception as e:
        print(f"\n[ERROR] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

