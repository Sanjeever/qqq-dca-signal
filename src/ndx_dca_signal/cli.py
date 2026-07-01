from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from ndx_dca_signal.backtest import Backtester
from ndx_dca_signal.config import load_config, mask_secrets, resolve_project_path
from ndx_dca_signal.database import Database
from ndx_dca_signal.launchd import install_launchd as install_launchd_plist
from ndx_dca_signal.launchd import uninstall_launchd as uninstall_launchd_plist
from ndx_dca_signal.llm import generate_analysis
from ndx_dca_signal.news import fetch_news_context
from ndx_dca_signal.notifier import send_notification
from ndx_dca_signal.rules import render_signal_markdown
from ndx_dca_signal.runner import DailyRunner, WarmCacheRunner, now_in_config_timezone
from ndx_dca_signal.sim_trading import build_portfolio_summary, record_signal_trade, settle_pending_trades


app = typer.Typer(no_args_is_help=True)
console = Console()


ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="配置文件路径，默认使用项目根目录 config.yaml。"),
]


def prepare(config_path: Path | None) -> tuple[dict, Database]:
    config = load_config(config_path)
    database = Database(resolve_project_path(config["paths"]["database"]))
    database.init()
    return config, database


@app.command("show-config")
def show_config(config: ConfigOption = None) -> None:
    loaded = load_config(config)
    console.print(yaml.safe_dump(mask_secrets(loaded), allow_unicode=True, sort_keys=False))


@app.command("run-daily")
def run_daily(
    config: ConfigOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="不发送推送，在终端打印内容。")] = False,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="指定运行时间，例如 2026-06-30T14:55:00+08:00。"),
    ] = None,
) -> None:
    loaded, database = prepare(config)
    run_at = datetime.fromisoformat(as_of) if as_of else now_in_config_timezone(loaded)
    start_content = (
        "# NDX定投信号：开始计算\n\n"
        f"- 时间：{run_at.isoformat()}\n"
        "- 状态：程序已启动，正在拉取行情并计算今日信号。"
    )
    if dry_run:
        console.print(start_content)
    else:
        send_notification("NDX定投信号：开始计算", start_content, loaded)

    result = DailyRunner(loaded).run(run_at, dry_run=dry_run)
    if not dry_run:
        record_signal_trade(result, loaded, database)
    result.sim_portfolio = build_portfolio_summary(result, loaded, database)

    try:
        result.news_context = fetch_news_context(loaded)
    except Exception as exc:
        result.news_errors.append(f"新闻上下文获取失败：{exc}")

    try:
        analysis = generate_analysis(result, loaded)
        result.llm_analysis = analysis
    except Exception as exc:
        result.llm_analysis = f"LLM 分析生成失败：{exc}"

    content = render_signal_markdown(result)
    database.record_signal(result)

    should_push = result.status != "SKIP_CALENDAR" or loaded["schedule"].get("push_on_non_trading_day", False)
    if dry_run or not should_push:
        console.print(content)
        return

    send_notification(result.title, content, loaded)
    console.print(f"已发送推送：{result.title}")


@app.command("warm-cache")
def warm_cache(
    config: ConfigOption = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="强制刷新当天历史溢价缓存。")] = False,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="指定预热时间，例如 2026-06-30T14:40:00+08:00。"),
    ] = None,
) -> None:
    loaded, _ = prepare(config)
    run_at = datetime.fromisoformat(as_of) if as_of else now_in_config_timezone(loaded)
    result = WarmCacheRunner(loaded).run(run_at, refresh=refresh)

    table = Table(title=f"Warm Cache: {result['status']}")
    table.add_column("代码")
    table.add_column("来源")
    table.add_column("行数", justify="right")
    table.add_column("说明")
    for item in result["updated"]:
        table.add_row(item["code"], item["source"], str(item["rows"]), item["warning"] or "")
    for code in result["skipped"]:
        table.add_row(code, "cache", "-", "当天缓存已存在")
    console.print(table)
    for error in result["errors"]:
        console.print(f"[red]{error}[/red]")


@app.command("settle-sim-trades")
def settle_sim_trades(
    config: ConfigOption = None,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="指定结算时间，例如 2026-06-30T15:10:00+08:00。"),
    ] = None,
) -> None:
    loaded, database = prepare(config)
    run_at = datetime.fromisoformat(as_of) if as_of else now_in_config_timezone(loaded)
    settled = settle_pending_trades(loaded, database, run_at)
    if not settled:
        console.print("没有待结算的模拟交易。")
        return

    table = Table(title="模拟交易结算")
    table.add_column("日期")
    table.add_column("代码")
    table.add_column("名称")
    table.add_column("数量", justify="right")
    table.add_column("成交价", justify="right")
    table.add_column("成交额", justify="right")
    for item in settled:
        table.add_row(
            str(item["trade_date"]),
            str(item["code"]),
            str(item["name"]),
            str(item["quantity"]),
            f"{float(item['fill_price']):.4f}",
            f"{float(item['fill_amount']):.2f}",
        )
    console.print(table)


@app.command("backtest")
def backtest(
    start: Annotated[str, typer.Option("--start", help="回测开始日期，格式 YYYY-MM-DD。")],
    end: Annotated[str, typer.Option("--end", help="回测结束日期，格式 YYYY-MM-DD。")],
    market_mode: Annotated[
        str,
        typer.Option("--market-mode", help="回测市场评分模式：daily-proxy 或 intraday-strict。"),
    ] = "daily-proxy",
    config: ConfigOption = None,
) -> None:
    if market_mode not in {"daily-proxy", "intraday-strict"}:
        raise typer.BadParameter("--market-mode 只能是 daily-proxy 或 intraday-strict")
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    loaded, database = prepare(config)
    reports_dir = resolve_project_path(loaded["paths"]["reports_dir"])
    summary, rows, markdown_path, html_path = Backtester(loaded, reports_dir).run(start_date, end_date, market_mode)
    params = {**loaded, "backtest": {"market_mode": market_mode}}
    run_id = database.record_backtest(
        start_date.isoformat(),
        end_date.isoformat(),
        params,
        summary,
        rows,
        markdown_path,
        html_path,
    )

    table = Table(title=f"Backtest #{run_id}")
    table.add_column("策略")
    table.add_column("买入次数", justify="right")
    table.add_column("总投入", justify="right")
    table.add_column("期末价值", justify="right")
    table.add_column("收益率", justify="right")
    table.add_column("平均溢价", justify="right")
    for strategy, item in summary.items():
        table.add_row(
            strategy,
            str(item["buy_days"]),
            f"{item['invested']:.2f}",
            f"{item['final_value']:.2f}",
            f"{item['return']:.2%}",
            f"{item['avg_premium']:.2%}",
        )
    console.print(table)
    console.print(f"Markdown: {markdown_path}")
    console.print(f"HTML: {html_path}")


@app.command("install-launchd")
def install_launchd() -> None:
    paths = install_launchd_plist()
    for path in paths:
        console.print(f"已安装 launchd: {path}")


@app.command("uninstall-launchd")
def uninstall_launchd() -> None:
    paths = uninstall_launchd_plist()
    for path in paths:
        console.print(f"已卸载 launchd: {path}")
