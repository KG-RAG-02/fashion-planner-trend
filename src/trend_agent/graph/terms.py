"""Term 계층 재구성. 문서: docs/schemas/graph_schema.md §2.5·§3 (계약 v1.1)

`_write_article`이 기사 단위로 Style·Item을 DETACH DELETE하고 Style id가 위치 기반(`<aid>-s<n>`)이라, Term과 Style의
관계는 적재할 때마다 끊긴다. 그래서 **적재할 때마다 Term 전체를 지우고 다시 만든다**:

- 원천 ① `term_map.jsonl`(T3, `resolver/term_map.py`) — alias·mention·profile 행
- 원천 ② 그래프 Style의 `aliases`(추출 `styles[].aliases`) — kind `alias`, origin = 기사 출처(label|extraction)
- `ALIAS_OF`: (article_id, Style `name_norm`)으로 현재 Style을 찾아 잇는다. 못 찾으면 `alias_unresolved`.
- `MAPS_TO`: 최신 profile 행의 노드형 속성(색·소재·패턴·디테일·상분류)만. 등급만 저장(수치 없음).
  fit·하분류는 Term `profile_json`(JSON 문자열)에 있다.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from neo4j import Driver

from trend_agent.resolver.term_map import TermMapRow
from trend_agent.schemas.graph_schema import MapsToEdge, TermNode, flatten, normalize_term, term_id

STYLE_ALIASES_CYPHER = """
MATCH (s:Style) WHERE size(coalesce(s.aliases, [])) > 0
OPTIONAL MATCH (a:Article {id: s.article_id})
RETURN s.id AS sid, s.article_id AS aid, s.name_norm AS name_norm, s.aliases AS aliases, a.source AS source
ORDER BY sid
"""
ALIAS_CYPHER = """
UNWIND $rows AS r
MATCH (t:Term {id: r.term_id})
OPTIONAL MATCH (s:Style {name_norm: r.style_norm}) WHERE r.article_id IS NULL OR s.article_id = r.article_id
WITH t, r, collect(s) AS ss
FOREACH (s IN ss | MERGE (t)-[e:ALIAS_OF]->(s) SET e.origin = r.origin, e.article_id = s.article_id)
RETURN sum(CASE WHEN size(ss) = 0 THEN 1 ELSE 0 END) AS unresolved, sum(size(ss)) AS linked
"""
_MAPS_LABEL = {"color": "Color", "material": "Material", "pattern": "Pattern", "detail": "Detail", "category": "Category"}
_ORIGIN_RANK = {"label": 0, "extraction": 1, "manual": 2, "term_mentions": 3, "llm": 4}
_KIND_RANK = {"alias": 0, "translation": 1, "trend_word": 2}


def maps_to_edges(profile: dict[str, Any], model: Optional[str], prompt_version: Optional[str]) -> list[MapsToEdge]:
    """Tier4 프로필(tier4.v1 검증본) → MAPS_TO 행. 노드형 속성만, 등급만."""
    focus = profile.get("focus") or {}
    out: list[MapsToEdge] = []
    for k, it in enumerate(profile.get("items") or []):
        if it.get("category_major"):
            out.append(MapsToEdge(attribute="category", value=it["category_major"], level=it.get("level"), item_idx=k,
                                  model=model, prompt_version=prompt_version))
        for attr, key in (("color", "colors"), ("material", "materials"), ("pattern", "patterns"), ("detail", "details")):
            for v in it.get(key) or []:
                out.append(MapsToEdge(attribute=attr, value=v["value"], level=focus.get(attr), value_level=v.get("value_level"),
                                      item_idx=k, model=model, prompt_version=prompt_version))
    return out


def build_term_rows(rows: Iterable[TermMapRow], style_aliases: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    """term_map 행 + Style aliases → Term 노드·ALIAS_OF·MAPS_TO 적재 행 (Neo4j 없이 계산 — 테스트 대상)."""
    rows = list(rows)
    candidates: dict[str, list[tuple[int, TermMapRow | dict[str, Any]]]] = {}
    aliases: list[dict[str, Any]] = []
    seen_alias: set[tuple] = set()

    def add_alias(norm: str, aid: Optional[str], style_norm: str, origin: str) -> None:
        k = (norm, aid, style_norm)
        if k not in seen_alias:
            seen_alias.add(k)
            aliases.append({"term_id": term_id(norm), "article_id": aid, "style_norm": style_norm, "origin": origin})

    order = 0
    for sa in style_aliases:
        origin = "extraction" if sa.get("source") == "extraction" else "label"
        for text in sa.get("aliases") or []:
            norm = normalize_term(text)
            if not norm or norm == sa.get("name_norm"):
                continue
            candidates.setdefault(norm, []).append((order, {"text": text, "kind": "alias", "origin": origin}))
            order += 1
            add_alias(norm, sa.get("aid"), sa["name_norm"], origin)
    for r in rows:
        candidates.setdefault(r.norm, []).append((order, r))
        order += 1
        if r.row == "alias":
            add_alias(r.norm, r.article_id, r.style_norm, r.origin)

    terms: list[dict[str, Any]] = []
    maps: dict[str, list[dict[str, Any]]] = {}
    for norm in sorted(candidates):
        cs = candidates[norm]
        def rank(c: tuple[int, Any]) -> tuple:
            o, x = c
            kind = x["kind"] if isinstance(x, dict) else x.kind
            origin = x["origin"] if isinstance(x, dict) else x.origin
            return (_KIND_RANK[kind], _ORIGIN_RANK[origin], o)
        _, first = min(cs, key=rank)
        text = first["text"] if isinstance(first, dict) else first.term
        kind = first["kind"] if isinstance(first, dict) else first.kind
        origin = first["origin"] if isinstance(first, dict) else first.origin
        mentions = [x.article_count for _, x in cs if isinstance(x, TermMapRow) and x.row == "mention" and x.article_count is not None]
        profiles = [(o, x) for o, x in cs if isinstance(x, TermMapRow) and x.row == "profile"]
        # 가장 최근(created_at) 프로필, 같은 시각이면 파일에서 나중 줄
        prof = max(profiles, key=lambda c: (c[1].created_at or "", c[0]))[1] if profiles else None
        if prof is not None and not (prof.profile or {}).get("recognized", True):
            if len(profiles) == len(cs):
                continue  # LLM도 모르는 질의어(프로필만 있음)는 캐시에만 두고 Term 노드로 만들지 않는다
            prof = None
        node = TermNode(
            id=term_id(norm), text=text, norm=normalize_term(text), kind=kind, origin=origin,
            article_count=max(mentions) if mentions else None,
            profile_json=json.dumps(prof.profile, ensure_ascii=False, sort_keys=True) if prof else None,
            profile_backend=prof.backend if prof else None, profile_model=prof.model if prof else None,
            profile_prompt_version=prof.prompt_version if prof else None)
        if node.norm != norm:  # 대표 표기가 다른 norm을 내면 계약 위반 — TermNode 검증과 같은 조건
            raise ValueError(f"Term 표기 {text!r}의 norm이 {norm!r}가 아니다")
        terms.append(flatten(node))
        if prof is not None and (prof.profile or {}).get("recognized", True):
            for e in maps_to_edges(prof.profile, prof.model, prof.prompt_version):
                maps.setdefault(_MAPS_LABEL[e.attribute], []).append(
                    {"term_id": node.id, "value": e.value, "props": flatten(e)})
    return {"terms": terms, "aliases": aliases, "maps": maps}


def rebuild_terms(driver: Driver, rows: Iterable[TermMapRow]) -> dict[str, int]:
    """Term 전체를 지우고 다시 만든다. 반환: {terms, alias_of, alias_unresolved, maps_to, maps_to_unresolved}."""
    style_aliases = [dict(r) for r in driver.execute_query(STYLE_ALIASES_CYPHER).records]
    built = build_term_rows(rows, style_aliases)
    driver.execute_query("MATCH (t:Term) DETACH DELETE t")
    if built["terms"]:
        driver.execute_query("UNWIND $rows AS r CREATE (t:Term) SET t = r", rows=built["terms"])
    linked = unresolved = 0
    if built["aliases"]:
        rec = driver.execute_query(ALIAS_CYPHER, rows=built["aliases"]).records[0]
        linked, unresolved = rec["linked"] or 0, rec["unresolved"] or 0
    maps = missing = 0
    for label, mrows in built["maps"].items():
        rec = driver.execute_query(
            f"UNWIND $rows AS r MATCH (t:Term {{id: r.term_id}}) OPTIONAL MATCH (a:{label} {{id: r.value}}) "
            "FOREACH (x IN CASE WHEN a IS NULL THEN [] ELSE [1] END | CREATE (t)-[e:MAPS_TO]->(a) SET e = r.props) "
            "RETURN sum(CASE WHEN a IS NULL THEN 1 ELSE 0 END) AS missing, sum(CASE WHEN a IS NULL THEN 0 ELSE 1 END) AS ok",
            rows=mrows).records[0]
        maps += rec["ok"] or 0
        missing += rec["missing"] or 0
    return {"terms": len(built["terms"]), "alias_of": linked, "alias_unresolved": unresolved,
            "maps_to": maps, "maps_to_unresolved": missing}
