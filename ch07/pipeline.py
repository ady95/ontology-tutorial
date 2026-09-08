"""구성 4 (07장): 검색 + SQL + 규칙 + 명시적 의미 모델.

구성 3과의 차이는 "의미 모델"이 파이프라인의 각 단계에 들어간다는 점입니다.
  1. 질문 분석 (07-1): 용어 사전(glossary)을 LLM 에 주고 요청 종류·개체·시점을 뽑는다.
     '취소' 처럼 여러 개념에 걸치는 표현은 확인 요청(hold)으로 처리한다.
  2. 조회·규칙 (07-2): 결제일로 규정 판을 고르고(governed_by), 약관 9조 3항(유리한 규정)을 적용하고,
     requires 가 비면 보류한다 — rules.py policy_mode="versioned"
  3. 근거 확장 (07-2): 규칙이 낸 근거 조항을 id 로 가져오고, 준용·대체 링크를 따라 실제 적용 조항까지 붙인다.
     정형 데이터가 필요 없는 질문은 하이브리드 검색 + 링크 확장으로 근거를 모은다.
  4. 컨텍스트 설계 (07-3): 관련 개념의 정의(concepts)와 근거 조항, 규칙 판단 결과를 구조화해 LLM 에 넘긴다.
  5. 답변 형식 (07-4): 결론 / 적용 조건 / 근거 조항 / 미확인 사항

실행:
  python ch07/pipeline.py --ask "고객 C004가 오늘 환불을 요청하면 어떤 규정을 적용하고 얼마를 환불하나요?"
  python ch07/pipeline.py --eval        # → results/config4_semantic.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import date

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db, llm, evaluate, retrieval  # noqa: E402
from ch06 import api  # noqa: E402
from ch07 import rules  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
GLOSSARY = yaml.safe_load((ROOT / "data" / "ontology" / "glossary.yaml").read_text(encoding="utf-8"))["terms"]

# ---------- 1. 질문 분석 ----------

ANALYZE_SYSTEM = """당신은 고객지원 질문을 구조화하는 분석기입니다. 반드시 JSON 만 출력합니다.
아래 용어 사전을 기준으로 질문의 표현을 표준 개념으로 바꾸세요. 표현이 두 개 이상의 개념에 해당하고
질문만으로 구분할 수 없으면 request_type 을 "ambiguous" 로 두고 candidates 에 후보를 적으세요.

출력 형식:
{
  "request_type": "Refund | Cancellation | PaymentReversal | SeatChange | IncidentCredit | PolicyQuestion | ImpactAnalysis | ambiguous",
  "candidates": ["..."],                 // ambiguous 일 때
  "customer_id": "C001" | null,
  "incident_id": "INC-01" | null,
  "incident_date": "YYYY-MM-DD" | null,
  "requested_at": "YYYY-MM-DD" | null,   // 질문이 특정 과거 시점의 요청을 가정하면
  "seats_new": 3 | null,
  "requester_role": "admin | member | null",
  "hours_since_payment": 12 | null,            // 결제 후 경과 시간(시간 단위). 질문에 없으면 null. "어제"처럼 모호하면 null
  "concepts": ["Unused", "CancellationRequest", ...],   // 질문에 관련된 개념 id
  "clause_hint": "refund_policy_v2:4" | null           // 질문이 특정 조항을 지목하면
}"""


def glossary_text() -> str:
    lines = []
    for t in GLOSSARY:
        forms = ", ".join(f"'{f['form']}'({f['source']})" for f in t.get("surface_forms", []))
        lines.append(f"- {t['canonical']} ({t['label']}): {t['definition']} | 표현: {forms}"
                     + (f" | 혼동 주의: {', '.join(t['confusable_with'])}" if t.get("confusable_with") else ""))
    return "\n".join(lines)


def analyze(question: str, as_of: date) -> dict:
    user = f"오늘: {as_of}\n\n용어 사전:\n{glossary_text()}\n\n질문: {question}"
    out = llm.chat_json(ANALYZE_SYSTEM, user)
    # 정규식으로 보정 (LLM 이 놓친 ID)
    m = re.search(r"(?<![A-Za-z0-9])C0\d{2}(?!\d)", question)
    if m and not out.get("customer_id"):
        out["customer_id"] = m.group()
    m = re.search(r"(?<![A-Za-z0-9])INC-\d{2}(?!\d)", question)
    if m and not out.get("incident_id"):
        out["incident_id"] = m.group()
    if not out.get("clause_hint"):  # "환불 규정 4조", "약관 9조 3항" 같은 지목을 조항 id 로
        m = re.search(r"(환불 규정|약관|이용약관)\s*(?:제)?(\d+)조(?:\s*(?:제)?(\d+)항)?", question)
        if m:
            doc = "refund_policy_v2" if m.group(1) == "환불 규정" else "terms_of_service"
            out["clause_hint"] = f"{doc}:{m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")
            if out.get("request_type") in (None, "PolicyQuestion") and re.search(r"개정|바뀌|변경", question):
                out["request_type"] = "ImpactAnalysis"
    return out


# ---------- 2·3. 조회·규칙·근거 확장 ----------

def expand_evidence(clause_ids: list[str]) -> list[dict]:
    """근거 조항 id → 본문. 준용(applies_mutatis_mutandis)·대체(supersedes) 링크를 한 단계 따라간다."""
    seen: list[str] = []
    for cid in clause_ids:
        if cid not in seen:
            seen.append(cid)
        for nxt in api.linked(cid, "applies_mutatis_mutandis"):
            if nxt not in seen:
                seen.append(nxt)
    out = []
    with db.connect() as c:
        for cid in seen:
            r = c.execute(
                """SELECT c.clause_id, c.doc_id, p.title, p.valid_from, p.valid_to, c.content
                   FROM clauses c JOIN policy_versions p USING (doc_id) WHERE clause_id=%s""", (cid,)).fetchone()
            if r:
                out.append({"clause_id": r[0], "doc_id": r[1], "title": r[2], "valid_from": r[3], "valid_to": r[4], "content": r[5]})
    return out


_KNOWN_CLAUSE_IDS: list[str] | None = None


def known_clause_ids() -> list[str]:
    """DB 의 조항 ID 전체를 긴 것부터 정렬해 둔다 (부분 문자열 오매칭 방지)."""
    global _KNOWN_CLAUSE_IDS
    if _KNOWN_CLAUSE_IDS is None:
        with db.connect() as c:
            rows = c.execute("SELECT clause_id FROM clauses").fetchall()
        _KNOWN_CLAUSE_IDS = sorted((r[0] for r in rows), key=len, reverse=True)
    return _KNOWN_CLAUSE_IDS


def cited_clauses(answer: str) -> list[str]:
    """답변 문장에서 실제로 인용된 조항 ID 를 뽑는다 (07-4 형식 검사·11-3 진단용).

    정규식으로 자르면 'faq:결제와 구독' 처럼 공백이 든 ID 가 'faq:결제와' 에서 끊긴다.
    그래서 DB 의 실제 ID 목록을 긴 것부터 대조한다. 목록에 없는 형태는 잡지 못하므로,
    이 함수가 빈 목록을 돌려준다는 것은 "조항 ID 문자열을 찾지 못했다"는 뜻이지
    "근거를 언급하지 않았다"는 뜻이 아니다 (07-4).
    """
    text = answer or ""
    out: list[str] = []
    for cid in known_clause_ids():
        if cid in text and cid not in out:
            out.append(cid)
    # 짧은 ID 가 긴 ID 의 일부인 경우 제거 (예: refund_policy_v2:4 vs refund_policy_v2:4.1)
    return [c for c in out if not any(c != o and c in o for o in out)]


def concept_text(concept_ids: list[str]) -> str:
    lines = []
    for cid in concept_ids:
        c = api.concept(cid)
        if c:
            lines.append(f"- {c['label']}({cid}): {c['definition']}" + (f" [정의 조항: {', '.join(c['defined_in'])}]" if c["defined_in"] else ""))
    return "\n".join(lines)


def valid_hours(value) -> float | None:
    """분석기가 낸 경과 시간을 검증한다. 0 이상의 유한한 수가 아니면 None(미확인)으로 본다 (07-1).

    LLM 출력이라 문자열이나 음수가 올 수 있고, 그대로 비교하면 예외가 나거나
    음수가 24 이하로 통과해 결제 취소로 처리된다.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        h = float(value)
    except (TypeError, ValueError):
        return None
    if h != h or h in (float("inf"), float("-inf")) or h < 0:
        return None
    return h


def decide(question: str, a: dict, as_of: date) -> tuple[rules.Verdict | None, list[dict], str]:
    """규칙 판단 + 근거 조항. 반환: (verdict, 근거 조항 목록, 설명)

    조회한 사실과 거쳐 온 판단 경로는 verdict.facts / verdict.trace 에 담아
    결과 파일까지 전달한다 (11-3 진단용). 조회를 하지 않은 경우와 조회했으나
    결과가 없는 경우를 구분하기 위해, 조회를 시도했으면 dict 를, 하지 않았으면 None 을 둔다.
    """
    rtype = a.get("request_type")
    ctx = api.customer_context(a["customer_id"]) if a.get("customer_id") else None
    when = date.fromisoformat(a["requested_at"]) if a.get("requested_at") else as_of

    if rtype == "ambiguous":
        v = rules.Verdict(status="hold", result="요청 종류 확인 필요: " + ", ".join(a.get("candidates", [])),
                          missing=["해지·환불·결제 취소 중 무엇을 원하는지"])
        v.fire("classify_request")
        return v, expand_evidence(v.evidence), "용어 확인"
    if rtype == "PaymentReversal":
        v = rules.Verdict(facts=ctx)
        v.fire("payment_reversal")
        v.trace.append("classify:PaymentReversal")
        hours = valid_hours(a.get("hours_since_payment"))
        if hours is None:
            v.status, v.missing = "hold", ["정확한 결제 시각(24시간 이내 여부)"]
            v.result = "결제 완료 후 24시간 이내면 결제 취소(승인 취소, 환불 규정 미적용), 지났으면 환불 규정 적용"
        elif hours <= 24:
            v.result = f"승인 취소 처리 (결제 후 {hours}시간 경과, 24시간 이내). 환불 규정 미적용"
        else:
            # 24시간을 넘겼으면 결제 취소가 아니라 환불 요청으로 넘긴다
            v.result = f"결제 후 {hours}시간이 지나 결제 취소 대상이 아닙니다. 환불 규정을 적용합니다"
            if ctx:
                rv = rules.evaluate_refund(ctx, when, policy_mode="versioned", requester_role=a.get("requester_role") or None)
                rv.notes.insert(0, v.result)
                for e in v.evidence:
                    if e not in rv.evidence:
                        rv.evidence.append(e)
                # 전환 전 판정도 추적에 남긴다 (S017)
                rv.rules_fired = v.rules_fired + rv.rules_fired
                rv.trace = v.trace + [f"payment_reversal:over_24h({hours}h)", "→refund"] + rv.trace
                rv.facts = ctx
                return rv, expand_evidence(rv.evidence), "결제 취소 → 환불"
            v.status, v.missing = "hold", ["환불 규정 적용에 필요한 고객·결제 정보"]
        v.evidence += ["ops_manual:4"]
        return v, expand_evidence(v.evidence), "결제 취소"
    if rtype == "Cancellation":
        v = rules.Verdict(); v.fire("cancellation_no_refund")
        v.result = "해지: 다음 결제 주기부터 갱신 중단, 금액 반환 없음, 현재 주기 종료까지 이용 가능"
        return v, expand_evidence(v.evidence), "해지"
    if rtype == "SeatChange" and ctx:
        v = rules.evaluate_seat_change(ctx, int(a.get("seats_new") or ctx["seats"]))
        v.facts = ctx; v.trace = ["classify:SeatChange", "lookup:customer_context"] + v.rules_fired
        return v, expand_evidence(v.evidence), "좌석 변경"
    if rtype == "IncidentCredit":
        inc = api.incident(a.get("incident_id")) if a.get("incident_id") else (
            api.incident(on=date.fromisoformat(a["incident_date"])) if a.get("incident_date") else None)
        if inc:
            v = rules.evaluate_incident_credit(inc, ctx, policy_mode="versioned")
            v.facts = {"incident": inc, "customer": ctx}
            v.trace = ["classify:IncidentCredit", "lookup:incident"] + v.rules_fired
            v.notes.insert(0, f"장애 {inc['incident_id']}: {inc['started_at']} ~ {inc['ended_at']} ({inc['duration_hours']}시간, 정기 점검 {inc['is_planned_maintenance']})")
            return v, expand_evidence(v.evidence), "장애 크레딧"
    if rtype == "ImpactAnalysis":
        if a.get("clause_hint"):
            imp = api.impact_of(a["clause_hint"])
            v = rules.Verdict(result="함께 점검할 항목: " + json.dumps(imp, ensure_ascii=False), evidence=["ops_manual:6"] + imp["cited_by"])
            return v, expand_evidence(v.evidence), "영향 범위(조항)"
        v = rules.recheck_targets(date(2025, 6, 30), as_of)
        return v, expand_evidence(v.evidence), "영향 범위(구독)"
    if rtype == "Refund" and ctx:
        v = rules.evaluate_refund(ctx, when, policy_mode="versioned", requester_role=a.get("requester_role") or None)
        v.facts = ctx
        v.trace = ["classify:Refund", "lookup:customer_context"] + v.rules_fired
        return v, expand_evidence(v.evidence), "환불"
    if rtype == "Refund" and a.get("customer_id"):
        # 고객 ID 는 잡았는데 조회 결과가 없다 — 조회 실패와 미조회를 구분해 보류한다
        v = rules.Verdict(status="hold", result=f"고객 {a['customer_id']} 의 구독 정보를 찾을 수 없어 결론을 보류합니다",
                          missing=["고객·구독 정보"], facts={}, trace=["classify:Refund", "lookup:customer_context:empty"])
        return v, [], "조회 실패"
    return None, [], "규정 질문"


# ---------- 4·5. 컨텍스트와 답변 ----------

ANSWER_SYSTEM = """당신은 노트클라우드 고객지원 담당자를 돕는 도우미입니다.
아래 자료(개념 정의, 근거 조항, 규칙 판단)만 근거로 한국어로 답하세요. 형식:

결론: (한두 문장. 금액·날짜는 숫자로)
적용 조건: (어떤 조건·시점·규정 판이 적용되었는지)
근거: (조항 id 를 그대로 나열)
미확인 사항: (있으면 무엇을 확인해야 하는지, 없으면 '없음')

규칙 판단이 '보류'이면 결론을 단정하지 말고 확인이 필요하다고 답하세요.
규칙 판단이 있으면 그 결과와 금액을 바꾸지 말고 문장으로 옮기세요."""


def answer(question: str, as_of: date) -> dict:
    a = analyze(question, as_of)
    verdict, evidence_rows, kind = decide(question, a, as_of)
    concept_ids = list(dict.fromkeys(a.get("concepts", []) or []))

    if verdict is None:  # 규정 질문: 검색 + 링크 확장
        rows = retrieval.hybrid_search(question, 6, valid_on=as_of)
        evidence_rows = expand_evidence([r["clause_id"] for r in rows])
        # 기준일 현재 판이 대체한 1판 조항이 검색됐다면 대체 관계를 알려 준다
        judgement = "규칙 판단: 없음 (규정 설명 질문). 기준일에 유효한 판의 조항이 우선합니다."
        status, missing, evid = "answer", [], [r["clause_id"] for r in evidence_rows]
    else:
        judgement = f"규칙 판단({kind}): [{verdict.status}] {verdict.result}"
        if verdict.amount is not None:
            judgement += f"\n금액: {verdict.amount:,}원"
        if verdict.approver:
            judgement += f"\n승인자: {verdict.approver}"
        if verdict.policy_version:
            judgement += f"\n적용 규정 판: {verdict.policy_version}"
        if verdict.notes:
            judgement += "\n계산·적용 과정:\n" + "\n".join("  - " + n for n in verdict.notes)
        if verdict.missing:
            judgement += "\n미확인: " + ", ".join(verdict.missing)
        status, missing, evid = verdict.status, verdict.missing, verdict.evidence

    user = (f"오늘 날짜: {as_of}\n\n"
            + (f"관련 개념 정의:\n{concept_text(concept_ids)}\n\n" if concept_ids else "")
            + f"근거 조항:\n{retrieval.format_context(evidence_rows)}\n\n"
            + f"{judgement}\n\n질문: {question}")
    text = llm.chat(ANSWER_SYSTEM, user)
    return {"answer": text, "status": status, "evidence": evid, "missing": missing,
            "cited": cited_clauses(text), "rules_fired": (verdict.rules_fired if verdict else []),
            "facts": (verdict.facts if verdict else None),
            "decision_trace": (verdict.trace if verdict else ["search_only"]),
            "analysis": a, "contexts": [r["clause_id"] for r in evidence_rows]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    as_of, _ = evaluate.load_questions()
    if args.ask:
        res = answer(args.ask, as_of)
        print("분석:", json.dumps(res["analysis"], ensure_ascii=False))
        print("근거:", res["evidence"])
        print(res["answer"])
    if args.eval:
        evaluate.run("config4_semantic", answer, only=args.only)
