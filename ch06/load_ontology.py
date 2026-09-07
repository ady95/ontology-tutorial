"""06-5 (2/2): 의미 모델·규정 문서를 PostgreSQL 에 적재합니다.

  1. data/ontology/model.yaml     → concepts, relation_types
  2. data/ontology/glossary.yaml  → glossary_terms, glossary_surface_forms
  3. data/docs/*.md               → policy_versions(프런트매터), clauses(조·항 단위로 분해)
  4. data/ontology/clause_links.yaml → clause_links
  5. --embed 를 주면 조항 본문을 임베딩해 clauses.embedding 에 저장 (LLM API 필요)

실행:
  python ch06/load_ontology.py            # 임베딩 없이 적재 (API 키 불필요)
  python ch06/load_ontology.py --embed    # 임베딩까지
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DOCS = ROOT / "data" / "docs"
ONT = ROOT / "data" / "ontology"


# ---------- 문서 → 규정 판 + 조항 ----------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


def split_clauses(doc_id: str, body: str) -> list[dict]:
    """'## 제N조 (제목)' 문서는 조·항 단위, 그 밖의 문서는 '## 절' 단위(+번호 항목)로 나눕니다."""
    clauses: list[dict] = []
    sections = re.split(r"^## ", body, flags=re.M)[1:]
    for sec in sections:
        head, _, rest = sec.partition("\n")
        head, rest = head.strip(), rest.strip()
        m = re.match(r"제(\d+)조\s*\((.*?)\)", head)
        if m:  # 조·항 구조
            art, title = m.group(1), m.group(2)
            clauses.append({"clause_no": art, "heading": title, "content": f"제{art}조 ({title})\n{rest}"})
            for im in re.finditer(r"^(\d+)\.\s+(.*?)(?=^\d+\.\s|\Z)", rest, re.S | re.M):
                para = im.group(1)
                clauses.append({"clause_no": f"{art}.{para}", "heading": title,
                                "content": f"제{art}조 제{para}항: {im.group(2).strip()}"})
        else:
            m2 = re.match(r"(\d+)\.\s*(.*)", head)
            if m2:  # 운영 매뉴얼: '## 1. 제목'
                no, title = m2.group(1), m2.group(2)
                clauses.append({"clause_no": no, "heading": title, "content": f"{no}. {title}\n{rest}"})
                for im in re.finditer(r"^(\d+)\.\s+(.*?)(?=^\d+\.\s|\Z)", rest, re.S | re.M):
                    clauses.append({"clause_no": f"{no}.{im.group(1)}", "heading": title,
                                    "content": f"{title} {im.group(1)}항: {im.group(2).strip()}"})
            else:  # FAQ·요금 안내: 절 제목이 id
                clauses.append({"clause_no": head, "heading": head, "content": f"{head}\n{rest}"})
    for c in clauses:
        c["clause_id"] = f"{doc_id}:{c['clause_no']}"
        c["doc_id"] = doc_id
    return clauses


def load(embed: bool = False) -> None:
    model = yaml.safe_load((ONT / "model.yaml").read_text(encoding="utf-8"))
    glossary = yaml.safe_load((ONT / "glossary.yaml").read_text(encoding="utf-8"))
    links = yaml.safe_load((ONT / "clause_links.yaml").read_text(encoding="utf-8"))["links"]

    with db.connect(autocommit=False) as conn:
        db.run_sql_file(conn, HERE / "schema_ontology.sql")

        # 1. 개념 (부모 먼저)
        concept_ids = {c["id"] for c in model["concepts"]}
        parent_of: dict[str, str] = {}
        for c in model["concepts"]:
            for sub in c.get("subtypes", []):
                parent_of[sub] = c["id"]
        for c in model["concepts"]:
            conn.execute(
                "INSERT INTO concepts VALUES (%s,%s,%s,NULL,%s)",
                (c["id"], c["label"], c["definition"], json.dumps(c.get("attributes", []), ensure_ascii=False)),
            )
            for sub in c.get("subtypes", []):
                if sub not in concept_ids:
                    conn.execute("INSERT INTO concepts VALUES (%s,%s,%s,NULL,'[]')",
                                 (sub, sub, f"{c['label']}의 하위 유형"))
        for sub, parent in parent_of.items():
            conn.execute("UPDATE concepts SET parent_id=%s WHERE concept_id=%s", (parent, sub))

        # 2. 관계 유형
        for r in model["relations"]:
            conn.execute(
                "INSERT INTO relation_types VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (r["id"], r["label"], r["from"], r["to"], r.get("cardinality"), r.get("derived"), r.get("note")),
            )

        # 3. 용어 사전
        for t in glossary["terms"]:
            conn.execute(
                "INSERT INTO glossary_terms VALUES (%s,%s,%s,%s,%s)",
                (t["canonical"], t["label"], t["definition"], t.get("note"), t.get("confusable_with", [])),
            )
            for sf in t.get("surface_forms", []):
                conn.execute("INSERT INTO glossary_surface_forms VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                             (t["canonical"], sf["form"], sf.get("source")))

        # 4. 규정 판 + 조항
        all_clauses: list[dict] = []
        for path in sorted(DOCS.glob("*.md")):
            fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO policy_versions VALUES (%s,%s,%s,%s)",
                         (fm["doc_id"], fm["title"], fm["valid_from"], fm.get("valid_to")))
            cl = split_clauses(fm["doc_id"], body)
            for c in cl:
                conn.execute("INSERT INTO clauses (clause_id, doc_id, clause_no, heading, content) VALUES (%s,%s,%s,%s,%s)",
                             (c["clause_id"], c["doc_id"], c["clause_no"], c["heading"], c["content"]))
            all_clauses.extend(cl)
            print(f"{fm['doc_id']:20s} {fm['valid_from']} ~ {fm.get('valid_to') or '현재':10} 조항 {len(cl):3d}개")

        # 5. 조항 관계 (참조 무결성은 코드로 검사: to_ref 가 concept: 이면 개념, 아니면 조항)
        known = {c["clause_id"] for c in all_clauses}
        attr_names = {a for c in model["concepts"] for a in c.get("attributes", [])}
        bad = []
        for l in links:
            to = l["to"]
            ok = to.split(":", 1)[1] in concept_ids | set(parent_of) | attr_names if to.startswith("concept:") else to in known
            if l["from"] not in known or not ok:
                bad.append(l)
                continue
            conn.execute("INSERT INTO clause_links VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                         (l["from"], l["rel"], to))
        if bad:
            print("경고: 존재하지 않는 조항·개념을 가리키는 링크", bad)
        conn.commit()

        n = conn.execute("SELECT count(*) FROM clauses").fetchone()[0]
        print(f"개념 {len(concept_ids) + len(parent_of)} / 관계 {len(model['relations'])} / 용어 {len(glossary['terms'])} / 조항 {n} / 링크 {len(links) - len(bad)}")

        if embed:
            from common import llm
            rows = conn.execute("SELECT clause_id, content FROM clauses ORDER BY clause_id").fetchall()
            vecs = llm.embed([r[1] for r in rows])
            for (cid, _), v in zip(rows, vecs):
                conn.execute("UPDATE clauses SET embedding=%s WHERE clause_id=%s", (v, cid))
            conn.commit()
            print(f"임베딩 {len(rows)}개 저장 (토큰 {llm.usage.embed_tokens:,})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed", action="store_true")
    load(embed=ap.parse_args().embed)
