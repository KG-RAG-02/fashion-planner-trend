"""02 다중 의류 분해 — 상품명 규칙. 근거: handoffs/T2-L0,T3-design-memo-g0.md §3.1, 계약 v1.1 ProductNode."""
import pytest

from trend_agent.ingestion.normalization.otpensource import OtpenNormalizer
from trend_agent.ingestion.normalization.set_split import SetSplitter, apply_split


@pytest.fixture(scope="module")
def sp():
    return SetSplitter()


def kinds(d):
    return [(c.category_minor, c.category_major) for c in d.components]


@pytest.mark.parametrize("name,expected", [
    ("[SET] 샘플 오버 후드 + 조거 팬츠", [("후드티", "상의"), ("조거팬츠", "하의")]),
    ("샘플 오버핏 기모 후드티 조거 트레이닝바지 세트", [("후드티", "상의"), ("조거팬츠", "하의")]),
    ("[SET]샘플 기모 후드+스웻셔츠", [("후드티", "상의"), ("티셔츠", "상의")]),
    ("샘플 퀼팅 패딩+베스트 세트", [("패딩", "아우터"), ("베스트", "아우터")]),
    ("[SET] Sample jacket & Sample Pleats Skirt", [("재킷", "아우터"), ("스커트", "하의")]),
    ("[SET UP]샘플 와이드 팬츠&오버핏 수트 정장 세트", [("팬츠", "하의"), ("재킷", "아우터")]),
    ("[세트] 샘플 아노락 후드 반바지", [("재킷", "아우터"), ("팬츠", "하의")]),
    ("샘플 린넨 나시가디건 세트", [("탑", "상의"), ("가디건", "아우터")]),
    ("SAMPLE BOLERO KNIT ONEPIECE SET GREY", [(None, "아우터"), ("드레스", "원피스")]),
    ("샘플 여름 4종세트 (가디건+티블라우스+팬츠+원피스)",
     [("가디건", "아우터"), ("블라우스", "상의"), ("팬츠", "하의"), ("드레스", "원피스")]),
    ("샘플 벨벳 셋업 상하의세트", [(None, "상의"), (None, "하의")]),
    ("[SET] 샘플 후드집업 팬츠 셋업", [("짚업", "아우터"), ("팬츠", "하의")]),
    ("샘플 브이넥 스웻 셔츠 + 스커트 SET", [("티셔츠", "상의"), ("스커트", "하의")]),
    ("[SET] 샘플 브레이커 자켓 + 샘플 브레이커 팬츠", [("재킷", "아우터"), ("팬츠", "하의")]),
    ("샘플 집업+샘플 플레어 팬츠", [("짚업", "아우터"), ("팬츠", "하의")]),
    ("샘플 니트 셋업 (니트+팬츠)", [("니트웨어", "상의"), ("팬츠", "하의")]),
    ("(SET) SAMPLE BOLERO & TUBE DRESS TOP - CHARCOAL", [(None, "아우터"), ("탑", "상의")]),
])
def test_split(sp, name, expected):
    d = sp.decide(name)
    assert d.decision == "split", (name, d.decision, d.reason)
    assert kinds(d) == expected


@pytest.mark.parametrize("name", [
    "[SET] 샘플 스트라이프 셔츠 & 스퀘어 팁 니트 타이 아이보리",
    "샘플 블레이저 & 넥타이 세트_다크 네이비",
    "샘플 퀼팅 자켓 + 머플러 세트 [아이보리]",
    "SAMPLE KNIT ONEPIECE & WARMER SET_blue",
    "[00-00]샘플 플리츠 롱스커트-벨트세트 브라운",
])
def test_accessory_set_not_split(sp, name):
    d = sp.decide(name)
    assert d.decision == "accessory_set" and len(d.components) == 1 and d.accessories, (name, d)


@pytest.mark.parametrize("name,reason", [
    ("샘플 카라 아노락 우븐 셋업 와이드핏 쇼츠(웜 그레이)", "marker_between_components"),
    ("샘플 셋업 자켓 (베이지)", "marker_single_kind"),
    ("샘플 울 셋업 수트 애쉬그레이", "marker_single_kind"),
    ("샘플 트위드 투피스", "marker_single_kind"),
    ("[SET][ 블레이저 OR 초어자켓 선택] 샘플 코듀로이 셋업 (브라운)", "choice"),
    ("Sample layered skirt pants set BLACK", "marker_single_kind"),
    ("Sample sleeveless two piece like jumpsuit", "marker_between_components"),
])
def test_candidate(sp, name, reason):
    d = sp.decide(name)
    assert (d.decision, d.reason) == ("candidate", reason), (name, d)


@pytest.mark.parametrize("name,size", [
    ("[2PACK] 샘플 롱 슬리브리스", 2), ("샘플 기모 레깅스 3PACK (유발 / 무발)", 3),
    ("[SET] 샘플 래글런 후드티 2종 세트", 2), ("[SET]샘플 밴딩 슬랙스+샘플 와이드 밴딩 슬랙스 - 4Color", 2),
    ("SAMPLE COTTON SHORTS PANTS 2PACK SET", 2), ("샘플 슬리브리스 탑_일반 기장 2팩", 2),
])
def test_multipack(sp, name, size):
    d = sp.decide(name)
    assert (d.decision, d.pack_size) == ("multipack", size), (name, d)


@pytest.mark.parametrize("name", [
    "sample straight denim pants black washed", "SAMPLE CORSET KNIT white", "KNIT&SAMPLE TRUCKER JACKET",
    "샘플 베이스볼 후드자켓(네이비+레드)", "샘플+ 박스 슬리브리스 블랙", "샘플 피그먼트 워싱팬츠 4종 브라운",
    "샘플 기모 카라 티셔츠(AB12CD) 그레이", "베이직 티셔츠",
])
def test_none(sp, name):
    assert sp.decide(name).decision == "none", (name, sp.decide(name))


# ---------- Product 생성 ----------
def _row(name, **kw):
    base = {"_file": "sorted_아우터/sorted_슈트_블레이저_재킷.json", "_index": 7, "sub_category": "슈트/블레이저 재킷",
            "big_category": "아우터", "product_name": name, "feature": "", "color": "블랙, 화이트", "material": "울",
            "gender": "여", "season": "FW", "image_url": "http://x/set.jpg"}
    base.update(kw)
    return base


def test_apply_split_builds_components_with_parent_image():
    norm = OtpenNormalizer()
    row = _row("[SET] Sample jacket & Sample Pleats Skirt")
    parent = norm.normalize_row(row)
    d = SetSplitter().decide(row["product_name"])
    comps = apply_split(parent, d, norm, row)
    pid = "otpensource:sorted_아우터/sorted_슈트_블레이저_재킷.json:7"
    assert [c.node.id for c in comps] == [pid + "#1", pid + "#2"]
    assert all(c.node.parent_id == pid and c.node.split_from == "name_rule" for c in comps)
    assert all(c.node.image_url == "http://x/set.jpg" and c.node.image_scope == "full_set" for c in comps)
    assert [(c.node.category_minor.value, c.category_major.value) for c in comps] == [("재킷", "아우터"), ("스커트", "하의")]
    assert [c.node.category_raw for c in comps] == ["jacket", "Skirt"]
    assert all([x.value for x in c.colors] == ["블랙", "화이트"] for c in comps)  # 색·소재는 부모 공유(한계)
    assert "플리츠" not in {x.value for x in comps[0].details}
    assert "플리츠" in {x.value for x in comps[1].details}


def test_apply_split_flags_on_candidate_and_multipack():
    norm = OtpenNormalizer()
    for name, flag in [("샘플 셋업 자켓 (베이지)", "split_candidate"), ("[2PACK] 샘플 롱 슬리브리스", "multipack")]:
        row = _row(name)
        parent = norm.normalize_row(row)
        out = apply_split(parent, SetSplitter().decide(name), norm, row)
        assert len(out) == 1 and getattr(out[0].node, flag) is True and out[0].node.id == parent.node.id


# ---------- 검수 덮어쓰기 (set_overrides.yaml, 사용자 채택) ----------
def test_overrides_split_and_clear(tmp_path):
    from trend_agent.ingestion.normalization.set_split import SetOverrides, decision_source

    cfg = tmp_path / "ov.yaml"
    cfg.write_text(
        'attribute: set_overrides\n'
        'split:\n'
        '  "otpensource:f.json:1":\n'
        '    name: "x"\n'
        '    components:\n'
        '      - {category_minor: "재킷", category_major: "아우터", raw: "블레이저"}\n'
        '      - {category_minor: "팬츠", category_major: "하의", raw: "셋업"}\n'
        'clear_candidate:\n'
        '  "otpensource:f.json:2": "오버 셋업 자켓"\n', encoding="utf-8")
    ov = SetOverrides(cfg)
    sp = SetSplitter()
    rule = sp.decide("[SET]샘플 크롭드 블레이저 자켓 셋업[브라운]")
    assert rule.decision == "candidate"
    d = ov.apply("otpensource:f.json:1", rule)
    assert (d.decision, d.reason, decision_source(d)) == ("split", "review", "review")
    assert [(c.category_minor, c.category_major) for c in d.components] == [("재킷", "아우터"), ("팬츠", "하의")]
    cleared = ov.apply("otpensource:f.json:2", sp.decide("샘플 셋업 자켓 (베이지)"))
    assert (cleared.decision, decision_source(cleared)) == ("none", "review")
    other = sp.decide("샘플 셋업 자켓 (그린)")
    assert ov.apply("otpensource:f.json:3", other) is other  # 목록에 없으면 규칙 그대로


def test_override_split_products_use_review_basis(tmp_path):
    from trend_agent.ingestion.normalization.set_split import SetOverrides

    cfg = tmp_path / "ov.yaml"
    cfg.write_text('split:\n  "otpensource:sorted_아우터/sorted_슈트_블레이저_재킷.json:7":\n    components:\n'
                   '      - {category_minor: "재킷", category_major: "아우터", raw: "블레이저"}\n'
                   '      - {category_minor: "팬츠", category_major: "하의", raw: "셋업"}\n', encoding="utf-8")
    norm = OtpenNormalizer()
    row = _row("샘플 울 헤링본 체크 블레이저 셋업", material="울")
    parent = norm.normalize_row(row)
    d = SetOverrides(cfg).apply(parent.node.id, SetSplitter().decide(row["product_name"]))
    comps = apply_split(parent, d, norm, row)
    assert [c.node.split_from for c in comps] == ["review", "review"]
    assert [c.node.parent_id for c in comps] == [parent.node.id] * 2 and comps[1].node.image_scope == "full_set"
    assert all("체크" in [p.value for p in c.patterns] for c in comps)  # 셋업은 같은 원단: 패턴은 부모에서
    assert all(c.details == [] or all(dd.raw == "울" for dd in c.details) for c in comps)


def test_repo_overrides_file_is_valid():
    from trend_agent.ingestion.normalization.set_split import SetOverrides

    ov = SetOverrides()  # configs/alias_map/set_overrides.yaml
    assert len(ov.split) == 14 and len(ov.clear) == 13 and not (set(ov.split) & ov.clear)
