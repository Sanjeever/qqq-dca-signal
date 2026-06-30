from __future__ import annotations

import math
import re
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import httpx
import akshare as ak
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

from qqq_dca_signal.models import FundConfig, FundHistoryRow, FundSnapshot


PERCENT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)%")


def parse_float(text: str) -> float | None:
    text = text.strip().replace(",", "")
    if text in {"", "-", "--", "nan", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_percent(text: str) -> float | None:
    match = PERCENT_RE.search(text.strip())
    if not match:
        return None
    return float(match.group(1)) / 100


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if pd.isna(value):
        return None
    if isinstance(value, str):
        return parse_float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quiet_call(func, *args, **kwargs):
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return func(*args, **kwargs)


class AShareCalendar:
    def __init__(self) -> None:
        self.calendar = xcals.get_calendar("XSHG")

    def is_trading_day(self, value: date) -> bool:
        return self.calendar.is_session(pd.Timestamp(value))


class HaoEtfClient:
    def __init__(self, timeout_seconds: int = 15) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_html(self, code: str) -> str:
        url = f"https://www.haoetf.com/qdii/{code}"
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text

    def fetch_snapshot(self, fund: FundConfig, as_of: datetime) -> FundSnapshot:
        html = self.fetch_html(fund.code)
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            raise ValueError(f"{fund.code} missing realtime table")

        row = table.find("tbody").find("tr") if table.find("tbody") else None
        if row is None:
            raise ValueError(f"{fund.code} missing realtime row")
        cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
        if len(cells) < 10:
            raise ValueError(f"{fund.code} realtime row has unexpected shape")

        estimate = parse_float(cells[2])
        premium = parse_percent(cells[3])
        price = parse_float(cells[7])
        turnover_wan = parse_float(cells[9])

        if estimate is None or premium is None or price is None:
            raise ValueError(f"{fund.code} missing realtime estimate, premium, or price")

        return FundSnapshot(
            code=fund.code,
            name=fund.name,
            price=price,
            estimate_value=estimate,
            premium=premium,
            turnover_wan=turnover_wan,
            source="haoetf",
            timestamp=as_of,
        )

    def fetch_history(self, code: str) -> list[FundHistoryRow]:
        html = self.fetch_html(code)
        soup = BeautifulSoup(html, "html.parser")
        heading = None
        for h5 in soup.find_all("h5"):
            if "历史数据" in h5.get_text(strip=True):
                heading = h5
                break
        if heading is None:
            raise ValueError(f"{code} missing history heading")
        table = heading.find_next("table")
        if table is None:
            raise ValueError(f"{code} missing history table")

        rows: list[FundHistoryRow] = []
        tbody = table.find("tbody")
        if tbody is None:
            return rows

        for tr in tbody.find_all("tr"):
            cells = [cell.get_text(strip=True) for cell in tr.find_all("td")]
            if len(cells) < 6:
                continue
            close = parse_float(cells[1])
            estimate = parse_float(cells[3])
            premium = parse_percent(cells[5])
            if close is None or estimate is None or premium is None:
                continue
            rows.append(
                FundHistoryRow(
                    code=code,
                    trade_date=date.fromisoformat(cells[0]),
                    close=close,
                    estimate_value=estimate,
                    premium=premium,
                )
            )
        return sorted(rows, key=lambda item: item.trade_date)


class AkShareClient:
    def fetch_fund_snapshots(self, funds: list[FundConfig], as_of: datetime) -> tuple[list[FundSnapshot], list[str]]:
        frame = self.fetch_fund_snapshots_frame(funds)
        by_code = {str(row["代码"]): row for _, row in frame.iterrows()}
        snapshots: list[FundSnapshot] = []
        errors: list[str] = []
        for fund in funds:
            row = by_code.get(fund.code)
            if row is None:
                errors.append(f"{fund.code} AkShare 未返回该基金")
                continue

            price = parse_number(row["最新价"])
            estimate = parse_number(row["IOPV实时估值"])
            turnover = parse_number(row["成交额"])
            if price is None or estimate is None or estimate <= 0:
                errors.append(f"{fund.code} AkShare 缺少最新价或 IOPV实时估值")
                continue

            timestamp = as_of
            raw_timestamp = row.get("更新时间")
            if raw_timestamp is not None and not pd.isna(raw_timestamp):
                timestamp = datetime.fromtimestamp(float(raw_timestamp), tz=as_of.tzinfo)

            snapshots.append(
                FundSnapshot(
                    code=fund.code,
                    name=fund.name,
                    price=price,
                    estimate_value=estimate,
                    premium=(price - estimate) / estimate,
                    turnover_wan=(turnover / 10000) if turnover is not None else None,
                    source="eastmoney.ulist",
                    timestamp=timestamp,
                    cross_checked=True,
                )
            )
        return snapshots, errors

    def fetch_fund_snapshots_frame(self, funds: list[FundConfig]) -> pd.DataFrame:
        secids = ",".join(f"{'1' if fund.market.lower() == 'sh' else '0'}.{fund.code}" for fund in funds)
        url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
        params = {
            "secids": secids,
            "fields": "f2,f6,f12,f14,f124,f402,f441,f297",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
        }
        with httpx.Client(timeout=15) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        rows = (payload.get("data") or {}).get("diff") or []
        if not rows:
            raise ValueError("EastMoney ETF realtime returned empty data")
        frame = pd.DataFrame(rows)
        frame.rename(
            columns={
                "f12": "代码",
                "f14": "名称",
                "f2": "最新价",
                "f6": "成交额",
                "f124": "更新时间",
                "f402": "基金折价率",
                "f441": "IOPV实时估值",
                "f297": "数据日期",
            },
            inplace=True,
        )
        return frame

    def fetch_fund_history_approx(self, fund: FundConfig) -> list[FundHistoryRow]:
        symbol = f"{fund.market.lower()}{fund.code}"
        price_frame = quiet_call(ak.fund_etf_hist_sina, symbol=symbol)
        nav_frame = quiet_call(ak.fund_etf_fund_info_em, fund=fund.code)
        required_price_columns = {"date", "close"}
        required_nav_columns = {"净值日期", "单位净值"}
        missing_price_columns = required_price_columns - set(price_frame.columns)
        missing_nav_columns = required_nav_columns - set(nav_frame.columns)
        if missing_price_columns:
            raise ValueError(f"AkShare ETF history missing columns: {sorted(missing_price_columns)}")
        if missing_nav_columns:
            raise ValueError(f"AkShare fund nav missing columns: {sorted(missing_nav_columns)}")

        prices = price_frame[["date", "close"]].copy()
        navs = nav_frame[["净值日期", "单位净值"]].copy()
        prices["trade_date"] = pd.to_datetime(prices["date"]).dt.strftime("%Y-%m-%d")
        navs["trade_date"] = pd.to_datetime(navs["净值日期"]).dt.strftime("%Y-%m-%d")
        merged = prices.merge(navs, on="trade_date", how="inner")

        rows: list[FundHistoryRow] = []
        for _, row in merged.iterrows():
            close = parse_number(row["close"])
            nav = parse_number(row["单位净值"])
            if close is None or nav is None or nav <= 0:
                continue
            rows.append(
                FundHistoryRow(
                    code=fund.code,
                    trade_date=date.fromisoformat(row["trade_date"]),
                    close=close,
                    estimate_value=nav,
                    premium=(close - nav) / nav,
                )
            )
        return sorted(rows, key=lambda item: item.trade_date)


class EastMoneyClient:
    def __init__(self, timeout_seconds: int = 10) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_price(self, fund: FundConfig) -> float:
        market_id = "1" if fund.market.lower() == "sh" else "0"
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": f"{market_id}.{fund.code}",
            "fields": "f43,f57,f58,f48",
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data") or {}
        raw_price = data.get("f43")
        if raw_price in (None, "-"):
            raise ValueError(f"{fund.code} missing EastMoney price")
        return float(raw_price) / 1000


class YahooClient:
    def qqq_history(self, symbol: str, lookback_days: int = 420) -> pd.DataFrame:
        frame = yf.download(symbol, period=f"{lookback_days}d", interval="1d", progress=False, auto_adjust=False)
        if frame.empty:
            raise ValueError(f"{symbol} daily history is empty")
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        return frame.dropna(subset=["Close"])

    def qqq_history_range(self, symbol: str, start: date, end: date, warmup_days: int = 420) -> pd.DataFrame:
        start_ts = pd.Timestamp(start) - pd.Timedelta(days=warmup_days)
        end_ts = pd.Timestamp(end) + pd.Timedelta(days=1)
        frame = yf.download(
            symbol,
            start=start_ts.date().isoformat(),
            end=end_ts.date().isoformat(),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        if frame.empty:
            raise ValueError(f"{symbol} daily history is empty")
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        return frame.dropna(subset=["Close"])

    def nq_realtime_change(self, symbol: str) -> float:
        ticker = yf.Ticker(symbol)
        info: dict[str, Any] = ticker.fast_info
        last_price = info.get("last_price")
        previous_close = info.get("previous_close")
        if not last_price or not previous_close or math.isnan(last_price) or math.isnan(previous_close):
            history = ticker.history(period="5d", interval="1d")
            if len(history) < 2:
                raise ValueError(f"{symbol} missing realtime change")
            previous_close = float(history["Close"].iloc[-2])
            last_price = float(history["Close"].iloc[-1])
        return (float(last_price) - float(previous_close)) / float(previous_close)

    def nq_intraday_at(self, symbol: str, target_date: date, tz: ZoneInfo) -> float | None:
        start = pd.Timestamp(target_date).tz_localize(tz) - pd.Timedelta(days=5)
        end = pd.Timestamp(target_date).tz_localize(tz) + pd.Timedelta(days=1)
        frame = yf.download(
            symbol,
            start=start.tz_convert("UTC").date().isoformat(),
            end=end.tz_convert("UTC").date().isoformat(),
            interval="5m",
            progress=False,
            auto_adjust=False,
        )
        if frame.empty:
            return None
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        local = frame.tz_convert(tz)
        day_frame = local[local.index.date == target_date]
        if day_frame.empty:
            return None
        target = datetime.combine(target_date, time(14, 55), tzinfo=tz)
        upto = day_frame[day_frame.index <= pd.Timestamp(target)]
        if upto.empty:
            return None
        price = float(upto["Close"].iloc[-1])
        previous = local[local.index < day_frame.index[0]]
        if previous.empty:
            return None
        previous_close = float(previous["Close"].iloc[-1])
        return (price - previous_close) / previous_close


def cross_check_snapshot(snapshot: FundSnapshot, eastmoney_price: float, max_diff: float) -> FundSnapshot:
    relative_diff = abs(snapshot.price - eastmoney_price) / eastmoney_price
    if relative_diff > max_diff:
        raise ValueError(
            f"{snapshot.code} price cross-check failed: "
            f"haoetf={snapshot.price:.4f}, eastmoney={eastmoney_price:.4f}, diff={relative_diff:.4%}"
        )
    return FundSnapshot(
        code=snapshot.code,
        name=snapshot.name,
        price=snapshot.price,
        estimate_value=snapshot.estimate_value,
        premium=snapshot.premium,
        turnover_wan=snapshot.turnover_wan,
        source=snapshot.source,
        timestamp=snapshot.timestamp,
        cross_checked=True,
        warnings=snapshot.warnings,
    )
