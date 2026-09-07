"""06-4: 의미 모델을 조회 함수(API)로 노출합니다.

7장의 파이프라인과 LLM 도구 호출이 쓰는 함수들입니다. 모두 SQL 한두 개로 이루어지며,
"어떤 규정 판이 적용되는가", "이 조항은 무엇을 대체·준용·인용하는가", "이 개념은 어떤 조항이
정의하는가" 같은 질문에 답합니다. 정형 데이터 조회(고객·구독·결제·사용량)도 여기 둡니다.

실행 예:  python ch06/api.py   (몇 가지 조회를 시연)
"""
from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db  # noqa: E402


# ---------- 규정 판 · 조항 ----------

def policy_version_for(paid_at: date, doc_prefix: str = "refund_policy") -> str | None:
    """결제 완료일 기준으로 적용되는 규정 판 (약관 2조 2항, 운영 매뉴얼 2절 1항)."""
    with db.connect() as c:
        r = c.execute(
            """SELECT doc_id FROM policy_versions
               WHERE doc_id LIKE %s AND valid_from <= %s AND (valid_to IS NULL OR valid_to >= %s)
               ORDER BY valid_from DESC LIMIT 1""",
            (doc_prefix + "%", paid_at, paid_at),
        ).fetchone()
    return r[0] if r else None


def current_policy_version(as_of: date, doc_prefix: str = "refund_policy") -> str | None:
    return policy_version_for(as_of, doc_prefix)


def clause(clause_id: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT clause_id, doc_id, clause_no, heading, content FROM clauses WHERE clause_id=%s",
                      (clause_id,)).fetchone()
    return dict(zip(["clause_id", "doc_id", "clause_no", "heading", "content"], r)) if r else None


def clauses_of(doc_id: str, article: str | None = None) -> list[dict]:
    """문서의 조항 목록. article 을 주면 그 조의 항들만."""
    with db.connect() as c:
        if article:
            rows = c.execute(
                "SELECT clause_id, clause_no, heading, content FROM clauses WHERE doc_id=%s AND (clause_no=%s OR clause_no LIKE %s) ORDER BY clause_no",
                (doc_id, article, article + ".%")).fetchall()
        else:
            rows = c.execute("SELECT clause_id, clause_no, heading, content FROM clauses WHERE doc_id=%s ORDER BY clause_no",
                             (doc_id,)).fetchall()
    return [dict(zip(["clause_id", "clause_no", "heading", "content"], r)) for r in rows]


def sub_clauses(clause_id: str) -> list[str]:
    """조(article) id 를 주면 그 아래 항 id 목록. 항이면 빈 목록. (조↔항 소속 관계, 09-2 참조)"""
    doc, no = clause_id.split(":", 1)
    if "." in no:
        return []
    with db.connect() as c:
        rows = c.execute("SELECT clause_id FROM clauses WHERE doc_id=%s AND clause_no LIKE %s ORDER BY clause_no",
                         (doc, no + ".%")).fetchall()
    return [r[0] for r in rows]


def linked(clause_id: str, rel: str, reverse: bool = False, include_sub: bool = True) -> list[str]:
    """조항 관계 탐색. reverse=True 면 '이 조항을 가리키는 쪽'을 찾는다 (영향 범위).
    include_sub=True 면 조 단위 id 에 대해 그 항들의 링크도 함께 본다."""
    ids = [clause_id] + (sub_clauses(clause_id) if include_sub else [])
    out: list[str] = []
    with db.connect() as c:
        for cid in ids:
            if reverse:
                rows = c.execute("SELECT from_clause FROM clause_links WHERE rel=%s AND to_ref=%s ORDER BY 1",
                                 (rel, cid)).fetchall()
            else:
                rows = c.execute("SELECT to_ref FROM clause_links WHERE rel=%s AND from_clause=%s ORDER BY 1",
                                 (rel, cid)).fetchall()
            for r in rows:
                if r[0] not in out:
                    out.append(r[0])
    return out


def resolve_mutatis_mutandis(clause_id: str) -> list[str]:
    """준용 관계를 따라가 실제로 적용할 조항들을 돌려준다 (학생 6조 1항 → 3조·4조)."""
    return linked(clause_id, "applies_mutatis_mutandis")


def superseded_by(old_clause_id: str) -> str | None:
    """제1판 조항을 대체한 제2판 조항."""
    rows = linked(old_clause_id, "supersedes", reverse=True)
    return rows[0] if rows else None


def impact_of(clause_id: str) -> dict:
    """조항이 바뀌었을 때 함께 점검할 것: 이 조항을 인용하는 문서 항목, 이 조항이 정의하는 개념, 준용하는 조항."""
    return {
        "cited_by": linked(clause_id, "cites", reverse=True),
        "defines": linked(clause_id, "defines"),
        "applied_via_mutatis_mutandis_by": linked(clause_id, "applies_mutatis_mutandis", reverse=True),
    }


# ---------- 개념 · 용어 ----------

def concept(concept_id: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT concept_id, label, definition, parent_id, attributes FROM concepts WHERE concept_id=%s",
                      (concept_id,)).fetchone()
        if not r:
            return None
        defs = c.execute("SELECT from_clause FROM clause_links WHERE rel='defines' AND to_ref=%s ORDER BY 1",
                         ("concept:" + concept_id,)).fetchall()
    d = dict(zip(["concept_id", "label", "definition", "parent_id", "attributes"], r))
    d["defined_in"] = [x[0] for x in defs]
    return d


def normalize_term(surface: str) -> list[dict]:
    """고객·문서의 표현을 표준 용어로. '취소' 처럼 여러 후보가 있으면 모두 돌려준다 (확인 필요)."""
    with db.connect() as c:
        rows = c.execute(
            """SELECT t.canonical, t.label, t.definition, t.confusable_with, f.form, f.source
               FROM glossary_surface_forms f JOIN glossary_terms t USING (canonical)
               WHERE %s LIKE '%%' || f.form || '%%' OR f.form LIKE '%%' || %s || '%%'
               ORDER BY length(f.form) DESC""",
            (surface, surface)).fetchall()
    return [dict(zip(["canonical", "label", "definition", "confusable_with", "form", "source"], r)) for r in rows]


# ---------- 정형 데이터 ----------

def customer_context(customer_id: str) -> dict | None:
    """고객 1명의 판단에 필요한 사실을 한 번에: 고객·구독·현재 주기 결제·사용량·팀 관리자."""
    with db.connect() as c:
        r = c.execute(
            """SELECT c.customer_id, c.customer_type, c.is_student, c.account_status, c.suspended_at,
                      s.subscription_id, s.plan, s.billing_cycle, s.seats, s.current_period_start, s.current_period_end,
                      s.promo_code, s.status, s.student_verified_until,
                      p.paid_at, p.amount, p.list_price, p.is_renewal,
                      u.docs_created, u.logins
               FROM customers c
               JOIN subscriptions s ON s.customer_id = c.customer_id
               LEFT JOIN payments p ON p.subscription_id = s.subscription_id AND p.paid_at = s.current_period_start
               LEFT JOIN usage_stats u ON u.subscription_id = s.subscription_id AND u.period_start = s.current_period_start
               WHERE c.customer_id = %s""",
            (customer_id,)).fetchone()
        if not r:
            return None
        cols = ["customer_id", "customer_type", "is_student", "account_status", "suspended_at",
                "subscription_id", "plan", "billing_cycle", "seats", "current_period_start", "current_period_end",
                "promo_code", "status", "student_verified_until",
                "paid_at", "amount", "list_price", "is_renewal", "docs_created", "logins"]
        d = dict(zip(cols, r))
        d["team_admins"] = [x[0] for x in c.execute(
            "SELECT member_name FROM team_members WHERE customer_id=%s AND role='admin'", (customer_id,)).fetchall()]
    d["usage_known"] = d["docs_created"] is not None
    d["unused"] = (d["docs_created"] == 0 and d["logins"] <= 3) if d["usage_known"] else None
    return d


def plan_prices(plan: str, at: date) -> dict | None:
    """요금 안내의 가격표 (2025-07-01 개정). 코드에 두는 대신 테이블로 옮겨도 된다."""
    new = {"basic": (9900, 99000), "pro": (19900, 199000), "team": (14900, 149000), "student": (4900, 49000)}
    old = {"basic": (8900, 89000), "pro": (17900, 179000), "team": (12900, 129000)}
    table = new if at >= date(2025, 7, 1) else old
    if plan not in table:
        return None
    m, y = table[plan]
    return {"monthly": m, "yearly": y}


def incidents_overlapping(period_start: date, period_end: date) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            """SELECT incident_id, started_at, ended_at, duration_hours, responsibility, is_planned_maintenance
               FROM incidents WHERE started_at::date <= %s AND ended_at::date >= %s ORDER BY started_at""",
            (period_end, period_start)).fetchall()
    return [dict(zip(["incident_id", "started_at", "ended_at", "duration_hours", "responsibility", "is_planned_maintenance"], r)) for r in rows]


def incident(incident_id: str | None = None, on: date | None = None) -> dict | None:
    with db.connect() as c:
        if incident_id:
            r = c.execute("SELECT incident_id, started_at, ended_at, duration_hours, responsibility, is_planned_maintenance, announced_at FROM incidents WHERE incident_id=%s", (incident_id,)).fetchone()
        else:
            r = c.execute("SELECT incident_id, started_at, ended_at, duration_hours, responsibility, is_planned_maintenance, announced_at FROM incidents WHERE started_at::date <= %s AND ended_at::date >= %s", (on, on)).fetchone()
    return dict(zip(["incident_id", "started_at", "ended_at", "duration_hours", "responsibility", "is_planned_maintenance", "announced_at"], r)) if r else None


def subscriptions_paid_before(cutoff: date, active_only: bool = True) -> list[dict]:
    """규정 개정 재점검 대상 후보: 개정 전에 결제되어 아직 유효한 구독 (운영 매뉴얼 6절 2항)."""
    with db.connect() as c:
        rows = c.execute(
            """SELECT s.subscription_id, s.customer_id, s.plan, s.billing_cycle, s.current_period_start, s.current_period_end,
                      s.promo_code, s.status, c.account_status
               FROM subscriptions s JOIN customers c USING (customer_id)
               WHERE s.current_period_start <= %s AND s.current_period_end >= %s
               ORDER BY s.subscription_id""",
            (cutoff, cutoff)).fetchall()
    out = [dict(zip(["subscription_id", "customer_id", "plan", "billing_cycle", "current_period_start", "current_period_end", "promo_code", "status", "account_status"], r)) for r in rows]
    return [o for o in out if not active_only or o["status"] != "ended"]


if __name__ == "__main__":
    print("적용 판(2025-06-20 결제):", policy_version_for(date(2025, 6, 20)))
    print("적용 판(2025-08-20 결제):", policy_version_for(date(2025, 8, 20)))
    print("준용(refund_policy_v2:6.1):", resolve_mutatis_mutandis("refund_policy_v2:6.1"))
    print("대체(refund_policy_v1:3.1 →):", superseded_by("refund_policy_v1:3.1"))
    print("영향(refund_policy_v2:4.1):", impact_of("refund_policy_v2:4.1"))
    print("개념(Unused):", concept("Unused"))
    print("용어('취소'):", [(t["canonical"], t["form"]) for t in normalize_term("취소해 주세요")])
    print("C007:", customer_context("C007"))
    print("개정 전 결제 유효 구독:", [(s["customer_id"], s["billing_cycle"], s["promo_code"], s["account_status"]) for s in subscriptions_paid_before(date(2025, 6, 30))])
