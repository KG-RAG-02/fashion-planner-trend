"""Trend Resolver — 용어 → Tier1~4 → 트렌드·Item·상품 + 트렌드 단위 Top-K → `TrendAnalysisResult` v1.1

위에서부터 시도하고 판정 사유를 `resolutions[].steps`에 남긴다(문서: trend_analysis_result.md §1.2·§3.2).
- Tier1 graph    : 정규화명이 Style `name_norm`·`aliases_norm`·Term(ALIAS_OF, origin≠llm)과 일치 → 기사 인용 필수
- Tier2 attribute: 별칭 사전으로 전부 분절되면(`vocab_full`) 가상 프로필 (근사 토큰은 쓰지 않는다)
- Tier3 semantic : C1 `embeddings.search()` 코사인 ≥ τ 이고 1·2위(다른 name_norm 그룹) 차 ≥ δ (τ·δ가 null이면 건너뜀)
- Tier4 llm      : `llm.call` + 폐쇄 어휘 등급 → 코드 수치화, 캐시는 term_map.jsonl
시즌: `season_policy`(weights.yaml flags, 기본 report)는 Tier1·3 이름 일치에만 적용한다. strict에서 시즌이 맞지 않으면
그 용어는 **그래프에 있는 트렌드**이므로 Tier2~4로 넘기지 않고 실패(season_mismatch·season_unknown_held)로 끝낸다.
평가 실행(T4): `force_tier`(그 Tier만 시도), `hide_styles`(name_norm 그룹 숨김), `tier4_context`(none|mentions).
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Literal, Optional

from trend_agent.config import load_env
from trend_agent.graph.search import TrendSearcher, scoring_limitations
from trend_agent.retrieval.scoring import ScoredProduct, ScoringOptions, attribute_values
from trend_agent.retrieval.trend_topk import ItemRanking, trend_topk
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import SCORED_ATTRIBUTES, ItemRecord, normalize_term
from trend_agent.schemas.trend_analysis_result import (
    QUALIFICATION_BY_MODE,
    AppliedFilters,
    AppliedSettings,
    DescriptionOut,
    EvidenceRecord,
    LLMTrace,
    ResolutionStep,
    TermResolution,
    Trend,
    TrendAnalysisResult,
    TrendStyle,
    evidence_id,
)
from trend_agent.schemas.trend_query import CategoryFilter, CategoryRef, SeasonRequest

from . import term_map as tm
from . import tier4 as t4
from .normalize import Segmenter, Segmentation
from .profile import VirtualItemSpec, VirtualProfile

STOP = "stop"  # Tier가 "다음 Tier로 넘기지 않고 실패"를 뜻할 때


@dataclass
class ResolveConfig:
    season: Optional[SeasonRequest] = None          # None → 다음 패션 시즌(default_next), search()와 같다
    season_policy: Optional[Literal["strict", "report"]] = None  # None → weights.yaml flags.season_policy
    categories: CategoryFilter = field(default_factory=CategoryFilter)
    top_k: int = 5
    force_tier: Optional[Literal[1, 2, 3, 4]] = None
    hide_styles: list[str] = field(default_factory=list)   # Style id 또는 이름 → name_norm 그룹 숨김
    tier4_context: Literal["none", "mentions"] = "mentions"
    use_llm: bool = True
    # Tier4 맥락에서 뺄 기사. hide_styles가 있으면 숨긴 그룹 Style의 출처 기사도 자동으로 뺀다(누출 방지, T4 요청)
    exclude_context_articles: list[str] = field(default_factory=list)


@dataclass
class Tier3Candidate:
    group_norm: str
    score: float
    style_ids: list[str]


@dataclass
class _Hit:
    tier: int
    mode: str
    styles: list[dict[str, Any]] = field(default_factory=list)
    season_status: dict[str, str] = field(default_factory=dict)
    similarity: Optional[float] = None
    profile: Optional[VirtualProfile] = None
    llm: Optional[LLMTrace] = None
    notes: list[str] = field(default_factory=list)


def _lucene_escape(q: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", q)


class TrendResolver:
    def __init__(self, searcher: TrendSearcher, cfg: Optional[ResolveConfig] = None, *,
                 llm: Any = None, embedder: Any = None,
                 mentions: Optional[dict[str, dict[str, Any]]] = None, crawl: Optional[dict[str, dict[str, Any]]] = None,
                 term_rows: Optional[list[tm.TermMapRow]] = None, term_map_path: Any = None,
                 llm_settings: Optional[tuple[str, Optional[str]]] = None, segmenter: Optional[Segmenter] = None):
        self.s = searcher
        self.w = searcher.weights
        self.cfg = cfg or ResolveConfig()
        self.policy = self.cfg.season_policy or self.w.flags.season_policy
        self.season = self.cfg.season or SeasonRequest.default_next(date.today())
        self.key = f"{self.season.year}{self.season.season.value}"
        self.segmenter = segmenter or Segmenter()
        self._llm = llm                      # trend_agent.llm 모듈과 같은 call() 을 가진 객체 (테스트는 가짜)
        self._embedder = embedder            # embed_texts()·search()를 가진 객체 (기본 trend_agent.embeddings)
        self.mentions = mentions if mentions is not None else {}
        self.crawl = crawl if crawl is not None else {}
        self.term_map_path = term_map_path
        self.term_rows = term_rows if term_rows is not None else tm.read_term_map(term_map_path)
        self._llm_settings = llm_settings
        self._styles: Optional[list[dict[str, Any]]] = None
        self._term_links: Optional[dict[str, list[str]]] = None
        self._term_ids: Optional[dict[str, list[str]]] = None
        self.hidden = self._hidden_groups(self.cfg.hide_styles)
        self.context_exclude = frozenset(self.cfg.exclude_context_articles) | frozenset(
            s["article_id"] for s in (self.styles() if self.hidden else []) if s["name_norm"] in self.hidden)
        self._weak_items: list[str] = []

    # ---------- 그래프 읽기 ----------
    def styles(self) -> list[dict[str, Any]]:
        if self._styles is None:
            out = []
            for s in self.s.styles():
                s = dict(s)
                s.setdefault("name_norm", normalize_term(s["name"]))
                s.setdefault("aliases_norm", [])
                s.setdefault("article_id", s["id"].rsplit("-s", 1)[0])
                out.append(s)
            self._styles = out
        return self._styles

    def _load_term_links(self) -> None:
        has = self.s._q("CALL db.relationshipTypes() YIELD relationshipType WHERE relationshipType = 'ALIAS_OF' RETURN count(*) AS c")
        rows = self.s._q("MATCH (t:Term)-[:ALIAS_OF]->(s:Style) RETURN t.id AS tid, t.norm AS n, t.origin AS o, collect(s.id) AS sids") \
            if has and has[0]["c"] else []
        self._term_links = {}
        self._term_ids = {}
        for r in rows:
            self._term_ids[r["tid"]] = list(r["sids"])
            if r["o"] != "llm":
                self._term_links.setdefault(r["n"], []).extend(r["sids"])

    def term_links(self) -> dict[str, list[str]]:
        if self._term_links is None:
            self._load_term_links()
        return self._term_links  # type: ignore[return-value]

    def _hidden_groups(self, entries: Iterable[str]) -> list[str]:
        if not entries:
            return []
        by_id = {s["id"]: s["name_norm"] for s in self.styles()}
        return sorted({by_id.get(e) or normalize_term(e) for e in entries})

    # ---------- 시즌 ----------
    def _season_status(self, s: dict[str, Any]) -> str:
        seasons = s.get("seasons") or []
        if not seasons:
            return "unknown"
        return "match" if self.key in seasons else "mismatch"

    def _gate(self, tier: int, mode: str, hits: list[dict[str, Any]], steps: list[ResolutionStep], code: str,
              detail: str, similarity: Optional[float] = None, best: Optional[float] = None,
              second: Optional[float] = None) -> Any:
        status = {s["id"]: self._season_status(s) for s in hits}
        if self.policy == "strict":
            kept = [s for s in hits if status[s["id"]] == "match"]
            if not kept:
                held = any(v == "unknown" for v in status.values())
                names = ", ".join(f"{s['name']}({'·'.join(s.get('seasons') or ['시즌 미확인'])})" for s in hits)
                steps.append(ResolutionStep(tier=tier, outcome="miss", code="season_unknown_held" if held else "season_mismatch",
                                            detail=f"이름은 일치하지만 {self.key} 시즌이 아니거나 확인되지 않음(strict): {names}",
                                            best=best, second=second))
                return STOP
        else:
            kept = hits
        steps.append(ResolutionStep(tier=tier, outcome="hit", code=code, detail=detail, best=best, second=second))
        off = [s for s in kept if status[s["id"]] != "match"]
        if self.policy == "report" and off:
            c = {k: sum(1 for s in kept if status[s["id"]] == k) for k in ("match", "unknown", "mismatch")}
            steps.append(ResolutionStep(tier=tier, outcome="hit", code="season_reported",
                                        detail=f"{self.key}: 일치 {c['match']} · 미확인 {c['unknown']} · 불일치 {c['mismatch']} (제외하지 않고 보고)"))
        return _Hit(tier=tier, mode=mode, styles=kept, season_status={s["id"]: status[s["id"]] for s in kept}, similarity=similarity)

    # ---------- Tier1 ----------
    def tier1(self, term: str, n: str, steps: list[ResolutionStep]) -> Any:
        linked = set(self.term_links().get(n, []))
        hits = [s for s in self.styles() if s["name_norm"] == n or n in s["aliases_norm"] or s["id"] in linked]
        hidden = [s for s in hits if s["name_norm"] in self.hidden]
        if hidden:
            steps.append(ResolutionStep(tier=1, outcome="skipped", code="hidden_by_eval",
                                        detail=f"평가 숨김 그룹: {', '.join(sorted({s['name_norm'] for s in hidden}))} ({len(hidden)}개 Style)"))
            hits = [s for s in hits if s["name_norm"] not in self.hidden]
        if not hits:
            steps.append(ResolutionStep(tier=1, outcome="miss", code="name_not_found", detail=self._suggest(term)))
            return None
        how = [("이름" if s["name_norm"] == n else "별칭" if n in s["aliases_norm"] else "Term ALIAS_OF") for s in hits]
        detail = ", ".join(f"{s['name']}({h})" for s, h in zip(hits, how))
        return self._gate(1, "graph", hits, steps, "name_exact", detail)

    def _suggest(self, term: str) -> str:
        """fulltext 부분 일치는 Tier1 히트가 아니라 제안으로만 적는다 (예: 맥시멀리즘 ≠ 절제된 맥시멀리즘)."""
        try:
            rows = self.s._q("CALL db.index.fulltext.queryNodes('style_names', $q) YIELD node, score "
                             "RETURN node.name AS name, node.name_norm AS nn ORDER BY score DESC LIMIT 5", q=_lucene_escape(term))
        except Exception:  # noqa: BLE001 — 인덱스가 없는 그래프
            return "그래프 Style·별칭·Term에 같은 정규화명이 없음"
        names = [r["name"] for r in rows if r["nn"] not in self.hidden]
        return "그래프 Style·별칭·Term에 같은 정규화명이 없음" + (f" — 부분 일치 제안(판정 아님): {', '.join(names)}" if names else "")

    # ---------- Tier2 ----------
    def tier2(self, term: str, n: str, steps: list[ResolutionStep]) -> Optional[_Hit]:
        seg = self.segmenter.segment(n)
        if seg.code != "vocab_full":
            steps.append(ResolutionStep(tier=2, outcome="miss", code=seg.code, detail=seg.describe()))
            return None
        profile = self.tier2_profile(term, n, seg)
        if not profile.items:
            steps.append(ResolutionStep(tier=2, outcome="miss", code="vocab_partial", detail=seg.describe() + " — 요청 카테고리와 겹치는 Item 없음"))
            return None
        steps.append(ResolutionStep(tier=2, outcome="hit", code="vocab_full", detail=seg.describe()))
        hit = _Hit(tier=2, mode="attribute", profile=profile)
        if seg.unscored:
            hit.notes.append(f"'{term}'의 기장·소매·실루엣 어휘({', '.join(t.text for t in seg.unscored)})는 점수에 쓰지 않았습니다.")
        return hit

    def _requested_majors(self) -> list[str]:
        inc = self.cfg.categories.include
        majors = []
        for r in inc:
            if r.minor is not None:
                majors.append(V.MINOR_TO_MAJOR[r.minor].value)
            elif r.major is not None:
                majors.append(r.major.value)
        return list(dict.fromkeys(majors)) or [m.value for m in V.CategoryMajor]

    def tier2_profile(self, term: str, n: str, seg: Segmentation) -> VirtualProfile:
        use = seg.usable
        minors = [t.value for t in use if t.attr == "category"]
        majors = [t.value for t in use if t.attr == "major"]
        refs: dict[str, list[tuple[str, str]]] = {"color": [], "material": [], "pattern": [], "detail": []}
        fit = None
        focus: list[str] = []
        for t in use:
            if t.attr in ("color", "material", "pattern"):
                refs[t.attr].append((t.value, "primary"))
                focus.append(t.attr)
            elif t.attr in ("detail", "neckline", "collar"):
                refs["detail"].append((V.detail_node_id(V.DetailType(t.attr), t.value), "primary"))
                focus.append("detail")
            elif t.attr == "fit" and fit is None:
                fit = t.value
                focus.append("fit")
            elif t.attr in ("category", "major"):
                focus.append("category")
        focus = list(dict.fromkeys(focus))
        if minors:
            cats = [(V.MINOR_TO_MAJOR[V.CategoryMinor(m)].value, m) for m in dict.fromkeys(minors)]
        elif majors:
            cats = [(m, None) for m in dict.fromkeys(majors)]
        else:
            cats = [(m, None) for m in self._requested_majors()]
        specs = [VirtualItemSpec(category_major=maj, category_minor=mi, level="core",
                                 colors=list(refs["color"]), materials=list(refs["material"]), patterns=list(refs["pattern"]),
                                 details=list(refs["detail"]), fit=fit) for maj, mi in cats]
        return VirtualProfile(tier=2, term=term, norm=n, definition=f"어휘 규칙(별칭 사전)으로 해석: {seg.describe()}",
                              focus_core=focus[:3], focus_supporting=focus[3:], items=specs)

    # ---------- Tier3 ----------
    def embedder(self) -> Any:
        if self._embedder is None:
            self._embedder = importlib.import_module("trend_agent.embeddings")
        return self._embedder

    def tier3_candidates(self, term: str, k: int = 20) -> list[Tier3Candidate]:
        """질의 용어와 가까운 트렌드 그룹(name_norm) — Style 벡터 + Term 벡터(ALIAS_OF로 Style에). T4 보정용 공개 함수."""
        emb = self.embedder()
        vec = emb.embed_texts([term])[0]
        by_id = {s["id"]: s for s in self.styles()}
        score: dict[str, float] = {}
        for sid, sc in emb.search("Style", vec, k=k):
            if sid in by_id:
                score[sid] = max(score.get(sid, -1.0), float(sc))
        try:
            term_hits = emb.search("Term", vec, k=k)
        except Exception:  # noqa: BLE001 — Term 벡터가 아직 없을 수 있다
            term_hits = []
        if term_hits:
            if self._term_ids is None:
                self._load_term_links()
            for tid, sc in term_hits:
                for sid in (self._term_ids or {}).get(tid, []):
                    if sid in by_id:
                        score[sid] = max(score.get(sid, -1.0), float(sc))
        groups: dict[str, Tier3Candidate] = {}
        for sid, sc in score.items():
            g = by_id[sid]["name_norm"]
            if g in self.hidden:
                continue
            c = groups.setdefault(g, Tier3Candidate(g, sc, []))
            c.score = max(c.score, sc)
            c.style_ids.append(sid)
        out = sorted(groups.values(), key=lambda c: (-c.score, c.group_norm))
        for c in out:
            c.style_ids.sort()
        return out

    def tier3(self, term: str, n: str, steps: list[ResolutionStep]) -> Any:
        tc = self.w.resolver.tier3
        if not tc.enabled:
            steps.append(ResolutionStep(tier=3, outcome="skipped", code="tier3_disabled", detail="τ·δ 미보정(weights.yaml resolver.tier3 = null, T4 보정 대기)"))
            return None
        try:
            cands = self.tier3_candidates(term)
        except Exception as e:  # noqa: BLE001
            steps.append(ResolutionStep(tier=3, outcome="error", code="tier3_disabled", detail=f"임베딩 사용 불가: {type(e).__name__}: {str(e)[:160]}"))
            return None
        if not cands:
            steps.append(ResolutionStep(tier=3, outcome="miss", code="below_tau", detail="후보 없음"))
            return None
        best = cands[0].score
        second = cands[1].score if len(cands) > 1 else None
        if best < tc.tau:
            steps.append(ResolutionStep(tier=3, outcome="miss", code="below_tau", best=best, second=second,
                                        detail=f"1위 {cands[0].group_norm} {best:.3f} < τ {tc.tau}"))
            return None
        if second is not None and best - second < tc.delta:
            steps.append(ResolutionStep(tier=3, outcome="miss", code="ambiguous_delta", best=best, second=second,
                                        detail=f"{cands[0].group_norm} {best:.3f} − {cands[1].group_norm} {second:.3f} < δ {tc.delta}"))
            return None
        hits = [s for s in self.styles() if s["id"] in set(cands[0].style_ids)]
        return self._gate(3, "semantic", hits, steps, "semantic_hit", f"{cands[0].group_norm} {best:.3f}",
                          similarity=round(best, 4), best=best, second=second)

    # ---------- Tier4 ----------
    def llm_settings(self) -> tuple[str, Optional[str]]:
        if self._llm_settings is None:
            env = load_env()
            self._llm_settings = (env.get("LLM_BACKEND") or "codex_cli", env.get("LLM_MODEL") or None)
        return self._llm_settings

    def llm(self) -> Any:
        if self._llm is None:
            self._llm = importlib.import_module("trend_agent.llm")
        return self._llm

    def context(self, n: str) -> list[t4.ContextBlock]:
        """Tier4 맥락 블록 (tier4_context=none이면 없음, 숨긴 그룹·지정 기사 제외)."""
        if self.cfg.tier4_context == "none":
            return []
        return t4.context_blocks(n, self.mentions, self.crawl, exclude_articles=self.context_exclude)

    def run_tier4(self, term: str, n: str) -> t4.Tier4Outcome:
        """캐시 조회 → (없으면) LLM 호출·검증·1회 재시도 → 성공하면 term_map.jsonl에 profile 행(캐시) 추가."""
        blocks = self.context(n)
        csha = t4.context_sha(blocks)
        backend, model = self.llm_settings()
        out = t4.Tier4Outcome(code="llm_unavailable", backend=backend, model=model, context_sha=csha)
        cached = tm.find_profile(self.term_rows, n, backend, model, t4.PROMPT_VERSION, csha)
        if cached is not None:
            p, errs = t4.check_profile(cached.profile or {}, blocks)
            if p is not None and not errs:
                out.code, out.profile, out.raw = "llm_cache_hit", p, cached.profile
                return out
        if not self.cfg.use_llm:
            out.detail = "LLM 호출 끔(--no-llm)"
            return out
        try:
            llm = self.llm()
            call = llm.call  # 지연 로딩: 구현(T1 client.py)이 없으면 여기서 실패한다
            output_error = getattr(llm, "LLMOutputError", None)
        except Exception as e:  # noqa: BLE001
            out.detail = f"llm 구현 없음: {type(e).__name__}: {str(e)[:160]}"
            return out
        schema = t4.tier4_schema()
        errs: list[str] = []
        for attempt in (1, 2):
            out.attempts = attempt
            prompt = t4.render_prompt(term, blocks, errs if attempt == 2 else None)
            try:
                res = call(prompt, schema, purpose=t4.PURPOSE, prompt_version=t4.PROMPT_VERSION)
            except Exception as e:  # noqa: BLE001
                if output_error is not None and isinstance(e, output_error):
                    errs = [f"출력 오류: {str(e)[:300]}"]
                    continue
                out.detail = f"{type(e).__name__}: {str(e)[:200]}"
                return out
            meta = getattr(res, "meta", {}) or {}
            out.backend = meta.get("backend") or backend
            out.model = meta.get("model") or model
            p, errs = t4.check_profile(res.data, blocks)
            if p is not None and not errs:
                out.code, out.profile, out.raw = "llm_profile", p, res.data
                self._cache(term, n, out)
                return out
        out.code, out.errors = "llm_invalid_output", errs
        out.detail = "; ".join(errs[:5])
        return out

    def _cache(self, term: str, n: str, out: t4.Tier4Outcome) -> None:
        row = tm.TermMapRow(row="profile", term=term, kind="trend_word", origin="llm", backend=out.backend, model=out.model,
                            prompt_version=t4.PROMPT_VERSION, context_sha=out.context_sha, profile=out.raw,
                            created_at=tm.now_kst(), added_by="T3")
        if row.norm != n:  # 표기 정규화가 질의 norm과 다르면 캐시 키가 어긋난다 — 쓰지 않는다
            return
        self.term_rows.append(row)
        tm.append_rows([row], self.term_map_path)

    def tier4(self, term: str, n: str, steps: list[ResolutionStep]) -> Any:
        blocks = self.context(n)
        if self.context_exclude and self.cfg.tier4_context != "none":
            ka, kr = t4.excluded_evidence(n, self.mentions, self.context_exclude)
            if kr:
                steps.append(ResolutionStep(tier=4, outcome="skipped", code="hidden_by_eval",
                                            detail=f"Tier4 맥락에서 평가 숨김·지정 기사 {ka}개의 근거 {kr}행을 뺐다"))
        o = self.run_tier4(term, n)
        trace = LLMTrace(backend=o.backend or "unknown", model=o.model, prompt_version=t4.PROMPT_VERSION,
                         cache="hit" if o.code == "llm_cache_hit" else "miss", context_sha=o.context_sha,
                         evidence="provided" if blocks else "none")
        self._last_trace = trace
        if o.code in ("llm_unavailable", "llm_invalid_output"):
            off = not self.cfg.use_llm and o.code == "llm_unavailable"
            steps.append(ResolutionStep(tier=4, outcome="skipped" if off else "error", code=o.code, detail=o.detail))
            return None
        assert o.profile is not None
        if not o.profile.recognized:
            steps.append(ResolutionStep(tier=4, outcome="miss", code="llm_unrecognized", detail=f"LLM이 패션 트렌드로 모르는 용어 '{term}' — 지어내지 않음"))
            return STOP
        vp = t4.to_virtual(o.profile, term, n, blocks)
        if self.cfg.categories.include or self.cfg.categories.exclude:
            vp.items = [i for i in vp.items if self._cat_ok(i.category_major, i.category_minor)]
        steps.append(ResolutionStep(tier=4, outcome="hit", code=o.code,
                                    detail=f"{o.backend}/{o.model} {t4.PROMPT_VERSION} · 근거 {'있음' if not o.profile.no_evidence else '없음(no_evidence)'} · Item {len(vp.items)}"))
        return _Hit(tier=4, mode="llm_inferred", profile=vp, llm=trace)

    # ---------- 카테고리 필터 ----------
    @staticmethod
    def _ref_ok(ref: CategoryRef, major: Optional[str], minor: Optional[str]) -> bool:
        if ref.minor is not None:
            return minor == ref.minor.value
        return ref.major is not None and major == ref.major.value

    def _cat_ok(self, major: Optional[str], minor: Optional[str]) -> bool:
        c = self.cfg.categories
        if c.include and not any(self._ref_ok(x, major, minor) for x in c.include):
            return False
        return not any(self._ref_ok(x, major, minor) for x in c.exclude)

    # ---------- 용어 하나 ----------
    def resolve_term(self, term: str) -> tuple[TermResolution, Optional[_Hit]]:
        n = normalize_term(term)
        steps: list[ResolutionStep] = []
        self._last_trace = None
        hit = None
        tiers = (self.tier1, self.tier2, self.tier3, self.tier4)
        for t, fn in enumerate(tiers, 1):
            if self.cfg.force_tier is not None and t != self.cfg.force_tier:
                steps.append(ResolutionStep(tier=t, outcome="skipped", code="forced_tier", detail=f"평가 실행: Tier{self.cfg.force_tier}만 시도"))
                continue
            r = fn(term, n, steps)
            if r is STOP:
                break
            if r is not None:
                hit = r
                break
        res = TermResolution(term=term, norm=n, status="resolved" if hit else "failed", steps=steps,
                             llm=hit.llm if hit else self._last_trace)
        if hit:
            res.tier, res.match_mode = hit.tier, hit.mode
            res.qualification = QUALIFICATION_BY_MODE[hit.mode]
            res.similarity = hit.similarity
        return res, hit

    # ---------- 조립 ----------
    def _pool(self) -> int:
        return max(self.cfg.top_k, self.w.trend_topk.pool_per_item)

    def _graph_trends(self, term: str, hit: _Hit, stats: dict[str, int], evidence: dict[str, EvidenceRecord]
                      ) -> tuple[list[Trend], list[ItemRanking]]:
        sids = [s["id"] for s in hit.styles]
        items_by = self.s.style_items(sids)
        rows: list[tuple[dict[str, Any], list[tuple[ItemRecord, list[ScoredProduct]]]]] = []
        rankings: list[ItemRanking] = []
        for s in hit.styles:
            core, sup = s.get("focus_core", []), s.get("focus_supporting", [])
            its = []
            for item in items_by.get(s["id"], []):
                minor = item.node.category_minor.value if item.node.category_minor else None
                if not self._cat_ok(item.category_major.value, minor):
                    continue
                ranked = self.s.rank_item(item, core, sup, top_k=self._pool(), stats=stats)
                self._note_weak(term, item)
                its.append((item, ranked))
                rankings.append(ItemRanking(item.node.id, item.node.name, 1.0, ranked))
            rows.append((s, its))
        ev, ev_by_node = self.s.evidence_for(sids + [i.node.id for _, its in rows for i, _ in its])
        evidence.update(ev)
        trends = []
        for s, its in rows:
            style = self.s.trend_style(s, ev_by_node.get(s["id"], []), season_status=hit.season_status.get(s["id"]))
            items = [self.s.trend_item(item, ranked[: self.cfg.top_k], [e["id"] for e in ev_by_node.get(item.node.id, [])])
                     for item, ranked in its]
            trends.append(Trend(style=style, items=items, term=term, match_mode=hit.mode,
                                qualification=QUALIFICATION_BY_MODE[hit.mode], similarity=hit.similarity))
        return trends, rankings

    def _virtual_trend(self, term: str, hit: _Hit, stats: dict[str, int], evidence: dict[str, EvidenceRecord]
                       ) -> tuple[list[Trend], list[ItemRanking]]:
        vp = hit.profile
        assert vp is not None
        ev_ids = []
        for aid, b, quote in vp.evidence:
            art = self.crawl.get(aid) or {}
            eid = evidence_id(aid, b, quote)
            evidence.setdefault(eid, EvidenceRecord(evidence_id=eid, article_id=aid, title=art.get("title"), url=art.get("url"),
                                                    published_at=(art.get("published_at") or "")[:10] or None, block_idx=b, quote=quote))
            ev_ids.append(eid)
        items, rankings = [], []
        for k, spec in enumerate(vp.items):
            if not self._cat_ok(spec.category_major, spec.category_minor):
                continue
            iid = vp.item_id(k)
            rec = spec.record(iid, spec.label(term))
            vw = spec.value_weights(self.w) if vp.value_weighted else None
            opts = ScoringOptions.from_weights(self.w, tier4=vp.tier == 4, value_weights=vw)
            ranked = self.s.rank_item(rec, vp.focus_core, vp.focus_supporting, top_k=self._pool(), options=opts, stats=stats)
            self._note_weak(term, rec)
            focus = vp.item_focus(spec, self.w)
            items.append(self.s.trend_item(rec, ranked[: self.cfg.top_k], [], virtual=True, item_focus=focus,
                                           value_weights=vw or {}))
            rankings.append(ItemRanking(iid, rec.node.name, focus, ranked))
        style = TrendStyle(id=vp.style_id, name=term, name_origin="query", definition=vp.definition,
                           description=[DescriptionOut(text=vp.definition, evidence_ids=ev_ids)],
                           focus_core=vp.focus_core, focus_supporting=vp.focus_supporting, focus_evidence_ids=[],
                           season_status="not_checked", virtual=True)
        return [Trend(style=style, items=items, term=term, match_mode=hit.mode,
                      qualification=QUALIFICATION_BY_MODE[hit.mode], similarity=hit.similarity)], rankings

    def _note_weak(self, term: str, item: ItemRecord) -> None:
        if not any(attribute_values(item, a) for a in SCORED_ATTRIBUTES):
            self._weak_items.append(f"{term}: {item.node.name}")

    def _limit_notes(self, res: TermResolution, hit: _Hit) -> list[str]:
        out = list(hit.notes)
        if hit.mode in ("graph", "semantic") and self.policy == "report":
            off = [s for s in hit.styles if hit.season_status.get(s["id"]) != "match"]
            if off:
                out.append(f"'{res.term}': {self.key} 시즌과 맞지 않거나 시즌이 확인되지 않은 트렌드도 제외하지 않고 보여 줍니다"
                           f"(season_policy=report): " + ", ".join(f"{s['name']}({hit.season_status[s['id']]})" for s in off))
        if hit.mode == "semantic":
            out.append(f"'{res.term}' — 그래프에 같은 이름이 없어 의미 유사도로 가장 가까운 트렌드를 골랐습니다(유사도 {hit.similarity}).")
        if hit.mode == "attribute":
            out.append(f"'{res.term}' — 그래프에 없어 별칭 사전 규칙으로 만든 가상 트렌드입니다(qualification=vocabulary_rule, 기사 근거 없음).")
        if hit.mode == "llm_inferred":
            ev = "있음" if hit.profile and hit.profile.evidence else "없음(LLM 일반 지식)"
            out.append(f"'{res.term}' — 그래프에 없어 LLM이 속성 등급만 제안하고 점수는 코드가 계산한 가상 트렌드입니다"
                       f"(qualification=agent_proposal, 기사 근거 {ev}). 사람 확인이 필요합니다.")
        return out

    def _finish(self, request_id: str, entries: list[tuple[TermResolution, Optional[_Hit]]], pre_limits: list[str]) -> TrendAnalysisResult:
        trends: list[Trend] = []
        evidence: dict[str, EvidenceRecord] = {}
        limitations = list(pre_limits)
        stats: dict[str, int] = {}
        self._weak_items = []
        uses_snap = has_vogue_images = False
        for res, hit in entries:
            if hit is None:
                limitations.append(f"'{res.term}' 해석 실패: " + " → ".join(f"T{s.tier}:{s.code}" for s in res.steps))
                continue
            if hit.mode in ("graph", "semantic"):
                ts, rankings = self._graph_trends(res.term, hit, stats, evidence)
            else:
                ts, rankings = self._virtual_trend(res.term, hit, stats, evidence)
            res.style_ids = [t.style.id for t in ts]
            res.top_products = trend_topk(rankings, self.cfg.top_k, image_first=self.s.options.image_first_tiebreak)
            if not res.top_products:
                res.steps.append(ResolutionStep(tier=hit.tier, outcome="miss", code="no_products",
                                                detail="해석은 됐지만 후보 상품이 없음(카테고리·속성 조건)"))
            limitations += self._limit_notes(res, hit)
            trends += ts
            uses_snap |= any(p.source == "musinsa_snap" for p in res.top_products)
            has_vogue_images |= any(it.image_urls for t in ts for it in t.items)
        produced = [r for r, _ in entries if r.top_products]
        status = "no_results" if not produced else "ok" if len(produced) == len(entries) else "partial"
        if uses_snap:
            limitations.append("musinsa_snap 상품은 착장 사진 속 아이템이라 구매 가능한 상품이 아니며 색 정보가 없습니다 (kind=snap_garment).")
        if has_vogue_images:
            limitations.append("Vogue 기사 이미지(image_urls)는 링크로만 제공합니다(G0-⑤: 임베드·핫링크·다운로드 금지).")
        limitations += scoring_limitations(stats)
        if self._weak_items:
            limitations.append("속성 값(색·소재·패턴·디테일·핏)이 없는 Item은 하분류 일치만으로 골라 동점이 많고 동점 순서는 id 순입니다"
                               "(순위 근거 약함): " + ", ".join(dict.fromkeys(self._weak_items)))
        if self.context_exclude and self.cfg.tier4_context != "none":
            limitations.append(f"평가 실행: Tier4 맥락에서 기사 {len(self.context_exclude)}개를 뺐습니다"
                               f"(숨긴 그룹의 출처 기사·지정 기사): {', '.join(sorted(self.context_exclude))}")
        tc = self.w.resolver.tier3
        settings = AppliedSettings(
            weights_sha=self.w.sha, season_policy=self.policy, image_first_tiebreak=self.s.options.image_first_tiebreak,
            core_unknown_decay=self.s.options.core_unknown_decay, snap_id_limit=self.s.options.snap_id_limit,
            item_gender_filter=self.s.options.item_gender_filter, value_weights=dict(self.w.tier4.value_weights),
            tier3={"model": tc.model, "tau": tc.tau, "delta": tc.delta}, top_k=self.cfg.top_k,
            hidden_style_groups=self.hidden, tier4_context=self.cfg.tier4_context, force_tier=self.cfg.force_tier)
        applied = AppliedFilters(season=self.season, categories=self.cfg.categories)
        return TrendAnalysisResult(request_id=request_id, status=status, applied_filters=applied, trends=trends,
                                   evidence=list(evidence.values()), limitations=list(dict.fromkeys(limitations)),
                                   resolutions=[r for r, _ in entries], applied_settings=settings)

    def _pre_limits(self) -> list[str]:
        out = []
        if self.cfg.season is None:
            out.append(f"시즌 미지정 → {self.season.year} {self.season.season.value}로 해석했습니다.")
        cats = self.cfg.categories
        if cats.include and all(r.major is None and r.minor is None for r in cats.include):
            out.append("요청한 카테고리가 모두 지원 범위(상의·하의·아우터·원피스) 밖입니다.")
        return out

    # ---------- 진입점 ----------
    def run(self, terms: list[str], request_id: str) -> TrendAnalysisResult:
        """용어 목록 → 결과. 같은 정규화명은 한 번만 해석한다."""
        seen: set[str] = set()
        entries = []
        for t in terms:
            n = normalize_term(t)
            if not n or n in seen:
                continue
            seen.add(n)
            entries.append(self.resolve_term(t))
        return self._finish(request_id, entries, self._pre_limits())

    def run_articles(self, article_ids: list[str], request_id: str) -> TrendAnalysisResult:
        """`--article-id` / `--from-extractions`: 그 기사의 Style을 용어 대신 Tier1로 바로 쓴다(시즌은 report로 보고만)."""
        pre = self._pre_limits()
        entries = []
        for aid in article_ids:
            styles = [s for s in self.styles() if s["article_id"] == aid]
            if not styles:
                pre.append(f"기사 {aid}의 Style이 그래프에 없습니다(적재 전이거나 Style 0개).")
                continue
            for s in styles:
                status = self._season_status(s)
                steps = [ResolutionStep(tier=1, outcome="hit", code="name_exact", detail=f"--article-id {aid}: {s['name']} ({s['id']})")]
                if status != "match":
                    steps.append(ResolutionStep(tier=1, outcome="hit", code="season_reported", detail=f"{self.key}: {status}"))
                res = TermResolution(term=s["name"], norm=s["name_norm"], status="resolved", tier=1, match_mode="graph",
                                     qualification="article_evidence", steps=steps)
                entries.append((res, _Hit(tier=1, mode="graph", styles=[s], season_status={s["id"]: status})))
        return self._finish(request_id, entries, pre)
