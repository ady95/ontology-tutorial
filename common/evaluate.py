"""평가 도구 (03-4): 같은 질문 세트로 여러 구성을 채점합니다.

파이프라인은 `answer(question: str, as_of: date) -> dict` 형태의 함수면 무엇이든 됩니다.
반환 dict 는 최소한 {"answer": str} 을 갖고, 선택적으로
{"status": "answer"|"hold", "evidence": [...], "contexts": [...]} 를 가질 수 있습니다.

채점 규칙 (규칙 기반, LLM 없음 → 재현 가능·무료):
  1. key_facts 가 모두 답변에 포함되면 정답 후보 ('표현1|표현2' 는 동의 표현: 하나만 있어도 충족)
  2. forbidden_facts 중 하나라도 포함되면 오답
  3. numeric_answer 가 있으면 답변의 숫자 중 tolerance 안의 값이 있어야 함
  4. expected_status 가 hold 인 질문은 답변이 확인 요청·보류 성격이어야 함 (key_facts 로 표현)

실행 예:  python -m common.evaluate results/config1.jsonl            (저장된 결과 재채점, 화면 출력만)
          python -m common.evaluate results/config1.jsonl --rewrite  (재채점 결과를 파일에 반영)
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import time
from collections import defaultdict
from datetime import date
from typing import Callable

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "data" / "eval" / "questions.yaml"
RESULTS_DIR = ROOT / "results"

ERROR_TYPE_LABEL = {"-": "기준선", "R": "검색", "D": "데이터", "M": "의미", "K": "규칙"}


def load_questions(path: pathlib.Path = QUESTIONS) -> tuple[date, list[dict]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    as_of = data["as_of"]
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    return as_of, data["questions"]


def _norm(s: str) -> str:
    s = s.replace(",", "").replace(" ", "")
    return s.lower()


def _numbers(s: str) -> list[float]:
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", s):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return out


def grade(q: dict, answer: str) -> dict:
    a = _norm(answer or "")
    # key_facts 항목은 '표현1|표현2' 처럼 동의 표현을 '|' 로 나열할 수 있다 (하나라도 있으면 충족)
    missing = [k for k in q.get("key_facts", []) if not any(_norm(alt) in a for alt in str(k).split("|"))]
    # 금지 표현은 공백을 유지한 채 비교한다 ("환불 가능" 이 공백 제거 후 "불가능" 에 걸리는 오판 방지)
    a_sp = (answer or "").replace(",", "").lower()
    forbidden = [k for k in q.get("forbidden_facts", []) if k.replace(",", "").lower() in a_sp]
    numeric_ok = True
    if "numeric_answer" in q:
        tol = q.get("numeric_tolerance", 0)
        numeric_ok = any(abs(n - q["numeric_answer"]) <= tol for n in _numbers(answer or ""))
    correct = not missing and not forbidden and numeric_ok
    return {"correct": correct, "missing": missing, "forbidden": forbidden, "numeric_ok": numeric_ok}


def run(config_name: str, pipeline: Callable[[str, date], dict], *, only: list[str] | None = None,
        verbose: bool = True) -> list[dict]:
    """질문 30개를 파이프라인에 넣고 채점 결과를 results/<config>.jsonl 에 저장합니다."""
    from common import llm  # 지연 임포트: 채점만 할 때는 LLM 모듈이 필요 없음

    as_of, questions = load_questions()
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{config_name}.jsonl"
    rows: list[dict] = []
    llm.usage.reset()
    with out_path.open("w", encoding="utf-8") as f:
        for q in questions:
            if only and q["id"] not in only:
                continue
            before = llm.usage.as_dict()
            t = time.time()
            try:
                res = pipeline(q["question"], as_of)
            except Exception as e:  # 파이프라인 오류도 결과로 남긴다
                res = {"answer": f"[ERROR] {e}", "status": "error"}
            elapsed = time.time() - t
            after = llm.usage.as_dict()
            g = grade(q, res.get("answer", ""))
            row = {
                "config": config_name,
                "id": q["id"],
                "error_type": q["error_type"],
                "needs_data": q["needs_data"],
                "expected_status": q.get("expected_status", "answer"),
                "question": q["question"],
                "answer": res.get("answer", ""),
                "status": res.get("status", "answer"),
                "evidence": res.get("evidence", []),
                "contexts": res.get("contexts", []),
                **g,
                "seconds": round(elapsed, 2),
                "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
                "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
                "calls": after["calls"] - before["calls"],
            }
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if verbose:
                mark = "O" if g["correct"] else "X"
                detail = "" if g["correct"] else f"  누락={g['missing']} 금지={g['forbidden']} 숫자={'OK' if g['numeric_ok'] else '불일치'}"
                print(f"{mark} {q['id']} [{q['error_type']}] {elapsed:5.1f}s{detail}")
    print()
    print(summary(rows))
    print(f"결과 저장: {out_path}")
    return rows


def summary(rows: list[dict]) -> str:
    by_type: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_type[r["error_type"]].append(r["correct"])
    lines = ["유형별 정답률"]
    for t in ["-", "R", "D", "M", "K"]:
        if t in by_type:
            v = by_type[t]
            lines.append(f"  {ERROR_TYPE_LABEL[t]:4s}({t}) {sum(v):2d}/{len(v):2d}")
    total = sum(r["correct"] for r in rows)
    lines.append(f"  전체        {total:2d}/{len(rows):2d}")
    tok = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows)
    sec = sum(r["seconds"] for r in rows)
    lines.append(f"토큰 {tok:,} / 소요 {sec:.1f}s / LLM 호출 {sum(r['calls'] for r in rows)}회")
    return "\n".join(lines)


def compare(*config_names: str) -> str:
    """여러 구성의 결과 파일을 질문별로 나란히 놓은 표(마크다운)를 만듭니다."""
    tables = {}
    for name in config_names:
        p = RESULTS_DIR / f"{name}.jsonl"
        tables[name] = {json.loads(l)["id"]: json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()}
    _, questions = load_questions()
    head = "| 질문 | 유형 | " + " | ".join(config_names) + " |"
    sep = "|---|---|" + "|".join("---" for _ in config_names) + "|"
    lines = [head, sep]
    for q in questions:
        cells = []
        for name in config_names:
            r = tables[name].get(q["id"])
            cells.append("-" if r is None else ("O" if r["correct"] else "X"))
        lines.append(f"| {q['id']} | {q['error_type']} | " + " | ".join(cells) + " |")
    totals = [str(sum(1 for r in tables[n].values() if r["correct"])) + f"/{len(tables[n])}" for n in config_names]
    lines.append("| **정답 수** | | " + " | ".join(totals) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = pathlib.Path(sys.argv[1])
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        _, qs = load_questions()
        qmap = {q["id"]: q for q in qs}
        changed = []
        for r in rows:
            before = r["correct"]
            r.update(grade(qmap[r["id"]], r["answer"]))
            if before != r["correct"]:
                changed.append(f"{r['id']}:{'X→O' if r['correct'] else 'O→X'}")
        print(summary(rows))
        if changed:
            print("판정 변경:", ", ".join(changed))
        if "--rewrite" in sys.argv:
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            print("파일 갱신:", path)
    else:
        as_of, qs = load_questions()
        print(f"기준일 {as_of}, 질문 {len(qs)}개")
        for q in qs:
            print(f"{q['id']} [{q['error_type']}] {'D' if q['needs_data'] else ' '} {q['question']}")
