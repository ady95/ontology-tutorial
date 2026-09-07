"""PostgreSQL 연결 공통 모듈.

DATABASE_URL 환경변수가 있으면 그 서버(예: docker compose)에 접속하고,
없으면 pgserver 패키지로 내장 PostgreSQL을 저장소 안 pgdata/ 폴더에 띄웁니다.
두 경우 모두 vector 확장을 활성화합니다.
"""
from __future__ import annotations

import os
import pathlib

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(os.environ.get("ENV_FILE", ROOT / ".env"))

_embedded = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    global _embedded
    if _embedded is None:
        import pgserver  # 내장 PostgreSQL (Docker 없는 환경용)

        _embedded = pgserver.get_server(str(ROOT / "pgdata"))
    return _embedded.get_uri()


def connect(autocommit: bool = True) -> psycopg.Connection:
    conn = psycopg.connect(database_url(), autocommit=autocommit)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)
    return conn


def run_sql_file(conn: psycopg.Connection, path: str | os.PathLike) -> None:
    sql = pathlib.Path(path).read_text(encoding="utf-8")
    conn.execute(sql)


if __name__ == "__main__":
    with connect() as c:
        print(c.execute("select version()").fetchone()[0])
        print("vector:", c.execute("select extversion from pg_extension where extname='vector'").fetchone()[0])
