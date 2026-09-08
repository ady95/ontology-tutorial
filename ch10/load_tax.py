"""10-6 (1/3): 세무 사례 데이터 적재 — 가상 납세자·사업장, 조문 요약 문서(조·항 단위 + 임베딩), 500자 청크.

노트클라우드의 ch06/load_structured.py + ch06/load_ontology.py + ch01/rag_basic.py --index 를
세무 사례용으로 합친 것입니다. 테이블 이름에 tax_ 접두어를 붙여 앞 장의 테이블과 분리합니다.

실행:  python ch10/load_tax.py            # 임베딩 없이 (API 키 불필요)
       python ch10/load_tax.py --embed    # 조항·청크 임베딩까지
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db  # noqa: E402
from ch06.load_ontology import parse_frontmatter, split_clauses  # noqa: E402
from ch01.rag_basic import chunk_text, strip_frontmatter  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TAX = ROOT / "data" / "tax"

SCHEMA = """
DROP TABLE IF EXISTS tax_chunks_basic, tax_clauses, tax_docs, tax_places, tax_taxpayers CASCADE;

CREATE TABLE tax_taxpayers (
    taxpayer_id          text PRIMARY KEY,
    display_name         text NOT NULL,
    business_start_date  date,
    registration_date    date,
    first_supply_date    date,
    bookkeeping_duty     boolean NOT NULL DEFAULT false,
    note                 text
);

CREATE TABLE tax_places (
    place_id                  text PRIMARY KEY,
    taxpayer_id               text NOT NULL REFERENCES tax_taxpayers,
    industry_code             text NOT NULL,
    industry_label            text NOT NULL,
    is_real_estate_rental     boolean NOT NULL,
    is_excluded_industry      boolean NOT NULL,
    supply_consideration_2024 bigint,          -- 공급대가 (부가세 포함). NULL = 미확인
    supply_value_2024         bigint,          -- 공급가액 (부가세 제외)
    vat_included_known        boolean NOT NULL -- 납세자가 말한 금액이 부가세 포함인지 확인됐는가
);

CREATE TABLE tax_docs (
    doc_id     text PRIMARY KEY,
    title      text NOT NULL,
    valid_from date,
    valid_to   date
);

CREATE TABLE tax_clauses (
    clause_id text PRIMARY KEY,
    doc_id    text NOT NULL REFERENCES tax_docs,
    clause_no text NOT NULL,
    heading   text,
    content   text NOT NULL,
    embedding vector(1536)
);

CREATE TABLE tax_chunks_basic (
    id        serial PRIMARY KEY,
    doc_id    text NOT NULL,
    chunk_no  int NOT NULL,
    content   text NOT NULL,
    embedding vector(1536)
);
"""


def load(embed: bool = False) -> None:
    with db.connect(autocommit=False) as conn:
        conn.execute(SCHEMA)
        for fname, table in [("taxpayers.csv", "tax_taxpayers"), ("business_places.csv", "tax_places")]:
            with (TAX / "structured" / fname).open(encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                header = next(reader)
                rows = [[(v if v != "" else None) for v in r] for r in reader]
            with conn.cursor() as cur:
                with cur.copy(f"COPY {table} ({', '.join(header)}) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
            print(f"{table:16s} {len(rows):3d} rows")

        all_clauses, all_chunks = [], []
        for path in sorted((TAX / "docs").glob("*.md")):
            fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO tax_docs VALUES (%s,%s,%s,%s)",
                         (fm["doc_id"], fm["title"], fm.get("valid_from"), fm.get("valid_to")))
            cl = split_clauses(fm["doc_id"], body)
            for c in cl:
                conn.execute("INSERT INTO tax_clauses (clause_id, doc_id, clause_no, heading, content) VALUES (%s,%s,%s,%s,%s)",
                             (c["clause_id"], c["doc_id"], c["clause_no"], c["heading"], c["content"]))
            chunks = chunk_text(strip_frontmatter(path.read_text(encoding="utf-8")))
            for i, ch in enumerate(chunks):
                conn.execute("INSERT INTO tax_chunks_basic (doc_id, chunk_no, content) VALUES (%s,%s,%s)", (fm["doc_id"], i, ch))
            all_clauses += cl
            all_chunks += [(fm["doc_id"], i) for i in range(len(chunks))]
            print(f"{fm['doc_id']:16s} {str(fm.get('valid_from')):10s} ~ {str(fm.get('valid_to') or '현재'):10s} 조항 {len(cl):3d} / 청크 {len(chunks):2d}")
        conn.commit()

        if embed:
            from common import llm
            rows = conn.execute("SELECT clause_id, content FROM tax_clauses ORDER BY clause_id").fetchall()
            for (cid, _), v in zip(rows, llm.embed([r[1] for r in rows])):
                conn.execute("UPDATE tax_clauses SET embedding=%s WHERE clause_id=%s", (v, cid))
            rows = conn.execute("SELECT id, content FROM tax_chunks_basic ORDER BY id").fetchall()
            for (i, _), v in zip(rows, llm.embed([r[1] for r in rows])):
                conn.execute("UPDATE tax_chunks_basic SET embedding=%s WHERE id=%s", (v, i))
            conn.commit()
            print(f"임베딩 저장: 조항 {len(all_clauses)} / 청크 {len(all_chunks)} (토큰 {llm.usage.embed_tokens:,})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed", action="store_true")
    load(embed=ap.parse_args().embed)
