# qqq-dca-signal

`qqq-dca-signal` 是一个运行在本地 macOS 的 A 股纳斯达克 100 / QQQ 等价 QDII-ETF 定投信号程序。

程序只发信号，不自动交易。默认策略适合“大资金、少交易、每日最多一次”的使用方式：A 股交易日 14:40 预热历史溢价缓存，14:55 拉取实时行情并计算信号，用户如收到买入信号，可按自己的策略在 14:57 挂涨停价买入。

## 功能

- 手工配置 QDII-ETF 基金池，默认覆盖当前纳斯达克 100 / QQQ 等价 A 股场内 ETF。
- 使用动态溢价过滤：近 60 个交易日 70% 分位 + 12% 硬上限。
- 使用 QQQ/NQ 市场评分，低于阈值则不买。
- 每日只选择一只最优基金。
- 通过 OpenAI 兼容 API 生成规则解释。
- 通过 PushPlus 推送 Markdown 信号。
- 买入信号标题直接包含推荐基金代码和名称。
- 使用 SQLite 保存历史缓存、每日信号和回测结果。
- 生成 Markdown 和 Plotly HTML 回测报告。

## 快速开始

```bash
cd ~/code/python/qqq-dca-signal
cp config.example.yaml config.yaml
uv run qqq-dca-signal show-config
uv run qqq-dca-signal warm-cache
uv run qqq-dca-signal run-daily --dry-run
```

`config.yaml` 可配置基金池、溢价规则、市场评分、OpenAI 兼容 API 和 PushPlus。真实密钥只放在本地 `config.yaml` 或环境变量中，不要提交。

PushPlus 只使用 `tokens` 字段；即使只有一个 token，也写成列表：

```yaml
pushplus:
  enabled: true
  tokens:
    - "${PUSHPLUS_TOKEN_1}"
    - "${PUSHPLUS_TOKEN_2}"
```

程序会向 `tokens` 列表里的每个 token 推送。

## 每日信号流程

```bash
uv run qqq-dca-signal warm-cache
uv run qqq-dca-signal run-daily --dry-run
uv run qqq-dca-signal run-daily
```

`warm-cache` 用于预热当天历史溢价缓存。`run-daily` 默认不再临时拉取全量历史数据；如果当天缓存缺失，会返回 `SKIP_DATA`，避免 14:55 信号路径变慢。

`--dry-run` 会正常拉数据、计算规则、调用 LLM、写 SQLite，但不会发送 PushPlus。

正式运行 `run-daily` 时，程序会先推送一条“开始计算”消息；计算完成后再推送最终买入或不买结论。`--dry-run` 只在终端打印，不发送 PushPlus。

最终信号正文中，LLM 分析会放在前部；候选基金以 Markdown 表格展示。

## 定时任务

安装 macOS `launchd` 定时任务：

```bash
uv run qqq-dca-signal install-launchd
```

该命令会安装两个任务：

- `14:40`：运行 `warm-cache`。
- `14:55`：运行 `run-daily`。

卸载：

```bash
uv run qqq-dca-signal uninstall-launchd
```

非 A 股交易日程序会跳过，默认不推送。

## 回测

一年以上回测建议使用默认模式 `daily-proxy`：

```bash
uv run qqq-dca-signal backtest --start 2025-07-01 --end 2026-06-30
```

严格模式适合近期短区间，或者未来接入付费 NQ 历史盘中数据后使用：

```bash
uv run qqq-dca-signal backtest --start 2026-06-01 --end 2026-06-30 --market-mode intraday-strict
```

回测模式说明：

- `daily-proxy`：使用基金历史溢价和 QQQ 日频指标，NQ 盘中分量记为中性半分，适合观察一年以上规则方向。
- `intraday-strict`：尝试使用 NQ 14:55 附近历史盘中数据，更接近实盘，但免费数据源不稳定。

回测会生成：

- 终端摘要。
- `reports/*.md`，适合交给 LLM 继续分析。
- `reports/*.html`，适合人工查看交互图表。
- SQLite 明细。

## 数据源

当前数据源组合：

- A 股 ETF 实时价格 / IOPV：东方财富窄接口。
- 历史溢价：HaoETF 优先。
- HaoETF 缺失时：AkShare ETF 日线收盘价 + 历史单位净值生成近似历史溢价。
- QQQ 日线：yfinance。
- NQ 实时 / 历史盘中：yfinance。

免费数据源可能缺失、延迟或字段变化。凡是参与规则的关键字段不可用，系统返回 `SKIP_DATA`，不会用旧数据、默认假数据或 LLM 推断。

## 常用命令

```bash
uv run qqq-dca-signal show-config
uv run qqq-dca-signal warm-cache
uv run qqq-dca-signal warm-cache --refresh
uv run qqq-dca-signal run-daily --dry-run
uv run qqq-dca-signal run-daily
uv run qqq-dca-signal run-daily --as-of 2026-06-30T14:55:00+08:00 --dry-run
uv run qqq-dca-signal backtest --start 2025-07-01 --end 2026-06-30
uv run qqq-dca-signal backtest --start 2026-06-01 --end 2026-06-30 --market-mode intraday-strict
uv run qqq-dca-signal install-launchd
uv run qqq-dca-signal uninstall-launchd
```

## 项目结构

```text
config.example.yaml          示例配置
config.yaml                  本地私有配置，不提交
data/qqq_dca.sqlite          SQLite 数据库
reports/                     回测报告
docs/design.md               策略和系统设计说明
src/qqq_dca_signal/          源码
```

## 设计文档

更完整的策略口径见 [docs/design.md](docs/design.md)。
