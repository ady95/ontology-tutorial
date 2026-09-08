"""부록 A·B 생성 — 질문 세트와 결과 파일에서 표를 만든다.

책의 부록은 손으로 적지 않고 이 스크립트로 생성한다. 수치가 결과 파일과 어긋나지 않게 하기 위해서다.
검수자가 표를 재현할 때도 이 스크립트를 쓴다.

실행:
  python ch08/make_appendix.py            # 표를 화면에 출력
  python ch08/make_appendix.py <원고폴더>  # <원고폴더>/pages/99-1·99-2 파일로 저장
"""
from __future__ import annotations

import io
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import evaluate  # noqa: E402

NAMES = ["config1_basic", "config2_search", "config3_sql", "config4_semantic", "config5_graph"]
LABELS = ["구성 1", "구성 2", "구성 3", "구성 4", "구성 5"]
CALLS = {"config1_basic": 30, "config2_search": 30, "config3_sql": 30, "config4_semantic": 60, "config5_graph": 60}
TYPE_LABEL = {"-": "기준선", "R": "검색", "D": "데이터", "M": "의미", "K": "규칙"}


def appendix_a() -> str:
    as_of, qs = evaluate.load_questions()
    out = ["[TOC]", "", f"> **[참고]** 예제 저장소 data/eval/questions.yaml 에서 생성한 표 (기준일 {as_of})", "",
           "책 전체의 실험에 쓰는 업무 질문 30개의 전체 목록입니다. 유형은 01-2의 기준으로 정한, 기본 RAG에서 예상되는 주된 오류 유형입니다. 근거는 조항 ID(문서 ID:조.항)이며 6-3의 조항 테이블과 같은 체계입니다. 이 표는 YAML 파일에서 스크립트(ch08/make_appendix.py)로 생성했으므로 파일이 원본입니다.", "",
           "금액 정답은 중간값을 반올림하지 않고 최종 금액만 한 번 반올림한 값입니다(07-2).", "",
           "### 질문·유형·정답 상태", "", "| ID | 유형 | 데이터 | 상태 | 질문 |", "|---|---|---|---|---|"]
    for q in qs:
        out.append(f"| {q['id']} | {TYPE_LABEL[q['error_type']]} | {'필요' if q['needs_data'] else '-'} | "
                   f"{'보류' if q.get('expected_status') == 'hold' else '결론'} | {q['question']} |")
    out += ["", "### 정답과 근거", ""]
    for q in qs:
        out += [f"#### {q['id']}. {q['question']}", "", q["answer"].strip(), ""]
        line = f"- 핵심 사실: {', '.join(q.get('key_facts', []))}"
        if q.get("forbidden_facts"):
            line += f" / 금지 표현: {', '.join(q['forbidden_facts'])}"
        if "numeric_answer" in q:
            line += f" / 숫자 정답: {q['numeric_answer']:,} (오차 {q.get('numeric_tolerance', 0)})"
        out += [line, f"- 근거: {', '.join(q['evidence'])}", ""]
    c = Counter(q["error_type"] for q in qs)
    out += ["### 유형별 집계", "", "| 유형 | 질문 수 |", "|---|---|"]
    out += [f"| {TYPE_LABEL[k]}({k}) | {c[k]} |" for k in ["-", "R", "D", "M", "K"]]
    out.append(f"| 정형 데이터 필요 | {sum(q['needs_data'] for q in qs)} |")
    out.append(f"| 보류가 정답 | {sum(q.get('expected_status') == 'hold' for q in qs)} |")
    return "\n".join(out) + "\n"


def appendix_b() -> str:
    aggs = {n: evaluate.aggregate(n, 3) for n in NAMES}
    _, qs = evaluate.load_questions()
    stable = {n: {q for q, d in aggs[n].items() if d["hits"] == d["runs"]} for n in NAMES}
    out = ["[TOC]", "", "> **[검증]** ontology-tutorial 다섯 구성 각 3회 실행 결과에서 스크립트로 생성 · 수치는 모델·환경에 따라 달라지므로 경향에 주목", "",
           "8장과 9장에서 비교한 다섯 구성의 전체 결과표입니다. 각 칸은 3회 실행 중 정답 횟수입니다. 결과 파일은 예제 저장소의 results 폴더에 실행할 때마다 다시 만들어지며, 이 표는 집필 시점의 실행을 옮긴 것입니다.", "",
           "### 구성 정의", "", "| 구성 | 이름 | 내용 |", "|---|---|---|",
           "| 1 | config1_basic | 500자 청크 벡터 검색 |",
           "| 2 | config2_search | 조항 단위 + 메타데이터 + 하이브리드 검색 + 유효 판 필터 |",
           "| 3 | config3_sql | 구성 2 + 정형 데이터 조회 + 규칙(현재 규정 기준) |",
           "| 4 | config4_semantic | 구성 3 + 의미 모델(용어·규정 판·준용·근거·보류·행동 제어) |",
           "| 5 | config5_graph | 구성 4 + 영향 범위 질문에 재귀 CTE 그래프 탐색 |", "",
           "### 질문별 정답 횟수 (3회 실행)", "", "| 질문 | 유형 | " + " | ".join(LABELS) + " |",
           "|---|---|" + "|".join("---" for _ in LABELS) + "|"]
    for q in qs:
        cells = [f"{aggs[n][q['id']]['hits']}/{aggs[n][q['id']]['runs']}" if q["id"] in aggs[n] else "-" for n in NAMES]
        out.append(f"| {q['id']} | {TYPE_LABEL[q['error_type']]} | " + " | ".join(cells) + " |")
    out += ["| **안정 정답 (3/3)** | | " + " | ".join(str(len(stable[n])) for n in NAMES) + " |",
            "| **불안정 (1~2/3)** | | " + " | ".join(str(sum(1 for d in aggs[n].values() if 0 < d["hits"] < d["runs"])) for n in NAMES) + " |",
            "| **평균 정답** | | " + " | ".join(f"{sum(d['hits'] for d in aggs[n].values()) / 3:.1f}" for n in NAMES) + " |", "",
            "### 유형별 평균 정답", "", "| 유형 (문항 수) | " + " | ".join(LABELS) + " |", "|---|" + "|".join("---" for _ in LABELS) + "|"]
    for t in ["-", "R", "D", "M", "K"]:
        ids = [q["id"] for q in qs if q["error_type"] == t]
        out.append(f"| {TYPE_LABEL[t]} ({len(ids)}) | " + " | ".join(f"{sum(aggs[n][i]['hits'] for i in ids) / 3:.1f}" for n in NAMES) + " |")
    out += ["", "### 안정 정답의 변화", "",
            '안정 정답은 3회 모두 맞힌 질문입니다. "직전 대비 신규"는 바로 앞 구성과 비교한 것이고, "전체에서 최초"는 앞선 모든 구성을 통틀어 처음 안정 정답이 된 것입니다. 한 번 빠졌다가 돌아오는 질문이 있어 두 값이 다릅니다. 08-5의 귀속 분석은 "전체에서 최초" 열을 기준으로 합니다.', "",
            "| 구성 | 안정 정답 | 직전 대비 신규 | 직전 대비 이탈 | 전체에서 최초 | 평균 정답 | 평균 순증 |", "|---|---|---|---|---|---|---|"]
    prev, union, prev_avg = set(), set(), None
    rows = []
    for n, l in zip(NAMES, LABELS):
        s = stable[n]
        new, lost, first = sorted(s - prev), sorted(prev - s), sorted(s - union)
        avg = sum(d["hits"] for d in aggs[n].values()) / 3
        out.append(f"| {l} | {len(s)} | {len(new)} | {len(lost)} | {len(first)} | {avg:.1f} | "
                   f"{'' if prev_avg is None else f'{avg - prev_avg:+.1f}'} |")
        rows.append((l, new, lost, first))
        prev, union, prev_avg = s, union | s, avg
    out += ["", "질문 목록입니다.", "", "| 구성 | 직전 대비 신규 | 직전 대비 이탈 | 전체에서 최초 |", "|---|---|---|---|"]
    for l, new, lost, first in rows:
        out.append(f"| {l} | {', '.join(new) or '없음'} | {', '.join(lost) or '없음'} | {', '.join(first) or '없음'} |")
    out += ["", "### 비용 (30문항 1회 실행 평균)", "", "| 구성 | LLM 호출 | 토큰 | 소요 시간 |", "|---|---|---|---|"]
    for n, l in zip(NAMES, LABELS):
        a = aggs[n]
        out.append(f"| {l} | {CALLS[n]} | {sum(d['tokens'] for d in a.values()) / 3:,.0f} | {sum(d['seconds'] for d in a.values()) / 3:.0f}초 |")
    out += ["", "### 읽는 법", "",
            "- 질문 정의와 정답은 부록 A, 유형 기준은 01-2에 있습니다.",
            '- 구성마다 여러 요소가 함께 바뀌므로, "전체에서 최초" 열은 그 구성에서 무엇이 풀렸는지를 보여 줄 뿐 요소 하나의 독립 기여를 뜻하지 않습니다(08-5).',
            "- 1~2/3인 칸은 실행에 따라 결과가 달라진 질문입니다. 판단의 편차인지 표현의 편차인지는 08-5와 09-3에서 질문별로 설명했습니다.",
            "- 채점 핵심어는 구성 1~3의 첫 실행 뒤 한 차례 정비했고, 그 뒤 모든 구성을 같은 기준으로 재채점했습니다(08-1).",
            "- 이 실행에 쓴 모델과 설정은 08-1의 실험 조건 표에 있습니다. 구성 4·5의 회차별 코드 차이는 09-3에 있습니다.", "",
            "이 표는 결과 파일에서 스크립트로 생성했습니다. 생성 코드는 예제 저장소의 ch08/make_appendix.py 입니다."]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    a, b = appendix_a(), appendix_b()
    if len(sys.argv) > 1:
        book = pathlib.Path(sys.argv[1]) / "pages"
        for name, text in [("99-1-부록A-실습-질문-세트-30개-전체와-정답-근거.md", a),
                           ("99-2-부록B-다섯-가지-구성-비교-결과표.md", b)]:
            io.open(book / name, "w", encoding="utf-8", newline="\n").write(text)
            print("생성:", book / name)
    else:
        print(b)
