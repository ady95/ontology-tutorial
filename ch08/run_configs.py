"""08장: 다섯 구성 비교 실험을 한 번에 실행하고 비교표를 만듭니다.

같은 모델(LLM_MODEL), 같은 문서(data/docs), 같은 질문(data/eval/questions.yaml) 을 쓰고
구성 요소만 바꿉니다. 각 구성은 results/<config>.jsonl 에 저장됩니다.

  config1_basic     ch01/rag_basic.py     500자 청크 벡터 검색
  config2_search    ch08/configs.py       조항 청크 + 메타데이터 + 하이브리드 검색 + 유효 판 필터
  config3_sql       ch08/configs.py       구성 2 + SQL 조회 + 규칙(현재 규정 기준)
  config4_semantic  ch07/pipeline.py      구성 3 + 의미 모델(용어·규정 판·준용·근거·보류)
  config5_graph     ch09/graph_pipeline.py 구성 4 + 영향 범위 질문을 재귀 CTE 그래프 탐색으로

실행:  python ch08/run_configs.py                 # 전부
       python ch08/run_configs.py --configs config2_search config3_sql
       python ch08/run_configs.py --compare-only  # 저장된 결과로 표만
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import evaluate  # noqa: E402

ORDER = ["config1_basic", "config2_search", "config3_sql", "config4_semantic", "config5_graph"]


def pipeline_for(name: str):
    if name == "config1_basic":
        from ch01 import rag_basic
        return rag_basic.answer
    if name in ("config2_search", "config3_sql"):
        from ch08 import configs
        return configs.CONFIGS[name]
    if name == "config4_semantic":
        from ch07 import pipeline
        return pipeline.answer
    if name == "config5_graph":
        from ch09 import graph_pipeline
        return graph_pipeline.answer
    raise KeyError(name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", nargs="*", default=ORDER)
    ap.add_argument("--compare-only", action="store_true")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--runs", type=int, default=1, help="반복 실행 횟수 (결과 파일은 <config>_r<i>.jsonl)")
    ap.add_argument("--start", type=int, default=1, help="반복 시작 번호 (이전 회차를 보존하고 이어서 돌릴 때)")
    a = ap.parse_args()
    if not a.compare_only:
        for i in range(a.start, a.start + a.runs):
            for name in a.configs:
                suffix = f"_r{i}" if (a.runs > 1 or a.start > 1) else ""
                print(f"\n===== {name}{suffix} =====")
                evaluate.run(name + suffix, pipeline_for(name), only=a.only)
    existing = [n for n in a.configs if (evaluate.RESULTS_DIR / f"{n}.jsonl").exists()]
    print()
    if existing:
        print(evaluate.compare(*existing))
    if any((evaluate.RESULTS_DIR / f"{n}_r1.jsonl").exists() for n in a.configs):
        print()
        print(evaluate.compare_runs(*a.configs, runs=max(a.runs + a.start - 1, 3)))
