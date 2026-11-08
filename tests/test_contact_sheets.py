"""build_contact_sheets: 셔플·헤더·카드 문구 규칙·구성품 이미지 승계·시트 분할 (합성 입력, 실제 상품 이미지 없음)"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_contact_sheets", ROOT / "scripts" / "build_contact_sheets.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def test_shuffle_is_deterministic_per_item_and_independent_of_input_order():
    ids = [f"p{k}" for k in range(30)]
    a, b = cs.shuffled("i1", ids), cs.shuffled("i1", list(reversed(ids)))
    assert a == b and sorted(a) == sorted(ids) and a != sorted(ids)
    assert cs.shuffled("i2", ids) != a


def test_card_text_has_no_scores_and_marks_snap_slot():
    snap = cs.card_text("musinsa_snap:12:하의", {"source": "musinsa_snap", "category_major": "하의", "category_minor": "팬츠",
                                                "content_sha": "x", "has_image": True}, True)
    assert "이 사진의 '하의' 슬롯" in snap and "여러 명 의심" in snap and "하의 > 팬츠" in snap
    cat = cs.card_text("otpensource:1", {"source": "otpensource", "category_major": "상의", "category_minor": None,
                                          "name": "아주 긴 상품명이 들어가서 잘려야 하는 경우의 예시 문자열입니다", "gender": "여"}, None)
    assert cat.startswith("catalog") and "성별 여" in cat and "상의 > -" in cat
    for t in (snap, cat):
        assert "score" not in t and "rank" not in t and "P0" not in t


def test_header_lists_item_attrs_focus_and_gender():
    h = cs.header_text({"item_id": "1-s0-i0", "style_name": "합성", "name": "셔츠", "category_major": "상의", "category_minor": "셔츠",
                        "gender": None, "attrs": {"color": ["핑크"]}, "focus_core": ["color"], "focus_supporting": []})
    assert "[1-s0-i0]" in h and "color 핑크" in h and "color(core)" in h and "성별: 명시 없음" in h


def test_image_for_inherits_parent_and_drops_missing_files(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    idx = {"otpensource:7": {"local_path": str(f), "image_status": "ok", "multi_wearer_suspected": None},
           "otpensource:8": {"local_path": str(tmp_path / "none.jpg"), "image_status": "ok"},
           "otpensource:9": {"local_path": str(f), "image_status": "broken"}}
    cat, cache = tmp_path / "catalog", tmp_path / "cache"
    kw = dict(catalog=cat, cache=cache)
    assert cs.image_for("otpensource:7#1", idx, **kw)["local_path"] == str(f)
    assert cs.image_for("otpensource:8", idx, **kw)["local_path"] is None
    assert cs.image_for("otpensource:9", idx, **kw)["local_path"] is None and cs.image_for("x", idx, **kw)["image_status"] is None
    url = "https://example.invalid/a.jpg"
    idx["otpensource:10"] = {"image_ref": url, "local_path": None, "image_status": "ok"}
    assert cs.image_for("otpensource:10", idx, **kw)["local_path"] is None          # 파일 없음, 받지 않음
    cat.mkdir()
    (cat / cs.url_file(url)).write_bytes(b"x")
    assert cs.image_for("otpensource:10", idx, **kw)["local_path"] == str(cat / cs.url_file(url))


def test_build_splits_into_pages_of_20_without_rendering(tmp_path):
    pool = tmp_path / "pool"
    pool.mkdir()
    rows = [{"item_id": "virtual:4:드뮤어-i0", "product_id": f"otpensource:{k}", "hits": [{"config": "P0", "rank": k, "score": 0.5}]} for k in range(23)]
    (pool / "pool.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    (pool / "items.jsonl").write_text(json.dumps({"item_id": "virtual:4:드뮤어-i0", "name": "블라우스"}, ensure_ascii=False), encoding="utf-8")
    (pool / "products.jsonl").write_text("\n".join(json.dumps({"product_id": f"otpensource:{k}", "source": "otpensource", "content_sha": str(k)})
                                                   for k in range(23)), encoding="utf-8")
    m = cs.build(pool, tmp_path / "out", {}, render=False)
    assert [(x["sheet"], len(x["cards"])) for x in m] == [("virtual_4_드뮤어-i0_p1.jpg", 20), ("virtual_4_드뮤어-i0_p2.jpg", 3)]
    assert m[0]["cards"][0]["card"] == "C01" and all(c["local_path"] is None for x in m for c in x["cards"])
    assert len((tmp_path / "out" / "sheets.jsonl").read_text(encoding="utf-8").splitlines()) == 2
