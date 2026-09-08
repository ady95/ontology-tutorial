"""10-6 (2/3): 간이과세 판단 규칙 — data/tax/policy_versions.yaml 을 코드로 옮긴 것.

ch07/rules.py 와 같은 구조입니다. 두 모드:
  policy_mode="current"   : 현행 기준 금액(1억 400만원)만 안다. 금액을 그대로 공급대가로 보고, 확인되지 않은
                            것도 있는 것으로 취급한다(닫힌 세계). 판 선택·용어 정규화·보류·해석 차이 없음 → 구성 3
  policy_mode="versioned" : 적용기간 개시일로 판을 고르고, 부가세 포함 여부·업종별 금액·사업장 구분이 확인되지
                            않으면 보류하며, 해석이 갈리는 지점은 split 으로 표시한다 → 구성 4

facts (dict):
  amount            총 공급대가 또는 매출 (원). None 이면 정형 데이터에서 가져온다
  vat_included      True/False/None  (None = 미확인)
  industries        [{"code": "Retail", "label": "소매업", "amount": 60000000 or None, "is_rental": bool}]
  places            사업장 수 (None = 미확인)
  same_place_mixed  한 사업장에서 임대와 다른 업종을 겸영하는가
  business_start_date, registration_date, first_supply_date  (date or None)
  taxpayer_id       정형 데이터 조회 키
"""
from __future__ import annotations

import calendar
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import date

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db  # noqa: E402

PV = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "data" / "tax" / "policy_versions.yaml").read_text(encoding="utf-8"))
EXCLUDED_CODES = {"Wholesale", "Mining", "Manufacturing", "RealEstateSales", "Professional", "Construction", "Utilities"}
RENTAL_LIMIT = 48_000_000


@dataclass
class Verdict:
    status: str = "answer"           # answer | hold | split
    result: str = ""
    threshold_version: str | None = None
    threshold: int | None = None
    basis_amount: int | None = None  # 판정에 쓴 공급대가(환산·합산 후)
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    interpretations: list[dict] = field(default_factory=list)

    def ev(self, *ids: str) -> None:
        for e in ids:
            if e not in self.evidence:
                self.evidence.append(e)


# ---------- 시점 ----------

def application_period(as_of: date) -> tuple[date, date, int]:
    """질문 시점이 속한 적용기간(7/1~6/30)과 그 판정 기준 연도(직전 연도)."""
    start = date(as_of.year, 7, 1) if as_of >= date(as_of.year, 7, 1) else date(as_of.year - 1, 7, 1)
    end = date(start.year + 1, 6, 30)
    return start, end, start.year - 1


def threshold_for(app_start: date) -> dict:
    for v in PV["versions"]:
        vf, vt = v.get("valid_from"), v.get("valid_to")
        if (vf is None or vf <= app_start) and (vt is None or app_start <= vt):
            return v
    return PV["versions"][-1]


def current_threshold() -> dict:
    return [v for v in PV["versions"] if v.get("valid_to") is None][0]


# ---------- 환산 ----------

def annualize(amount: int, start: date, year_end: date) -> tuple[int, int, str]:
    """사업 개시일 ~ 과세기간 종료일 공급대가를 12개월로 환산. 1개월 미만 끝수는 1개월."""
    months = (year_end.year - start.year) * 12 + (year_end.month - start.month)
    if year_end.day >= start.day or start.day == 1:
        months += 1                       # 시작한 달의 끝수 → 1개월
    else:
        months += 1
    months = max(1, months)
    annual = round(amount * 12 / months)
    return annual, months, f"{start} ~ {year_end}: {months}개월 → {amount:,} × 12 ÷ {months} = {annual:,}원"


# ---------- 정형 데이터 ----------

def taxpayer_context(taxpayer_id: str) -> dict | None:
    with db.connect() as c:
        t = c.execute("SELECT taxpayer_id, business_start_date, registration_date, first_supply_date, bookkeeping_duty FROM tax_taxpayers WHERE taxpayer_id=%s", (taxpayer_id,)).fetchone()
        if not t:
            return None
        rows = c.execute("""SELECT place_id, industry_code, industry_label, is_real_estate_rental, is_excluded_industry,
                                   supply_consideration_2024, supply_value_2024, vat_included_known
                            FROM tax_places WHERE taxpayer_id=%s ORDER BY place_id""", (taxpayer_id,)).fetchall()
    inds = [{"code": r[1], "label": r[2], "is_rental": r[3], "excluded": r[4], "amount": r[5], "supply_value": r[6], "vat_known": r[7]} for r in rows]
    known = all(i["amount"] is not None for i in inds)
    return {"taxpayer_id": t[0], "business_start_date": t[1], "registration_date": t[2], "first_supply_date": t[3],
            "industries": inds, "places": len(rows), "amount": sum(i["amount"] or 0 for i in inds) if known else None,
            "vat_included": True if known else None, "same_place_mixed": any("같은 사업장" in (i["label"] or "") for i in inds)}


# ---------- 판정 ----------

def judge(facts: dict, as_of: date, *, policy_mode: str = "versioned") -> Verdict:
    v = Verdict()
    app_start, app_end, prev_year = application_period(as_of)
    year_end = date(prev_year, 12, 31)
    if policy_mode == "current":
        th = current_threshold()
    else:
        th = threshold_for(app_start)
        v.notes.append(f"적용기간 {app_start} ~ {app_end}, 판정 기준 연도 {prev_year}년. 적용기간 개시일에 시행 중인 기준 {th['amount']:,}원({th['id']}) 적용")
        v.ev("vat_act:62.1", "vat_decree:109.1")
    v.threshold_version, v.threshold = th["id"], th["amount"]
    v.ev("vat_act:61.1")

    inds = facts.get("industries") or []
    # 1. 배제 업종 (금액 무관)
    for i in inds:
        if i.get("code") in EXCLUDED_CODES or i.get("excluded"):
            v.result = f"간이과세 배제. {i.get('label') or i.get('code')}은(는) 규모와 관계없이 간이과세가 적용되지 않는 배제 업종입니다"
            v.ev("vat_act:61.1", "vat_decree:109.2")
            return v

    # 2. 금액 확정: 부가세 포함 여부
    amount = facts.get("amount")
    if amount is None:
        v.status, v.result = "hold", "결론 보류: 직전 연도 공급대가를 확인해야 합니다"
        v.missing.append("직전 연도(2024년) 공급대가")
        return v
    vat_inc = facts.get("vat_included")
    if vat_inc is False:
        amount = round(amount * 1.1)
        v.notes.append(f"제시된 금액은 부가가치세를 뺀 공급가액이므로 1.1을 곱해 공급대가 {amount:,}원으로 판단합니다")
        v.ev("vat_act:61.1")
    elif vat_inc is None and policy_mode == "versioned":
        v.status = "hold"
        v.missing.append("제시된 금액이 부가가치세를 포함한 공급대가인지 (공급가액이면 1.1을 곱해야 함)")

    # 3. 임대·유흥 업종 4,800만원 기준
    rentals = [i for i in inds if i.get("is_rental")]
    if rentals:
        v.ev("vat_act:61.1.3")
        for r in rentals:
            if r.get("amount") is None:
                if policy_mode == "versioned":
                    v.status = "hold"
                    v.missing.append("부동산임대업만의 직전 연도 공급대가 (4,800만원 이상이면 합계와 관계없이 배제)")
                    if facts.get("places") is None:
                        v.missing.append("임대와 다른 업종이 같은 사업장인지 별도 사업장인지")
            elif r["amount"] >= RENTAL_LIMIT:
                v.result = f"간이과세 배제. 부동산임대업의 직전 연도 공급대가 {r['amount']:,}원이 4,800만원 이상입니다 (2025-07-01부터 일반과세)"
                v.basis_amount = r["amount"]
                return v
            else:
                v.notes.append(f"부동산임대업 공급대가 {r['amount']:,}원은 4,800만원 미만이므로 임대업 자체로는 배제되지 않습니다")
        if facts.get("same_place_mixed") and all(r.get("amount") is not None and r["amount"] < RENTAL_LIMIT for r in rentals) and policy_mode == "versioned":
            v.status = "split"
            v.interpretations = [
                {"id": "A", "reading": "61조 1항 3호를 업종별로 읽어 임대업 공급대가만 4,800만원과 비교 → 미만이므로 합계를 1억 400만원과 비교", "evidence": ["vat_act:61.1.3", "vat_act:61.1"]},
                {"id": "B", "reading": "임대업을 겸영하는 사업장 전체를 부동산임대업 경영 사업자로 보아 사업장 공급대가 합계에 4,800만원을 적용 → 배제", "evidence": ["vat_act:61.1.3"]},
            ]
            v.ev("interpretation:I-01")

    if v.status == "hold":
        v.result = "결론 보류: " + " / ".join(v.missing)
        return v

    # 4. 신규 사업자 환산
    start = facts.get("business_start_date")
    first_supply = facts.get("first_supply_date")
    reg = facts.get("registration_date")
    if start and start.year == prev_year:
        if policy_mode == "versioned" and first_supply and first_supply.year > prev_year and amount == 0:
            v.status = "split"
            v.interpretations = [
                {"id": "A", "reading": f"사업 개시일을 사업자등록일({reg or start})로 보아 {prev_year}년에 사업 기간이 있음. 공급대가 0원을 환산해도 0원 → 기준 미달", "evidence": ["vat_act:61.2", "vat_decree:109.3"]},
                {"id": "B", "reading": f"사업 개시일을 실제 공급 개시일({first_supply})로 보아 {prev_year}년에는 사업을 시작하지 않은 것 → {first_supply.year}년이 최초 과세기간이며 환산 대상 아님", "evidence": ["vat_decree:6", "vat_act:61.3", "vat_act:61.4"]},
            ]
            v.ev("interpretation:I-02", "vat_act:61.2", "vat_decree:6")
            v.result = "두 해석 모두 2025-07-01부터 간이과세 적용이라는 결론은 같지만 근거 조항과 이후 판정 시점이 다릅니다"
            v.basis_amount = 0
            return v
        annual, months, note = annualize(amount, start, year_end)
        v.notes.append("직전 과세기간에 신규로 사업을 시작했으므로 12개월로 환산합니다. " + note)
        v.ev("vat_act:61.2", "vat_decree:109.3")
        amount = annual

    # 5. 둘 이상 사업장 합산
    places = facts.get("places")
    if places and places >= 2:
        v.notes.append(f"사업장이 {places}곳이므로 모든 사업장의 공급대가를 합산해 판단합니다 (합계 {amount:,}원)")
        v.ev("vat_act:61.1.4")

    # 6. 기준 비교
    v.basis_amount = amount
    if amount < th["amount"]:
        v.result = f"간이과세 적용. 판정 공급대가 {amount:,}원이 기준 금액 {th['amount']:,}원 미만입니다 ({app_start} ~ {app_end})"
    else:
        v.result = f"간이과세 배제. 판정 공급대가 {amount:,}원이 기준 금액 {th['amount']:,}원 이상입니다 ({app_start}부터 일반과세)"
    if v.status == "split":
        v.result = "해석이 갈리는 사안. 해석 A: " + v.interpretations[0]["reading"] + f" ({v.result}) / 해석 B: " + v.interpretations[1]["reading"]
    return v


if __name__ == "__main__":
    as_of = date(2025, 9, 8)
    print(application_period(as_of), threshold_for(date(2025, 7, 1))["id"], threshold_for(date(2023, 7, 1))["id"])
    for tid in ["T001", "T004", "T007", "T008", "T009", "T010"]:
        ctx = taxpayer_context(tid)
        r = judge(ctx, as_of)
        print(tid, r.status, "|", r.result[:110], "|", r.missing)
    print(judge({"amount": 98_000_000, "vat_included": False, "industries": [{"code": "Retail"}], "places": 1}, as_of).result)
    print(judge({"amount": 95_000_000, "vat_included": None, "industries": [{"code": "Cafe"}, {"code": "RealEstateRental", "is_rental": True, "amount": None}], "places": None}, as_of).result)
    print(judge({"amount": 20_000_000, "vat_included": True, "industries": [{"code": "Retail"}], "places": 1, "business_start_date": date(2024, 10, 15)}, as_of).result)
    print("current T003:", judge({"amount": 98_000_000, "vat_included": False, "industries": [{"code": "Retail"}], "places": 1}, as_of, policy_mode="current").result)
