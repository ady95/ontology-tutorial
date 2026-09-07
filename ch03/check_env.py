"""03-2 연결 확인: PostgreSQL(pgvector)과 LLM API가 모두 준비됐는지 점검합니다.

실행:  python ch03/check_env.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db, llm  # noqa: E402


def main() -> None:
    print("[1] PostgreSQL 접속")
    with db.connect() as conn:
        ver = conn.execute("select version()").fetchone()[0]
        vec = conn.execute("select extversion from pg_extension where extname='vector'").fetchone()[0]
        age = conn.execute("select 1 from pg_available_extensions where name='age'").fetchone()
    print("    ", ver.split(",")[0])
    print("     pgvector:", vec, "/ Apache AGE:", "사용 가능" if age else "없음 (09장 AGE 절만 영향)")

    print("[2] LLM 호출:", llm.LLM_MODEL)
    answer = llm.chat("한 문장으로 답하세요.", "PostgreSQL을 한 문장으로 설명해 주세요.")
    print("    ", answer.strip())

    print("[3] 임베딩:", llm.EMBEDDING_MODEL)
    vec = llm.embed(["환불 규정"])[0]
    print("     차원:", len(vec))
    print("[OK] 준비 완료. usage =", llm.usage.as_dict())


if __name__ == "__main__":
    main()
