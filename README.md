# ndx-dca-signal

`ndx-dca-signal` 是一个运行在本地 macOS 的 A 股纳斯达克 100 / NDX 等价 QDII-ETF 定投信号程序。

程序只发信号，不自动交易。默认策略适合“大资金、少交易、每日最多一次”的使用方式：A 股交易日 14:40 预热历史溢价缓存，14:55 拉取实时行情并计算信号，用户如收到买入信号，可按自己的策略在 14:57 挂涨停价买入。

## 功能

- 手工配置 QDII-ETF 基金池，默认覆盖当前纳斯达克 100 / NDX 等价 A 股场内 ETF。
- 使用动态溢价过滤：近 60 个交易日 70% 分位 + 12% 硬上限。
- 使用 NDX/NQ 市场评分，低于阈值则不买。
- 每日只选择一只最优基金。
- 通过 OpenAI 兼容 API 生成规则解释。
- 可选使用 AnySearch 拉取新闻上下文，供 LLM 补充风险解释。
- 通过 Bark / PushPlus 推送 Markdown 信号。
- 买入信号标题直接包含推荐基金代码和名称。
- 可选开启本地模拟交易账本：买入信号发出后记录 14:57 挂涨停价模拟买入，15:10 按当日收盘价结算。
- 使用 SQLite 保存历史缓存、每日信号和回测结果。
- 生成 Markdown 和 Plotly HTML 回测报告。

## 快速开始

```bash
cd ~/code/python/ndx-dca-signal
cp config.example.yaml config.yaml
uv run ndx-dca-signal show-config
uv run ndx-dca-signal warm-cache
uv run ndx-dca-signal run-daily --dry-run
```

`config.yaml` 可配置基金池、溢价规则、市场评分、OpenAI 兼容 API 和推送通道。真实密钥只放在本地 `config.yaml` 或环境变量中，不要提交。

推荐使用 Bark：

```yaml
bark:
  enabled: true
  server_url: "https://api.day.app"
  keys:
    - "${BARK_KEY}"
  group: "ndx-dca-signal"
  is_archive: true
  timeout_seconds: 10
```

PushPlus 仍可作为可选通道。它只使用 `tokens` 字段；即使只有一个 token，也写成列表：

```yaml
pushplus:
  enabled: false
  tokens:
    - "${PUSHPLUS_TOKEN_1}"
    - "${PUSHPLUS_TOKEN_2}"
```

如果 Bark 和 PushPlus 同时开启，程序会向所有已开启通道推送。

新闻上下文默认关闭。需要让 LLM 结合近期新闻分析时，在本地 `config.yaml` 中开启并填入 `ANYSEARCH_API_KEY`：

```yaml
news:
  enabled: true
  provider: anysearch
  endpoint: "https://api.anysearch.com/mcp"
  api_key: "${ANYSEARCH_API_KEY}"
  lookback_hours: 24
  max_results: 6
  queries:
    - "Nasdaq 100 纳斯达克100 科技股"
    - "Nvidia Microsoft Apple Meta Amazon Tesla Nasdaq news"
    - "Federal Reserve CPI nonfarm payrolls Nasdaq risk"
```

新闻只作为 LLM 分析上下文，不参与 `BUY` / `SKIP` 规则判断，也不能覆盖规则信号。

模拟交易默认关闭。需要开启时在本地 `config.yaml` 中配置：

```yaml
sim_trading:
  enabled: true
  order_amount: 100000
  order_time: "14:57:00"
  settle_time: "15:10:00"
  lot_size: 100
```

`lot_size` 是一手的份额数量。A 股 ETF 场内交易通常一手是 100 份，所以保持 `100` 即可。模拟数量按 `order_amount / 信号价格` 向下取整到 `lot_size` 的整数倍。

这只是本地模拟账本，不连接券商，不会真实下单。`run-daily` 只在正式运行且信号为 `BUY` 时写入模拟挂单；`--dry-run` 不写模拟交易。15:10 使用 ETF 当日收盘价结算。

## 每日信号流程

```bash
uv run ndx-dca-signal warm-cache
uv run ndx-dca-signal run-daily --dry-run
uv run ndx-dca-signal run-daily
uv run ndx-dca-signal settle-sim-trades
```

`warm-cache` 用于预热当天历史溢价缓存。`run-daily` 默认不再临时拉取全量历史数据；如果当天缓存缺失，会返回 `SKIP_DATA`，避免 14:55 信号路径变慢。

`--dry-run` 会正常拉数据、计算规则、调用 LLM、写 SQLite，但不会发送推送。

正式运行 `run-daily` 时，程序会先推送一条“开始计算”消息；计算完成后再推送最终买入或不买结论。`--dry-run` 只在终端打印，不发送推送。

最终信号正文中，LLM 分析会放在前部；候选基金以 Markdown 表格展示。

如果开启新闻上下文，最终信号正文会显示“新闻上下文”段，LLM 分析也会结合新闻解释风险，但不会改变规则信号。

如果开启模拟交易，最终信号正文会增加“模拟账户”段，每天展示模拟持仓、持仓成本、最新市值、浮动盈亏、浮动收益率、待结算挂单和最近模拟交易。如果当天最终信号为 `BUY`，还会增加“模拟交易”段，展示本次模拟挂单时间、下单金额、数量和结算状态。

## 定时任务

安装 macOS `launchd` 定时任务：

```bash
uv run ndx-dca-signal install-launchd
```

该命令会安装三个任务：

- `14:40`：运行 `warm-cache`。
- `14:55`：运行 `run-daily`。
- `15:10`：运行 `settle-sim-trades`。

三个时间分别来自 `config.yaml` 的 `schedule.warm_cache_time`、`schedule.run_time` 和 `sim_trading.settle_time`。修改普通策略、密钥、基金池配置不需要重新安装定时任务；修改这些运行时间后需要重新执行 `install-launchd`。

卸载：

```bash
uv run ndx-dca-signal uninstall-launchd
```

非 A 股交易日程序会跳过，默认不推送。

## 回测

一年以上回测建议使用默认模式 `daily-proxy`：

```bash
uv run ndx-dca-signal backtest --start 2025-07-01 --end 2026-06-30
```

严格模式适合近期短区间，或者未来接入付费 NQ 历史盘中数据后使用：

```bash
uv run ndx-dca-signal backtest --start 2026-06-01 --end 2026-06-30 --market-mode intraday-strict
```

回测模式说明：

- `daily-proxy`：使用基金历史溢价和 NDX 日频指标，NQ 盘中分量记为中性半分，适合观察一年以上规则方向。
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
- NDX 日线：yfinance。
- NQ 实时 / 历史盘中：yfinance。

免费数据源可能缺失、延迟或字段变化。凡是参与规则的关键字段不可用，系统返回 `SKIP_DATA`，不会用旧数据、默认假数据或 LLM 推断。

## 常用命令

```bash
uv run ndx-dca-signal show-config
uv run ndx-dca-signal warm-cache
uv run ndx-dca-signal warm-cache --refresh
uv run ndx-dca-signal run-daily --dry-run
uv run ndx-dca-signal run-daily
uv run ndx-dca-signal run-daily --as-of 2026-06-30T14:55:00+08:00 --dry-run
uv run ndx-dca-signal settle-sim-trades
uv run ndx-dca-signal settle-sim-trades --as-of 2026-06-30T15:10:00+08:00
uv run ndx-dca-signal backtest --start 2025-07-01 --end 2026-06-30
uv run ndx-dca-signal backtest --start 2026-06-01 --end 2026-06-30 --market-mode intraday-strict
uv run ndx-dca-signal install-launchd
uv run ndx-dca-signal uninstall-launchd
```

## 项目结构

```text
config.example.yaml          示例配置
config.yaml                  本地私有配置，不提交
data/ndx_dca.sqlite          SQLite 数据库
reports/                     回测报告
docs/design.md               策略和系统设计说明
src/ndx_dca_signal/          源码
```

## 设计文档

更完整的策略口径见 [docs/design.md](docs/design.md)。
