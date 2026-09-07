-- 06-5 (2/2): 의미 모델·규정 판·조항·용어 사전 테이블
-- 06-1의 "선택지 C(혼합)" — 핵심 업무 개체는 전용 테이블(정형 데이터),
-- 의미 모델(개념·관계·용어)과 규정(판·조항·조항 관계)은 범용 테이블에 둔다.

DROP TABLE IF EXISTS clause_links, clauses, policy_versions,
                     glossary_surface_forms, glossary_terms,
                     relation_types, concepts CASCADE;

-- 개념 (model.yaml concepts)
CREATE TABLE concepts (
    concept_id  text PRIMARY KEY,
    label       text NOT NULL,
    definition  text NOT NULL,
    parent_id   text REFERENCES concepts,          -- subtypes 관계
    attributes  jsonb NOT NULL DEFAULT '[]'
);

-- 관계 유형 (model.yaml relations)
CREATE TABLE relation_types (
    relation_id  text PRIMARY KEY,
    label        text NOT NULL,
    from_concept text NOT NULL REFERENCES concepts,
    to_concept   text NOT NULL REFERENCES concepts,
    cardinality  text,
    derived      text,                             -- 저장이 아니라 계산으로 얻는 관계면 그 정의
    note         text
);

-- 용어 사전 (glossary.yaml)
CREATE TABLE glossary_terms (
    canonical   text PRIMARY KEY,                  -- 개념 id 또는 속성명
    label       text NOT NULL,
    definition  text NOT NULL,
    note        text,
    confusable_with text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE glossary_surface_forms (
    canonical text NOT NULL REFERENCES glossary_terms,
    form      text NOT NULL,
    source    text,
    PRIMARY KEY (canonical, form)
);

-- 규정 판 (문서 프런트매터)
CREATE TABLE policy_versions (
    doc_id     text PRIMARY KEY,
    title      text NOT NULL,
    valid_from date NOT NULL,
    valid_to   date,                               -- NULL = 현재 유효
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- 조항: 답변의 근거 단위. 임베딩은 검색용(6-3)
CREATE TABLE clauses (
    clause_id  text PRIMARY KEY,                   -- 'refund_policy_v2:4.2'
    doc_id     text NOT NULL REFERENCES policy_versions,
    clause_no  text NOT NULL,                      -- '4.2' / '4' / '결제와 구독'
    heading    text,
    content    text NOT NULL,
    embedding  vector(1536)
);
CREATE INDEX ON clauses (doc_id);

-- 조항 관계 (clause_links.yaml): 대체·준용·인용·정의
CREATE TABLE clause_links (
    from_clause text NOT NULL REFERENCES clauses,
    rel         text NOT NULL CHECK (rel IN ('supersedes','applies_mutatis_mutandis','cites','defines')),
    to_ref      text NOT NULL,                     -- 조항 id 또는 'concept:ID'
    PRIMARY KEY (from_clause, rel, to_ref)
);
CREATE INDEX ON clause_links (to_ref);
