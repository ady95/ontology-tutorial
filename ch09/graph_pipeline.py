"""구성 5 (09-3): 구성 4 + 필요한 일부만 그래프 탐색.

구성 4와 같은 파이프라인을 쓰되, "영향 범위" 성격의 질문(어떤 조항이 바뀌면 무엇을 점검해야 하는가,
어떤 구독을 재점검해야 하는가)에서만 재귀 CTE(ch09/graph_cte.py)로 링크 그래프를 깊이 있게 탐색합니다.
그 밖의 질문은 구성 4와 완전히 같습니다 — 그래프가 유리한 질문 유형이 어디인지 보여 주는 것이 목적입니다.

실행:  python ch09/graph_pipeline.py --eval     # → results/config5_graph.jsonl
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import llm, evaluate, retrieval  # noqa: E402
from ch07 import pipeline, rules  # noqa: E402
from ch09 import graph_cte  # noqa: E402


def impact_context(clause_id: str) -> tuple[str, list[str]]:
    """재귀 CTE 로 조항·조 단위 영향 범위를 모아 사람이 읽을 수 있는 목록으로."""
    rows = graph_cte.impact(clause_id, max_depth=3)
    article = clause_id.rsplit(".", 1)[0] if "." in clause_id.split(":")[1] else None
    if article and article != clause_id:
        rows += graph_cte.impact(article, max_depth=3)        # 항 → 그 조를 가리키는 링크(준용 등)
    from ch06 import api
    for sub in api.sub_clauses(clause_id):                       # 조 → 그 항들을 가리키는 링크(인용 등)
        rows += graph_cte.impact(sub, max_depth=3)
    fwd = graph_cte.forward(clause_id, rels=("supersedes",), max_depth=2)
    lines, ids = [], []
    for depth, rel, dep, path in sorted(set(rows)):
        lines.append(f"- 깊이 {depth} [{rel}] {dep}  ({path})")
        if dep not in ids:
            ids.append(dep)
    for depth, rel, f, t, path in fwd:
        lines.append(f"- 이 조항이 대체한 이전 판 조항: {t}")
    return "\n".join(lines), ids


def answer(question: str, as_of: date) -> dict:
    a = pipeline.analyze(question, as_of)
    if a.get("request_type") != "ImpactAnalysis" or not a.get("clause_hint"):
        # 구성 4와 동일 (분석 결과 재사용을 위해 내부 함수를 그대로 호출)
        verdict, evidence_rows, kind = pipeline.decide(question, a, as_of)
        res = _compose(question, a, verdict, evidence_rows, kind, as_of)
        return res
    text, ids = impact_context(a["clause_hint"])
    v = rules.Verdict(result="그래프 탐색으로 찾은 영향 범위:\n" + text, evidence=["ops_manual:6"] + ids)
    evidence_rows = pipeline.expand_evidence(v.evidence)
    return _compose(question, a, v, evidence_rows, "영향 범위(그래프)", as_of)


def _compose(question, a, verdict, evidence_rows, kind, as_of) -> dict:
    """pipeline.answer 의 4·5단계와 같다 (분석 단계만 앞에서 끝냈으므로 분리)."""
    concept_ids = list(dict.fromkeys(a.get("concepts", []) or []))
    if verdict is None:
        rows = retrieval.hybrid_search(question, 6, valid_on=as_of)
        evidence_rows = pipeline.expand_evidence([r["clause_id"] for r in rows])
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
            + (f"관련 개념 정의:\n{pipeline.concept_text(concept_ids)}\n\n" if concept_ids else "")
            + f"근거 조항:\n{retrieval.format_context(evidence_rows)}\n\n{judgement}\n\n질문: {question}")
    text = llm.chat(pipeline.ANSWER_SYSTEM, user)
    return {"answer": text, "status": status, "evidence": evid, "missing": missing,
            "cited": pipeline.cited_clauses(text), "rules_fired": (verdict.rules_fired if verdict else []),
            "analysis": a, "contexts": [r["clause_id"] for r in evidence_rows]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    if args.eval:
        evaluate.run("config5_graph", answer, only=args.only)
    else:
        print(impact_context("refund_policy_v2:4.1")[0])
