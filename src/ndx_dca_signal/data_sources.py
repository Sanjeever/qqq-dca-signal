from __future__ import annotations

import math
import re
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import httpx
import akshare as ak
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

from ndx_dca_signal.models import FundConfig, FundHistoryRow, FundSnapshot


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


class EtfirstClient:
    BASE_URL = "https://etfapp.euler.southernfund.com:13000"
    LOGIN_PATH = "/etfapp/retail/auth/cli-login"
    REALTIME_PATH = "/etfapp/retail/product/getRealTimeData"
    ETF_AGGREGATE_PATH = "/etfapp/retail/skill/product/aggregate-etf"
    SUCCESS_CODES = {"0", "200", "0000", "00000", "000000"}
    PRICE_SCALE = 10000
    PERCENT_SCALE = 100

    def __init__(
        self,
        api_key: str,
        timeout_seconds: int = 10,
        max_staleness_seconds: int = 60,
    ) -> None:
        if not api_key:
            raise ValueError("ETFirst API Key 未配置")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_staleness_seconds = max_staleness_seconds

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.BASE_URL,
            timeout=self.timeout_seconds,
            headers={
                "Accept": "application/json;charset=UTF-8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                    "Safari/537.36 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"
                ),
                "X-Client-Type": "cli",
                "X-Client-Version": "0.2.3",
            },
        )

    def _parse_response(self, response: httpx.Response, path: str) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"ETFirst {path} 返回格式错误")
        code = str(payload.get("code", ""))
        if code not in self.SUCCESS_CODES:
            message = payload.get("message") or payload.get("msg") or "未知错误"
            raise ValueError(f"ETFirst {path} 失败 [{code}]：{message}")
        return payload

    def _login(self, client: httpx.Client) -> str:
        response = client.post(self.LOGIN_PATH, headers={"X-Api-Key": self.api_key})
        self._parse_response(response, self.LOGIN_PATH)
        session = (
            response.headers.get("session")
            or response.cookies.get("SESSION")
            or client.cookies.get("SESSION")
        )
        if not session:
            raise ValueError("ETFirst 登录成功但未返回 Session")
        return session

    def _post(
        self,
        client: httpx.Client,
        session: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        response = client.post(path, json=body, headers={"session": session})
        payload = self._parse_response(response, path)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"ETFirst {path} 缺少 data")
        return data

    def fetch_fund_snapshots(self, funds: list[FundConfig], as_of: datetime) -> list[FundSnapshot]:
        if as_of.tzinfo is None:
            raise ValueError("ETFirst 实时行情要求带时区的 as_of")
        snapshots: list[FundSnapshot] = []
        with self._client() as client:
            session = self._login(client)
            for fund in funds:
                data = self._post(
                    client,
                    session,
                    self.REALTIME_PATH,
                    {"productCode": fund.code, "type": "2"},
                )
                snapshots.append(self._snapshot_from_realtime(fund, data, as_of))
        return snapshots

    def _snapshot_from_realtime(
        self,
        fund: FundConfig,
        data: dict[str, Any],
        as_of: datetime,
    ) -> FundSnapshot:
        raw_day = str(data.get("tradingDay") or "")
        raw_time = str(data.get("time") or "")
        if len(raw_time) < 6 or not raw_time.isdigit():
            raise ValueError(f"{fund.code} ETFirst 行情时间无效：{raw_time or '空'}")
        try:
            trade_date = datetime.strptime(raw_day, "%Y%m%d").date()
            quote_time = time(int(raw_time[:2]), int(raw_time[2:4]), int(raw_time[4:6]))
        except ValueError as exc:
            raise ValueError(f"{fund.code} ETFirst 行情日期时间无效：{raw_day} {raw_time}") from exc
        timestamp = datetime.combine(trade_date, quote_time, tzinfo=as_of.tzinfo)
        age_seconds = (as_of - timestamp).total_seconds()
        if trade_date != as_of.date():
            raise ValueError(f"{fund.code} ETFirst 行情日期过期：{trade_date} != {as_of.date()}")
        if age_seconds > self.max_staleness_seconds:
            raise ValueError(f"{fund.code} ETFirst 行情已过期：{age_seconds:.0f} 秒")
        if age_seconds < -30:
            raise ValueError(f"{fund.code} ETFirst 行情时间晚于运行时间：{-age_seconds:.0f} 秒")

        raw_price = parse_number(data.get("closePrice"))
        raw_iopv = parse_number(data.get("iopv"))
        raw_premium = parse_number(data.get("premDisRto"))
        turnover = parse_number(data.get("turnover"))
        if raw_price is None or raw_iopv is None or raw_premium is None:
            raise ValueError(f"{fund.code} ETFirst 缺少最新价、IOPV 或溢价率")
        price = raw_price / self.PRICE_SCALE
        estimate = raw_iopv / self.PRICE_SCALE
        premium = raw_premium / self.PERCENT_SCALE
        if price <= 0 or estimate <= 0:
            raise ValueError(f"{fund.code} ETFirst 最新价或 IOPV 无效")
        calculated_premium = (price - estimate) / estimate
        if abs(calculated_premium - premium) > 0.0001:
            raise ValueError(
                f"{fund.code} ETFirst 溢价率不一致：接口={premium:.4%}，复算={calculated_premium:.4%}"
            )

        return FundSnapshot(
            code=fund.code,
            name=fund.name,
            price=price,
            estimate_value=estimate,
            premium=premium,
            turnover_wan=(turnover / 10000) if turnover is not None else None,
            source="etfirst",
            timestamp=timestamp,
        )

    def ndx_history(self, product_code: str, end: date, lookback_days: int = 420) -> pd.DataFrame:
        return self.ndx_history_range(product_code, end - timedelta(days=lookback_days), end)

    def ndx_history_range(
        self,
        product_code: str,
        start: date,
        end: date,
        warmup_days: int = 0,
    ) -> pd.DataFrame:
        query_start = start - timedelta(days=warmup_days)
        with self._client() as client:
            session = self._login(client)
            data = self._post(
                client,
                session,
                self.ETF_AGGREGATE_PATH,
                {
                    "productCode": product_code,
                    "startDate": query_start.strftime("%Y%m%d"),
                    "endDate": end.strftime("%Y%m%d"),
                    "netInflowType": "3",
                },
            )
        errors = data.get("errors") or {}
        if errors.get("getReturnTrendList"):
            raise ValueError(f"ETFirst NDX 日线查询失败：{errors['getReturnTrendList']}")
        results = data.get("results") or {}
        trend = results.get("getReturnTrendList") or {}
        rows = trend.get("NDX") or []
        if not rows:
            raise ValueError("ETFirst 未返回 NDX 日线")

        dates: list[datetime] = []
        closes: list[float] = []
        for row in rows:
            raw_date = str(row.get("tradeDate") or "")
            close = parse_number(row.get("closePrice"))
            if close is None:
                raise ValueError(f"ETFirst NDX 日线缺少收盘价：{raw_date or '未知日期'}")
            try:
                dates.append(datetime.strptime(raw_date, "%Y%m%d"))
            except ValueError as exc:
                raise ValueError(f"ETFirst NDX 日线日期无效：{raw_date}") from exc
            closes.append(close)

        frame = pd.DataFrame({"Close": closes}, index=pd.DatetimeIndex(dates))
        if frame.index.has_duplicates:
            raise ValueError("ETFirst NDX 日线包含重复日期")
        return frame.sort_index()


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

    def fetch_fund_close(self, fund: FundConfig, trade_date: date) -> float:
        symbol = f"{fund.market.lower()}{fund.code}"
        price_frame = quiet_call(ak.fund_etf_hist_sina, symbol=symbol)
        required_columns = {"date", "close"}
        missing_columns = required_columns - set(price_frame.columns)
        if missing_columns:
            raise ValueError(f"AkShare ETF history missing columns: {sorted(missing_columns)}")

        frame = price_frame[["date", "close"]].copy()
        frame["trade_date"] = pd.to_datetime(frame["date"]).dt.date
        matched = frame[frame["trade_date"] == trade_date]
        if matched.empty:
            raise ValueError(f"{fund.code} missing close price for {trade_date.isoformat()}")
        close = parse_number(matched.iloc[-1]["close"])
        if close is None or close <= 0:
            raise ValueError(f"{fund.code} invalid close price for {trade_date.isoformat()}")
        return close


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
