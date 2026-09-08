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
        verbose: bool = True, questions_path: pathlib.Path | None = None) -> list[dict]:
    """질문 세트를 파이프라인에 넣고 채점 결과를 results/<config>.jsonl 에 저장합니다.
    questions_path 를 주면 다른 질문 세트(예: 10장 data/tax/questions.yaml)로 채점합니다."""
    from common import llm  # 지연 임포트: 채점만 할 때는 LLM 모듈이 필요 없음

    as_of, questions = load_questions(questions_path or QUESTIONS)
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
                "model": llm.LLM_MODEL,              # 어떤 모델로 낸 수치인지 결과에 남긴다
                "embedding_model": llm.EMBEDDING_MODEL,
                "id": q["id"],
                "error_type": q["error_type"],
                "needs_data": q["needs_data"],
                "expected_status": q.get("expected_status", "answer"),
                "question": q["question"],
                "answer": res.get("answer", ""),
                "status": res.get("status", "answer"),
                "evidence": res.get("evidence", []),      # 시스템이 고른 근거 조항
                "cited": res.get("cited", []),            # 답변이 실제로 인용한 조항 (11-3 진단용)
                "rules_fired": res.get("rules_fired", []),
                "decision_trace": res.get("decision_trace", []),
                "analysis": res.get("analysis", {}),
                "contexts": res.get("contexts", []),
                **g,
                "seconds": round(elapsed, 2),
                "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
                "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
                "calls": after["calls"] - before["calls"],
                "facts": res.get("facts"),
            }
            rows.append(row)
            # facts 에 date 등 JSON 이 모르는 타입이 섞이므로 문자열로 떨어뜨린다
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
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
            lines.append(f"  {ERROR_TYPE_LABEL.get(t, t):4s}({t}) {sum(v):2d}/{len(v):2d}")
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


def aggregate(config_name: str, runs: int = 3) -> dict:
    """config_r1..rN 결과를 모아 질문별 정답 횟수를 센다. 반환: {qid: {"hits": n, "runs": N}}"""
    out: dict[str, dict] = {}
    for i in range(1, runs + 1):
        p = RESULTS_DIR / f"{config_name}_r{i}.jsonl"
        if not p.exists():
            continue
        for l in p.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            r = json.loads(l)
            d = out.setdefault(r["id"], {"hits": 0, "runs": 0, "type": r["error_type"], "tokens": 0, "seconds": 0.0})
            d["runs"] += 1
            d["hits"] += int(r["correct"])
            d["tokens"] += r["prompt_tokens"] + r["completion_tokens"]
            d["seconds"] += r["seconds"]
    return out


def compare_runs(*config_names: str, runs: int = 3, questions_path: pathlib.Path | None = None) -> str:
    """구성별로 N회 중 정답 횟수를 표로. 3/3 = 안정 정답, 0/3 = 안정 오답, 그 사이 = 불안정."""
    aggs = {n: aggregate(n, runs) for n in config_names}
    _, questions = load_questions(questions_path or QUESTIONS)
    head = "| 질문 | 유형 | " + " | ".join(config_names) + " |"
    lines = [head, "|---|---|" + "|".join("---" for _ in config_names) + "|"]
    for q in questions:
        cells = []
        for n in config_names:
            d = aggs[n].get(q["id"])
            cells.append("-" if not d else f"{d['hits']}/{d['runs']}")
        lines.append(f"| {q['id']} | {q['error_type']} | " + " | ".join(cells) + " |")
    tot = []
    for n in config_names:
        a = aggs[n]
        stable = sum(1 for d in a.values() if d["hits"] == d["runs"])
        unstable = sum(1 for d in a.values() if 0 < d["hits"] < d["runs"])
        mean = sum(d["hits"] for d in a.values()) / max(1, runs)
        tot.append(f"안정 {stable} / 불안정 {unstable} / 평균 {mean:.1f}")
    lines.append("| **집계** | | " + " | ".join(tot) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = pathlib.Path(sys.argv[1])
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        qpath = pathlib.Path(sys.argv[sys.argv.index("--questions") + 1]) if "--questions" in sys.argv else QUESTIONS
        _, qs = load_questions(qpath)
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
