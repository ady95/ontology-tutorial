"""07-2: 판단 규칙 평가 — data/ontology/rules.yaml 을 코드로 옮긴 것.

LLM 은 이 모듈을 호출하지 않습니다. 파이프라인이 사실(facts)을 모아 이 모듈에 넘기고,
결론·근거·미확인 사항을 받아 LLM 에는 "문장으로 다듬는 일"만 시킵니다 (05-5 경계 나누기).

두 가지 모드:
  policy_mode="current"   : 현재 시행 중인 규정(제2판)만 안다. 시점·유리한 규정 적용·준용 없음 → 구성 3
  policy_mode="versioned" : 결제일로 판을 고르고 약관 9조 3항(유리한 규정)을 적용하며 requires 가 비면 보류 → 구성 4

실행:  python ch07/rules.py   (정형 데이터가 필요한 질문을 손으로 매핑해 규칙만 검증)
"""
from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field
from datetime import date

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ch06 import api  # noqa: E402

RULES = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "data" / "ontology" / "rules.yaml").read_text(encoding="utf-8"))
_RULE = {r["id"]: r for r in RULES["rules"]}
FULL_REFUND_DAYS = {"refund_policy_v1": 14, "refund_policy_v2": 30}
MONTHLY_FULL_DAYS = 7


def ev(rule_id: str) -> list[str]:
    return list(_RULE[rule_id]["evidence"])


@dataclass
class Verdict:
    status: str = "answer"            # answer | hold
    result: str = ""                  # 한 줄 결론
    amount: int | None = None         # 환불·크레딧 금액
    approver: str | None = None
    policy_version: str | None = None
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # hold 일 때 확인할 항목
    notes: list[str] = field(default_factory=list)     # 계산 과정·적용 이유
    rules_fired: list[str] = field(default_factory=list)

    def fire(self, rule_id: str, note: str | None = None) -> None:
        self.rules_fired.append(rule_id)
        for e in ev(rule_id):
            if e not in self.evidence:
                self.evidence.append(e)
        if note:
            self.notes.append(note)


# ---------- 보조 계산 ----------

def prorate(amount: int, paid_at: date, period_end: date, as_of: date, version: str,
            monthly_list_price: int) -> tuple[int, str]:
    total = (period_end - paid_at).days + 1
    remaining = (period_end - as_of).days          # 요청일 다음 날 ~ 종료일
    used = total - remaining
    base = amount * remaining / total
    if version == "refund_policy_v1":
        refund = base * 0.9
        note = f"잔여 {remaining}일/{total}일 일할 {base:,.0f}원에서 위약금 10% 공제 → {refund:,.0f}원 (제1판 3조 2항)"
    else:
        discount = monthly_list_price * 12 - amount
        claw = discount * used / total
        refund = base - claw
        note = f"잔여 {remaining}일/{total}일 일할 {base:,.0f}원에서 연간 할인 {discount:,}원의 사용 {used}일분 {claw:,.0f}원 환수 → {refund:,.0f}원 (제2판 4조 2항)"
    return round(refund), note


def approval_authority(amount: int) -> str:
    for row in _RULE["approval_authority"]["table"]:
        if row["max"] is None or amount <= row["max"]:
            return row["approver"]
    return "운영 책임자"


# ---------- 환불 ----------

def evaluate_refund(ctx: dict, as_of: date, *, policy_mode: str = "versioned",
                    requester_role: str | None = None) -> Verdict:
    v = Verdict()
    paid_at: date = ctx["paid_at"]
    days = (as_of - paid_at).days
    prices = api.plan_prices(ctx["plan"], paid_at)
    monthly_list = (prices["monthly"] if prices else ctx["list_price"]) * (ctx["seats"] or 1)   # 팀은 좌석 수만큼

    # 제외 조건 (priority 100)
    if ctx["account_status"] == "suspended":
        v.fire("exclude_suspended", f"{ctx['suspended_at']} 계정 정지")
        v.result = "환불 불가 (계정 정지 상태)"
        return v
    if ctx["customer_type"] == "team":
        if requester_role is None:
            if policy_mode == "versioned":
                v.status, v.missing = "hold", ["요청자가 팀 관리자인지"]
                v.fire("exclude_non_admin_team")
                v.result = "팀 요금제는 관리자만 요청할 수 있어 요청자 확인이 필요합니다"
                return v
        elif requester_role != "admin":
            v.fire("exclude_non_admin_team")
            v.status, v.result = "hold", f"처리 불가. 팀 관리자({', '.join(ctx['team_admins'])})가 요청해야 합니다"
            return v

    # 적용 판 결정 (시점 규칙)
    if policy_mode == "current":
        versions = ["refund_policy_v2"]
        v.policy_version = "refund_policy_v2"
    else:
        base_version = api.policy_version_for(paid_at)
        v.policy_version = base_version
        v.evidence += ["terms_of_service:2.2"]
        if ctx.get("is_renewal"):
            v.notes.append("자동 갱신 결제이므로 갱신 결제일을 결제 완료일로 봅니다 (약관 3조 3항)")
            v.evidence.append("terms_of_service:3.3")
        versions = [base_version] + (["refund_policy_v2"] if base_version == "refund_policy_v1" else [])

    outcomes: list[tuple[str, Verdict]] = []
    for version in versions:
        o = Verdict(policy_version=version)
        # 프로모션
        if ctx.get("promo_code"):
            if version == "refund_policy_v1":
                o.fire("promo_v1_no_refund"); o.result = "환불 불가 (제1판: 프로모션 결제 환불 제외)"; o.amount = 0
                outcomes.append((version, o)); continue
            o.fire("promo_v2_window_only")
            limit = MONTHLY_FULL_DAYS if ctx["billing_cycle"] == "monthly" else FULL_REFUND_DAYS[version]
            if days > limit:
                o.result = f"환불 불가 (프로모션 결제, 전액 환불 기간 {limit}일 경과)"; o.amount = 0
                outcomes.append((version, o)); continue
            # 기간 안이면 아래 일반 규칙으로 전액 환불 여부 판단
        # 학생 요금제
        if ctx["plan"] == "student":
            o.fire("student_applies_basic", "학생 요금제는 베이직 규정을 준용합니다 (제2판 6조 1항)")
            if ctx.get("student_verified_until") and ctx["student_verified_until"] < paid_at and ctx.get("is_renewal") and days <= MONTHLY_FULL_DAYS:
                o.fire("student_expired_renewal", "학생 인증 만료 후 베이직 가격으로 갱신된 결제 → 사용 여부와 무관하게 전액 환불")
                o.result, o.amount = "전액 환불", ctx["amount"]
                outcomes.append((version, o)); continue
        if ctx["billing_cycle"] == "monthly":
            if days > MONTHLY_FULL_DAYS:
                o.fire("monthly_no_refund", f"결제 후 {days}일 경과 (7일 초과)")
                o.result, o.amount = "환불 불가. 해지 안내", 0
            elif not ctx["usage_known"]:
                if policy_mode == "versioned":
                    o.status, o.missing = "hold", ["사용량(문서 생성 수·로그인 횟수)"]
                    o.fire("monthly_full_refund", f"결제 후 {days}일 경과로 기간 요건은 충족하나 사용량 기록이 없어 미사용 여부를 판정할 수 없습니다")
                    o.result = "판단 보류: 사용량 확인 필요"
                else:  # 현재 규정만 아는 구성은 기록 없음을 미사용으로 취급하기 쉽다 (닫힌 세계)
                    o.fire("monthly_full_refund", f"결제 후 {days}일 경과, 사용 기록 없음")
                    o.result, o.amount = "전액 환불", ctx["amount"]
            elif ctx["unused"]:
                o.fire("monthly_full_refund", f"결제 후 {days}일 경과, 문서 {ctx['docs_created']}건·로그인 {ctx['logins']}회로 미사용")
                o.result, o.amount = "전액 환불", ctx["amount"]
            else:
                o.fire("monthly_no_refund", f"문서 {ctx['docs_created']}건·로그인 {ctx['logins']}회로 사용 상태")
                o.result, o.amount = "환불 불가. 해지 안내", 0
        else:  # yearly
            limit = FULL_REFUND_DAYS[version]
            if days <= limit:
                o.fire("yearly_full_refund", f"결제 후 {days}일 경과 ({limit}일 이내)")
                o.result, o.amount = "전액 환불", ctx["amount"]
            else:
                amt, note = prorate(ctx["amount"], paid_at, ctx["current_period_end"], as_of, version, monthly_list)
                o.fire("yearly_prorated_refund", f"결제 후 {days}일 경과 ({limit}일 초과). " + note)
                o.result, o.amount = "일할 환불", amt
        outcomes.append((version, o))

    # 유리한 규정 선택 (약관 9조 3항)
    best_version, best = outcomes[0]
    if len(outcomes) == 2:
        (v1n, o1), (v2n, o2) = outcomes
        if (o2.amount or 0) > (o1.amount or 0):
            best_version, best = v2n, o2
            best.notes.insert(0, f"제1판 적용 시 {o1.result} {o1.amount:,}원, 제2판 적용 시 {o2.amount:,}원 → 고객에게 유리한 제2판을 적용합니다 (약관 9조 3항)")
            best.evidence.append("terms_of_service:9.3"); best.evidence.append("ops_manual:2.2")
        else:
            best.notes.append(f"제2판을 적용해도 결과가 같거나 불리하므로 제1판을 적용합니다")
    v.status = best.status
    v.result, v.amount, v.missing = best.result, best.amount, best.missing
    v.policy_version = best_version
    v.notes += best.notes
    v.rules_fired += best.rules_fired
    for e in best.evidence:
        if e not in v.evidence:
            v.evidence.append(e)
    if v.amount:
        v.approver = approval_authority(v.amount)
        v.fire("approval_authority", f"환불액 {v.amount:,}원 → {v.approver} 승인")
    return v


# ---------- 좌석 변경 ----------

def evaluate_seat_change(ctx: dict, new_seats: int) -> Verdict:
    v = Verdict()
    if ctx["plan"] == "team" and new_seats < 3:
        v.fire("seat_minimum"); v.result = "불가. 팀 요금제 최소 좌석은 3좌석이며 프로 요금제로 전환해야 합니다"
        return v
    if new_seats < ctx["seats"]:
        nxt = ctx["current_period_end"].fromordinal(ctx["current_period_end"].toordinal() + 1)
        v.fire("seat_decrease", f"현재 주기 {ctx['current_period_start']} ~ {ctx['current_period_end']}")
        v.result = f"환불 없음. 좌석 축소는 다음 결제 주기({nxt})부터 반영"
    else:
        v.result = "좌석 추가는 즉시 반영, 잔여 일수 일할 청구"
        v.evidence = ["refund_policy_v2:5.2"]
    return v


# ---------- 장애 크레딧 ----------

def evaluate_incident_credit(inc: dict, ctx: dict | None = None, *, policy_mode: str = "versioned") -> Verdict:
    v = Verdict()
    if inc["is_planned_maintenance"]:
        v.fire("maintenance_not_incident", f"{inc['incident_id']}는 공지된 정기 점검 ({inc['duration_hours']}시간)")
        v.result = "보상 대상 아님 (정기 점검은 장애가 아님)"; v.amount = 0
        return v
    if inc["duration_hours"] < 24:
        v.fire("incident_no_credit_short", f"{inc['incident_id']} {inc['duration_hours']}시간 (24시간 미만)")
        v.result = "보상 대상 아님"; v.amount = 0
        return v
    v.fire("incident_credit", f"{inc['incident_id']} 회사 책임 {inc['duration_hours']}시간 장애")
    if ctx is None:
        v.result = "크레딧 지급 (다음 결제에서 차감)"
        return v
    monthly_fee = ctx["amount"] if ctx["billing_cycle"] == "monthly" else ctx["amount"] / 12
    credit = round(monthly_fee * inc["duration_hours"] / 720)
    v.amount = credit
    v.notes.append(f"월 환산 요금 {monthly_fee:,.0f}원 × {inc['duration_hours']}시간 ÷ 720시간 = {credit:,}원")
    if ctx["status"] == "cancel_scheduled" and policy_mode == "versioned":
        v.fire("credit_cash_if_cancel_scheduled", "해지 신청 상태라 다음 결제가 없으므로 현금 환불")
        v.result = f"크레딧 {credit:,}원을 현금으로 환불"
    else:
        v.result = f"크레딧 {credit:,}원 지급, 다음 결제에서 차감"
    return v


# ---------- 규정 개정 재점검 (Q29) ----------

def recheck_targets(cutoff: date, as_of: date) -> Verdict:
    v = Verdict()
    v.evidence = ["ops_manual:6.2", "terms_of_service:9.3", "terms_of_service:3.3"]
    targets, excluded = [], []
    for s in api.subscriptions_paid_before(cutoff):
        why = None
        if s["account_status"] == "suspended":
            why = "계정 정지로 환불 제외"
        elif s["promo_code"] and (as_of - s["current_period_start"]).days > 30:
            why = "프로모션 결제이며 전액 환불 기간 경과"
        elif s["billing_cycle"] == "monthly":
            why = "월간 결제는 갱신 시 새 결제일 기준"
        (excluded if why else targets).append((s["customer_id"], why or "연간 결제, 위약금 폐지·30일 조항 해당"))
    v.result = "재점검 대상: " + ", ".join(f"{c}({w})" for c, w in targets) + " / 제외: " + ", ".join(f"{c}({w})" for c, w in excluded)
    v.notes = [f"{cutoff} 이전 결제 유효 구독 {len(targets) + len(excluded)}건 중 대상 {len(targets)}건"]
    return v


if __name__ == "__main__":
    as_of = date(2025, 9, 8)
    print("== 환불 (versioned)")
    for cid in ["C001", "C002", "C003", "C004", "C005", "C006", "C007", "C008", "C009", "C010", "C012"]:
        ctx = api.customer_context(cid)
        r = evaluate_refund(ctx, as_of, requester_role="admin" if ctx["customer_type"] == "team" else None)
        print(f"{cid}: [{r.status}] {r.result} amount={r.amount} approver={r.approver} ver={r.policy_version}")
        for n in r.notes:
            print("     -", n)
        print("     근거:", r.evidence)
    print("== C004 2025-07-10 요청:", evaluate_refund(api.customer_context("C004"), date(2025, 7, 10)).amount)
    print("== C011 구성원 요청:", evaluate_refund(api.customer_context("C011"), as_of, requester_role="member").result)
    print("== C007 current 모드:", evaluate_refund(api.customer_context("C007"), as_of, policy_mode="current").result)
    print("== 좌석:", evaluate_seat_change(api.customer_context("C005"), 3).result, "|", evaluate_seat_change(api.customer_context("C005"), 2).result)
    for iid, cid in [("INC-01", "C002"), ("INC-03", None), ("INC-02", None), ("INC-01", "C013")]:
        r = evaluate_incident_credit(api.incident(iid), api.customer_context(cid) if cid else None)
        print("== 장애", iid, cid, "→", r.result)
    print("== 재점검:", recheck_targets(date(2025, 6, 30), as_of).result)
