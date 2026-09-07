"""04-5 최소 의미 모델 검증: model.yaml / glossary.yaml / questions.yaml 의 정합성을 점검합니다.

외부 API·DB 없이 pyyaml 만으로 동작하는 순수 파이썬 스크립트입니다.

실행:  python ch04/validate_model.py            (저장소 루트에서)
       python ch04/validate_model.py --verbose  (질문별 매칭 키워드까지 표시)

검사 항목
  [1] 관계(relations)의 from/to 가 모두 정의된 개념(또는 하위 개념) id 인가
  [2] 용어 사전(glossary)의 canonical 이 개념 id·하위 개념 id·속성명 중 하나인가
  [3] 질문 30개의 evidence 에 적힌 문서 id 가 data/docs/ 에 실제로 있는가
  [4] 질문 30개가 각각 어떤 개념에 닿는가 (키워드 사전 기반) —
      어느 개념에도 닿지 않는 질문, 어느 질문도 요구하지 않는 개념을 경고

종료 코드: 오류(FAIL)가 하나라도 있으면 1, 없으면 0. 경고(WARN)는 종료 코드에 영향 없음.
"""
import argparse
import pathlib
import re
import sys
from collections import defaultdict

import yaml

# Windows 콘솔(cp949)에서 한글·특수문자 출력 오류 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL_PATH = DATA / "ontology" / "model.yaml"
GLOSSARY_PATH = DATA / "ontology" / "glossary.yaml"
QUESTIONS_PATH = DATA / "eval" / "questions.yaml"
DOCS_DIR = DATA / "docs"

# [4] 질문 → 개념 키워드 사전.
# 왼쪽은 질문 문장에 나타나는 표현(정규식), 오른쪽은 model.yaml 의 개념 id 또는 하위 개념 id.
# 04-2 용어 사전의 surface_forms 를 코드로 옮긴 최소 판입니다. 7장의 질문 분석기가 이를 확장합니다.
KEYWORDS = [
    (r"해지", "CancellationRequest"),
    (r"환불", "RefundRequest"),
    (r"결제 취소|취소하고", "PaymentReversalRequest"),
    (r"좌석", "SeatChangeRequest"),
    (r"좌석", "Seat"),
    (r"크레딧|보상", "Credit"),
    (r"정기 점검", "PlannedMaintenance"),
    (r"장애|중단|서비스가 안 됐", "Incident"),
    (r"규정|약관|개정", "PolicyVersion"),
    (r"\d+조", "Clause"),
    (r"학생|요금제|프로모션", "Plan"),
    (r"프로모션|갱신|결제", "Payment"),
    (r"구독|연간|월간|남은 기간|잔여 기간|이번 달", "Subscription"),
    (r"사용하지|쓰지 않|사용량|로그인|문서 \d+개|문서 생성", "Usage"),
    (r"사용하지 않|쓰지 않", "Unused"),
    (r"팀원|구성원|관리자", "TeamMember"),
    (r"팀 요금제|팀의", "TeamCustomer"),
    (r"고객|C0\d\d|인증|계정|정지", "Customer"),
    (r"승인자|승인", "ApprovalAuthority"),
]


def load_yaml(path: pathlib.Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class Report:
    def __init__(self) -> None:
        self.fail = 0
        self.warn = 0

    def ok(self, msg: str) -> None:
        print(f"  [OK]   {msg}")

    def warning(self, msg: str) -> None:
        self.warn += 1
        print(f"  [WARN] {msg}")

    def failure(self, msg: str) -> None:
        self.fail += 1
        print(f"  [FAIL] {msg}")


def check_relations(model: dict, rep: Report) -> None:
    concepts = {c["id"] for c in model["concepts"]}
    subtypes = {s for c in model["concepts"] for s in c.get("subtypes", [])}
    known = concepts | subtypes
    print(f"[1] 관계 from/to 검사 — 개념 {len(concepts)}개, 하위 개념 {len(subtypes)}개, 관계 {len(model['relations'])}개")
    bad = 0
    for r in model["relations"]:
        for side in ("from", "to"):
            target = r[side]
            if target not in known:
                bad += 1
                rep.failure(f"{r['id']}.{side} = {target} — 정의되지 않은 개념")
    if bad == 0:
        rep.ok("모든 관계의 from/to 가 정의된 개념을 가리킵니다")
    derived = [r["id"] for r in model["relations"] if r.get("derived")]
    rep.ok(f"파생(derived) 관계 {len(derived)}개: {', '.join(derived)} — 저장하지 않고 계산합니다")
    # 하위 개념이 별도 개념으로도 중복 정의됐는지(RefundRequest 처럼 정의를 가진 경우는 허용) 확인
    dup = sorted(s for s in subtypes if s in concepts)
    rep.ok(f"정의 본문을 가진 하위 개념 {len(dup)}개: {', '.join(dup)}")
    # 다른 개념에서 계산되는 파생 개념(derived_from)은 원천 개념이 정의돼 있어야 한다
    for c in model["concepts"]:
        src = c.get("derived_from")
        if src and src not in known:
            rep.failure(f"{c['id']}.derived_from = {src} — 정의되지 않은 개념")
        elif src:
            rep.ok(f"파생 개념 {c['id']} — {src} 에서 계산. 저장하지 않습니다")
    # 관계에 한 번도 등장하지 않는 개념은 참조 표이거나 파생 개념이어야 한다
    linked = {r["from"] for r in model["relations"]} | {r["to"] for r in model["relations"]}
    isolated = [c["id"] for c in model["concepts"] if c["id"] not in linked and c["id"] not in subtypes]
    rep.ok(f"관계 없는 개념 {len(isolated)}개: {', '.join(isolated)} — 참조 표·파생·속성 후보인지 확인")


def check_glossary(model: dict, glossary: dict, rep: Report) -> None:
    concepts = {c["id"] for c in model["concepts"]}
    subtypes = {s for c in model["concepts"] for s in c.get("subtypes", [])}
    attrs: dict[str, list[str]] = defaultdict(list)
    for c in model["concepts"]:
        for a in c.get("attributes", []):
            attrs[a].append(c["id"])
    print(f"[2] 용어 사전 canonical 검사 — 용어 {len(glossary['terms'])}개")
    for t in glossary["terms"]:
        canon = t["canonical"]
        forms = len(t.get("surface_forms", []))
        if canon in concepts:
            rep.ok(f"{canon:<24} 개념        표현 {forms}개")
        elif canon in subtypes:
            rep.ok(f"{canon:<24} 하위 개념   표현 {forms}개")
        elif canon in attrs:
            rep.ok(f"{canon:<24} 속성({'/'.join(attrs[canon])})  표현 {forms}개")
        else:
            hint = [a for a in attrs if a.lower().startswith(canon.lower()[:4])]
            extra = f" — 유사 속성: {', '.join(hint)}" if hint else ""
            rep.warning(f"{canon:<24} 개념·속성 어디에도 없음{extra}")


def check_evidence(questions: dict, rep: Report) -> None:
    doc_ids = {}
    for p in sorted(DOCS_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^doc_id:\s*(\S+)", text, re.M)
        doc_ids[m.group(1) if m else p.stem] = p.name
    print(f"[3] 근거 문서 id 검사 — docs/ 문서 {len(doc_ids)}개, 질문 {len(questions['questions'])}개")
    used = defaultdict(int)
    bad = 0
    for q in questions["questions"]:
        for ev in q.get("evidence", []):
            doc = ev.split(":", 1)[0]
            if doc in doc_ids:
                used[doc] += 1
            else:
                bad += 1
                rep.failure(f"{q['id']} evidence {ev} — docs/ 에 없는 문서 id")
    if bad == 0:
        rep.ok("모든 evidence 의 문서 id 가 docs/ 에 존재합니다")
    ranked = sorted(doc_ids, key=lambda d: -used.get(d, 0))
    rep.ok("문서별 인용 횟수: " + ", ".join(f"{d}={used.get(d, 0)}" for d in ranked))
    for doc in doc_ids:
        if used.get(doc, 0) == 0:
            rep.warning(f"{doc} — 어떤 질문의 근거로도 쓰이지 않음")


def check_coverage(model: dict, questions: dict, rep: Report, verbose: bool) -> None:
    concepts = [c["id"] for c in model["concepts"]]
    subtype_of = {s: c["id"] for c in model["concepts"] for s in c.get("subtypes", [])}
    known = set(concepts) | set(subtype_of)
    for _, cid in KEYWORDS:
        if cid not in known:
            rep.failure(f"키워드 사전이 정의되지 않은 개념 {cid} 를 가리킵니다")
    print(f"[4] 질문 → 개념 매핑 — 키워드 {len(KEYWORDS)}개")
    print(f"  {'id':<4} {'유형':<4} 개념")
    hits: dict[str, set[str]] = defaultdict(set)
    for q in questions["questions"]:
        found: list[str] = []
        matched: list[str] = []
        for pattern, cid in KEYWORDS:
            m = re.search(pattern, q["question"])
            if m and cid not in found:
                found.append(cid)
                matched.append(m.group(0))
        for cid in found:
            hits[cid].add(q["id"])
            if cid in subtype_of:
                hits[subtype_of[cid]].add(q["id"])
        label = ", ".join(found) if found else "(없음)"
        print(f"  {q['id']:<4} {q['error_type']:<4} {label}")
        if verbose:
            print(f"       키워드: {', '.join(matched)}")
        if not found:
            rep.warning(f"{q['id']} 는 어느 개념에도 닿지 않습니다: {q['question']}")
    print("  개념별 등장 질문 수")
    for cid in concepts:
        subs = [s for s, p in subtype_of.items() if p == cid]
        sub_txt = "  ".join(f"{s}={len(hits.get(s, ()))}" for s in subs)
        line = f"  {cid:<24} {len(hits.get(cid, ())):>2}"
        print(f"{line}   {sub_txt}" if sub_txt else line)
    for cid in concepts:
        if not hits.get(cid):
            rep.warning(f"{cid} — 질문 문장에 직접 나타나지 않는 개념 (답변 과정에서만 쓰이는지 확인)")


def main() -> None:
    ap = argparse.ArgumentParser(description="04-5 최소 의미 모델 검증")
    ap.add_argument("--verbose", action="store_true", help="질문별 매칭 키워드 표시")
    args = ap.parse_args()

    model = load_yaml(MODEL_PATH)
    glossary = load_yaml(GLOSSARY_PATH)
    questions = load_yaml(QUESTIONS_PATH)
    rep = Report()

    print(f"model={MODEL_PATH.relative_to(ROOT).as_posix()}  glossary={GLOSSARY_PATH.relative_to(ROOT).as_posix()}  "
          f"questions={QUESTIONS_PATH.relative_to(ROOT).as_posix()}  as_of={questions['as_of']}")
    check_relations(model, rep)
    check_glossary(model, glossary, rep)
    check_evidence(questions, rep)
    check_coverage(model, questions, rep, args.verbose)

    print(f"결과: FAIL {rep.fail}건 / WARN {rep.warn}건")
    sys.exit(1 if rep.fail else 0)


if __name__ == "__main__":
    main()
