from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ndx_dca_signal.backtest import enabled_funds
from ndx_dca_signal.cache import HistoryCache
from ndx_dca_signal.config import resolve_project_path
from ndx_dca_signal.data_sources import (
    AShareCalendar,
    AkShareClient,
    HaoEtfClient,
    YahooClient,
)
from ndx_dca_signal.models import SignalResult
from ndx_dca_signal.rules import build_signal, calculate_market_score, evaluate_funds


def fetch_history_for_fund(fund, haoetf: HaoEtfClient, akshare: AkShareClient):
    try:
        return "haoetf", haoetf.fetch_history(fund.code), None
    except Exception as exc:
        try:
            return "akshare_approx", akshare.fetch_fund_history_approx(fund), f"{fund.code} 使用 AkShare 历史净值近似溢价，HaoETF 原因：{exc}"
        except Exception as fallback_exc:
            raise RuntimeError(f"{fund.code} 历史溢价缺失：{exc}；AkShare 兜底失败：{fallback_exc}") from fallback_exc


class DailyRunner:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.calendar = AShareCalendar()
        self.akshare = AkShareClient()
        self.haoetf = HaoEtfClient()
        self.yahoo = YahooClient()
        self.history_cache = HistoryCache(resolve_project_path(config["paths"]["database"]))

    def run(self, as_of: datetime, dry_run: bool = False) -> SignalResult:
        if not self.calendar.is_trading_day(as_of.date()):
            return SignalResult(
                status="SKIP_CALENDAR",
                as_of=as_of,
                selected_fund=None,
                fund_evaluations=[],
                market_score=None,
                reasons=["非 A 股交易日"],
                dry_run=dry_run,
            )

        funds = enabled_funds(self.config)
        histories = {}
        data_errors: list[str] = []
        try:
            snapshots, snapshot_errors = self.akshare.fetch_fund_snapshots(funds, as_of)
            data_errors.extend(snapshot_errors)
        except Exception as exc:
            return SignalResult(
                status="SKIP_DATA",
                as_of=as_of,
                selected_fund=None,
                fund_evaluations=[],
                market_score=None,
                reasons=[f"ETF 实时数据缺失：{exc}"],
                dry_run=dry_run,
            )

        for snapshot in snapshots:
            cached = self.history_cache.get(snapshot.code, as_of.date())
            if cached is not None:
                histories[snapshot.code] = cached
                continue
            data_errors.append(f"{snapshot.code} 缺少当天历史溢价缓存，请先运行 warm-cache")

        if not snapshots:
            return SignalResult(
                status="SKIP_DATA",
                as_of=as_of,
                selected_fund=None,
                fund_evaluations=[],
                market_score=None,
                reasons=["全部基金实时数据缺失", *data_errors],
                dry_run=dry_run,
            )
        if not histories:
            return SignalResult(
                status="SKIP_DATA",
                as_of=as_of,
                selected_fund=None,
                fund_evaluations=[],
                market_score=None,
                reasons=["全部基金历史溢价数据缺失", *data_errors],
                dry_run=dry_run,
            )

        evaluations = evaluate_funds(snapshots, histories, self.config)
        try:
            ndx_history = self.yahoo.ndx_history(self.config["data_sources"]["ndx_symbol"])
            nq_change = self.yahoo.nq_realtime_change(self.config["data_sources"]["nq_symbol"])
            market_score = calculate_market_score(ndx_history, nq_change, self.config)
        except Exception as exc:
            return SignalResult(
                status="SKIP_DATA",
                as_of=as_of,
                selected_fund=None,
                fund_evaluations=evaluations,
                market_score=None,
                reasons=[f"市场数据缺失：{exc}", *data_errors],
                dry_run=dry_run,
            )

        result = build_signal(as_of, evaluations, market_score, self.config, dry_run=dry_run)
        if data_errors:
            result.reasons.extend([f"非关键数据源警告：{item}" for item in data_errors])
        return result


class WarmCacheRunner:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.calendar = AShareCalendar()
        self.akshare = AkShareClient()
        self.haoetf = HaoEtfClient()
        self.history_cache = HistoryCache(resolve_project_path(config["paths"]["database"]))

    def run(self, as_of: datetime, refresh: bool = False) -> dict:
        if not self.calendar.is_trading_day(as_of.date()):
            return {
                "status": "SKIP_CALENDAR",
                "as_of": as_of.isoformat(),
                "updated": [],
                "skipped": [],
                "errors": ["非 A 股交易日"],
            }

        updated: list[dict] = []
        skipped: list[str] = []
        errors: list[str] = []
        for fund in enabled_funds(self.config):
            if not refresh and self.history_cache.get(fund.code, as_of.date()) is not None:
                skipped.append(fund.code)
                continue
            try:
                source, rows, warning = fetch_history_for_fund(fund, self.haoetf, self.akshare)
                self.history_cache.set(fund.code, source, rows, as_of.date())
                updated.append({"code": fund.code, "source": source, "rows": len(rows), "warning": warning})
            except Exception as exc:
                errors.append(str(exc))

        return {
            "status": "OK" if not errors else "PARTIAL",
            "as_of": as_of.isoformat(),
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
        }


def now_in_config_timezone(config: dict) -> datetime:
    return datetime.now(ZoneInfo(config["timezone"]))
