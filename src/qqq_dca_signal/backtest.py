from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from qqq_dca_signal.data_sources import AShareCalendar, AkShareClient, HaoEtfClient, YahooClient
from qqq_dca_signal.models import FundConfig, FundHistoryRow
from qqq_dca_signal.reports import write_html_report, write_markdown_report
from qqq_dca_signal.rules import calculate_market_score, calculate_market_score_daily_proxy, quantile


STRATEGIES = ("daily_buy", "premium_only", "premium_plus_market")


def enabled_funds(config: dict) -> list[FundConfig]:
    return [
        FundConfig(
            code=str(item["code"]),
            name=str(item["name"]),
            market=str(item["market"]),
            enabled=bool(item.get("enabled", True)),
        )
        for item in config["fund_universe"]["include"]
        if item.get("enabled", True)
    ]


class Backtester:
    def __init__(self, config: dict, reports_dir: Path) -> None:
        self.config = config
        self.reports_dir = reports_dir
        self.akshare = AkShareClient()
        self.haoetf = HaoEtfClient()
        self.yahoo = YahooClient()
        self.calendar = AShareCalendar()

    def run(self, start: date, end: date, market_mode: str = "daily-proxy") -> tuple[dict, list[dict], Path, Path]:
        if market_mode not in {"daily-proxy", "intraday-strict"}:
            raise ValueError("market_mode must be daily-proxy or intraday-strict")
        funds = enabled_funds(self.config)
        histories = {fund.code: self._load_fund_history(fund) for fund in funds}
        histories_by_date = {
            code: {row.trade_date: row for row in rows}
            for code, rows in histories.items()
        }
        qqq = self.yahoo.qqq_history_range(self.config["data_sources"]["qqq_symbol"], start, end)
        qqq.index = pd.to_datetime(qqq.index).date

        rows: list[dict] = []
        states = {
            strategy: {"holdings": {}, "cash_invested": 0.0, "last_prices": {}}
            for strategy in STRATEGIES
        }
        for current in pd.date_range(start, end, freq="D").date:
            if not self.calendar.is_trading_day(current):
                continue
            daily_candidates = self._candidate_rows(current, histories, histories_by_date)
            today_prices = {row.code: row.close for row in daily_candidates}
            selected_for_daily = self._lowest_premium(daily_candidates)
            selected_for_premium = self._premium_eligible(current, daily_candidates, histories)
            market_score = self._market_score_for_date(current, qqq, market_mode)

            decisions = {
                "daily_buy": selected_for_daily,
                "premium_only": selected_for_premium,
                "premium_plus_market": selected_for_premium if market_score and market_score.passed else None,
            }
            for strategy, selected in decisions.items():
                state = states[strategy]
                state["last_prices"].update(today_prices)
                status = "BUY" if selected else "SKIP_RULE"
                if strategy == "premium_plus_market" and selected_for_premium and market_score is None:
                    status = "SKIP_DATA"
                if selected:
                    holdings = state["holdings"]
                    holdings[selected.code] = holdings.get(selected.code, 0.0) + 1.0 / selected.close
                    state["cash_invested"] += 1.0

                portfolio_value = sum(
                    shares * state["last_prices"].get(code, 0.0)
                    for code, shares in state["holdings"].items()
                )
                rows.append(
                    {
                        "date": current.isoformat(),
                        "strategy": strategy,
                        "status": status,
                        "selected_fund": selected.code if selected else None,
                        "buy_price": selected.close if selected else None,
                        "premium": selected.premium if selected else (selected_for_daily.premium if selected_for_daily else None),
                        "market_score": market_score.total if market_score else None,
                        "market_mode": market_mode,
                        "portfolio_value": portfolio_value,
                        "cash_invested": state["cash_invested"],
                    }
                )

        summary = self._summarize(rows)
        suffix = f"{start.isoformat()}_{end.isoformat()}_{market_mode}"
        markdown_path = self.reports_dir / f"backtest_{suffix}.md"
        html_path = self.reports_dir / f"backtest_{suffix}.html"
        report_params = {**self.config, "backtest": {"market_mode": market_mode}}
        write_markdown_report(markdown_path, summary, rows, report_params)
        write_html_report(html_path, rows)
        return summary, rows, markdown_path, html_path

    def _load_fund_history(self, fund: FundConfig) -> list[FundHistoryRow]:
        try:
            return self.haoetf.fetch_history(fund.code)
        except Exception:
            return self.akshare.fetch_fund_history_approx(fund)

    def _candidate_rows(
        self,
        current: date,
        histories: dict[str, list[FundHistoryRow]],
        histories_by_date: dict[str, dict[date, FundHistoryRow]],
    ) -> list[FundHistoryRow]:
        rows = []
        for code in histories:
            row = histories_by_date[code].get(current)
            if row:
                rows.append(row)
        return rows

    def _lowest_premium(self, rows: list[FundHistoryRow]) -> FundHistoryRow | None:
        if not rows:
            return None
        return sorted(rows, key=lambda item: item.premium)[0]

    def _premium_eligible(
        self,
        current: date,
        rows: list[FundHistoryRow],
        histories: dict[str, list[FundHistoryRow]],
    ) -> FundHistoryRow | None:
        premium_config = self.config["premium_filter"]
        lookback_days = int(premium_config["lookback_days"])
        min_history_days = int(premium_config["min_history_days"])
        max_percentile = float(premium_config["max_percentile"])
        hard_cap = float(premium_config["hard_cap"])
        eligible = []
        by_code = {row.code: row for row in rows}
        for code, today in by_code.items():
            past = [row for row in histories[code] if row.trade_date < current][-lookback_days:]
            premiums = [row.premium for row in past]
            if len(premiums) < min_history_days:
                continue
            if today.premium > hard_cap:
                continue
            if today.premium > quantile(premiums, max_percentile):
                continue
            eligible.append(today)
        return self._lowest_premium(eligible)

    def _market_score_for_date(self, current: date, qqq: pd.DataFrame, market_mode: str):
        qqq_until_previous = qqq[qqq.index < current]
        if qqq_until_previous.empty:
            return None
        if market_mode == "daily-proxy":
            try:
                return calculate_market_score_daily_proxy(qqq_until_previous, self.config)
            except Exception:
                return None
        try:
            nq_change = self.yahoo.nq_intraday_at(
                self.config["data_sources"]["nq_symbol"],
                current,
                ZoneInfo(self.config["timezone"]),
            )
            if nq_change is None:
                return None
            return calculate_market_score(qqq_until_previous, nq_change, self.config)
        except Exception:
            return None

    def _summarize(self, rows: list[dict]) -> dict:
        frame = pd.DataFrame(rows)
        summary = {}
        if frame.empty:
            return summary
        for strategy, group in frame.groupby("strategy"):
            buys = group[group["status"] == "BUY"]
            invested = float(group["cash_invested"].iloc[-1]) if not group.empty else 0.0
            final_value = float(group["portfolio_value"].iloc[-1]) if not group.empty else 0.0
            avg_premium = float(buys["premium"].mean()) if not buys.empty else 0.0
            summary[strategy] = {
                "buy_days": int(len(buys)),
                "invested": invested,
                "final_value": final_value,
                "return": (final_value / invested - 1) if invested else 0.0,
                "avg_premium": avg_premium,
            }
        return summary
