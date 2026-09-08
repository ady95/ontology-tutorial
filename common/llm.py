"""LLM·임베딩 호출 공통 모듈 (OpenAI API 호환).

모든 장의 실험이 같은 모델·같은 온도(0)를 쓰도록 여기서 고정합니다.
호출 횟수·토큰·소요 시간을 누적해 비용 비교(08-6)에 사용합니다.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv
from openai import BadRequestError, OpenAI

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(os.environ.get("ENV_FILE", ROOT / ".env"))

LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.6-luna")  # 이 책의 모든 수치를 낸 모델
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1536"))

_client: OpenAI | None = None
_supports_temperature = True   # 모델이 temperature 를 거부하면 False 로 바뀐다


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None)
    return _client


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embed_tokens: int = 0
    seconds: float = 0.0
    log: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "embed_tokens": self.embed_tokens,
            "seconds": round(self.seconds, 2),
        }

    def reset(self) -> None:
        self.calls = self.prompt_tokens = self.completion_tokens = self.embed_tokens = 0
        self.seconds = 0.0
        self.log.clear()


usage = Usage()


def chat(system: str, user: str, *, json_mode: bool = False, temperature: float = 0.0) -> str:
    t = time.time()
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    global _supports_temperature
    try:
        if _supports_temperature:
            resp = client().chat.completions.create(model=LLM_MODEL, temperature=temperature, messages=messages, **kwargs)
        else:
            resp = client().chat.completions.create(model=LLM_MODEL, messages=messages, **kwargs)
    except BadRequestError as e:
        # 일부 모델(추론 모델 계열)은 temperature 를 받지 않는다 → 기본값으로 재시도하고 이후 호출부터 생략
        if "temperature" not in str(e):
            raise
        _supports_temperature = False
        resp = client().chat.completions.create(model=LLM_MODEL, messages=messages, **kwargs)
    usage.calls += 1
    usage.prompt_tokens += resp.usage.prompt_tokens
    usage.completion_tokens += resp.usage.completion_tokens
    usage.seconds += time.time() - t
    return resp.choices[0].message.content or ""


def chat_json(system: str, user: str) -> dict:
    text = chat(system, user, json_mode=True)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    t = time.time()
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        batch = texts[i : i + 100]
        resp = client().embeddings.create(model=EMBEDDING_MODEL, input=batch, dimensions=EMBEDDING_DIM)
        usage.embed_tokens += resp.usage.total_tokens
        out.extend(d.embedding for d in resp.data)
    usage.seconds += time.time() - t
    return out


if __name__ == "__main__":
    print("model:", LLM_MODEL, "/ embedding:", EMBEDDING_MODEL, EMBEDDING_DIM)
    print(chat("한 문장으로 답하세요.", "온톨로지를 한 문장으로 설명해 주세요."))
    v = embed(["환불 규정"])[0]
    print("embedding dim:", len(v))
    print("usage:", usage.as_dict())
