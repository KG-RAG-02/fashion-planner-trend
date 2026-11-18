"""compare_extraction: 정규화·Style/Item 매칭·속성 건수·음성·_review·스키마 실패·test 가드·백엔드 열 (합성 입력, Vogue 원문 없음)"""
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

from trend_agent.schemas.article_extraction import ArticleExtraction

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("compare_extraction", ROOT / "scripts" / "compare_extraction.py")
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


def it(name, raw, minor=None, major=None, ev=(0,), **kw):
    d = {"name": name, "raw_text": raw, "category_minor": minor, "category_major": major,
         "evidence": [{"block_idx": b, "quote": "q"} for b in ev]}
    d.update(kw)
    return d


def st(name, items=(), seasons=(), focus=(), aliases=()):
    return {"name": name, "name_origin": "article", "aliases": list(aliases), "definition": "d",
            "seasons": [{"year": y, "season": s} for y, s in seasons],
            "focus_attributes": [{"attribute": a, "level": l, "evidence": [{"block_idx": 0, "quote": "q"}]} for a, l in focus],
            "items": list(items)}


def ax(*styles, aid="000001"):
    return ArticleExtraction.model_validate({"article_id": aid, "styles": list(styles)})


# ---------- 정규화·매칭 ----------
def test_style_keys_split_parentheses_and_aliases():
    assert ce.style_keys("보라색(퍼플)") == {"보라색", "퍼플"}
    assert ce.style_keys("뉴 로", ["New Raw"]) == {"뉴로", "newraw"}
    assert ce.style_keys("2026FW 트렌드(레이디라이크, 아르데코·몸으로의 회귀)") >= {"레이디라이크", "아르데코", "몸으로의회귀"}


def test_match_styles_exact_by_alias_then_similar_name_then_unmatched():
    gold = ax(st("뉴 로", aliases=["New Raw"]), st("절제된 맥시멀리즘"), st("바스크 웨이스트")).styles
    pred = ax(st("New Raw 트렌드"), st("절제된 맥시멀리즘 룩"), st("전혀 다른 것")).styles
    pairs, mg, mp = ce.match_styles(gold, pred)
    assert sorted(pairs) == [(0, 0, "exact"), (1, 1, "exact")]  # 비교 도구는 끝의 일반어('트렌드'·'룩')를 무시하는 느슨한 키를 쓴다
    assert mg == [2] and mp == [2]
    pairs, _, _ = ce.match_styles(ax(st("시럽코어 컬러")).styles, ax(st("시럽코어 칼라")).styles)
    assert pairs == [(0, 0, "similar")]


def test_match_items_strict_relaxed_and_unsupported():
    g = ax(st("s", [it("핑크 셔츠", "핑크 셔츠가 채운다", "셔츠", ev=(3,)),
                    it("트랙 팬츠", "폴카 도트 트랙 팬츠", "팬츠"),
                    it("뮬", "디올의 조각 같은 뮬", category_raw="뮬"),
                    it("타이츠", "핑크 타이츠")])).styles[0].items
    p = ax(st("s", [it("핑크색 셔츠", "다름 아닌 핑크 셔츠가 채운다", "셔츠", ev=(3,)),
                    it("트랙 팬츠", "폴카 도트 트랙 팬츠", "조거팬츠"),
                    it("조각 같은 뮬", "디올의 조각 같은 뮬")])).styles[0].items
    pairs, mg, mp = ce.match_items(g, p)
    assert sorted(pairs) == [(0, 0, "strict"), (1, 1, "relaxed"), (2, 2, "strict")]  # 뮬: 하분류·상분류 둘 다 null
    assert mg == [3] and mp == []


# ---------- 집계 ----------
def test_compare_article_counts_styles_seasons_focus_items_attrs():
    gold = ax(st("핑크", [it("핑크 셔츠", "핑크 셔츠", "셔츠", colors=[{"raw": "핑크", "value": "핑크"}],
                            details=[{"raw": "러플", "type": "detail", "value": "러플"}], fit_raw="오버", fit="오버사이즈"),
                        it("핑크 타이츠", "핑크 타이츠", category_raw="타이츠")],
                 seasons=[(2026, "SS")], focus=[("color", "core"), ("fit", "supporting")]),
              st("없어진 트렌드", [it("니트", "니트", "니트웨어")]))
    pred = ax(st("핑크", [it("핑크 셔츠", "핑크 셔츠", "셔츠", colors=[{"raw": "핑크", "value": "핑크"}, {"raw": "흰", "value": "화이트"}],
                            materials=[{"raw": "새틴", "value": None}], gender="여")],
                 seasons=[(2026, "SS"), (2026, "FW")], focus=[("color", "supporting"), ("pattern", "core")]),
              st("지어낸 트렌드", [it("코트", "코트", "코트")]))
    c = Counter()
    det = ce.compare_article(gold, pred, c, None)
    assert c["Style|일치"] == 1 and c["Style|누락"] == 1 and c["Style|오추출"] == 1
    assert c["시즌|부분"] == 1
    assert (c["focus (속성, level)|level 다름"], c["focus (속성, level)|누락"], c["focus (속성, level)|오추출"]) == (1, 1, 1)
    assert c["Item|strict 일치"] == 1 and c["Item|누락(범위 밖)"] == 1 and c["Item|누락(범위 내)"] == 1 and c["Item|오추출(범위 내)"] == 1
    assert c["속성 category_minor|일치"] == 1 and c["속성 category_major|일치"] == 1
    assert c["속성 fit|누락"] == 1 and c["속성 gender|오추출"] == 1 and c["속성 shape|둘 다 null"] == 1
    assert (c["속성 colors|일치"], c["속성 colors|오추출"]) == (1, 1)
    assert c["속성 details|누락"] == 1 and c["속성 materials|표준값 없음(예측)"] == 1 and c["속성 materials|오추출"] == 0
    assert det["missed_styles"] == ["없어진 트렌드"] and det["false_styles"] == ["지어낸 트렌드"]
    assert det["styles"][0]["items"][0]["diff"]["colors"] == {"누락": [], "오추출": ["화이트"]}


def test_raw_only_prediction_counts_as_raw_match():
    """D-43: 정답 드롭웨이스트(raw '낮은 허리선') ↔ 예측 value=null raw '낮은 허리선' → 원문 일치, 누락 아님."""
    gold = ax(st("비율", [it("드레스", "드레스", "드레스",
                             details=[{"raw": "낮은 허리선", "type": "detail", "value": "드롭웨이스트"}],
                             colors=[{"raw": "칠흑 같은 블랙", "value": "블랙"}, {"raw": "흰", "value": "화이트"}])]))
    pred = ax(st("비율", [it("드레스", "드레스", "드레스",
                             details=[{"raw": "낮은 허리선", "type": "detail", "value": None}],
                             colors=[{"raw": "블랙 컬러", "value": None}, {"raw": "체크", "value": None}])]))
    c = Counter()
    det = ce.compare_article(gold, pred, c, None)
    assert c["속성 details|원문 일치"] == 1 and c["속성 details|누락"] == 0 and c["속성 details|표준값 없음(예측)"] == 0
    # 색: 블랙은 예측 raw가 표준 값 이름을 포함 → 원문 일치, 화이트는 맞는 raw가 없어 누락, '체크'는 표준값 없음(예측)으로 남음
    assert (c["속성 colors|원문 일치"], c["속성 colors|누락"], c["속성 colors|표준값 없음(예측)"]) == (1, 1, 1)
    diff = det["styles"][0]["items"][0]["diff"]
    assert diff["details"]["원문 일치"] == ["드롭웨이스트 ← 낮은 허리선"] and diff["colors"]["누락"] == ["화이트"]


def test_raw_equivalent_rules():
    assert ce.raw_equivalent("얼룩진", "디스트로이드", "얼룩")          # 포함 관계
    assert ce.raw_equivalent("허리선이 내려간", "드롭웨이스트", "드롭 웨이스트 라인")  # 표준 값 이름 포함
    assert not ce.raw_equivalent("얼룩진", "디스트로이드", "주름")
    assert not ce.raw_equivalent("레이스", "레이스", "")


def test_negative_article_counts_invented_styles():
    c = Counter()
    ce.compare_article(ax(), ax(st("가짜1"), st("가짜2")), c, None)
    assert (c["음성 기사|기사 수"], c["음성 기사|Style 낸 기사"], c["음성 기사|낸 Style 수"], c["Style|오추출"]) == (1, 1, 2, 2)
    c = Counter()
    ce.compare_article(ax(), ax(), c, None)
    assert c["음성 기사|기사 수"] == 1 and c["음성 기사|Style 낸 기사"] == 0


def test_v2_v3_counted_on_predictions():
    texts = {0: "핑크 셔츠가 빈자리를 채운다", 2: "소제목"}
    pred = ax(st("핑크", [{"name": "셔츠", "raw_text": "셔츠", "category_minor": "셔츠", "fit_raw": "루즈",
                          "evidence": [{"block_idx": 0, "quote": "없는 문장"}]}]))
    c = Counter()
    det = ce.compare_article(ax(), pred, c, texts)
    assert c["예측 근거|V2 위반"] == 1 and c["예측 근거|V3 위반"] == 1 and len(det["v2"]) == 1


def test_length_rule_maps_long_to_maxi_only_for_lower_and_onepiece():
    e = ax(st("s", [it("스커트", "s", "스커트", length_body_raw="롱", length_body="롱"),
                    it("코트", "c", "코트", length_body_raw="롱", length_body="롱"),
                    it("드레스", "d", "드레스", length_body_raw="길고", length_body="롱")]))
    assert ce.apply_length_rule(e) == 2
    assert [i.length_body.value for i in e.styles[0].items] == ["맥시", "롱", "맥시"]
    assert ce.apply_length_rule(e) == 0  # 멱등


# ---------- 입력·가드·출력 ----------
def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_load_pred_formats_review_and_schema_failure(tmp_path):
    good = {"article_id": "1", "styles": []}
    _write(tmp_path / "1.json", {"label_meta": {"prompt_version": "article-extraction"}, "extraction": good})
    _write(tmp_path / "2.json", good)                                  # ArticleExtraction 단독
    _write(tmp_path / "_review" / "3.json", {"label_meta": {}, "extraction": good})
    _write(tmp_path / "4.json", {"extraction": {"article_id": "4", "styles": [{"name": "x"}]}})
    assert ce.load_pred(tmp_path, "1", False)[1:] == ("ok", {"prompt_version": "article-extraction"})
    assert ce.load_pred(tmp_path, "2", False)[1] == "ok"
    assert ce.load_pred(tmp_path, "3", False)[:2] == (None, "review")
    assert ce.load_pred(tmp_path, "3", True)[1] == "review_compared"
    assert ce.load_pred(tmp_path, "4", False)[:2] == (None, "schema_fail")
    assert ce.load_pred(tmp_path, "5", False)[:2] == (None, "missing")


@pytest.fixture
def repo(tmp_path):
    """라벨 3건(dev 2 · test 1) + manifest + 백엔드 2개의 합성 예측."""
    labels = tmp_path / "labels"
    gold = {"d1": ax(st("핑크", [it("핑크 셔츠", "핑크 셔츠", "셔츠")]), aid="d1"),
            "d2": ax(aid="d2"), "t1": ax(st("화이트"), aid="t1")}
    for k, v in gold.items():
        _write(labels / f"{k}.json", {"label_meta": {}, "extraction": v.model_dump(mode="json")})
    man = tmp_path / "manifest.yaml"
    man.write_text("articles:\n- {article_id: d1, split: dev}\n- {article_id: d2, split: dev}\n- {article_id: t1, split: test}\n", encoding="utf-8")
    a, b = tmp_path / "pred_a", tmp_path / "pred_b"
    _write(a / "d1.json", {"label_meta": {"prompt_version": "pv1"}, "extraction": gold["d1"].model_dump(mode="json")})
    _write(a / "d2.json", {"label_meta": {"prompt_version": "pv1"}, "extraction": ax(st("가짜"), aid="d2").model_dump(mode="json")})
    _write(b / "_review" / "d1.json", {"extraction": gold["d1"].model_dump(mode="json")})
    _write(a / "t1.json", {"extraction": gold["t1"].model_dump(mode="json")})
    return tmp_path, labels, man, a, b


def test_select_ids_and_test_guard(repo, tmp_path):
    root, labels, man, a, b = repo
    m = ce.load_manifest(man)
    assert ce.select_ids(m, labels, "dev", None) == ["d1", "d2"]
    assert ce.select_ids(m, labels, "all", None) == ["d1", "d2", "t1"]
    assert ce.needs_test_confirm(m, ["d1", "t1"]) == ["t1"]
    out = tmp_path / "out"
    rc = ce.main(["--pred", f"a={a}", "--split", "test", "--labels", str(labels), "--manifest", str(man), "--out", str(out),
                  "--access-log", str(tmp_path / "acc.jsonl")])
    assert rc == 2 and not out.exists() and not (tmp_path / "acc.jsonl").exists()   # 확인 없이는 열지 않는다
    rc = ce.main(["--pred", f"a={a}", "--ids", "t1", "--confirm-test", "--labels", str(labels), "--manifest", str(man),
                  "--out", str(out), "--access-log", str(tmp_path / "acc.jsonl")])
    assert rc == 0
    rec = json.loads((tmp_path / "acc.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["test_ids"] == ["t1"] and rec["kind"] == "compare_extraction" and "a" in rec["pred"]


def test_main_writes_two_backend_report(repo, tmp_path, monkeypatch):
    root, labels, man, a, b = repo
    monkeypatch.setattr(ce.VL, "load_articles", lambda: {})
    out = tmp_path / "run"
    assert ce.main(["--pred", f"backend_a={a}", "--pred", f"openai_api={b}", "--labels", str(labels), "--manifest", str(man),
                    "--out", str(out)]) == 0
    s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    A, B = s["counts"]["backend_a"], s["counts"]["openai_api"]
    assert A["기사|대상"] == 2 and A["Style|일치"] == 1 and A["음성 기사|낸 Style 수"] == 1 and A["Item|strict 일치"] == 1
    assert B["기사|예측 없음(검토 대기)"] == 1 and B["기사|예측 없음(파일 없음)"] == 1
    assert s["meta"]["pred_meta"]["backend_a"]["prompt_version=pv1"] == 2
    assert s["meta"]["length_rule"] is False  # D-38: 롱은 롱 — L-맥시 보정은 기본 끔
    rep = (out / "report.md").read_text(encoding="utf-8")
    assert "| 지표 | backend_a | openai_api |" in rep and "| Style | 일치 1 · 유사 이름 0 · 누락 0 · 오추출 1 |" in rep
    rows = [json.loads(l) for l in (out / "details.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(r["backend"], r["article_id"], r["status"]) for r in rows} >= {("openai_api", "d1", "review"), ("backend_a", "d2", "ok")}
    assert ce.main(["--pred", f"openai_api={b}", "--include-review", "--labels", str(labels), "--manifest", str(man),
                    "--out", str(tmp_path / "run2")]) == 0
    s2 = json.loads((tmp_path / "run2" / "summary.json").read_text(encoding="utf-8"))
    for flags, want in ((["--length-rule"], True), (["--no-length-rule"], False)):  # 재현용 켜기 · 예전 플래그 호환
        o = tmp_path / ("run_" + flags[0].strip("-"))
        assert ce.main(["--pred", f"openai_api={b}", "--labels", str(labels), "--manifest", str(man), "--out", str(o), *flags]) == 0
        assert json.loads((o / "summary.json").read_text(encoding="utf-8"))["meta"]["length_rule"] is want
    assert s2["counts"]["openai_api"]["기사|검토 대기 포함 비교"] == 1 and s2["counts"]["openai_api"]["Style|일치"] == 1
