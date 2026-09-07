"""구성 1: 문서 검색 기반 기본 RAG (01-4, 08-2).

- 규정 문서 6종을 고정 길이로 자르고(청크) pgvector 에 저장
- 질문과 가장 가까운 청크 5개를 골라 LLM 에 그대로 전달
- 메타데이터·정형 데이터·규칙 처리 없음. 많은 RAG 서비스의 첫 버전 모습입니다.

실행:
  python ch01/rag_basic.py --index          # 청크 생성·임베딩 저장 (1회)
  python ch01/rag_basic.py --ask "질문"      # 한 질문
  python ch01/rag_basic.py --eval           # 30개 질문 채점 → results/config1_basic.jsonl
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db, llm, evaluate  # noqa: E402

DOCS_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "docs"
TABLE = "chunks_basic"
CHUNK_CHARS = 500
TOP_K = 5

SYSTEM = (
    "당신은 노트클라우드 고객지원 담당자를 돕는 도우미입니다. "
    "아래 참고 문서만 근거로 한국어로 간결하게 답하세요. "
    "금액을 답할 때는 숫자를 명시하세요."
)


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip()
    return text


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """문단 경계를 존중하면서 size 글자 안팎으로 자릅니다."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 > size and buf:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def build_index() -> None:
    with db.connect() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(
            f"""CREATE TABLE {TABLE} (
                   id serial PRIMARY KEY,
                   doc_id text NOT NULL,
                   chunk_no int NOT NULL,
                   content text NOT NULL,
                   embedding vector({llm.EMBEDDING_DIM}) NOT NULL)"""
        )
        total = 0
        for path in sorted(DOCS_DIR.glob("*.md")):
            text = strip_frontmatter(path.read_text(encoding="utf-8"))
            chunks = chunk_text(text)
            vecs = llm.embed(chunks)
            for i, (c, v) in enumerate(zip(chunks, vecs)):
                conn.execute(
                    f"INSERT INTO {TABLE}(doc_id, chunk_no, content, embedding) VALUES (%s,%s,%s,%s)",
                    (path.stem, i, c, v),
                )
            total += len(chunks)
            print(f"{path.stem:22s} {len(chunks):3d} chunks")
        print(f"총 {total} chunks / 임베딩 토큰 {llm.usage.embed_tokens:,}")


def retrieve(question: str, k: int = TOP_K) -> list[dict]:
    qv = llm.embed([question])[0]
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT doc_id, chunk_no, content, 1 - (embedding <=> %s::vector) AS score
                FROM {TABLE} ORDER BY embedding <=> %s::vector LIMIT %s""",
            (qv, qv, k),
        ).fetchall()
    return [{"doc_id": r[0], "chunk_no": r[1], "content": r[2], "score": round(float(r[3]), 3)} for r in rows]


def answer(question: str, as_of: date) -> dict:
    ctx = retrieve(question)
    context_text = "\n\n".join(f"[{c['doc_id']}#{c['chunk_no']}]\n{c['content']}" for c in ctx)
    user = f"오늘 날짜: {as_of}\n\n참고 문서:\n{context_text}\n\n질문: {question}"
    text = llm.chat(SYSTEM, user)
    return {"answer": text, "contexts": [f"{c['doc_id']}#{c['chunk_no']}({c['score']})" for c in ctx]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--ask")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    if a.index:
        build_index()
    if a.ask:
        as_of, _ = evaluate.load_questions()
        res = answer(a.ask, as_of)
        print("검색된 청크:", res["contexts"])
        print(res["answer"])
    if a.eval:
        evaluate.run("config1_basic", answer, only=a.only)
