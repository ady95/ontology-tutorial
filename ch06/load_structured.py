"""06-5 (1/2): 정형 데이터 적재 — data/structured/*.csv 를 PostgreSQL 에 COPY 합니다.

실행:  python ch06/load_structured.py
"""
from __future__ import annotations

import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE.parent / "data" / "structured"

# (csv 파일, 테이블, 컬럼 순서)  — usage.csv 만 테이블명이 다릅니다 (usage 는 SQL 예약어 성격)
FILES = [
    ("customers.csv", "customers", None),
    ("subscriptions.csv", "subscriptions", None),
    ("payments.csv", "payments", None),
    ("usage.csv", "usage_stats", None),
    ("incidents.csv", "incidents", None),
    ("team_members.csv", "team_members", None),
]


def load() -> None:
    with db.connect() as conn:
        db.run_sql_file(conn, HERE / "schema_structured.sql")
        for fname, table, _ in FILES:
            path = DATA / fname
            with path.open(encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                header = next(reader)
                rows = [[(v if v != "" else None) for v in r] for r in reader]
            cols = ", ".join(header)
            with conn.cursor() as cur:
                with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
            n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"{table:14s} {n:3d} rows")


if __name__ == "__main__":
    load()
