from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def write_markdown_report(path: Path, summary: dict, rows: list[dict], params: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    lines = [
        "# NDX 定投策略回测报告",
        "",
        "## 参数快照",
        "",
        "```yaml",
        f"backtest: {params.get('backtest')}",
        f"premium_filter: {params.get('premium_filter')}",
        f"market_score: {params.get('market_score')}",
        "```",
        "",
        "## 基准对比",
        "",
        "| 策略 | 买入次数 | 总投入 | 期末价值 | 收益率 | 平均溢价 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for strategy, item in summary.items():
        lines.append(
            f"| {strategy} | {item['buy_days']} | {item['invested']:.2f} | "
            f"{item['final_value']:.2f} | {item['return']:.2%} | {item['avg_premium']:.2%} |"
        )

    lines.extend(
        [
            "",
            "## 数据限制",
            "",
            "- 免费源没有长期 14:55 历史快照，daily-proxy 模式将 NQ 盘中分量记为中性半分。",
            "- intraday-strict 模式才会尝试使用 yfinance NQ 5分钟历史数据，缺失时完整策略会记录 SKIP_DATA。",
            "- 本报告适合观察规则方向，不应视为精确成交回放。",
            "",
            "## LLM Review Context",
            "",
            "### Observed Summary",
        ]
    )
    for strategy, item in summary.items():
        lines.append(f"- {strategy}: buy_days={item['buy_days']}, return={item['return']:.2%}, avg_premium={item['avg_premium']:.2%}")
    lines.extend(["", "### Daily Rows Sample", ""])
    if not frame.empty:
        lines.append(frame.head(20).to_markdown(index=False))
    path.write_text("\n".join(lines), encoding="utf-8")


def write_html_report(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    if frame.empty:
        path.write_text("<html><body><h1>No backtest data</h1></body></html>", encoding="utf-8")
        return

    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        subplot_titles=("策略净值", "买入点", "溢价率", "市场评分"),
        vertical_spacing=0.08,
    )
    for strategy, group in frame.groupby("strategy"):
        fig.add_trace(
            go.Scatter(x=group["date"], y=group["portfolio_value"], name=f"{strategy} value"),
            row=1,
            col=1,
        )
        buys = group[group["status"] == "BUY"]
        fig.add_trace(
            go.Scatter(
                x=buys["date"],
                y=buys["buy_price"],
                mode="markers",
                name=f"{strategy} buys",
            ),
            row=2,
            col=1,
        )
        if "premium" in group:
            fig.add_trace(
                go.Scatter(x=group["date"], y=group["premium"], name=f"{strategy} premium"),
                row=3,
                col=1,
            )
        if "market_score" in group:
            fig.add_trace(
                go.Scatter(x=group["date"], y=group["market_score"], name=f"{strategy} score"),
                row=4,
                col=1,
            )
    fig.add_hline(y=60, line_dash="dash", row=4, col=1)
    fig.update_layout(height=1100, title="NDX 定投策略回测", hovermode="x unified")
    path.write_text(fig.to_html(full_html=True, include_plotlyjs="cdn"), encoding="utf-8")
