"""03 musinsa-snap-new 정규화 (합성 입력). 규칙: docs/vocabulary.md"""
from fashion_planner_trend.config import Neo4jSettings, load_env, neo4j_settings
from fashion_planner_trend.ingestion.normalization.musinsa_snap import SnapNormalizer, parse_label

N = SnapNormalizer()


def rec(slot, attrs, file_no="1", k=0, row=0):
    return N.normalize_slot({"file_no": file_no, "slot": slot, "attrs": attrs, "k": k, "row": row})


def test_basic_top_maps_attributes_and_ids():
    r = rec("상의", {"카테고리": "탑", "디테일": "후드", "프린트": "무지", "소재": "나일론", "기장": "노멀",
                   "소매기장": "긴팔", "넥라인": "유넥", "핏": "루즈"})
    assert r.node.id == "musinsa_snap:0:상의" and "file 1" in r.node.source_row and r.node.source == "musinsa_snap" and r.node.kind == "snap_garment"
    assert r.category_major.value == "상의" and r.node.category_minor == "탑"
    assert [p.value for p in r.patterns] == ["무지"] and [m.value for m in r.materials] == ["합성섬유"]
    assert {(d.type.value, d.value) for d in r.details} == {("detail", "후드"), ("neckline", "유넥")}  # 후드와 넥라인은 다른 노드
    assert (r.node.fit, r.node.length_body, r.node.length_sleeve) == ("루즈", "노멀", "긴팔")
    assert r.colors == [] and r.node.approx == []  # 03은 주색상이 없다


def test_minor_decides_major_over_slot():
    r = rec("상의", {"카테고리": "가디건"})
    assert r.node.category_minor == "가디건" and r.category_major.value == "아우터"


def test_cross_routing_from_detail_field():
    r = rec("상의", {"카테고리": "티셔츠", "디테일": "스트라이프, 민소매, 홀터넥, 프린트"})
    assert [p.value for p in r.patterns] == ["스트라이프"]            # 프린트는 값 불명이라 버린다
    assert r.node.length_sleeve == "민소매"
    assert {(d.type.value, d.value) for d in r.details} == {("neckline", "홀터넥")}


def test_fit_slim_is_tight_approx_and_shape_value_moves():
    slim = rec("하의", {"카테고리": "팬츠", "핏": "슬림"})
    assert slim.node.fit == "타이트" and "fit" in slim.node.approx and slim.usable_scalar("fit") is None  # 1차 검색 제외
    flare = rec("하의", {"카테고리": "팬츠", "핏": "플레어"})
    assert flare.node.fit is None and flare.node.shape == "벨보텀/플레어"


def test_non_garment_and_empty_values():
    assert rec("상의", {"카테고리": "모자"}).category_major is None
    r = rec("하의", {"카테고리": "팬츠", "소재": "0", "프린트": "0", "핏": "0"})
    assert r.materials == [] and r.patterns == [] and r.node.fit is None and r.node.fit_raw is None


def test_multi_item_slot_gets_suffix_and_duplicate_file_numbers_stay_distinct():
    assert rec("상의", {"카테고리": "탑"}, k=1).node.id == "musinsa_snap:0:상의:1"
    a, b = rec("상의", {"카테고리": "탑"}, file_no="9", row=3), rec("상의", {"카테고리": "탑"}, file_no="9", row=4)
    assert a.node.id != b.node.id  # 같은 파일 번호라도 다른 행은 다른 Product


def test_parse_label_handles_nested_dict_strings():
    d = parse_label('{"a": "{\'카테고리\': \'탑\'}", "b": 3}')
    assert d["a"] == {"카테고리": "탑"} and d["b"] == 3


def test_neo4j_settings_never_print_password():
    s = neo4j_settings({"NEO4J_URI": "bolt://x:1", "NEO4J_USER": "u", "NEO4J_PASSWORD": "secret-pw"})
    assert s.password == "secret-pw" and "secret-pw" not in repr(s)
    assert isinstance(load_env("does-not-exist.env"), dict)


def test_blank_env_values_fall_back_to_defaults():
    # `.env.example`를 복사만 하고 채우지 않은 빈 값은 기본값을 쓴다
    s = neo4j_settings({"NEO4J_URI": "", "NEO4J_USER": "", "NEO4J_PASSWORD": ""})
    assert (s.uri, s.user, s.password) == ("bolt://localhost:7687", "neo4j", "")
