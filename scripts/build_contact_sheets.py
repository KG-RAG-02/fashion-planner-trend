"""검색 판정용 콘택트시트: Item 1개 × 후보 카드 최대 20장(5열×4행) 이미지 + 매니페스트.

실행 (레포 루트, 공유 venv):
  python scripts/build_contact_sheets.py --pool "$SHARED/eval/pool/<run>" --out "$SHARED/eval/sheets/<run>" [--items <id,…>] [--font <ttf/ttc>]

입력: eval_retrieval.py pool 산출물(pool.jsonl·items.jsonl·products.jsonl) + C2 이미지 인덱스($SHARED/images/index.parquet).
출력: <out>/<item>_p<k>.jpg · sheets.jsonl({sheet, item_id, page, header_text, cards:[{card, product_id, content_sha, card_text, local_path, image_status}]}) · index.html
규칙(검수 설계 — T4 설계 메모 §5.3):
- 헤더는 Item 설명(텍스트)만. Vogue 기사 이미지는 넣지 않는다(G0-⑤·Q1 범위 = 파생 텍스트·상품 이미지).
- 카드에는 점수·순위·설정·정규화 속성 값을 넣지 않는다(채점기와의 순환 방지). 상분류>하분류·상품명·성별·출처만.
- 카드 순서는 Item별 시드 셔플(위치 편향 방지). 이미지는 contain(크롭 없음), 없거나 깨지면 '이미지 없음'.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = Path(os.environ.get("SHARED", ROOT.parent / "_shared"))
INDEX = SHARED / "images" / "index.parquet"
CATALOG = SHARED / "images" / "catalog"        # C2b: 02 이미지 = <sha256(image_url)[:16]>.jpg
IMG_CACHE = SHARED / "eval" / "img_cache"      # T4: 풀에 든 02 중 CATALOG에 없는 것만 스트리밍 GET
SEED = 20261108
FONTS = ("/System/Library/Fonts/AppleSDGothicNeo.ttc", str(Path.home() / "Library/Fonts/Pretendard-Regular.otf"))
COLS, ROWS, CARD_W, IMG_H, TEXT_H, PAD, HEAD_H = 5, 4, 300, 300, 78, 10, 210
PER_SHEET = COLS * ROWS


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s)


def header_text(it: dict) -> str:
    """Item 설명 — 판정자가 보는 기준. 명시 속성은 라벨(정답) 값이다(상품 속성이 아니다)."""
    attrs = " · ".join(f"{k} {', '.join(v)}" for k, v in (it.get("attrs") or {}).items()) or "명시 속성 없음"
    focus = ", ".join([f"{a}(core)" for a in it.get("focus_core", [])] + [f"{a}(supporting)" for a in it.get("focus_supporting", [])]) or "없음"
    cat = f"{it.get('category_major') or '?'} > {it.get('category_minor') or '(하분류 없음)'}"
    lines = [f"[{it['item_id']}] Style: {it.get('style_name') or ''} / Item: {it.get('name') or ''}",
             f"분류: {cat} · 성별: {it.get('gender') or '명시 없음'}",
             f"명시 속성: {attrs}", f"Style 핵심 속성: {focus}"]
    if it.get("raw_text"):
        lines.append(f"원문 표현: {it['raw_text']}")
    return "\n".join(lines)


def card_text(product_id: str, p: dict, multi_wearer) -> str:
    if p.get("source") == "musinsa_snap":
        slot = product_id.split(":")[-1]
        src = f"snap · 이 사진의 '{slot}' 슬롯 옷" + (" · 여러 명 의심" if multi_wearer else "")
    else:
        src = "catalog"
    name = (p.get("name") or "")[:24]
    lines = [src, f"{p.get('category_major') or '?'} > {p.get('category_minor') or '-'}"]
    if p.get("source") == "otpensource":
        lines.append(f"{name} · 성별 {p.get('gender') or '미상'}")
    return "\n".join(lines)


def shuffled(item_id: str, product_ids: list[str], seed: int = SEED) -> list[str]:
    ids = sorted(product_ids)
    random.Random(f"{seed}:{item_id}").shuffle(ids)
    return ids


def load_index(path: Path = INDEX) -> dict[str, dict]:
    import pyarrow.parquet as pq

    cols = ["product_id", "image_ref", "local_path", "image_status", "multi_wearer_suspected"]
    return {r["product_id"]: r for r in pq.read_table(path, columns=cols).to_pylist()}


def url_file(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + ".jpg"


def fetch(url: str, dest: Path, timeout: float = 20.0) -> bool:
    """스트리밍 GET 1회(HEAD·Range 없음, G0-C2와 같은 방식). 2xx + image/*만 저장."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "trend-eval/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if not (200 <= r.status < 300 and r.headers.get_content_type().startswith("image/")):
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            with tmp.open("wb") as f:
                while chunk := r.read(65536):
                    f.write(chunk)
            tmp.replace(dest)
            return True
    except Exception:
        return False


def image_for(product_id: str, index: dict, catalog: Path = CATALOG, cache: Path = IMG_CACHE, allow_fetch: bool = False) -> dict:
    """03 = 인덱스 local_path. 02 = C2b 카탈로그 파일 → T4 캐시 → (allow_fetch면) 스트리밍 GET.
    구성품 id(<원 id>#k)는 원 상품 사진을 쓴다(세트 승계, G0-C2). image_status가 ok가 아니면 쓰지 않는다."""
    r = index.get(product_id) or index.get(product_id.split("#")[0]) or {}
    out = {"local_path": None, "image_status": r.get("image_status"), "multi_wearer": r.get("multi_wearer_suspected")}
    if r.get("image_status") != "ok":
        return out
    if r.get("local_path") and Path(r["local_path"]).exists():
        out["local_path"] = r["local_path"]
    elif r.get("image_ref", "").startswith("http"):
        name = url_file(r["image_ref"])
        for d in (catalog, cache):
            if (d / name).exists():
                out["local_path"] = str(d / name)
                break
        else:
            if allow_fetch and fetch(r["image_ref"], cache / name):
                out["local_path"] = str(cache / name)
    return out


def _font(path: str | None, size: int):
    from PIL import ImageFont

    for p in ([path] if path else []) + list(FONTS):
        if p and Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    out = []
    for para in text.split("\n"):
        line = ""
        for ch in para:
            if draw.textlength(line + ch, font=font) > width:
                out.append(line)
                line = ch
            else:
                line += ch
        out.append(line)
    return out


def render_sheet(header: str, cards: list[dict], font_path: str | None):
    from PIL import Image, ImageDraw

    W = COLS * CARD_W + (COLS + 1) * PAD
    H = HEAD_H + ROWS * (IMG_H + TEXT_H + PAD) + PAD
    sheet = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(sheet)
    fh, fc, fn = _font(font_path, 20), _font(font_path, 15), _font(font_path, 22)
    y = PAD
    for line in _wrap(d, header, fh, W - 2 * PAD)[:8]:
        d.text((PAD, y), line, fill="black", font=fh)
        y += 25
    for k, c in enumerate(cards):
        col, row = k % COLS, k // COLS
        x0, y0 = PAD + col * (CARD_W + PAD), HEAD_H + row * (IMG_H + TEXT_H + PAD)
        d.rectangle([x0, y0, x0 + CARD_W, y0 + IMG_H], outline="#999", fill="#f4f4f4")
        if c.get("local_path"):
            try:
                with Image.open(c["local_path"]) as im:
                    im = im.convert("RGB")
                    im.thumbnail((CARD_W - 4, IMG_H - 4))  # contain — 크롭하지 않는다
                    sheet.paste(im, (x0 + (CARD_W - im.width) // 2, y0 + (IMG_H - im.height) // 2))
            except Exception:
                c["image_status"] = "decode_failed"
                d.text((x0 + 90, y0 + 140), "이미지 없음", fill="#666", font=fc)
        else:
            d.text((x0 + 90, y0 + 140), "이미지 없음", fill="#666", font=fc)
        d.rectangle([x0, y0, x0 + 58, y0 + 30], fill="black")
        d.text((x0 + 6, y0 + 3), c["card"], fill="white", font=fn)
        ty = y0 + IMG_H + 4
        for line in _wrap(d, c["card_text"], fc, CARD_W)[:4]:
            d.text((x0, ty), line, fill="black", font=fc)
            ty += 18
    return sheet


def build(pool_dir: Path, out: Path, index: dict, only: set[str] | None = None, font: str | None = None, seed: int = SEED,
          render: bool = True, allow_fetch: bool = False) -> list[dict]:
    pool = read_jsonl(pool_dir / "pool.jsonl")
    items = {r["item_id"]: r for r in read_jsonl(pool_dir / "items.jsonl")}
    products = {r["product_id"]: r for r in read_jsonl(pool_dir / "products.jsonl")}
    by_item: dict[str, list[str]] = {}
    for r in pool:
        by_item.setdefault(r["item_id"], []).append(r["product_id"])
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for iid in sorted(by_item):
        if only and iid not in only:
            continue
        order = shuffled(iid, by_item[iid], seed)
        head = header_text(items[iid])
        for page, start in enumerate(range(0, len(order), PER_SHEET), 1):
            cards = []
            for n, pid in enumerate(order[start:start + PER_SHEET], 1):
                img = image_for(pid, index, allow_fetch=allow_fetch)
                cards.append({"card": f"C{n:02d}", "product_id": pid, "content_sha": products[pid]["content_sha"],
                              "card_text": card_text(pid, products[pid], img["multi_wearer"]),
                              "local_path": img["local_path"], "image_status": img["image_status"]})
            name = f"{safe_name(iid)}_p{page}.jpg"
            if render:
                render_sheet(head, cards, font).save(out / name, "JPEG", quality=85)
            manifest.append({"sheet": name, "item_id": iid, "page": page, "header_text": head, "cards": cards})
    with (out / "sheets.jsonl").open("w", encoding="utf-8") as f:
        for m in manifest:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    (out / "index.html").write_text(
        "<!DOCTYPE html><meta charset='utf-8'><title>콘택트시트</title><h1>콘택트시트 " + str(len(manifest)) + "장</h1>"
        + "".join(f"<h3>{html.escape(m['sheet'])}</h3><img src='{html.escape(m['sheet'])}' style='max-width:100%'>" for m in manifest),
        encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--index", type=Path, default=INDEX)
    ap.add_argument("--items", help="쉼표로 구분한 Item id만")
    ap.add_argument("--font")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--fetch-missing", action="store_true", help="카탈로그에 없는 02 이미지를 스트리밍 GET으로 $SHARED/eval/img_cache에 받는다")
    a = ap.parse_args(argv)
    m = build(a.pool, a.out, load_index(a.index), set(a.items.split(",")) if a.items else None, a.font, a.seed,
              allow_fetch=a.fetch_missing)
    n_cards = sum(len(x["cards"]) for x in m)
    no_img = sum(1 for x in m for c in x["cards"] if not c["local_path"])
    print(f"wrote {len(m)} sheets, {n_cards} cards (이미지 없음 {no_img}) → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
