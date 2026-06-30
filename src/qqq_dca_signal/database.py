from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from qqq_dca_signal.models import SignalResult


SCHEMA = """
create table if not exists signals (
  id integer primary key autoincrement,
  as_of text not null,
  status text not null,
  selected_fund text,
  market_score real,
  reasons_json text not null,
  payload_json text not null,
  dry_run integer not null default 0,
  created_at text not null default current_timestamp
);

create table if not exists fund_snapshots (
  id integer primary key autoincrement,
  as_of text not null,
  code text not null,
  name text not null,
  price real not null,
  estimate_value real not null,
  premium real not null,
  turnover_wan real,
  source text not null,
  cross_checked integer not null default 0
);

create table if not exists backtest_runs (
  id integer primary key autoincrement,
  started_at text not null default current_timestamp,
  start_date text not null,
  end_date text not null,
  params_json text not null,
  summary_json text not null,
  markdown_path text,
  html_path text
);

create table if not exists backtest_daily_results (
  id integer primary key autoincrement,
  run_id integer not null,
  trade_date text not null,
  strategy text not null,
  status text not null,
  selected_fund text,
  buy_price real,
  portfolio_value real,
  payload_json text not null
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma foreign_keys=on")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def record_signal(self, result: SignalResult) -> None:
        payload = result.to_record()
        with self.connect() as conn:
            conn.execute(
                """
                insert into signals (
                  as_of, status, selected_fund, market_score, reasons_json, payload_json, dry_run
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.as_of.isoformat(),
                    result.status,
                    payload["selected_fund"],
                    payload["market_score"],
                    json.dumps(result.reasons, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    1 if result.dry_run else 0,
                ),
            )
            for item in result.fund_evaluations:
                s = item.snapshot
                conn.execute(
                    """
                    insert into fund_snapshots (
                      as_of, code, name, price, estimate_value, premium, turnover_wan, source, cross_checked
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.as_of.isoformat(),
                        s.code,
                        s.name,
                        s.price,
                        s.estimate_value,
                        s.premium,
                        s.turnover_wan,
                        s.source,
                        1 if s.cross_checked else 0,
                    ),
                )

    def record_backtest(
        self,
        start_date: str,
        end_date: str,
        params: dict,
        summary: dict,
        rows: list[dict],
        markdown_path: Path,
        html_path: Path,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into backtest_runs (
                  start_date, end_date, params_json, summary_json, markdown_path, html_path
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    start_date,
                    end_date,
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(summary, ensure_ascii=False),
                    str(markdown_path),
                    str(html_path),
                ),
            )
            run_id = int(cursor.lastrowid)
            for row in rows:
                conn.execute(
                    """
                    insert into backtest_daily_results (
                      run_id, trade_date, strategy, status, selected_fund, buy_price,
                      portfolio_value, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        row["date"],
                        row["strategy"],
                        row["status"],
                        row.get("selected_fund"),
                        row.get("buy_price"),
                        row.get("portfolio_value"),
                        json.dumps(row, ensure_ascii=False),
                    ),
                )
            return run_id
