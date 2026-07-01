from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from ndx_dca_signal.backtest import enabled_funds
from ndx_dca_signal.data_sources import AkShareClient
from ndx_dca_signal.database import Database
from ndx_dca_signal.models import FundConfig, SignalResult, SimTrade


def is_enabled(config: dict) -> bool:
    return bool(config.get("sim_trading", {}).get("enabled", False))


def order_time_for_signal(signal_time: datetime, config: dict) -> datetime:
    raw = str(config["sim_trading"].get("order_time", "14:57:00"))
    parsed = time.fromisoformat(raw)
    return datetime.combine(signal_time.date(), parsed, tzinfo=signal_time.tzinfo)


def build_sim_trade(result: SignalResult, config: dict) -> SimTrade | None:
    if not is_enabled(config) or result.status != "BUY" or result.selected_fund is None:
        return None

    sim_config = config["sim_trading"]
    snapshot = result.selected_fund.snapshot
    order_amount = float(sim_config["order_amount"])
    lot_size = int(sim_config.get("lot_size", 100))
    quantity = int(order_amount // snapshot.price // lot_size * lot_size)
    if quantity <= 0:
        return SimTrade(
            status="REJECTED",
            trade_date=result.as_of.date(),
            code=snapshot.code,
            name=snapshot.name,
            order_time=order_time_for_signal(result.as_of, config),
            order_amount=order_amount,
            quantity=0,
            order_price_type="UP_LIMIT",
            order_price_reference=snapshot.price,
            signal_as_of=result.as_of,
            message=f"下单金额不足一手：{order_amount:.2f}",
        )

    return SimTrade(
        status="SUBMITTED",
        trade_date=result.as_of.date(),
        code=snapshot.code,
        name=snapshot.name,
        order_time=order_time_for_signal(result.as_of, config),
        order_amount=order_amount,
        quantity=quantity,
        order_price_type="UP_LIMIT",
        order_price_reference=snapshot.price,
        signal_as_of=result.as_of,
        message="模拟 14:57 挂涨停价买入，等待收盘价结算",
    )


def record_signal_trade(result: SignalResult, config: dict, database: Database) -> SimTrade | None:
    trade = build_sim_trade(result, config)
    if trade is None:
        return None
    database.record_sim_trade(trade)
    result.sim_trade = trade
    return trade


def build_portfolio_summary(result: SignalResult, config: dict, database: Database) -> dict | None:
    if not is_enabled(config):
        return None

    mark_prices = {item.snapshot.code: item.snapshot.price for item in result.fund_evaluations}
    rows = database.sim_trades()
    positions_by_code: dict[str, dict] = {}
    for row in rows:
        if row["status"] != "FILLED":
            continue
        code = str(row["code"])
        quantity = int(row["quantity"])
        cost = float(row["fill_amount"] or 0.0)
        position = positions_by_code.setdefault(
            code,
            {
                "code": code,
                "name": str(row["name"]),
                "quantity": 0,
                "cost": 0.0,
            },
        )
        position["quantity"] += quantity
        position["cost"] += cost

    positions: list[dict] = []
    total_cost = 0.0
    total_market_value = 0.0
    all_marked = True
    for position in positions_by_code.values():
        quantity = int(position["quantity"])
        cost = float(position["cost"])
        mark_price = mark_prices.get(str(position["code"]))
        market_value = mark_price * quantity if mark_price is not None else None
        pnl = market_value - cost if market_value is not None else None
        return_rate = pnl / cost if pnl is not None and cost > 0 else None
        total_cost += cost
        if market_value is None:
            all_marked = False
        else:
            total_market_value += market_value
        positions.append(
            {
                **position,
                "avg_cost": cost / quantity if quantity > 0 else 0.0,
                "mark_price": mark_price,
                "market_value": market_value,
                "pnl": pnl,
                "return_rate": return_rate,
            }
        )

    pending = [row for row in rows if row["status"] == "SUBMITTED"]
    today = result.as_of.date().isoformat()
    today_trades = [row for row in rows if row["trade_date"] == today]
    total_pnl = total_market_value - total_cost if all_marked else None
    total_return = total_pnl / total_cost if total_pnl is not None and total_cost > 0 else None
    return {
        "positions": sorted(positions, key=lambda item: item["code"]),
        "total_cost": total_cost,
        "total_market_value": total_market_value if all_marked else None,
        "total_pnl": total_pnl,
        "total_return": total_return,
        "pending_count": len(pending),
        "today_trades": today_trades,
        "recent_trades": database.recent_sim_trades(5),
    }


def fund_by_code(config: dict, code: str) -> FundConfig:
    for fund in enabled_funds(config):
        if fund.code == code:
            return fund
    raise ValueError(f"{code} is not in enabled fund universe")


def settle_pending_trades(config: dict, database: Database, as_of: datetime | None = None) -> list[dict]:
    if not is_enabled(config):
        return []

    tz = ZoneInfo(config["timezone"])
    run_at = as_of or datetime.now(tz)
    trade_date = run_at.date().isoformat()
    akshare = AkShareClient()
    settled: list[dict] = []
    for row in database.pending_sim_trades(trade_date):
        fund = fund_by_code(config, str(row["code"]))
        close_price = akshare.fetch_fund_close(fund, run_at.date())
        message = "按当日收盘价模拟成交"
        settled.append(database.fill_sim_trade(int(row["id"]), close_price, message))
    return settled
