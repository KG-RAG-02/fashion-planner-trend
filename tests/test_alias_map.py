import pytest

from fashion_planner_trend.ingestion.normalization.alias_map import AliasMap, norm_key
from fashion_planner_trend.ingestion.normalization.otpensource import OtpenNormalizer


@pytest.fixture(scope="module")
def alias():
    return AliasMap()


def values(results):
    return [(r.value, r.approx) for r in results]


def test_norm_key():
    assert norm_key("Sky Blue") == "skyblue"
    assert norm_key("라이트-그레이") == "라이트그레이"


def test_standard_and_alias_color(alias):
    assert alias.normalize("color", "BLACK").value == "블랙"
    assert alias.normalize("color", "스카이 블루").value == "스카이블루"
    for raw, expected in [("아이보리", "화이트"), ("차콜", "그레이"), ("카멜", "브라운"), ("올리브", "카키"),
                          ("버건디", "와인"), ("연청", "스카이블루"), ("코랄", "오렌지")]:
        r = alias.normalize("color", raw)
        assert (r.value, r.approx) == (expected, False), raw  # 04 규격이 정한 매핑은 근사가 아니다


def test_color_modifiers_multi_and_non_color(alias):
    assert values(alias.normalize_color("다크 네이비")) == [("네이비", False)]
    assert values(alias.normalize_color("워시드 블랙")) == [("블랙", False)]
    assert values(alias.normalize_color("블랙, 화이트")) == [("블랙", False), ("화이트", False)]
    assert values(alias.normalize_color("2color")) == [(None, False)]
    assert values(alias.normalize_color("199")) == [(None, False)]
    assert values(alias.normalize_color("")) == []


def test_color_trailing_base_is_approx_and_unknown_stays_none(alias):
    assert values(alias.normalize_color("sage green")) == [("그린", True)]
    assert values(alias.normalize_color("오트밀")) == [(None, False)]  # 억지로 맞추지 않는다


def test_fit_slim_is_approx_tight_and_shape_routing(alias):
    r = alias.normalize("fit", "슬림")
    assert (r.value, r.approx) == ("타이트", True)
    for raw in ["정상", "normal", "스탠다드", "정핏", "레귤러"]:
        assert alias.normalize("fit", raw).value == "노멀"
    r = alias.normalize("fit", "부츠컷")
    assert (r.route_to, r.value) == ("shape", "부츠컷")
    assert alias.normalize("fit", "UNKNOWN").matched_by == "null_value"
    assert alias.normalize("fit", "조거핏").value is None


def test_material_alias_approx_and_category_hint(alias):
    assert alias.normalize("material", "코튼").value == "면"
    assert alias.normalize("material", "Cotton").value == "면"
    r = alias.normalize("material", "기모")
    assert (r.value, r.approx) == ("플리스", True)
    for raw in ["다운", "구스다운", "패딩"]:
        r = alias.normalize("material", raw)
        assert r.value is None and r.hint == {"category_minor": "패딩"}
    assert alias.normalize("material", "퀼팅").route_to == "detail"
    assert values(alias.normalize_tokens("material", "울 블렌드")) == [("울/캐시미어", False)]
    assert [r.value for r in alias.normalize_tokens("material", "울/캐시미어")] == ["울/캐시미어"]  # 나누지 않는다
    assert [r.value for r in alias.normalize_tokens("material", "레더, 무스탕")] == ["가죽", "무스탕"]


def test_pattern_solid_only_when_explicit_and_routes(alias):
    assert alias.normalize("pattern", "민무늬").value == "무지"
    assert alias.normalize("pattern", "줄무늬").value == "스트라이프"
    assert alias.normalize("pattern", "데님").route_to == "material"
    assert alias.normalize("pattern", "").value is None  # 미언급은 값이 아니다


def test_detail_routing_from_musinsa_labels(alias):
    r = alias.normalize("detail", "스트라이프")
    assert (r.route_to, r.value) == ("pattern", "스트라이프")
    r = alias.normalize("detail", "민소매")
    assert (r.route_to, r.value) == ("sleeve_length", "민소매")
    assert alias.normalize("detail", "버튼").value == "단추"
    r = alias.normalize("detail", "벨트")
    assert (r.value, r.approx) == ("띠", True)


def test_length_aliases(alias):
    assert alias.normalize("sleeve_length", "long").value == "긴팔"
    assert alias.normalize("sleeve_length", "짧은팔").value == "반팔"
    assert alias.normalize("body_length", "무릎").value == "니렝스"
    r = alias.normalize("body_length", "롱스커트")
    assert (r.value, r.approx) == ("맥시", True)


@pytest.mark.parametrize(
    "sub,name,big,minor,major",
    [
        ("긴소매 티셔츠", "x", "상의", "티셔츠", "상의"),
        ("후드 집업", "x", "아우터", "짚업", "아우터"),
        ("점프 슈트/오버올", "x", "바지", "점프수트", "원피스"),  # 대분류는 바지지만 표준은 원피스
        ("롱스커트", "x", "원피스_스커트", "스커트", "하의"),
        ("미니원피스", "x", "원피스_스커트", "드레스", "원피스"),
        ("셔츠/블라우스", "플라워 블라우스", "상의", "블라우스", "상의"),
        ("셔츠/블라우스", "옥스포드 남방", "상의", "셔츠", "상의"),
        ("셔츠/블라우스", "기본 탑", "상의", None, "상의"),
        ("기타 아우터", "x", "아우터", None, "아우터"),
        ("", "x", "바지", None, "하의"),  # 소분류가 비면 대분류로 상분류만
        ("", "미디 원피스", "원피스_스커트", "드레스", "원피스"),
        ("", "알수없음", "원피스_스커트", None, None),
    ],
)
def test_otpensource_category(alias, sub, name, big, minor, major):
    c = alias.map_otpensource_category(sub, name, big)
    assert (c.category_minor, c.category_major) == (minor, major)


def _row(**kw):
    base = {"_file": "f/x.json", "_index": 0, "sub_category": "긴소매 티셔츠", "big_category": "상의",
            "product_name": "베이직 티셔츠", "feature": "", "color": "", "material": "", "gender": "여", "season": "FW",
            "image_url": "http://x/1.jpg"}
    base.update(kw)
    return base


def test_normalize_otpensource_row(alias):
    n = OtpenNormalizer(alias)
    rec = n.normalize_row(_row(color="다크 네이비", material="코튼, 기모", product_name="스트라이프 포켓 셔츠", gender="성별무관", season="정보 없음"))
    assert rec.category_major.value == "상의" and rec.node.category_minor.value == "티셔츠"
    assert [(c.value, c.raw) for c in rec.colors] == [("네이비", "다크 네이비")]
    assert [(m.value, m.approx) for m in rec.materials] == [("면", False), ("플리스", True)]
    assert [p.value for p in rec.patterns] == ["스트라이프"]
    assert "포켓" in [d.value for d in rec.details]
    assert rec.node.gender.value == "공용" and rec.node.season is None
    assert rec.node.length_sleeve.value == "긴팔" and rec.node.length_sleeve_raw == "긴소매"
    assert rec.node.product_url is None and rec.node.size is None


def test_down_material_gives_padding_category_hint_only_when_minor_missing(alias):
    n = OtpenNormalizer(alias)
    rec = n.normalize_row(_row(sub_category="기타 아우터", big_category="아우터", material="구스다운"))
    assert rec.node.category_minor.value == "패딩" and rec.materials == []
    rec = n.normalize_row(_row(sub_category="트러커 재킷", big_category="아우터", material="다운"))
    assert rec.node.category_minor.value == "재킷"  # 이미 정해진 하분류를 덮어쓰지 않는다


def test_category_overlap_details_skipped_when_category_known(alias):
    n = OtpenNormalizer(alias)
    known = n.normalize_row(_row(sub_category="후드 티셔츠", product_name="후드 티셔츠 집업"))
    assert not {"후드", "집업"} & {d.value for d in known.details}
    unknown = n.normalize_row(_row(sub_category="기타 상의", product_name="후드 집업"))
    assert {"후드", "집업"} <= {d.value for d in unknown.details}


def test_missing_pattern_is_not_solid(alias):
    rec = OtpenNormalizer(alias).normalize_row(_row(product_name="베이직 티셔츠"))
    assert rec.patterns == []  # 언급이 없으면 무지가 아니라 미확인
