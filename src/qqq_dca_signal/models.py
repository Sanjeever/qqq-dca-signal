from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal


SignalStatus = Literal["BUY", "SKIP_RULE", "SKIP_DATA", "SKIP_CALENDAR", "ERROR"]


@dataclass(frozen=True)
class FundConfig:
    code: str
    name: str
    market: str
    enabled: bool = True


@dataclass(frozen=True)
class FundSnapshot:
    code: str
    name: str
    price: float
    estimate_value: float
    premium: float
    turnover_wan: float | None
    source: str
    timestamp: datetime
    cross_checked: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FundHistoryRow:
    code: str
    trade_date: date
    close: float
    estimate_value: float
    premium: float


@dataclass(frozen=True)
class FundEvaluation:
    snapshot: FundSnapshot
    premium_percentile: float
    premium_threshold: float
    eligible: bool
    reasons: list[str]
    history_days: int


@dataclass(frozen=True)
class MarketScore:
    total: float
    threshold: float
    passed: bool
    components: dict[str, float]
    hard_filters: list[str]
    metrics: dict[str, float]


@dataclass
class SignalResult:
    status: SignalStatus
    as_of: datetime
    selected_fund: FundEvaluation | None
    fund_evaluations: list[FundEvaluation]
    market_score: MarketScore | None
    reasons: list[str]
    llm_analysis: str = ""
    dry_run: bool = False

    @property
    def title(self) -> str:
        if self.status == "BUY":
            if self.selected_fund:
                s = self.selected_fund.snapshot
                return f"QQQ定投信号：今日可买 {s.code} {s.name}"
            return "QQQ定投信号：今日可买"
        if self.status == "SKIP_CALENDAR":
            return "QQQ定投信号：非A股交易日"
        return "QQQ定投信号：今日不买"

    def to_record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of.isoformat(),
            "selected_fund": self.selected_fund.snapshot.code if self.selected_fund else None,
            "market_score": self.market_score.total if self.market_score else None,
            "reasons": self.reasons,
            "dry_run": self.dry_run,
        }
