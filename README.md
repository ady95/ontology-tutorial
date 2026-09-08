# 온톨로지 따라하기 — 예제 코드

위키독스 도서 **[온톨로지 따라하기 (RAG 다음의 설계: PostgreSQL로 시작하는 실전 온톨로지)](https://wikidocs.net/book/21274)** 의 실습 예제 저장소입니다.

가상 기업 "노트클라우드"의 환불·해지·좌석 변경 업무 질문 30개를, 같은 LLM·같은 문서로 다섯 가지 구성(기본 RAG → 검색 개선 → SQL·규칙 결합 → 명시적 의미 모델 → 일부 그래프 탐색)으로 풀어 비교합니다.

## 기준 버전

| 구성 요소 | 버전 |
|---|---|
| Python | 3.10 이상 |
| PostgreSQL | 16 |
| pgvector | 0.6.x 이상 |
| Apache AGE | 1.5 (09장 일부, Docker 이미지 기준) |
| psycopg | 3.x |
| openai (Python SDK) | 1.x |

LLM은 OpenAI API 기준으로 서술합니다. 책에 실린 정답률·토큰·시간 수치는 모두 `gpt-5.6-luna` + `text-embedding-3-small`(1536차원)로 측정했으므로 기본값도 그렇게 두었습니다. `LLM_MODEL`을 바꾸면 다른 모델로 실행할 수 있고, `OPENAI_BASE_URL`을 바꾸면 호환 API를 쓸 수 있습니다. 모델을 바꾸면 절대 수치는 달라집니다.

## 시작하기

### 1. PostgreSQL

Docker가 있으면:

```bash
docker compose up -d
```

Docker가 없으면 아무것도 하지 않아도 됩니다. `DATABASE_URL`이 비어 있으면 `pgserver` 패키지가 저장소 안 `pgdata/` 폴더에 내장 PostgreSQL(16 + pgvector)을 자동으로 띄웁니다. 이 경우 Apache AGE는 사용할 수 없습니다(09-2의 AGE 절만 영향).

### 2. Python 환경

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  /  macOS·Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. 환경변수

```bash
cp .env.example .env
```

`.env`에 `OPENAI_API_KEY`를 넣습니다. Docker를 쓰면 `DATABASE_URL`은 예시 값 그대로, 내장 PostgreSQL을 쓰면 그 줄을 비웁니다.

### 4. 연결 확인

```bash
python ch03/check_env.py
```

## 폴더 구조

| 폴더 | 내용 | 관련 장 |
|---|---|---|
| `common/` | DB 연결, LLM 호출, 평가 도구 | 03 |
| `data/docs/` | 규정 문서 6종 (환불 규정 1·2판, 약관, 요금 안내, 운영 매뉴얼, FAQ) | 01, 05, 06 |
| `data/structured/` | 고객·구독·결제·사용량·장애·팀원 CSV | 01, 06 |
| `data/eval/` | 업무 질문 30개와 정답·근거·오류 유형 | 01, 03, 08 |
| `data/ontology/` | 의미 모델(model.yaml), 용어 사전(glossary.yaml), 판단 규칙(rules.yaml) | 04, 05 |
| `ch01/` | 구성 1: 기본 RAG | 01, 08 |
| `ch03/` | 환경 확인 | 03 |
| `ch06/` | 스키마·적재 (정형 데이터 + 의미 모델 + 조항·임베딩) | 06 |
| `ch07/` | 구성 4: 검색 + SQL + 규칙 + 의미 모델 파이프라인 | 07, 08 |
| `ch08/` | 구성 2·3 및 비교 실험 실행 | 08 |
| `ch09/` | 재귀 CTE·AGE 그래프 탐색, 구성 5 | 09 |

## 실행 순서

```bash
python ch03/check_env.py            # 환경 확인
python ch06/load_structured.py      # 정형 데이터 적재
python ch01/rag_basic.py --index    # 구성 1 인덱스 생성
python ch01/rag_basic.py --eval     # 구성 1 채점 → results/config1_basic.jsonl
```

이후 장별 실행 방법은 각 스크립트 상단 주석과 책 본문을 참고하세요.

## 데이터에 관하여

`data/`의 회사·고객·규정·거래는 모두 이 책을 위해 만든 가상 데이터입니다. 자세한 설명은 [data/README.md](data/README.md)에 있습니다.

## 라이선스

MIT
