"""11-5: OKF 번들을 '에이전트처럼' 읽어 질문에 답하기 — 점진적 공개(progressive disclosure) 흉내.

데이터베이스도 규칙 코드도 쓰지 않습니다. 오직 마크다운 파일과 LLM 만으로:
  1. 루트 index.md 를 읽고
  2. 어느 폴더 index 를 열지 LLM 이 고르고
  3. 열어 볼 문서를 LLM 이 고르고 (최대 2단계 링크 따라가기)
  4. 모은 문서만으로 답한다

목적: 온톨로지를 OKF 로 내보냈을 때 "준용·유효기간" 같은 관계 정보가 문서 안에서 살아 있는지,
      규칙 코드 없이 문서만 읽는 에이전트가 Q09(학생 요금제 준용)와 Q30(4조 개정 영향)에 답할 수 있는지 확인.

실행:  python ch11/read_okf.py okf_bundle "학생 요금제를 연간으로 결제했는데 환불 기준이 어떻게 되나요?"
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import llm  # noqa: E402

PICK_SYSTEM = """당신은 OKF 위키 번들을 읽는 에이전트입니다. 아래 안내판(index)과 지금까지 읽은 문서를 보고,
질문에 답하기 위해 다음에 열어야 할 문서 경로를 JSON 으로 고르세요. 경로는 안내판이나 문서 링크에 나온 것만 씁니다.
형식: {"open": ["/concepts/Xxx.md", ...], "reason": "..."}  (최대 5개. 더 열 필요가 없으면 빈 목록)"""

ANSWER_SYSTEM = """당신은 OKF 위키 번들만 근거로 답하는 도우미입니다. 읽은 문서 밖의 지식을 쓰지 마세요.
형식: 결론 / 적용 조건 / 근거(문서 경로) / 미확인 사항. 문서의 x_valid_to 가 있으면 현재 유효하지 않은 판이므로 그 사실을 밝히세요."""


def read(bundle: pathlib.Path, rel: str) -> str:
    p = bundle / rel.lstrip("/")
    return p.read_text(encoding="utf-8") if p.exists() else f"(없음: {rel})"


def answer(bundle: pathlib.Path, question: str, as_of: str = "2025-09-08", hops: int = 3) -> dict:
    opened: dict[str, str] = {"/index.md": read(bundle, "/index.md")}
    trail = ["/index.md"]
    for _ in range(hops):
        ctx = "\n\n".join(f"=== {p}\n{t}" for p, t in opened.items())
        pick = llm.chat_json(PICK_SYSTEM, f"오늘: {as_of}\n질문: {question}\n\n읽은 문서:\n{ctx}")
        new = [p for p in pick.get("open", []) if p not in opened][:5]
        if not new:
            break
        for p in new:
            opened[p] = read(bundle, p)
            trail.append(p)
    ctx = "\n\n".join(f"=== {p}\n{t}" for p, t in opened.items() if not p.endswith("index.md"))
    text = llm.chat(ANSWER_SYSTEM, f"오늘: {as_of}\n\n읽은 문서:\n{ctx}\n\n질문: {question}")
    return {"answer": text, "trail": trail, "tokens": llm.usage.as_dict()}


if __name__ == "__main__":
    bundle = pathlib.Path(sys.argv[1])
    q = sys.argv[2]
    res = answer(bundle, q)
    print("읽은 경로:", " → ".join(res["trail"]))
    print(res["answer"])
    print("usage:", res["tokens"])
