"""키워드 행렬 파서 (scripts/build_keyword_matrix.py)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bkm", ROOT / "scripts" / "build_keyword_matrix.py")
bkm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bkm)


def test_parse_manifest_style():
    assert bkm.parse_manifest_style("뉴 로(New Raw)") == ("뉴 로", ["New Raw"])
    assert bkm.parse_manifest_style("2026FW: 1980년대, 가지색, 머플러") == ("2026FW", ["1980년대", "가지색", "머플러"])
    assert bkm.parse_manifest_style("런던 가을 트렌드 3(정통 트렌치 코트, 오버사이즈 실루엣 …)") == \
        ("런던 가을 트렌드 3", ["정통 트렌치 코트", "오버사이즈 실루엣"])
    assert bkm.parse_manifest_style("2026FW 남성복 트렌드 12(소제목)") == ("2026FW 남성복 트렌드 12", [])
    assert bkm.parse_manifest_style("조용한 럭셔리") == ("조용한 럭셔리", [])


def test_quote_core_and_generic_patterns():
    assert bkm.QUOTE_RE.findall("올가을 ‘절제된 맥시멀리즘’과 '이 컬러'") == ["절제된 맥시멀리즘", "이 컬러"]
    assert bkm.GENERIC_RE.match("이 컬러") and not bkm.GENERIC_RE.match("이브닝")
    found = {m.group(1) + "코어" for m in bkm.CORE_RE.finditer("놈코어와 발레 코어, 마이클 코어스")}
    assert found == {"놈코어", "발레코어"}  # 마이클 코어스는 제외


def test_kfashion_styles_from_repo_doc():
    basics, subs = bkm.kfashion_styles(ROOT / "docs/schemas/sources/kfashion_guideline.md")
    assert len(basics) == 10 and len(subs) == 23 and "스트리트" in subs and "키치/키덜트" in subs


def test_squash_ignores_space_and_case():
    assert bkm.squash("조용한 럭셔리") == bkm.squash("조용한럭셔리") and bkm.squash("Quiet  Luxury") == "quietluxury"
