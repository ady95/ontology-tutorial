"""구성 2·3 (08-2, 08-3).

구성 2 (config2_search): 검색만 개선한 RAG
  - 조항 단위 청크 + 문서 제목·유효기간 메타데이터 + 하이브리드 검색(벡터+키워드, RRF) + 기준일 유효 판 필터
  - 정형 데이터 없음, 규칙 없음, 의미 모델 없음

구성 3 (config3_sql): 구성 2 + SQL 조회 + 규칙 처리(현재 규정 기준)
  - 질문에서 고객 ID·장애 ID·날짜·좌석 수를 정규식으로 뽑아 정형 데이터를 조회
  - ch07/rules.py 를 policy_mode="current" 로 호출: 날짜 계산·금액 계산·제외 조건은 코드로 처리하지만
    규정 판 선택(시점), 유리한 규정 적용, 준용, 용어 정규화, 정보 부족 시 보류는 하지 않는다
  - 즉 "규칙은 있지만 의미 모델은 없는" 구성

실행:  python ch08/configs.py --eval config2_search
       python ch08/configs.py --eval config3_sql
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import llm, evaluate, retrieval  # noqa: E402
from ch06 import api  # noqa: E402
from ch07 import rules  # noqa: E402

SYSTEM = (
    "당신은 노트클라우드 고객지원 담당자를 돕는 도우미입니다. "
    "아래 참고 자료만 근거로 한국어로 간결하게 답하세요. 금액은 숫자로 명시하세요. "
    "참고 자료에 없는 내용은 지어내지 마세요."
)

TOP_K = 8


# ---------- 구성 2 ----------

def answer_config2(question: str, as_of: date) -> dict:
    rows = retrieval.hybrid_search(question, TOP_K, valid_on=as_of)
    user = f"오늘 날짜: {as_of}\n\n참고 조항:\n{retrieval.format_context(rows)}\n\n질문: {question}"
    text = llm.chat(SYSTEM, user)
    return {"answer": text, "contexts": [f"{r['clause_id']}({r['score']})" for r in rows]}


# ---------- 구성 3 ----------

_CUST = re.compile(r"(?<![A-Za-z0-9])C0\d{2}(?!\d)")
_INC = re.compile(r"(?<![A-Za-z0-9])INC-\d{2}(?!\d)")
_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_SEATS = re.compile(r"(\d+)\s*(?:개|좌석)\s*(?:에서|→|->)\s*(\d+)\s*(?:개|좌석)")


def structured_facts(question: str, as_of: date) -> tuple[str, dict]:
    """정규식으로 개체를 찾아 정형 데이터를 조회하고, 규칙(현재 규정 기준)을 적용한 사실을 문장으로 만든다."""
    facts: list[str] = []
    meta: dict = {}
    cust = _CUST.search(question)
    ctx = api.customer_context(cust.group()) if cust else None
    if ctx:
        meta["customer"] = ctx["customer_id"]
        facts.append(
            f"고객 {ctx['customer_id']}: {ctx['customer_type']}, 요금제 {ctx['plan']}, 결제 주기 {ctx['billing_cycle']}, "
            f"좌석 {ctx['seats']}, 현재 주기 {ctx['current_period_start']}~{ctx['current_period_end']}, "
            f"결제일 {ctx['paid_at']}, 결제액 {ctx['amount']:,}원, 프로모션 {ctx['promo_code'] or '없음'}, "
            f"구독 상태 {ctx['status']}, 계정 상태 {ctx['account_status']}, "
            f"사용량 문서 {ctx['docs_created']}건·로그인 {ctx['logins']}회, 팀 관리자 {ctx['team_admins'] or '해당 없음'}"
        )
        seats = _SEATS.search(question)
        if seats:
            v = rules.evaluate_seat_change(ctx, int(seats.group(2)))
            facts.append("좌석 변경 판단(규칙): " + v.result)
        elif "환불" in question:
            role = "member" if re.search(r"구성원|팀원", question) else None
            v = rules.evaluate_refund(ctx, as_of, policy_mode="current", requester_role=role)
            facts.append(f"환불 판단(현재 규정 기준 규칙): {v.result}" + (f", 금액 {v.amount:,}원" if v.amount else "")
                         + (f", 승인자 {v.approver}" if v.approver else ""))
            facts += ["  - " + n for n in v.notes]
    inc = _INC.search(question)
    d = _DATE.search(question)
    incident = api.incident(inc.group()) if inc else (api.incident(on=date(*map(int, d.groups()))) if d and "장애" in question or (d and "안 됐" in question) else None)
    if incident:
        v = rules.evaluate_incident_credit(incident, ctx, policy_mode="current")
        facts.append(f"장애 {incident['incident_id']}: {incident['started_at']}~{incident['ended_at']} {incident['duration_hours']}시간, "
                     f"책임 {incident['responsibility']}, 정기 점검 {incident['is_planned_maintenance']} → 판단(규칙): {v.result}")
    return "\n".join(facts), meta


def answer_config3(question: str, as_of: date) -> dict:
    rows = retrieval.hybrid_search(question, TOP_K, valid_on=as_of)
    facts, meta = structured_facts(question, as_of)
    user = (f"오늘 날짜: {as_of}\n\n참고 조항:\n{retrieval.format_context(rows)}\n\n"
            + (f"조회된 사실과 규칙 판단:\n{facts}\n\n" if facts else "")
            + f"질문: {question}")
    text = llm.chat(SYSTEM, user)
    return {"answer": text, "contexts": [f"{r['clause_id']}({r['score']})" for r in rows], "facts": facts}


CONFIGS = {"config2_search": answer_config2, "config3_sql": answer_config3}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", choices=list(CONFIGS))
    ap.add_argument("--ask")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    as_of, _ = evaluate.load_questions()
    if a.ask:
        fn = CONFIGS[a.eval or "config3_sql"]
        res = fn(a.ask, as_of)
        print(res.get("facts", "")); print(res["contexts"]); print(res["answer"])
    elif a.eval:
        evaluate.run(a.eval, CONFIGS[a.eval], only=a.only)
