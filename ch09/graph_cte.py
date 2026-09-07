"""09-2: PostgreSQL 안에서 그래프 다루기 — 재귀 CTE 로 조항 관계를 탐색합니다.

clause_links 테이블(from_clause, rel, to_ref)은 그 자체가 간선 목록(edge list)입니다.
그래프 DB 없이도 WITH RECURSIVE 로 다음을 풀 수 있습니다.
  1. 준용·대체 사슬 따라가기 (학생 6조 1항 → 3조·4조 → 1판 대응 조항)
  2. 어떤 조항이 바뀌면 영향받는 문서 항목 전부 (역방향, 깊이 제한)
  3. 두 조항 사이의 경로

실행:  python ch09/graph_cte.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db  # noqa: E402

# 1. 정방향 탐색: 준용·대체를 따라 실제로 적용할 조항까지
FORWARD = """
WITH RECURSIVE walk AS (
    SELECT from_clause, rel, to_ref, 1 AS depth,
           ARRAY[from_clause, to_ref] AS path
    FROM clause_links
    WHERE from_clause = %(start)s AND rel = ANY(%(rels)s)
  UNION ALL
    SELECT l.from_clause, l.rel, l.to_ref, w.depth + 1,
           w.path || l.to_ref
    FROM clause_links l
    JOIN walk w ON l.from_clause = w.to_ref
    WHERE l.rel = ANY(%(rels)s)
      AND NOT l.to_ref = ANY(w.path)          -- 순환 방지
      AND w.depth < %(max_depth)s
)
SELECT depth, rel, from_clause, to_ref, array_to_string(path, ' -> ') AS path
FROM walk ORDER BY depth, to_ref;
"""

# 2. 역방향 탐색: 이 조항을 가리키는 모든 것 (영향 범위)
IMPACT = """
WITH RECURSIVE impact AS (
    SELECT to_ref AS target, from_clause AS dependent, rel, 1 AS depth,
           ARRAY[to_ref, from_clause] AS path
    FROM clause_links
    WHERE to_ref = %(start)s
  UNION ALL
    SELECT i.dependent, l.from_clause, l.rel, i.depth + 1,
           i.path || l.from_clause
    FROM clause_links l
    JOIN impact i ON l.to_ref = i.dependent
    WHERE NOT l.from_clause = ANY(i.path)
      AND i.depth < %(max_depth)s
)
SELECT DISTINCT depth, rel, dependent, array_to_string(path, ' <- ') AS path
FROM impact ORDER BY depth, dependent;
"""

# 3. 조항이 같은 판 안에서 어느 조(article)에 속하는지: 항 → 조 는 id 문자열로 계산 (part_of)
ARTICLE_OF = """
SELECT clause_id, doc_id, split_part(clause_no, '.', 1) AS article
FROM clauses WHERE clause_id = %(cid)s;
"""


def forward(start: str, rels=("applies_mutatis_mutandis", "supersedes"), max_depth: int = 4) -> list[tuple]:
    with db.connect() as c:
        return c.execute(FORWARD, {"start": start, "rels": list(rels), "max_depth": max_depth}).fetchall()


def impact(start: str, max_depth: int = 3) -> list[tuple]:
    with db.connect() as c:
        return c.execute(IMPACT, {"start": start, "max_depth": max_depth}).fetchall()


def impact_with_article(start: str, max_depth: int = 3) -> list[tuple]:
    """항(4.1) 단위 시작점에 더해, 그 조(4) 전체를 가리키는 링크도 함께 본다 — 준용은 조 단위로 걸려 있기 때문."""
    with db.connect() as c:
        row = c.execute(ARTICLE_OF, {"cid": start}).fetchone()
    results = impact(start, max_depth)
    if row and "." in row[2] is False:
        return results
    article_id = f"{row[1]}:{row[2]}" if row else None
    if article_id and article_id != start:
        results += [(d, r, dep, p + f" (조 단위 {article_id})") for d, r, dep, p in impact(article_id, max_depth)]
    return sorted(set(results))


if __name__ == "__main__":
    print("== 정방향: 학생 요금제 6조 1항이 준용하는 조항과 그 조항이 대체한 1판 조항")
    for d, rel, f, t, p in forward("refund_policy_v2:6.1"):
        print(f"  depth {d} [{rel:24s}] {p}")
    print()
    print("== 정방향: 2판 4조 1항이 대체한 1판 조항")
    for d, rel, f, t, p in forward("refund_policy_v2:4.1"):
        print(f"  depth {d} [{rel:24s}] {p}")
    print()
    print("== 영향 범위: 2판 4조 1항(연간 30일 전액 환불)이 바뀌면")
    for d, rel, dep, p in impact("refund_policy_v2:4.1"):
        print(f"  depth {d} [{rel:8s}] {dep:28s} {p}")
    print()
    print("== 영향 범위(조 단위 포함): 2판 4조")
    for d, rel, dep, p in impact("refund_policy_v2:4"):
        print(f"  depth {d} [{rel:24s}] {dep:28s} {p}")
    print()
    print("== 영향 범위: 약관 9조 3항(유리한 규정 적용)")
    for d, rel, dep, p in impact("terms_of_service:9.3"):
        print(f"  depth {d} [{rel:8s}] {dep:28s} {p}")
