# AGENTS.md

本文件是本项目的协作约定，供 Codex、Claude Code 等代码代理读取。除非用户另有明确要求，后续修改应遵守这里的项目边界。

## 项目定位

`qqq-dca-signal` 是一个本地 macOS 定时运行的 A 股纳斯达克 100 / QQQ 等价 QDII-ETF 定投信号程序。

程序只负责：

- 拉取 A 股基金、QQQ、NQ 行情。
- 根据明确规则计算 `BUY` / `SKIP_RULE` / `SKIP_DATA` / `SKIP_CALENDAR`。
- 使用 LLM 解释规则结果。
- 通过 PushPlus 推送信号，支持一个或多个 token。
- 写入 SQLite 审计记录、回测记录和本地模拟交易记录。

程序不负责：

- 自动下单。
- 连接券商真实交易接口。
- 管理资金规模。
- 自动覆盖规则信号。
- 用 LLM 猜测缺失数据。

## 核心策略

- A 股交易日 14:40 运行 `warm-cache`，预热历史溢价缓存。
- A 股交易日 14:55 运行 `run-daily`，只拉实时行情和市场数据。
- A 股交易日 15:10 运行 `settle-sim-trades`，对本地模拟挂单按收盘价结算。
- `launchd` 安装时从 `config.yaml` 读取运行时间；修改运行时间后需要重新安装 launchd。
- `run-daily` 正式运行开始时先推送“开始计算”，完成后再推送最终信号。
- 如果最终信号为 `BUY`，PushPlus 标题必须包含推荐基金代码和名称。
- 最终信号正文中，LLM 分析应靠前展示，候选基金使用 Markdown 表格。
- 如果开启本地模拟交易，最终信号正文应展示模拟账户摘要、模拟持仓和最近模拟交易。
- 用户如收到买入信号，按自己的策略在 14:57 挂涨停价买入。
- 如果开启本地模拟交易，`run-daily` 只在正式运行且 `BUY` 时记录模拟 14:57 挂涨停价买入；`--dry-run` 不写模拟交易。
- 回测成交价使用当日收盘价。
- 每日最多发出一次买入信号。
- 每日只选择一只最优基金。

默认主策略是 `premium_plus_market`：

- 先做动态溢价过滤。
- 再做 QQQ/NQ 市场评分。
- 市场评分低于阈值则不买。

## 数据源约定

当前数据源组合：

- A 股 ETF 实时价格 / IOPV：东方财富窄接口。
- 历史溢价：HaoETF 优先。
- HaoETF 缺失时：AkShare ETF 日线收盘价 + 历史单位净值生成近似历史溢价。
- QQQ 日线：yfinance。
- NQ 实时 / 历史盘中：yfinance。

关键数据缺失时，必须返回 `SKIP_DATA`，不要使用静默兜底、旧数据、默认假数据或 LLM 推断。

## 回测口径

回测支持两种市场评分模式：

- `daily-proxy`：默认模式，适合一年以上回测。使用基金历史溢价和 QQQ 日频指标，NQ 盘中分量记为中性半分。
- `intraday-strict`：严格模式，尝试使用 NQ 14:55 附近历史盘中数据。适合短期回测，或未来接入付费 NQ 历史盘中数据后使用。

回测报告必须明确标注 `market_mode`，不要把 `daily-proxy` 解释为完整实盘口径。

## 常用命令

```bash
uv run qqq-dca-signal show-config
uv run qqq-dca-signal warm-cache
uv run qqq-dca-signal run-daily --dry-run
uv run qqq-dca-signal settle-sim-trades
uv run qqq-dca-signal backtest --start 2025-07-01 --end 2026-06-30
uv run qqq-dca-signal backtest --start 2026-06-01 --end 2026-06-30 --market-mode intraday-strict
uv run qqq-dca-signal install-launchd
uv run qqq-dca-signal uninstall-launchd
```

## 开发约定

- 使用 `uv` 管理依赖和运行命令。
- 不直接使用 `python`、`pip` 或 `uv pip install`。
- 新依赖使用 `uv add`。
- 配置文件为 `config.yaml`，不要提交真实密钥。
- `config.example.yaml` 只放示例配置。
- `data/`、`reports/`、`logs/` 是运行产物，不应作为核心源码维护。
- 修改策略规则时，同步更新 `README.md` 和 `docs/design.md`。
- 修改 CLI 命令时，同步更新 `README.md`。

## 验证约定

用户级指令要求：任务完成后不主动运行编译、测试、类型检查，除非用户明确要求。

文档或小改动可以运行轻量命令验证入口，例如：

```bash
uv run qqq-dca-signal --help
uv run qqq-dca-signal show-config
```

不要在没有用户要求时主动跑长周期回测、真实 PushPlus 推送或带真实 LLM 的耗时命令。
