"""Product 정규화 점검·버그 수정 (a)~(i) — 합성 입력."""
import pytest

from trend_agent.ingestion.normalization.alias_map import AliasMap
from trend_agent.ingestion.normalization.musinsa_snap import SnapNormalizer, mark_snap_duplicates
from trend_agent.ingestion.normalization.otpensource import OtpenNormalizer, is_empty_row, mark_exact_duplicates
from trend_agent.ingestion.normalization.text_match import TextMatcher, tokenize


@pytest.fixture(scope="module")
def alias():
    return AliasMap()


@pytest.fixture(scope="module")
def snap(alias):
    return SnapNormalizer(alias)


@pytest.fixture(scope="module")
def otp(alias):
    return OtpenNormalizer(alias)


def srec(snap, slot, attrs, row=0, k=0):
    return snap.normalize_slot({"file_no": "1", "slot": slot, "attrs": attrs, "k": k, "row": row})


def orow(**kw):
    base = {"_file": "f/x.json", "_index": 0, "sub_category": "긴소매 티셔츠", "big_category": "상의",
            "product_name": "베이직 티셔츠", "feature": "", "color": "", "material": "", "gender": "여",
            "season": "FW", "image_url": "http://x/1.jpg"}
    base.update(kw)
    return base


# ---------- (a) 03 노이즈 카테고리: 슬롯 상분류로 채우지 않는다 (사용자 결정: 노이즈 → null) ----------
@pytest.mark.parametrize("raw", ["타이트", "UNKNOWN", " unknown", "기타", "상", "모자", "", "이너웨어", "트레이닝복", "의류"])
def test_a_noise_category_is_null_not_slot(snap, raw):
    r = srec(snap, "상의", {"카테고리": raw})
    assert r.category_major is None and r.node.category_minor is None
    assert r.node.category_raw == (raw.strip() or None)  # 원문은 보존


@pytest.mark.parametrize("raw,slot,major", [("아우터", "상의", "아우터"), ("상의", "아우터", "상의"),
                                            ("무스탕", "아우터", "아우터"), ("망토", "상의", "아우터")])
def test_a_major_name_or_garment_without_minor_gives_major_only(snap, raw, slot, major):
    r = srec(snap, slot, {"카테고리": raw})
    assert r.category_major.value == major and r.node.category_minor is None  # 슬롯이 아니라 카테고리 값으로


@pytest.mark.parametrize("raw,minor,major", [
    ("스웨트셔츠", "티셔츠", "상의"), ("폴로셔츠", "티셔츠", "상의"), ("코튼 티셔츠", "티셔츠", "상의"),
    ("타-shirt", "티셔츠", "상의"), ("슬리브리스", "탑", "상의"), ("톱", "탑", "상의"), ("크롭탑", "탑", "상의"),
    ("타이츠", "레깅스", "하의"), ("Jacket", "재킷", "아우터"), ("앤드 재킷", "재킷", "아우터"),
    ("오버코트", "코트", "아우터"), ("카디건", "가디건", "아우터"), ("잔털 후드티", "후드티", "상의"),
    ("스크르트", "스커트", "하의"),
])
def test_a_alias_extension_recovers_minor(snap, raw, minor, major):
    r = srec(snap, "하의", {"카테고리": raw})
    assert (r.node.category_minor, r.category_major.value) == (minor, major)


# ---------- (b) 리스트형 속성 값 ----------
def test_b_split_tokens_flattens_lists(alias):
    assert alias.split_tokens(["후드", "포켓"]) == ["후드", "포켓"]
    assert alias.split_tokens(("블랙, 화이트", ["레드"])) == ["블랙", "화이트", "레드"]
    assert [n.value for n in alias.normalize_tokens("detail", ["후드", "포켓"])] == ["후드", "포켓"]
    assert [n.value for n in alias.normalize_color(["블랙", "화이트"])] == ["블랙", "화이트"]


def test_b_snap_list_detail_keeps_every_value(snap):
    r = srec(snap, "상의", {"카테고리": "티셔츠", "디테일": ["후드", "포켓"], "서브컬러": ["블랙", "화이트"]})
    assert {d.value for d in r.details} == {"후드", "포켓"}
    assert r.node.sub_colors == ["블랙", "화이트"]
    assert srec(snap, "상의", {"카테고리": "티셔츠", "디테일": ["unknown"]}).details == []


def test_b_snap_scalar_list_uses_first_value(snap):
    r = srec(snap, "하의", {"카테고리": ["팬츠", "스커트"], "핏": ["루즈"]})
    assert r.node.category_minor == "팬츠" and r.node.fit == "루즈"


# ---------- (c) dup_of · (d) snap_id ----------
def test_c_same_row_identical_label_dict_marked_dup(snap):
    slots = [
        {"file_no": "1", "slot": "상의", "attrs": {"카테고리": "탑", "핏": "루즈"}, "k": 0, "row": 5},
        {"file_no": "1", "slot": "상의", "attrs": {"핏": "루즈 ", "카테고리": "탑"}, "k": 1, "row": 5},
        {"file_no": "1", "slot": "아우터", "attrs": {"카테고리": "탑", "핏": "루즈"}, "k": 0, "row": 5},
        {"file_no": "1", "slot": "상의", "attrs": {"카테고리": "탑", "핏": "루즈"}, "k": 0, "row": 6},
        {"file_no": "1", "slot": "하의", "attrs": {"카테고리": "팬츠"}, "k": 0, "row": 6},
    ]
    recs = mark_snap_duplicates([snap.normalize_slot(s) for s in slots], slots)
    assert [r.node.dup_of for r in recs] == [None, "musinsa_snap:5:상의", "musinsa_snap:5:상의", None, None]


def test_d_snap_id_is_parquet_row(snap):
    r = srec(snap, "하의", {"카테고리": "팬츠"}, row=42, k=1)
    assert r.node.snap_id == 42 and r.node.id == "musinsa_snap:42:하의:1"
    assert r.node.source_row.startswith("disl/musinsa-snap-new#row42:")


# ---------- (e) 패딩 힌트: 상분류가 비었거나 아우터일 때만 ----------
def test_e_padding_hint_on_bottom_goes_to_detail(otp):
    r = otp.normalize_row(orow(sub_category="기타 하의", big_category="바지", material="패딩"))
    assert r.category_major.value == "하의" and r.node.category_minor is None
    assert ("detail", "패딩") in {(d.type.value, d.value) for d in r.details}


def test_e_padding_hint_on_outer_still_sets_minor(otp):
    r = otp.normalize_row(orow(sub_category="기타 아우터", big_category="아우터", material="구스다운"))
    assert (r.node.category_minor.value, r.category_major.value) == ("패딩", "아우터")


def test_e_snap_padding_hint_rules(snap):
    outer = srec(snap, "아우터", {"카테고리": "아우터", "소재": "패딩"})
    assert (outer.node.category_minor, outer.category_major.value) == ("패딩", "아우터")
    pants = srec(snap, "하의", {"카테고리": "하의", "소재": "다운"})
    assert pants.category_major.value == "하의" and pants.node.category_minor is None
    assert "패딩" in {d.value for d in pants.details}


# ---------- (f) 텍스트 키워드: 토큰·단어 경계·가드 ----------
def test_f_tokenize_keeps_hyphen_and_splits_symbols():
    assert tokenize("[SET] A-LINE 스커트(블랙)/포켓+후드, 집업") == ["SET", "A-LINE", "스커트", "블랙", "포켓", "후드", "집업"]


@pytest.mark.parametrize("name", ["비즈니스 셔츠", "무지개 니트", "빈티지 브라운 코트", "오렌지 브라운 팬츠",
                                  "PUFFER JACKET", "STUDIO TEE", "Dot-com hoodie", "내셔널지오그래픽 맨투맨",
                                  "커스터드 니트", "애슬릿 팬츠", "믹스드 니트", "쿨믹스 팬츠",
                                  "트래블러 플리스 셔츠", "라이프 릴랙스 팬츠", "화이트라벨 트렌치코트",
                                  "Alpaca Blended Coat"])
def test_f_false_positives_removed(otp, name):
    r = otp.normalize_row(orow(sub_category="기타 상의", product_name=name))
    found = {p.value for p in r.patterns} | {d.value for d in r.details}
    assert not found & {"비즈", "무지", "지브라", "퍼프", "스터드", "도트", "그래픽", "슬릿", "러플", "프릴", "띠",
                        "니트꽈배기"}, found


@pytest.mark.parametrize("name,kind,value", [
    ("무지 티셔츠", "pattern", "무지"), ("반집업 니트", "detail", "집업"), ("빅로고 맨투맨", "pattern", "그래픽"),
    ("STUDDED DENIM", "detail", "스터드"), ("STRIPED SHIRT", "pattern", "스트라이프"), ("지브라 패턴 셔츠", "pattern", "지브라"),
    ("체크셔츠", "pattern", "체크"), ("DOT BLOUSE", "pattern", "도트"), ("드롭 숄더 티", "detail", "드롭숄더"),
    ("Suede ribbed one pocket skirt", "detail", "포켓"), ("라쿤퍼 트리밍 야상", "detail", "퍼트리밍"),
])
def test_f_true_positives_kept(otp, name, kind, value):
    r = otp.normalize_row(orow(sub_category="기타 상의", product_name=name))
    vals = {p.value for p in r.patterns} if kind == "pattern" else {d.value for d in r.details}
    assert value in vals, (name, vals)


def test_f_name_and_feature_are_tokenized_separately(otp):
    r = otp.normalize_row(orow(sub_category="기타 상의", product_name="빈티지", feature="브라운 패턴"))
    assert "지브라" not in {p.value for p in r.patterns}  # 상품명 끝과 feature 앞을 이어 붙이지 않는다


def test_f_matcher_bigram_exact_only():
    m = TextMatcher([("bootscut", "부츠컷", False), ("지브라", "지브라", False)], guards={"지브라": ["지브라운"]})
    assert [v for v, _, _ in m.find("BOOTS CUT 데님")] == ["부츠컷"]
    assert m.find("빈티지브라운") == [] and m.find("빈티지 브라운") == []
    assert [v for v, _, _ in m.find("지브라")] == ["지브라"]


# ---------- (g) 복합 토큰: 표준 복합값 3개만 보호 ----------
def test_g_compound_guard_only_protects_standard_values(alias):
    assert [n.value for n in alias.normalize_tokens("material", "울/폴리")] == ["울/캐시미어", "합성섬유"]
    assert [n.value for n in alias.normalize_tokens("material", "울/캐시미어")] == ["울/캐시미어"]
    assert [n.value for n in alias.normalize_tokens("material", "울/캐시미어 블렌드, 폴리")] == ["울/캐시미어", "합성섬유"]
    assert [n.value for n in alias.normalize_tokens("material", "시퀸/글리터")] == ["시퀸/글리터"]
    assert [n.value for n in alias.normalize_tokens("material", "비닐/pvc, 코튼")] == ["비닐/PVC", "면"]
    assert [n.value for n in alias.normalize_tokens("material", "고무/폴리에스터")] == [None, "합성섬유"]


# ---------- (h) 02 빈 행 · 완전 중복 ----------
def test_h_empty_row_kept_but_out_of_graph_scope(otp):
    row = orow(sub_category="", product_name="", image_url="", big_category="상의", season="정보 없음")
    assert is_empty_row(row)
    r = otp.normalize_row(row)
    assert r.category_major is None and not r.in_graph_scope  # 정규화는 행을 남기고 적재에서만 뺀다((h)). v2 빌드는 drop_without_image로 저장하지 않는다(D-33)


def test_h_exact_duplicates_marked_not_removed(otp):
    rows = [orow(_index=0), orow(_index=1), orow(_index=2, product_name="다른 이름"),
            orow(_index=3, sub_category="", product_name="", image_url=""),
            orow(_index=4, sub_category="", product_name="", image_url="")]
    recs = mark_exact_duplicates([otp.normalize_row(r) for r in rows], rows)
    assert [r.node.dup_of for r in recs] == [None, "otpensource:f/x.json:0", None, None, None]  # 빈 행끼리는 중복 표시 안 함


# ---------- (i) 한국어 기본색 별칭 (T3 #27) ----------
@pytest.mark.parametrize("raw,value", [("보라", "퍼플"), ("보라색", "퍼플"), ("violet", "퍼플"), ("빨강", "레드"),
                                       ("빨간색", "레드"), ("분홍", "핑크"), ("분홍색", "핑크"), ("주황", "오렌지"),
                                       ("주황색", "오렌지"), ("노랑", "옐로우"), ("노란색", "옐로우"), ("초록", "그린"),
                                       ("초록색", "그린"), ("파랑", "블루"), ("파란색", "블루"), ("갈색", "브라운"),
                                       ("연보라", "라벤더")])
def test_i_korean_basic_color_aliases(alias, raw, value):
    r = alias.normalize("color", raw)
    assert (r.value, r.approx) == (value, False)
