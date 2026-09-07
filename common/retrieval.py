"""조항 단위 검색 (구성 2~4 공용).

구성 1(ch01/rag_basic.py)은 문서를 500자로 잘라 벡터 검색만 했습니다.
여기서는 06-3의 clauses 테이블(조·항 단위 + 문서 제목·유효기간 메타데이터 + 임베딩)을 씁니다.

  - vector_search : 임베딩 유사도
  - keyword_score : 질문과 조항의 공통 어절 수 (한국어 형태소 분석기 없이 쓸 수 있는 가장 단순한 키워드 점수)
  - hybrid_search : 두 점수를 합산(RRF, reciprocal rank fusion)
  - valid_only    : 기준일에 유효한 판만 (메타데이터 필터)

인덱스 준비:  python ch06/load_ontology.py --embed
"""
from __future__ import annotations

import re
from datetime import date

from common import db, llm

_TOKEN = re.compile(r"[가-힣A-Za-z0-9]+")
_STOP = {"이", "가", "은", "는", "을", "를", "의", "에", "로", "와", "과", "도", "하나요", "있나요", "되나요", "인가요", "합니다", "입니다"}


def _tokens(s: str) -> set[str]:
    out = set()
    for t in _TOKEN.findall(s):
        if t in _STOP or len(t) < 2:
            continue
        out.add(t)
        # 조사 붙은 어절을 거칠게 자른다: '환불되나요' → '환불'
        for k in (2, 3):
            if len(t) > k:
                out.add(t[:k])
    return out


def clause_rows(conn, valid_on: date | None):
    if valid_on:
        return conn.execute(
            """SELECT c.clause_id, c.doc_id, p.title, p.valid_from, p.valid_to, c.content
               FROM clauses c JOIN policy_versions p USING (doc_id)
               WHERE p.valid_from <= %s AND (p.valid_to IS NULL OR p.valid_to >= %s)""",
            (valid_on, valid_on)).fetchall()
    return conn.execute(
        """SELECT c.clause_id, c.doc_id, p.title, p.valid_from, p.valid_to, c.content
           FROM clauses c JOIN policy_versions p USING (doc_id)""").fetchall()


def vector_search(question: str, k: int = 8, valid_on: date | None = None) -> list[dict]:
    qv = llm.embed([question])[0]
    filt = "AND p.valid_from <= %(d)s AND (p.valid_to IS NULL OR p.valid_to >= %(d)s)" if valid_on else ""
    with db.connect() as c:
        rows = c.execute(
            f"""SELECT c.clause_id, c.doc_id, p.title, p.valid_from, p.valid_to, c.content,
                       1 - (c.embedding <=> %(v)s::vector) AS score
                FROM clauses c JOIN policy_versions p USING (doc_id)
                WHERE c.embedding IS NOT NULL {filt}
                ORDER BY c.embedding <=> %(v)s::vector LIMIT %(k)s""",
            {"v": qv, "d": valid_on, "k": k}).fetchall()
    return [_row(r) for r in rows]


def keyword_search(question: str, k: int = 8, valid_on: date | None = None) -> list[dict]:
    qt = _tokens(question)
    with db.connect() as c:
        rows = clause_rows(c, valid_on)
    scored = []
    for r in rows:
        ct = _tokens(r[5])
        s = len(qt & ct)
        if s:
            scored.append((s, r))
    scored.sort(key=lambda x: (-x[0], len(x[1][5])))
    return [_row(r, score=s) for s, r in scored[:k]]


def hybrid_search(question: str, k: int = 8, valid_on: date | None = None) -> list[dict]:
    """RRF: 각 순위 목록에서 1/(60+rank) 를 더한다."""
    vec = vector_search(question, k * 2, valid_on)
    kw = keyword_search(question, k * 2, valid_on)
    fused: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for lst in (vec, kw):
        for rank, r in enumerate(lst):
            fused[r["clause_id"]] = fused.get(r["clause_id"], 0) + 1 / (60 + rank)
            rows[r["clause_id"]] = r
    top = sorted(fused.items(), key=lambda x: -x[1])[:k]
    out = []
    for cid, s in top:
        r = dict(rows[cid]); r["score"] = round(s, 4); out.append(r)
    return out


def _row(r, score=None) -> dict:
    d = {"clause_id": r[0], "doc_id": r[1], "title": r[2], "valid_from": r[3], "valid_to": r[4], "content": r[5]}
    d["score"] = round(float(r[6]), 3) if score is None and len(r) > 6 else score
    return d


def format_context(rows: list[dict]) -> str:
    """조항을 LLM 에 넘길 때 문서 제목과 유효기간을 앞에 붙인다 (메타데이터)."""
    parts = []
    for r in rows:
        until = r["valid_to"] or "현재"
        parts.append(f"[{r['clause_id']}] ({r['title']}, 유효 {r['valid_from']} ~ {until})\n{r['content']}")
    return "\n\n".join(parts)
