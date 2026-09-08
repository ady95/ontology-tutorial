"""10-6 (3/3): 간이과세 질문 10개를 세 가지 구성으로 비교합니다.

  tax1_basic    : 조문 요약·안내문을 500자 청크로 벡터 검색, 상위 5개 → LLM (구성 1과 같은 방식)
  tax3_rules    : 조항 단위 하이브리드 검색(유효 문서 필터) + 정규식으로 금액·업종·사업장 수 추출
                  + 정형 데이터(납세자 ID) + 규칙(현행 기준만, 금액을 그대로 공급대가로 취급, 보류 없음)
  tax4_semantic : 용어 사전을 준 LLM 분석(부가세 포함 여부·업종별 금액·사업장 구분·개업일)
                  + 규칙(판 선택·보류·해석 분기) + 개념 정의·근거 조항 + 행동 제어 답변 형식

실행:
  python ch10/tax_pipeline.py --eval tax1_basic
  python ch10/tax_pipeline.py --eval-all            # 세 구성 순서대로 → results/tax*_r1.jsonl
  python ch10/tax_pipeline.py --eval-all --runs 3
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

from common import db, llm, evaluate  # noqa: E402
from ch10 import tax_rules  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TAX = ROOT / "data" / "tax"
QUESTIONS = TAX / "questions.yaml"
GLOSSARY = yaml.safe_load((TAX / "glossary.yaml").read_text(encoding="utf-8"))["terms"]

SYSTEM_PLAIN = ("당신은 세무 상담을 돕는 도우미입니다. 아래 참고 자료만 근거로 한국어로 간결하게 답하세요. "
                "금액은 숫자로 명시하세요. 참고 자료에 없는 내용은 지어내지 마세요.")


# ---------- 검색 ----------

def _rows(r):
    return {"clause_id": r[0], "doc_id": r[1], "title": r[2], "valid_from": r[3], "valid_to": r[4], "content": r[5], "score": round(float(r[6]), 3)}


def basic_search(question: str, k: int = 5) -> list[dict]:
    qv = llm.embed([question])[0]
    with db.connect() as c:
        rows = c.execute("""SELECT doc_id || '#' || chunk_no, doc_id, doc_id, NULL, NULL, content, 1 - (embedding <=> %s::vector)
                            FROM tax_chunks_basic WHERE embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s""", (qv, qv, k)).fetchall()
    return [_rows(r) for r in rows]


_TOKEN = re.compile(r"[가-힣A-Za-z0-9]+")


def _tokens(s: str) -> set[str]:
    out = set()
    for t in _TOKEN.findall(s):
        if len(t) >= 2:
            out.add(t); out.add(t[:2])
            if len(t) > 3:
                out.add(t[:3])
    return out


def clause_search(question: str, as_of: date, k: int = 8) -> list[dict]:
    """벡터 + 키워드 RRF, 기준일에 유효한 문서만."""
    qv = llm.embed([question])[0]
    with db.connect() as c:
        vec = c.execute("""SELECT c.clause_id, c.doc_id, d.title, d.valid_from, d.valid_to, c.content, 1 - (c.embedding <=> %(v)s::vector)
                           FROM tax_clauses c JOIN tax_docs d USING (doc_id)
                           WHERE c.embedding IS NOT NULL AND (d.valid_from IS NULL OR d.valid_from <= %(d)s) AND (d.valid_to IS NULL OR d.valid_to >= %(d)s)
                           ORDER BY c.embedding <=> %(v)s::vector LIMIT %(k)s""", {"v": qv, "d": as_of, "k": k * 2}).fetchall()
        allrows = c.execute("""SELECT c.clause_id, c.doc_id, d.title, d.valid_from, d.valid_to, c.content, 0
                               FROM tax_clauses c JOIN tax_docs d USING (doc_id)
                               WHERE (d.valid_from IS NULL OR d.valid_from <= %(d)s) AND (d.valid_to IS NULL OR d.valid_to >= %(d)s)""", {"d": as_of}).fetchall()
    qt = _tokens(question)
    kw = sorted([(len(qt & _tokens(r[5])), r) for r in allrows], key=lambda x: -x[0])[: k * 2]
    fused, rows = {}, {}
    for rank, r in enumerate(vec):
        fused[r[0]] = fused.get(r[0], 0) + 1 / (60 + rank); rows[r[0]] = r
    for rank, (s, r) in enumerate(kw):
        if s:
            fused[r[0]] = fused.get(r[0], 0) + 1 / (60 + rank); rows[r[0]] = r
    top = sorted(fused.items(), key=lambda x: -x[1])[:k]
    return [dict(_rows(rows[cid]), score=round(s, 4)) for cid, s in top]


def fmt(rows: list[dict]) -> str:
    out = []
    for r in rows:
        meta = f" ({r['title']}, 유효 {r['valid_from']} ~ {r['valid_to'] or '현재'})" if r.get("valid_from") else ""
        out.append(f"[{r['clause_id']}]{meta}\n{r['content']}")
    return "\n\n".join(out)


def clauses_by_id(ids: list[str]) -> list[dict]:
    out = []
    with db.connect() as c:
        for cid in ids:
            base = cid
            if cid.count(".") == 2:            # 'vat_act:61.1.3' → 조항 테이블에는 '61.1' 까지만 있음
                base = cid.rsplit(".", 1)[0]
            r = c.execute("SELECT c.clause_id, c.doc_id, d.title, d.valid_from, d.valid_to, c.content, 1 FROM tax_clauses c JOIN tax_docs d USING (doc_id) WHERE clause_id=%s", (base,)).fetchone()
            if r and all(x["clause_id"] != r[0] for x in out):
                out.append(_rows(r))
    return out


# ---------- 구성 1 ----------

def answer_tax1(question: str, as_of: date) -> dict:
    rows = basic_search(question)
    text = llm.chat(SYSTEM_PLAIN, f"오늘 날짜: {as_of}\n\n참고 자료:\n{fmt(rows)}\n\n질문: {question}")
    return {"answer": text, "contexts": [r["clause_id"] for r in rows]}


# ---------- 구성 3: 정규식 사실 추출 + 현행 규칙 ----------

_AMT = re.compile(r"(?:(\d+)억)?\s*(\d[\d,]*)?\s*만원")
_TID = re.compile(r"(?<![A-Za-z0-9])T0\d{2}(?!\d)")
INDUSTRY_KW = [("도매", "Wholesale", False), ("임대|세놓", "RealEstateRental", True), ("카페", "Cafe", False), ("음식점", "Restaurant", False),
               ("소매|옷가게|가게", "Retail", False), ("온라인|통신판매", "OnlineRetail", False)]


def parse_amounts(text: str) -> list[int]:
    out = []
    for m in _AMT.finditer(text):
        eok, man = m.group(1), m.group(2)
        if not eok and not man:
            continue
        val = (int(eok) * 100_000_000 if eok else 0) + (int(man.replace(",", "")) * 10_000 if man else 0)
        out.append(val)
    return out


def regex_facts(question: str) -> dict:
    tid = _TID.search(question)
    if tid:
        ctx = tax_rules.taxpayer_context(tid.group())
        if ctx:
            return ctx
    inds = []
    for pat, code, rental in INDUSTRY_KW:
        if re.search(pat, question):
            inds.append({"code": code, "label": code, "is_rental": rental, "amount": None})
    amts = parse_amounts(question)
    # 기준 금액(1억 400만원, 8,000만원, 4,800만원)은 질문에 언급된 기준이므로 제외
    amts = [a for a in amts if a not in (104_000_000, 80_000_000, 48_000_000)]
    if len(inds) == 1 and amts:                 # 업종이 하나면 총액이 그 업종의 금액
        inds[0]["amount"] = amts[0]
    m = re.search(r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일", question)
    start = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    return {"amount": amts[0] if amts else None, "vat_included": True, "industries": inds,
            "places": 2 if re.search(r"두 곳|2곳|사업장이 둘", question) else 1, "business_start_date": start}


def answer_tax3(question: str, as_of: date) -> dict:
    rows = clause_search(question, as_of)
    facts = regex_facts(question)
    v = tax_rules.judge(facts, as_of, policy_mode="current") if facts.get("amount") is not None or facts.get("industries") else None
    judgement = ""
    if v:
        judgement = f"\n\n조회·규칙 판단(현행 기준): {v.result}" + ("\n  - " + "\n  - ".join(v.notes) if v.notes else "")
    text = llm.chat(SYSTEM_PLAIN, f"오늘 날짜: {as_of}\n\n참고 조항:\n{fmt(rows)}{judgement}\n\n질문: {question}")
    return {"answer": text, "contexts": [r["clause_id"] for r in rows], "facts": json.dumps(facts, ensure_ascii=False, default=str)}


# ---------- 구성 4: 용어 사전 기반 분석 + 판 선택·보류·해석 ----------

def glossary_text() -> str:
    lines = []
    for t in GLOSSARY:
        forms = ", ".join(f"'{f['form']}'({f.get('layer','')})" for f in t.get("surface_forms", []))
        lines.append(f"- {t['canonical']} ({t['label']}): {t['definition']} | 표현: {forms}")
    return "\n".join(lines)


ANALYZE_SYSTEM = """당신은 세무 상담 질문을 구조화하는 분석기입니다. 반드시 JSON 만 출력합니다.
아래 용어 사전을 기준으로, 납세자가 말한 금액이 공급대가(부가세 포함)인지 공급가액(부가세 제외)인지 판단하되
질문에 명시되지 않았으면 null 로 두세요. 추측하지 마세요.

출력 형식:
{
  "taxpayer_id": "T007" | null,
  "amount_krw": 70000000 | null,              // 납세자가 말한 총액 (원). 기준 금액(1억 400만원 등)은 제외
  "vat_included": true | false | null,        // 부가세 포함 여부. '부가세 빼고' → false, '공급대가' → true, 그 외 → null
  "industries": [{"code": "Retail|Restaurant|Cafe|RealEstateRental|Wholesale|OnlineRetail|Other", "label": "…", "amount_krw": 60000000 | null, "is_rental": false}],
  "places": 1 | 2 | null,                     // 사업장 수. 같은 사업장에서 겸영이면 1, 불분명하면 null
  "same_place_mixed": true | false,           // 한 사업장에서 임대와 다른 업종을 함께 하는가
  "business_start_date": "YYYY-MM-DD" | null,
  "registration_date": "YYYY-MM-DD" | null,
  "first_supply_date": "YYYY-MM-DD" | null,
  "concepts": ["SupplyConsideration", "PrecedingYear", ...]
}"""

ANSWER_SYSTEM = """당신은 세무 상담을 돕는 도우미입니다. 아래 자료(개념 정의, 근거 조항, 규칙 판단)만 근거로 한국어로 답하세요. 형식:

결론: (한두 문장)
적용 조건: (적용기간, 판정 기준 연도, 기준 금액과 그 판, 환산·합산·예외 적용 여부)
근거: (조항 id 를 그대로 나열)
미확인 사항: (있으면 무엇을 확인해야 하는지, 없으면 '없음')

규칙 판단이 '보류'이면 결론을 단정하지 말고 무엇을 확인해야 하는지 답하세요.
규칙 판단이 '해석 차이'이면 어느 한쪽을 택하지 말고 두 해석을 나란히 적고 검토가 필요하다고 표시하세요.
규칙 판단이 있으면 그 결과와 금액을 바꾸지 말고 문장으로 옮기세요. 이 답변은 실제 세무 판단이 아니라 설계 실습입니다."""


def analyze(question: str, as_of: date) -> dict:
    out = llm.chat_json(ANALYZE_SYSTEM, f"오늘: {as_of}\n\n용어 사전:\n{glossary_text()}\n\n질문: {question}")
    m = _TID.search(question)
    if m and not out.get("taxpayer_id"):
        out["taxpayer_id"] = m.group()
    return out


def to_facts(a: dict) -> dict:
    if a.get("taxpayer_id"):
        ctx = tax_rules.taxpayer_context(a["taxpayer_id"])
        if ctx:
            return ctx
    def d(s):
        return date.fromisoformat(s) if s else None
    inds = [{"code": i.get("code"), "label": i.get("label"), "amount": i.get("amount_krw"), "is_rental": bool(i.get("is_rental")) or i.get("code") == "RealEstateRental"}
            for i in (a.get("industries") or [])]
    amount = a.get("amount_krw")
    if amount is None and inds and all(i["amount"] is not None for i in inds):
        amount = sum(i["amount"] for i in inds)
    return {"amount": amount, "vat_included": a.get("vat_included"), "industries": inds, "places": a.get("places"),
            "same_place_mixed": bool(a.get("same_place_mixed")), "business_start_date": d(a.get("business_start_date")),
            "registration_date": d(a.get("registration_date")), "first_supply_date": d(a.get("first_supply_date"))}


def concept_text(ids: list[str]) -> str:
    by = {t["canonical"]: t for t in GLOSSARY}
    return "\n".join(f"- {by[i]['label']}({i}): {by[i]['definition']}" for i in ids if i in by)


def answer_tax4(question: str, as_of: date) -> dict:
    a = analyze(question, as_of)
    facts = to_facts(a)
    v = tax_rules.judge(facts, as_of, policy_mode="versioned")
    status_label = {"answer": "결론", "hold": "보류", "split": "해석 차이"}[v.status]
    judgement = f"규칙 판단: [{status_label}] {v.result}"
    if v.threshold:
        judgement += f"\n기준 금액: {v.threshold:,}원 ({v.threshold_version})"
    if v.basis_amount is not None:
        judgement += f"\n판정 공급대가: {v.basis_amount:,}원"
    if v.notes:
        judgement += "\n적용 과정:\n" + "\n".join("  - " + n for n in v.notes)
    if v.missing:
        judgement += "\n미확인: " + " / ".join(v.missing)
    if v.interpretations:
        judgement += "\n해석:\n" + "\n".join(f"  - 해석 {i['id']}: {i['reading']} (근거 {', '.join(i['evidence'])})" for i in v.interpretations)
    ev_rows = clauses_by_id([e for e in v.evidence if not e.startswith("interpretation")])
    user = (f"오늘 날짜: {as_of}\n\n관련 개념 정의:\n{concept_text(a.get('concepts') or [])}\n\n"
            f"근거 조항:\n{fmt(ev_rows)}\n\n{judgement}\n\n질문: {question}")
    text = llm.chat(ANSWER_SYSTEM, user)
    return {"answer": text, "status": v.status, "evidence": v.evidence, "missing": v.missing,
            "contexts": [r["clause_id"] for r in ev_rows], "facts": json.dumps(facts, ensure_ascii=False, default=str)}


CONFIGS = {"tax1_basic": answer_tax1, "tax3_rules": answer_tax3, "tax4_semantic": answer_tax4}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", choices=list(CONFIGS))
    ap.add_argument("--eval-all", action="store_true")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--ask")
    a = ap.parse_args()
    as_of, _ = evaluate.load_questions(QUESTIONS)
    if a.ask:
        res = CONFIGS[a.eval or "tax4_semantic"](a.ask, as_of)
        print(res.get("facts")); print(res["answer"])
    elif a.eval_all or a.eval:
        names = list(CONFIGS) if a.eval_all else [a.eval]
        for i in range(a.start, a.start + a.runs):
            for n in names:
                print(f"\n===== {n}_r{i} =====")
                evaluate.run(f"{n}_r{i}", CONFIGS[n], questions_path=QUESTIONS)
        print(evaluate.compare_runs(*names, runs=a.start + a.runs - 1) if (a.start + a.runs - 1) >= 1 else "")
