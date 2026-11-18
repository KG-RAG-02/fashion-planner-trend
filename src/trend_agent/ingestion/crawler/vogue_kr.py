"""Vogue Korea 패션 트렌드 기사 크롤러 (교육·연구용 예시 코드).

- 목록: WordPress admin-ajax(`get_posts_custom_v2`)로 페이지 단위 조회
- 기사: HTML 파싱 (메타태그 + `div.contt` 본문 + figure/figcaption)
- 이미지는 파일을 받지 않고 URL과 캡션만 저장한다.
- 시즌·컬렉션 정보는 원문에 명시된 표현만 수집하고, 없으면 비워 둔다.

수집 예절: 이 프로젝트를 밝히는 User-Agent를 쓰고, 요청 전에 robots.txt를 그 User-Agent로 확인해 금지된 URL이면
멈춘다(`RobotsDisallowed`). 요청 사이에는 대기하고 기본 수집 수는 작다.
주의: 작성자들은 사이트 이용약관을 검토하지 않았다. 수집한 기사는 저작권이 발행처에 있으므로 재배포하지 않는다.
실행과 그 결과에 대한 책임은 실행한 사람에게 있다.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.vogue.co.kr"
LIST_URL = f"{BASE_URL}/fashion/fashion-trend/"
AJAX_URL = f"{BASE_URL}/wp-admin/admin-ajax.php"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
PROJECT_URL = "https://github.com/KG-RAG-02/fashion-planner-trend"
USER_AGENT = f"fashion-planner-trend/1.0 (+{PROJECT_URL})"  # 브라우저로 위장하지 않는다
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": LIST_URL,
}
DEFAULT_LIMIT = 5  # --limit·--since를 주지 않을 때 받는 기사 수

# 목록 AJAX 파라미터 (목록 페이지의 인라인 스크립트에서 확인한 값)
LIST_PARAMS = {
    "action": "get_posts_custom_v2",
    "posts_per_page": 12,
    "post_type": "post",
    "tax1_slug": "fashion",
    "tax2_slug": "fashion-trend",
    "term2_id": "42",
}

# 원문에 명시된 "연도 + 시즌" 표현. 표기가 다른 시즌어는 정규화하지 않고 raw만 보존한다.
SEASON_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:년\s*)?"
    r"(?P<season>S/S|F/W|A/W|SS|FW|AW|봄\s*/\s*여름|가을\s*/\s*겨울|봄|여름|가을|겨울|"
    r"리조트|Resort|Cruise|프리폴|Pre-?Fall)",
    re.IGNORECASE,
)
SEASON_NORMALIZE = {
    "S/S": "SS", "SS": "SS", "봄/여름": "SS",
    "F/W": "FW", "FW": "FW", "A/W": "FW", "AW": "FW", "가을/겨울": "FW",
}


RETRIES = 3  # 요청 재시도 횟수 (D-45). 대기는 1·2·4초로 늘린다


def _request(method: str, url: str, retries: int = RETRIES, **kwargs) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, headers=HEADERS, timeout=20, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _get(url: str, **kwargs) -> requests.Response:
    return _request("GET", url, **kwargs)


class RobotsDisallowed(RuntimeError):
    """robots.txt가 이 크롤러의 User-Agent에 금지한 URL이다. 수집을 멈춘다."""


def robots_parser() -> RobotFileParser:
    """robots.txt를 이 크롤러의 User-Agent로 받아 해석한다.

    없거나 4xx면 모두 허용(robots 규약), 5xx·네트워크 오류면 확인할 수 없으므로 모두 금지로 본다.
    표준 라이브러리 `RobotFileParser`는 먼저 나온 규칙을 따른다. 그래서 Disallow 뒤에 더 구체적인 Allow가 오는
    파일은 보수적으로(막힌 쪽으로) 해석될 수 있다.
    """
    rp = RobotFileParser(ROBOTS_URL)
    try:
        resp = requests.get(ROBOTS_URL, headers=HEADERS, timeout=20)
    except requests.RequestException:
        rp.disallow_all = True
        return rp
    if resp.status_code >= 500:
        rp.disallow_all = True
    elif resp.status_code >= 400:
        rp.allow_all = True
    else:
        rp.parse(resp.text.splitlines())
    return rp


def check_robots(url: str, robots: RobotFileParser) -> None:
    if not robots.can_fetch(USER_AGENT, url):
        raise RobotsDisallowed(f"robots.txt가 이 User-Agent의 접근을 막는다: {url}")


def list_date(item: dict) -> str:
    """목록 항목의 post_date('2026.11.06') → '2026-11-06'. 없으면 ''."""
    return (item.get("post_date") or "").strip().replace(".", "-")


def fetch_article_list(limit: int | None = DEFAULT_LIMIT, delay: float = 1.0, since: str | None = None,
                       max_pages: int = 200, robots: RobotFileParser | None = None) -> list[dict]:
    """목록 AJAX를 페이지 단위로 호출해 기사 메타(post_id, permalink, title, date 등)를 반환한다.

    limit: 최대 개수(None = 제한 없음). since: 'YYYY-MM-DD' — 이 날짜 이상인 기사만 남긴다(D-45).
    목록은 대체로 최신순이지만 조금 섞여 있어, 한 페이지 전체가 since보다 오래되면 한 페이지 더 보고 멈춘다.
    robots: 미리 읽은 robots.txt(없으면 여기서 받는다). 목록 주소가 금지면 `RobotsDisallowed`.
    """
    robots = robots or robots_parser()
    check_robots(AJAX_URL, robots)
    items: list[dict] = []
    page, old_pages = 1, 0
    while (limit is None or len(items) < limit) and page <= max_pages:
        resp = _request("POST", AJAX_URL, data={**LIST_PARAMS, "paged": page})
        posts = (resp.json() or {}).get("current_posts") or []
        if not posts:
            break
        if since:
            keep = [p for p in posts if list_date(p) >= since]
            items.extend(keep)
            old_pages = old_pages + 1 if not keep else 0
            if old_pages >= 2:
                break
        else:
            items.extend(posts)
        page += 1
        time.sleep(delay)
    return items if limit is None else items[:limit]


def normalize_season(year: str, season: str) -> str | None:
    key = re.sub(r"\s+", "", season).upper()
    key = {"봄/여름": "봄/여름", "가을/겨울": "가을/겨울"}.get(key, key)
    code = SEASON_NORMALIZE.get(key)
    return f"{year}{code}" if code else None


# 캡션 끝에 붙는 사진 출처 (예: "Chanel 2027 S/S RTW. Getty Images")
CREDIT = (
    r"(?:Getty Images|GoRunway|Acielle\s*/\s*Style Du Monde|Kim Weston Arnold(?:/[^.]*)?|"
    r"Launchmetrics Spotlight|Indigital\s*/\s*Courtesy of [^.]*|Courtesy of [^.]*|Backgrid|"
    r"Photographed by [^.]*|@[\w.]+)"
)
CREDIT_TAIL_RE = re.compile(r"(?:^|\.\s+)(?P<credit>" + CREDIT + r")\s*$", re.IGNORECASE)
COLLECTION_CAPTION_RE = re.compile(
    r"^(?P<brand>.+?)\s+(?P<year>(?:19|20)\d{2})\s*"
    r"(?P<season>S/S|F/W|SS|FW|AW|봄\s*/\s*여름|가을\s*/\s*겨울)?\s*"
    r"(?P<kind>RTW|Menswear|Haute Couture|Couture|Métiers d[’']Art|Resort|Cruise|Pre-?Fall)?$",
    re.IGNORECASE,
)


def parse_caption(caption: str | None) -> dict:
    """이미지 캡션을 구조화한다. 원문에 있는 표현만 분리하며 추정하지 않는다.

    type: collection(브랜드+연도+시즌/종류) | credit_only(사진 출처뿐) | description(한글 설명) | other | none
    """
    out = {"type": "none", "brand": None, "year": None, "season_raw": None, "season": None,
           "kind": None, "credit": None, "text": None}
    if not caption or not caption.strip():
        return out
    cap = caption.strip()
    m = CREDIT_TAIL_RE.search(cap)
    if m:
        out["credit"] = m.group("credit")
        cap = cap[: m.start()].strip().rstrip(".").strip()
        if not cap:  # 사진 출처만 있는 캡션
            out["type"] = "credit_only"
            return out
    out["text"] = cap
    c = COLLECTION_CAPTION_RE.match(cap)
    if c and (c.group("season") or c.group("kind")):
        out.update(
            type="collection",
            brand=c.group("brand").strip(),
            year=int(c.group("year")),
            season_raw=c.group("season"),
            season=normalize_season(c.group("year"), c.group("season")) if c.group("season") else None,
            kind=c.group("kind"),
        )
    elif re.search(r"[가-힣]", cap):
        out["type"] = "description"
    else:
        out["type"] = "other"
    return out


def find_seasons(text: str, source: str) -> list[dict]:
    """텍스트에서 명시된 시즌 표현을 찾아 {raw, value, source, count} 목록으로 반환."""
    found: dict[str, dict] = {}
    for m in SEASON_RE.finditer(text or ""):
        raw = m.group(0)
        entry = found.setdefault(
            raw,
            {
                "raw": raw,
                "value": normalize_season(m.group("year"), m.group("season")),
                "source": source,
                "count": 0,
            },
        )
        entry["count"] += 1
    return list(found.values())


def _meta(soup: BeautifulSoup, key: str) -> str | None:
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return tag.get("content") if tag and tag.get("content") else None


def _img_url(img) -> str | None:
    # lazyload 이미지는 src가 base64 placeholder이므로 data-src를 우선한다.
    url = img.get("data-src") or img.get("src")
    if not url or url.startswith("data:"):
        return None
    return urljoin(BASE_URL, url)


def _real_imgs(el) -> list:
    """<noscript> 안의 중복 img를 제외한 실제 이미지."""
    return [i for i in el.find_all("img") if i.find_parent("noscript") is None]


def _text(el) -> str:
    return el.get_text(" ", strip=True)


# 본문(div.contt) 직계 자식 중 수집하지 않는 블록. 관련기사·쇼핑 상품 목록은 기사 본문이 아니다.
SKIP_CLASSES = {"relate_group", "relate_content", "shopping_list", "item_list"}


def parse_body(box) -> tuple[list[dict], dict]:
    """본문을 읽는 순서대로 blocks(paragraph/heading/image)로 변환한다.

    Returns: (blocks, meta) — meta에는 제외한 블록과 처리하지 못한 블록 정보를 담는다.
    """
    blocks: list[dict] = []
    meta = {"skipped": [], "unhandled": []}
    group_id = 0

    def add_image(img, caption, layout, group):
        src = _img_url(img)
        if src:
            blocks.append(
                {
                    "type": "image",
                    "url": src,
                    "caption": caption or None,
                    "alt": img.get("alt") or None,
                    "layout": layout,
                    "group_id": group,
                }
            )

    for el in box.find_all(recursive=False):
        classes = set(el.get("class") or [])
        if el.name in ("script", "style"):
            continue
        if classes & SKIP_CLASSES:
            meta["skipped"].append(sorted(classes & SKIP_CLASSES)[0])
        elif el.name == "p":
            if text := _text(el):
                blocks.append({"type": "paragraph", "text": text})
            for img in _real_imgs(el):  # 문단 안에 이미지가 끼어 있는 경우
                add_image(img, None, "inline", None)
        elif el.name in ("h2", "h3", "h4"):
            if text := _text(el):
                blocks.append({"type": "heading", "level": int(el.name[1]), "text": text})
        elif el.name == "figure":
            imgs = _real_imgs(el)
            cap = el.find("figcaption")
            if imgs:
                add_image(imgs[0], _text(cap) if cap else None, "single", None)
        elif "wp-block-dsmgn-two-column-images" in classes:
            group_id += 1
            for col in el.select("div.content_column"):
                imgs = _real_imgs(col)
                cap = col.select_one("p.caption")
                if imgs:
                    add_image(imgs[0], _text(cap) if cap else None, "two_column", group_id)
        elif "gallery_wrap" in classes:
            group_id += 1
            for slide in el.select("div.swiper-slide"):
                imgs = _real_imgs(slide)
                if imgs:  # 캡션은 img의 caption 속성에 들어 있다
                    add_image(imgs[0], imgs[0].get("caption"), "gallery", group_id)
        else:
            meta["unhandled"].append(f"{el.name}.{'.'.join(sorted(classes))}")
    return blocks, meta


def annotate(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """blocks에 idx를 매기고 섹션(h2 기준)과 이미지 주변 문단 위치를 계산한다.

    위치 정보는 원문 순서에서 계산한 값이다. 이미지가 어느 트렌드에 속하는지는 판단하지 않는다.
    """
    para_idx = []
    for i, b in enumerate(blocks):
        b["idx"] = i
        if b["type"] == "paragraph":
            para_idx.append(i)

    sections = [{"idx": 0, "heading": None, "heading_block_idx": None, "block_idxs": [], "paragraph_idxs": [], "image_idxs": []}]
    for b in blocks:
        if b["type"] == "heading" and b["level"] == 2:
            sections.append(
                {"idx": len(sections), "heading": b["text"], "heading_block_idx": b["idx"],
                 "block_idxs": [], "paragraph_idxs": [], "image_idxs": []}
            )
        cur = sections[-1]
        b["section_idx"] = cur["idx"]
        if not (b["type"] == "heading" and b["level"] == 2):
            cur["block_idxs"].append(b["idx"])
        if b["type"] == "paragraph":
            cur["paragraph_idxs"].append(b["idx"])

    image_no = 0
    run_no = -1
    for i, b in enumerate(blocks):
        if b["type"] == "image":
            b["image_idx"] = image_no
            image_no += 1
            # 연속된 이미지 블록은 같은 run (문단 사이에 낀 이미지 묶음)
            if i == 0 or blocks[i - 1]["type"] != "image":
                run_no += 1
            b["run_idx"] = run_no
            b["caption_parsed"] = parse_caption(b["caption"])
            sec = sections[b["section_idx"]]
            sec["image_idxs"].append(b["idx"])
            b["section_heading"] = sec["heading"]
            b["prev_paragraph_idx"] = max((p for p in para_idx if p < b["idx"]), default=None)
            b["next_paragraph_idx"] = min((p for p in para_idx if p > b["idx"]), default=None)
    return blocks, sections


def parse_article(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    tags = [t.strip() for t in (_meta(soup, "ds:tags") or "").split(",") if t.strip()]
    tags_html = [a.get_text(strip=True) for a in soup.select("ul.tag_list a")]
    tags = list(dict.fromkeys(tags + tags_html))

    # 본문: 읽는 순서를 보존한 blocks. (JSON-LD articleBody는 문단 구분이 없음)
    body_box = soup.select_one("div.post_content div.contt")
    blocks, sections, body_meta = [], [], {"skipped": [], "unhandled": []}
    if body_box:
        blocks, body_meta = parse_body(body_box)
        blocks, sections = annotate(blocks)
    images = [
        {k: b[k] for k in ("image_idx", "url", "caption", "alt", "layout", "group_id", "idx",
                           "section_idx", "section_heading", "prev_paragraph_idx", "next_paragraph_idx",
                           "run_idx", "caption_parsed")}
        | {"block_idx": b["idx"]}
        for b in blocks if b["type"] == "image"
    ]
    for im in images:
        del im["idx"]
    body = "\n\n".join(b["text"] for b in blocks if b["type"] in ("paragraph", "heading"))

    title = _meta(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else None)
    decoded_url = unquote(url)
    captions = " | ".join(i["caption"] for i in images if i["caption"])

    season_candidates = (
        find_seasons(" ".join(tags), "tag")
        + find_seasons(title or "", "title")
        + find_seasons(decoded_url, "url")
        + find_seasons(captions, "caption")
        + find_seasons(body, "body")
    )
    # 대표 시즌: 태그 → 제목 순으로 정규화된 값이 있을 때만 채운다. 그 외는 후보로만 남긴다.
    season = None
    for src in ("tag", "title"):
        hit = next((c for c in season_candidates if c["source"] == src and c["value"]), None)
        if hit:
            season = {"value": hit["value"], "source": src, "raw": hit["raw"]}
            break

    return {
        "article_id": _meta(soup, "ds:p_id"),
        "url": url,
        "title": title,
        "description": _meta(soup, "description"),
        "author": _meta(soup, "article:author"),
        "category": [m.get("content") for m in soup.find_all("meta", attrs={"property": "article:section"})],
        "published_at": _meta(soup, "article:published_time"),
        "modified_at": _meta(soup, "article:modified_time"),
        "tags": tags,
        "season": season,
        "season_candidates": season_candidates,
        "body": body or None,
        "body_length": len(body),
        "blocks": blocks,
        "sections": sections,
        "body_skipped": body_meta["skipped"],
        "body_unhandled": body_meta["unhandled"],
        "thumbnail_url": _meta(soup, "og:image"),
        "images": images,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def existing_ids(paths: list[str]) -> set[str]:
    """이미 받은 풀 파일들의 article_id (오류 레코드 제외)."""
    ids: set[str] = set()
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    if not r.get("error") and r.get("article_id"):
                        ids.add(str(r["article_id"]))
    return ids


def crawl(limit: int | None = DEFAULT_LIMIT, delay: float = 1.0, since: str | None = None,
          skip_ids: set[str] | None = None, robots: RobotFileParser | None = None) -> list[dict]:
    """목록 수집 → 기사별 파싱. 개별 기사 실패는 error 필드로 기록하고 계속 진행한다.
    skip_ids에 있는 post_id(= article_id)는 받지 않는다(D-45).
    robots.txt가 막는 주소를 만나면 기록하지 않고 `RobotsDisallowed`로 멈춘다."""
    robots = robots or robots_parser()
    listing = fetch_article_list(limit, delay, since=since, robots=robots)
    skip_ids = skip_ids or set()
    listing = [it for it in listing if str(it.get("post_id")) not in skip_ids]
    records = []
    for item in listing:
        url = urljoin(BASE_URL, item["permalink"])
        check_robots(url, robots)
        try:
            resp = _get(url)
            rec = parse_article(resp.content.decode("utf-8"), resp.url)
            rec["list_post_date"] = item.get("post_date")
            rec["list_title"] = item.get("post_title")
            rec["error"] = None
        except Exception as e:  # 타당성 확인이 목적이므로 실패 사유를 남긴다
            rec = {"url": url, "list_title": item.get("post_title"), "error": repr(e)}
        records.append(rec)
        time.sleep(delay)
    return records


def save_jsonl(records: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def published_since(records: list[dict], since: str | None) -> tuple[list[dict], list[dict]]:
    """범위는 기사 발행일(published_at) 기준이다(D-45). → (범위 안, 발행일이 since보다 이른 것). 오류 레코드는 범위 안으로 둔다."""
    if not since:
        return list(records), []
    keep, out = [], []
    for r in records:
        pub = (r.get("published_at") or "")[:10]
        (out if (not r.get("error") and pub and pub < since) else keep).append(r)
    return keep, out


def merge_pools(existing: list[str], new_records: list[dict], out: str) -> dict[str, int]:
    """기존 풀 파일의 줄을 **그대로** 복사하고(바이트 그대로), 오류 없고 아직 없는 새 기사만 덧붙인다 (D-45)."""
    seen: set[str] = set()
    kept = added = 0
    with open(out, "wb") as w:
        for p in existing:
            with open(p, "rb") as f:
                for raw in f:
                    if not raw.strip():
                        continue
                    aid = str(json.loads(raw).get("article_id"))
                    if aid in seen:
                        continue
                    seen.add(aid)
                    w.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    kept += 1
        for r in new_records:
            aid = str(r.get("article_id"))
            if r.get("error") or not r.get("article_id") or aid in seen:
                continue
            seen.add(aid)
            w.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
            added += 1
    return {"kept": kept, "added": added}
