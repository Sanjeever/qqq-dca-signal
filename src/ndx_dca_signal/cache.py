from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from ndx_dca_signal.models import FundHistoryRow


SCHEMA = """
create table if not exists fund_history_cache (
  code text primary key,
  source text not null,
  fetched_on text not null,
  payload_json text not null
);
"""


class HistoryCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, code: str, today: date) -> list[FundHistoryRow] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select payload_json from fund_history_cache where code = ? and fetched_on = ?",
                (code, today.isoformat()),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        return [
            FundHistoryRow(
                code=item["code"],
                trade_date=date.fromisoformat(item["trade_date"]),
                close=float(item["close"]),
                estimate_value=float(item["estimate_value"]),
                premium=float(item["premium"]),
            )
            for item in payload
        ]

    def set(self, code: str, source: str, rows: list[FundHistoryRow], today: date) -> None:
        payload = [
            {
                "code": row.code,
                "trade_date": row.trade_date.isoformat(),
                "close": row.close,
                "estimate_value": row.estimate_value,
                "premium": row.premium,
            }
            for row in rows
        ]
        with self.connect() as conn:
            conn.execute(
                """
                insert into fund_history_cache (code, source, fetched_on, payload_json)
                values (?, ?, ?, ?)
                on conflict(code) do update set
                  source = excluded.source,
                  fetched_on = excluded.fetched_on,
                  payload_json = excluded.payload_json
                """,
                (code, source, today.isoformat(), json.dumps(payload, ensure_ascii=False)),
            )
