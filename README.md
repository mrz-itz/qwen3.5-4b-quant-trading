# 大模型量化交易(同花顺模拟盘)

基于本地 **Qwen3.5-4B**(多模态)+ **雷电模拟器内同花顺 App「模拟炒股」**的自动量化程序。
- 股票池来自 `股票信息.doc`(程序启动时解析)
- **决策**:东财实时行情 + 本地 MA/RSI/MACD 文本喂给大模型 → 返回买卖 JSON
- **确认**:关键界面(下单结果/持仓/图表)截图再交给模型读图核实
- **下单**:全自动在模拟盘执行(虚拟资金,非真实交易)

## ⚠️ 免责声明
纯**模拟盘**、虚拟资金。程序仅供学习/演示,不构成任何投资建议。

---

## 一、你需要先做的(前置条件)
程序**不**负责同花顺的安装与登录:
1. 启动雷电模拟器(`雷电模拟器14`),分辨率建议保持 1440×2560。
2. 在模拟器内安装**同花顺 Android App**,用手机号登录,进入底部**交易 → 模拟炒股**(模拟账户)。
3. 点一次加自选也行(非必须)。
4. 内存吃紧的话关掉无关大程序(本机只剩 ~2G 空闲,模型要占显存 5G+)。

装好后跑 `python run.py emu start` 让程序接管,并告诉我可进入“校准”。

## 二、安装环境(只需一次)
```bash
# 1) 建 venv(已建)
/d/Anaconda/python.exe -m venv --system-site-packages .venv

# 2) 装 CUDA 版 torch(约 2.7G;必须装进 venv 覆盖基座 CPU 版)
.venv/Scripts/python.exe -m pip install --force-reinstall "torch==2.10.0+cu128" --index-url https://download.pytorch.org/whl/cu128

# 3) 补小依赖
.venv/Scripts/python.exe -m pip install -r requirements.txt
```
> 之后一律用 `.venv/Scripts/python.exe run.py ...`(或把该 python 配到 IDE)。

## 三、使用
```bash
.venv/Scripts/python.exe run.py env              # 环境自检(GPU/模型/接口/股票池)
.venv/Scripts/python.exe run.py serve            # 前台起模型服务(可另开窗口)
.venv/Scripts/python.exe run.py emu start        # 启动雷电模拟器
.venv/Scripts/python.exe run.py probe            # 校准:导出当前屏 UI 元素/截图
.venv/Scripts/python.exe run.py once --dry       # 只分析一轮,打印拟下单,不下单(推荐先跑)
.venv/Scripts/python.exe run.py loop --dry       # 全自动盯盘(不真实下单)
.venv/Scripts/python.exe run.py loop --live      # 全自动盯盘 + 模拟盘真实下单
```
- 参数 `--force` 无视交易时段立即跑(周末演示用)。
- 盯盘默认只在 A 股时段(09:30–11:30 / 13:00–15:00,周一至五)动作。

## 四、急停与风控
- 单笔 ≤ 可用资金 10%;单只 ≤ 资产 20%;每日最多 8 笔;每分钟最多 1 笔。
- **急停**:新建文件 `data\STOP` 后立即暂停下单(删除即恢复)。
- 所有决策/成交记在 `data\trades.csv`;资产快照 `data\equity.jsonl`、`data\status.json`。
- 修改风控参数:`config.py`。

## 五、目录结构
```
config.py / stocks_loader.py   配置 & 解析股票信息.doc
market_data.py                 东财实时行情 + 腾讯日K + 本地指标
model_server.py                常驻模型服务(/analyze_text /analyze_image)
model_client.py                模型服务 HTTP 客户端
strategy.py                    组装 prompt、解析并规整模型 JSON
vision.py                      截图送模型读图确认
emulator.py                    雷电/adb 驱动(截图、点按、uiautomator)
ths_app.py                     同花顺页驱动(搜索/持仓/下单)
selectors.json                 同花顺 UI 锚点(校准后填写)
main_loop.py / run.py          盯盘调度与命令行
data/                          运行产物(日志/截图/账本)
```

## 六、校准(同花顺 UI 会随版本变化)
1. `run.py emu start` 启动模拟器,把同花顺切到你要用的页面(如交易/持仓/个股)。
2. `run.py probe` 会把当前屏可见元素坐标与截图存到 `data/`。
3. 对照 `selectors.json`,把“搜索入口/交易/模拟/持仓/买/卖/数量/确认”的文案填成你 App 里的真实文案。
4. 先在 `once --dry` 验证决策链路,再 `--live` 用一小笔(100 股)实测下单并核对持仓页与 `data/trades.csv`。

## 七、已知限制 / 说明
- `input text` 只能输数字/字母 → 搜索与数量用代码数字,安全。
- 个别北交所新股(920 段)可能缺历史日K,模型收到的指标少一些,不影响其他。
- 行情接口为公开免费源,偶发限流/断连:本轮抓取失败会跳过该股并继续,不会硬下单。
- 盘中调模型的频率受 `config.MODEL_CALL_MIN_INTERVAL` 与触发条件节流,避免把显存/接口打爆。
