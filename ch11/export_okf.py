"""11-5: 의미 모델을 OKF(Open Knowledge Format) 번들로 내보내기.

6장 테이블(concepts, relation_types, glossary_terms, policy_versions, clauses, clause_links)을 읽어
마크다운 + YAML frontmatter 문서의 디렉터리 트리(OKF 번들)로 씁니다.

OKF 규칙(OpenWiki & OKF 따라하기 4장 기준):
  - 문서마다 frontmatter 필수 필드 type, 권장 필드 title / description / tags
  - 문서 사이 관계는 일반 마크다운 링크(절대 경로 /concepts/xxx.md). 링크에 타입이 없음
  - index.md 는 안내판(예약 문서), log.md 는 변경 이력
  - v0.2: generated / sources / status / stale_after 로 신뢰·출처 표현. 생산자 확장 필드 허용

이 스크립트가 보여 주려는 것:
  - 개념·조항·용어는 OKF 문서로 자연스럽게 옮겨진다
  - 그러나 "준용·대체·인용" 같은 관계의 타입과 규정 판의 유효기간은 OKF 표준 필드에 자리가 없다
    → 본문 문장(OKF 방식)과 확장 필드 x_relations / x_valid_from / x_valid_to (온톨로지 방식)에 둘 다 적는다

실행:  python ch11/export_okf.py [출력폴더=okf_bundle]
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import db  # noqa: E402

REL_LABEL = {"supersedes": "대체한다", "applies_mutatis_mutandis": "준용한다", "cites": "인용한다", "defines": "정의한다"}
REL_LABEL_REV = {"supersedes": "이 조항을 대체한 조항", "applies_mutatis_mutandis": "이 조항을 준용하는 조항", "cites": "이 조항을 인용하는 문서", "defines": "이 개념을 정의하는 조항"}
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9가-힣._-]+", "-", s).strip("-")


def fm(d: dict) -> str:
    return "---\n" + yaml.safe_dump(d, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n"


def clause_path(clause_id: str) -> str:
    doc, no = clause_id.split(":", 1)
    return f"/clauses/{doc}/{slug(no)}.md"


def export(out: pathlib.Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "concepts").mkdir(exist_ok=True)
    (out / "clauses").mkdir(exist_ok=True)
    (out / "glossary").mkdir(exist_ok=True)
    counts = {"concepts": 0, "clauses": 0, "glossary": 0}
    with db.connect() as c:
        concepts = c.execute("SELECT concept_id, label, definition, parent_id, attributes FROM concepts ORDER BY concept_id").fetchall()
        rels = c.execute("SELECT relation_id, label, from_concept, to_concept, cardinality, derived, note FROM relation_types ORDER BY relation_id").fetchall()
        terms = c.execute("SELECT canonical, label, definition, note, confusable_with FROM glossary_terms ORDER BY canonical").fetchall()
        forms = c.execute("SELECT canonical, form, source FROM glossary_surface_forms ORDER BY canonical, form").fetchall()
        docs = c.execute("SELECT doc_id, title, valid_from, valid_to FROM policy_versions ORDER BY doc_id").fetchall()
        clauses = c.execute("SELECT clause_id, doc_id, clause_no, heading, content FROM clauses ORDER BY doc_id, clause_no").fetchall()
        links = c.execute("SELECT from_clause, rel, to_ref FROM clause_links ORDER BY 1,2,3").fetchall()

    doc_meta = {d[0]: {"title": d[1], "valid_from": d[2], "valid_to": d[3]} for d in docs}
    out_links = {}
    in_links = {}
    for f, r, t in links:
        out_links.setdefault(f, []).append((r, t))
        in_links.setdefault(t, []).append((r, f))
    rels_by_concept = {}
    for rid, label, fc, tc, card, derived, note in rels:
        rels_by_concept.setdefault(fc, []).append(("out", rid, label, tc, card, derived, note))
        rels_by_concept.setdefault(tc, []).append(("in", rid, label, fc, card, derived, note))

    # ---- 개념 문서 ----
    for cid, label, definition, parent, attrs in concepts:
        defining = [f for r, f in in_links.get("concept:" + cid, []) if r == "defines"]
        body = [f"{definition}", ""]
        if parent:
            body.append(f"상위 개념: [{parent}](/concepts/{parent}.md)")
        if attrs:
            body.append("속성: " + ", ".join(attrs))
        body.append("")
        body.append("### 관계")
        x_rel = []
        for direction, rid, rlabel, other, card, derived, note in rels_by_concept.get(cid, []):
            arrow = f"이 개념은 [{other}](/concepts/{other}.md)을(를) **{rlabel}**({rid})" if direction == "out" else f"[{other}](/concepts/{other}.md)이(가) 이 개념을 **{rlabel}**({rid})"
            extra = (f" · 카디널리티 {card}" if card else "") + (f" · 파생: {derived}" if derived else "") + (f" · {note}" if note else "")
            body.append(f"- {arrow}{extra}")
            x_rel.append({"relation": rid, "direction": direction, "target": f"/concepts/{other}.md", **({"derived": derived} if derived else {})})
        if defining:
            body.append("")
            body.append("### 정의 조항")
            for f in defining:
                body.append(f"- [{f}]({clause_path(f)}) — 이 조항이 개념을 정의한다")
        front = {"type": "Concept", "title": label, "description": definition.split(". ")[0], "tags": ["notecloud", "ontology"] + (["derived"] if any(x.get("derived") for x in x_rel) else []),
                 "generated": {"by": "script:ch11/export_okf.py", "at": NOW}, "status": "stable",
                 "sources": [{"id": f, "resource": clause_path(f)} for f in defining],
                 "x_concept_id": cid, "x_parent": parent, "x_relations": x_rel}
        (out / "concepts" / f"{cid}.md").write_text(fm(front) + "\n" + "\n".join(body) + "\n", encoding="utf-8", newline="\n")
        counts["concepts"] += 1

    # ---- 조항 문서 ----
    for cid, doc, no, heading, content in clauses:
        p = out / pathlib.Path(clause_path(cid).lstrip("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = doc_meta[doc]
        body = [content, "", "### 관계"]
        x_rel = []
        for r, t in out_links.get(cid, []):
            if t.startswith("concept:"):
                body.append(f"- 이 조항은 [{t[8:]}](/concepts/{t[8:]}.md)을(를) **{REL_LABEL[r]}**")
                x_rel.append({"relation": r, "target": f"/concepts/{t[8:]}.md"})
            else:
                body.append(f"- 이 조항은 [{t}]({clause_path(t)})을(를) **{REL_LABEL[r]}**")
                x_rel.append({"relation": r, "target": clause_path(t)})
        for r, f in in_links.get(cid, []):
            body.append(f"- {REL_LABEL_REV[r]}: [{f}]({clause_path(f)})")
            x_rel.append({"relation": r, "direction": "in", "target": clause_path(f)})
        if len(body) == 3:
            body.append("- (링크 없음)")
        front = {"type": "Clause", "title": f"{meta['title']} {no}" + (f" ({heading})" if heading else ""), "description": content.split("\n")[0][:80],
                 "tags": ["notecloud", "policy", doc], "generated": {"by": "script:ch11/export_okf.py", "at": NOW},
                 "status": "deprecated" if meta["valid_to"] else "stable",
                 **({"stale_after": str(meta["valid_to"])} if meta["valid_to"] else {}),
                 "x_clause_id": cid, "x_doc_id": doc, "x_valid_from": str(meta["valid_from"]), "x_valid_to": str(meta["valid_to"]) if meta["valid_to"] else None,
                 "x_relations": x_rel}
        p.write_text(fm(front) + "\n" + "\n".join(body) + "\n", encoding="utf-8", newline="\n")
        counts["clauses"] += 1

    # ---- 용어 문서 ----
    forms_by = {}
    for canon, form, source in forms:
        forms_by.setdefault(canon, []).append((form, source))
    for canon, label, definition, note, confusable in terms:
        body = [definition, "", "### 표현"]
        for form, source in forms_by.get(canon, []):
            body.append(f"- \"{form}\" — {source}")
        if confusable:
            body += ["", "### 혼동 주의", ", ".join(f"[{x}](/glossary/{x}.md)" if any(t[0] == x for t in terms) else f"[{x}](/concepts/{x}.md)" for x in confusable)]
        if note:
            body += ["", f"참고: {note}"]
        target = f"/concepts/{canon}.md" if any(cc[0] == canon for cc in concepts) else None
        if target:
            body += ["", f"개념 문서: [{canon}]({target})"]
        front = {"type": "Term", "title": label, "description": definition.split(". ")[0], "tags": ["notecloud", "glossary"],
                 "generated": {"by": "script:ch11/export_okf.py", "at": NOW}, "status": "stable", "x_canonical": canon, "x_confusable_with": list(confusable or [])}
        (out / "glossary" / f"{canon}.md").write_text(fm(front) + "\n" + "\n".join(body) + "\n", encoding="utf-8", newline="\n")
        counts["glossary"] += 1

    # ---- index.md / 폴더 index / log.md ----
    root = ["---", 'okf_version: "0.1"', "title: 노트클라우드 업무 지식 (온톨로지 내보내기)", "---", "",
            "노트클라우드 고객지원 업무의 개념·규정 조항·용어를 OKF 번들로 내보낸 것입니다. 원본은 PostgreSQL의 의미 모델 테이블이며, 이 번들은 파생물입니다.", "",
            "* [개념](/concepts/index.md) - 고객·구독·결제·규정 판·조항 등 업무 개념과 관계",
            "* [규정 조항](/clauses/index.md) - 환불 규정 1판·2판, 약관, 요금 안내, 운영 매뉴얼, FAQ 의 조·항 단위 문서. 유효기간 포함",
            "* [용어](/glossary/index.md) - 해지·환불·결제 취소처럼 뜻이 갈리는 표현의 표준 용어", "",
            "읽는 순서: 질문의 표현을 용어에서 표준 용어로 바꾼 뒤, 개념 문서의 관계를 따라 조항으로 가세요. 조항 문서의 x_valid_from / x_valid_to 로 현재 유효한 판인지 확인하세요."]
    (out / "index.md").write_text("\n".join(root) + "\n", encoding="utf-8", newline="\n")
    (out / "concepts" / "index.md").write_text("\n".join([f"* [{l}](/concepts/{cid}.md) - {d.split('. ')[0]}" for cid, l, d, *_ in concepts]) + "\n", encoding="utf-8", newline="\n")
    (out / "glossary" / "index.md").write_text("\n".join([f"* [{l}](/glossary/{c}.md) - {d.split('. ')[0]}" for c, l, d, *_ in terms]) + "\n", encoding="utf-8", newline="\n")
    cl_index = []
    for doc, meta in doc_meta.items():
        until = meta["valid_to"] or "현재"
        cl_index.append(f"* [{meta['title']}](/clauses/{doc}/index.md) - 유효 {meta['valid_from']} ~ {until}")
        sub = [f"* [{no}{' (' + h + ')' if h else ''}]({clause_path(cid)})" for cid, d, no, h, _ in clauses if d == doc]
        (out / "clauses" / doc / "index.md").write_text(f"유효 {meta['valid_from']} ~ {until}\n\n" + "\n".join(sub) + "\n", encoding="utf-8", newline="\n")
    (out / "clauses" / "index.md").write_text("\n".join(cl_index) + "\n", encoding="utf-8", newline="\n")
    (out / "log.md").write_text(f"## {NOW[:10]}\n- Exported {counts['concepts']} concepts, {counts['clauses']} clauses, {counts['glossary']} terms from PostgreSQL semantic model (ch11/export_okf.py)\n", encoding="utf-8", newline="\n")
    return counts


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "okf_bundle")
    print(export(out), "->", out.resolve())
