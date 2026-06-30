from __future__ import annotations

from datetime import datetime

import pandas as pd

from qqq_dca_signal.models import FundEvaluation, FundHistoryRow, FundSnapshot, MarketScore, SignalResult


def percentile_rank(values: list[float], value: float) -> float:
    if not values:
        raise ValueError("cannot calculate percentile with empty values")
    below_or_equal = sum(1 for item in values if item <= value)
    return below_or_equal / len(values)


def quantile(values: list[float], q: float) -> float:
    return float(pd.Series(values).quantile(q))


def evaluate_funds(
    snapshots: list[FundSnapshot],
    histories: dict[str, list[FundHistoryRow]],
    config: dict,
) -> list[FundEvaluation]:
    premium_config = config["premium_filter"]
    lookback_days = int(premium_config["lookback_days"])
    min_history_days = int(premium_config["min_history_days"])
    max_percentile = float(premium_config["max_percentile"])
    hard_cap = float(premium_config["hard_cap"])

    evaluations: list[FundEvaluation] = []
    for snapshot in snapshots:
        history = histories.get(snapshot.code, [])[-lookback_days:]
        premiums = [row.premium for row in history]
        reasons: list[str] = []

        if len(premiums) < min_history_days:
            evaluations.append(
                FundEvaluation(
                    snapshot=snapshot,
                    premium_percentile=1.0,
                    premium_threshold=hard_cap,
                    eligible=False,
                    reasons=[f"溢价历史不足：{len(premiums)} < {min_history_days}"],
                    history_days=len(premiums),
                )
            )
            continue

        threshold = quantile(premiums, max_percentile)
        rank = percentile_rank(premiums, snapshot.premium)
        if snapshot.premium > hard_cap:
            reasons.append(f"溢价 {snapshot.premium:.2%} 超过硬上限 {hard_cap:.2%}")
        if snapshot.premium > threshold:
            reasons.append(f"溢价 {snapshot.premium:.2%} 高于近{lookback_days}日 {max_percentile:.0%} 分位阈值 {threshold:.2%}")

        evaluations.append(
            FundEvaluation(
                snapshot=snapshot,
                premium_percentile=rank,
                premium_threshold=threshold,
                eligible=not reasons,
                reasons=reasons,
                history_days=len(premiums),
            )
        )
    return evaluations


def select_best_fund(evaluations: list[FundEvaluation], config: dict) -> FundEvaluation | None:
    eligible = [item for item in evaluations if item.eligible]
    if not eligible:
        return None

    near_tie = float(config["fund_selection"]["near_tie_threshold"])
    min_premium = min(item.snapshot.premium for item in eligible)

    def key(item: FundEvaluation) -> tuple[float, float, int, float]:
        premium_bucket = 0.0 if item.snapshot.premium - min_premium <= near_tie else item.snapshot.premium
        data_quality = 1 if item.snapshot.cross_checked else 0
        turnover = item.snapshot.turnover_wan or 0.0
        return (premium_bucket, item.premium_percentile, -data_quality, -turnover)

    return sorted(eligible, key=key)[0]


def rsi(series: pd.Series, period: int) -> float:
    delta = series.diff().dropna()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(period).mean().iloc[-1]
    avg_loss = losses.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def score_half_or_full(condition_full: bool, condition_partial: bool, weight: float) -> float:
    if condition_full:
        return weight
    if condition_partial:
        return weight * 0.5
    return 0.0


def calculate_market_score(qqq_history: pd.DataFrame, nq_change: float, config: dict) -> MarketScore:
    score_config = config["market_score"]
    hard_config = config["hard_filters"]
    close = qqq_history["Close"].astype(float)

    ma_days = int(score_config["medium_trend"]["ma_days"])
    rsi_days = int(score_config["overbought_control"]["rsi_days"])
    pullback_days = int(score_config["short_pullback"]["window_days"])
    vol_days = int(score_config["volatility_risk"]["volatility_days"])
    vol_lookback = int(score_config["volatility_risk"]["percentile_lookback_days"])

    if len(close) < max(ma_days, rsi_days + 1, pullback_days + 1, vol_lookback + vol_days):
        raise ValueError("QQQ history is not long enough for market score")

    last_close = float(close.iloc[-1])
    ma = float(close.rolling(ma_days).mean().iloc[-1])
    pullback = float(close.iloc[-1] / close.iloc[-1 - pullback_days] - 1)
    rsi_value = rsi(close, rsi_days)
    returns = close.pct_change().dropna()
    vol = returns.rolling(vol_days).std().dropna()
    current_vol = float(vol.iloc[-1])
    recent_vol = vol.iloc[-vol_lookback:]
    vol_percentile = percentile_rank([float(item) for item in recent_vol], current_vol)

    components: dict[str, float] = {}
    components["medium_trend"] = score_half_or_full(
        last_close > ma * 1.02,
        ma * 0.98 <= last_close <= ma * 1.02,
        float(score_config["medium_trend"]["weight"]),
    )


def calculate_market_score_daily_proxy(qqq_history: pd.DataFrame, config: dict) -> MarketScore:
    score_config = config["market_score"]
    hard_config = config["hard_filters"]
    close = qqq_history["Close"].astype(float)

    ma_days = int(score_config["medium_trend"]["ma_days"])
    rsi_days = int(score_config["overbought_control"]["rsi_days"])
    pullback_days = int(score_config["short_pullback"]["window_days"])
    vol_days = int(score_config["volatility_risk"]["volatility_days"])
    vol_lookback = int(score_config["volatility_risk"]["percentile_lookback_days"])

    if len(close) < max(ma_days, rsi_days + 1, pullback_days + 1, vol_lookback + vol_days):
        raise ValueError("QQQ history is not long enough for market score")

    last_close = float(close.iloc[-1])
    ma = float(close.rolling(ma_days).mean().iloc[-1])
    pullback = float(close.iloc[-1] / close.iloc[-1 - pullback_days] - 1)
    rsi_value = rsi(close, rsi_days)
    returns = close.pct_change().dropna()
    vol = returns.rolling(vol_days).std().dropna()
    current_vol = float(vol.iloc[-1])
    recent_vol = vol.iloc[-vol_lookback:]
    vol_percentile = percentile_rank([float(item) for item in recent_vol], current_vol)

    components: dict[str, float] = {}
    components["medium_trend"] = score_half_or_full(
        last_close > ma * 1.02,
        ma * 0.98 <= last_close <= ma * 1.02,
        float(score_config["medium_trend"]["weight"]),
    )
    components["short_pullback"] = score_half_or_full(
        -0.05 <= pullback <= -0.005,
        (-0.08 <= pullback < -0.05) or (-0.005 < pullback <= 0.01),
        float(score_config["short_pullback"]["weight"]),
    )
    components["overbought_control"] = score_half_or_full(
        rsi_value <= 65,
        65 < rsi_value < 75,
        float(score_config["overbought_control"]["weight"]),
    )
    components["nq_intraday_position"] = float(score_config["nq_intraday_position"]["weight"]) * 0.5
    components["volatility_risk"] = score_half_or_full(
        vol_percentile <= 0.70,
        0.70 < vol_percentile <= 0.85,
        float(score_config["volatility_risk"]["weight"]),
    )

    hard_filters: list[str] = []
    if last_close < ma * float(hard_config["qqq_trend_break_ratio"]):
        hard_filters.append("QQQ 跌破 MA120 * 0.95 趋势过滤")

    total = sum(components.values())
    threshold = float(score_config["threshold"])
    return MarketScore(
        total=total,
        threshold=threshold,
        passed=total >= threshold and not hard_filters,
        components=components,
        hard_filters=hard_filters,
        metrics={
            "qqq_close": last_close,
            "qqq_ma120": ma,
            "qqq_3d_return": pullback,
            "qqq_rsi14": rsi_value,
            "nq_proxy_neutral_score": components["nq_intraday_position"],
            "qqq_volatility_percentile": vol_percentile,
        },
    )
    components["short_pullback"] = score_half_or_full(
        -0.05 <= pullback <= -0.005,
        (-0.08 <= pullback < -0.05) or (-0.005 < pullback <= 0.01),
        float(score_config["short_pullback"]["weight"]),
    )
    components["overbought_control"] = score_half_or_full(
        rsi_value <= 65,
        65 < rsi_value < 75,
        float(score_config["overbought_control"]["weight"]),
    )
    components["nq_intraday_position"] = score_half_or_full(
        -0.015 <= nq_change <= 0.005,
        (-0.025 <= nq_change < -0.015) or (0.005 < nq_change <= 0.01),
        float(score_config["nq_intraday_position"]["weight"]),
    )
    components["volatility_risk"] = score_half_or_full(
        vol_percentile <= 0.70,
        0.70 < vol_percentile <= 0.85,
        float(score_config["volatility_risk"]["weight"]),
    )

    hard_filters: list[str] = []
    if nq_change <= float(hard_config["nq_panic_change"]):
        hard_filters.append(f"NQ 跌幅 {nq_change:.2%} 触发恐慌过滤")
    if last_close < ma * float(hard_config["qqq_trend_break_ratio"]):
        hard_filters.append("QQQ 跌破 MA120 * 0.95 趋势过滤")

    total = sum(components.values())
    threshold = float(score_config["threshold"])
    return MarketScore(
        total=total,
        threshold=threshold,
        passed=total >= threshold and not hard_filters,
        components=components,
        hard_filters=hard_filters,
        metrics={
            "qqq_close": last_close,
            "qqq_ma120": ma,
            "qqq_3d_return": pullback,
            "qqq_rsi14": rsi_value,
            "nq_change": nq_change,
            "qqq_volatility_percentile": vol_percentile,
        },
    )


def build_signal(
    as_of: datetime,
    evaluations: list[FundEvaluation],
    market_score: MarketScore | None,
    config: dict,
    dry_run: bool = False,
) -> SignalResult:
    selected = select_best_fund(evaluations, config)
    if selected is None:
        return SignalResult(
            status="SKIP_RULE",
            as_of=as_of,
            selected_fund=None,
            fund_evaluations=evaluations,
            market_score=market_score,
            reasons=["全部候选基金均未通过溢价过滤"],
            dry_run=dry_run,
        )
    if market_score is None:
        return SignalResult(
            status="SKIP_DATA",
            as_of=as_of,
            selected_fund=selected,
            fund_evaluations=evaluations,
            market_score=None,
            reasons=["缺少市场评分所需数据"],
            dry_run=dry_run,
        )
    if market_score.hard_filters:
        return SignalResult(
            status="SKIP_RULE",
            as_of=as_of,
            selected_fund=selected,
            fund_evaluations=evaluations,
            market_score=market_score,
            reasons=market_score.hard_filters,
            dry_run=dry_run,
        )
    if not market_score.passed:
        return SignalResult(
            status="SKIP_RULE",
            as_of=as_of,
            selected_fund=selected,
            fund_evaluations=evaluations,
            market_score=market_score,
            reasons=[f"市场评分 {market_score.total:.1f} < 阈值 {market_score.threshold:.1f}"],
            dry_run=dry_run,
        )
    return SignalResult(
        status="BUY",
        as_of=as_of,
        selected_fund=selected,
        fund_evaluations=evaluations,
        market_score=market_score,
        reasons=["溢价过滤通过，市场评分通过，未触发极端行情过滤"],
        dry_run=dry_run,
    )


def render_signal_markdown(result: SignalResult) -> str:
    lines = [
        f"# {result.title}",
        "",
        "## 结论",
        f"- 信号：{result.status}",
        f"- 时间：{result.as_of.isoformat()}",
    ]
    if result.selected_fund:
        s = result.selected_fund.snapshot
        lines.extend(
            [
                f"- 推荐/观察基金：{s.code} {s.name}",
                f"- 当前价格：{s.price:.4f}",
                f"- 当前溢价：{s.premium:.2%}",
                f"- 近60日溢价分位：{result.selected_fund.premium_percentile:.0%}",
            ]
        )
    lines.append(f"- 主因：{'；'.join(result.reasons)}")

    if result.llm_analysis:
        lines.extend(["", "## LLM 分析", result.llm_analysis])

    if result.market_score:
        lines.extend(
            [
                "",
                "## 市场评分",
                f"- 总分：{result.market_score.total:.1f} / 100",
                f"- 阈值：{result.market_score.threshold:.1f}",
            ]
        )
        for name, value in result.market_score.components.items():
            lines.append(f"- {name}: {value:.1f}")
        for name, value in result.market_score.metrics.items():
            if "return" in name or "change" in name or "percentile" in name:
                lines.append(f"- {name}: {value:.2%}")
            else:
                lines.append(f"- {name}: {value:.4f}")

    lines.extend(["", "## 候选基金"])
    lines.append("| 代码 | 名称 | 状态 | 溢价 | 分位 | 阈值 | 原因 |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | --- |")
    for item in sorted(result.fund_evaluations, key=lambda x: x.snapshot.premium):
        status = "可选" if item.eligible else "剔除"
        reason = "；".join(item.reasons) if item.reasons else "通过"
        lines.append(
            f"| {item.snapshot.code} | {item.snapshot.name} | {status} | "
            f"{item.snapshot.premium:.2%} | {item.premium_percentile:.0%} | "
            f"{item.premium_threshold:.2%} | {reason} |"
        )

    if result.dry_run:
        lines.extend(["", "> dry-run：未发送 PushPlus。"])
    return "\n".join(lines)
