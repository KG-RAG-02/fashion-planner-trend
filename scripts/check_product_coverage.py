"""정답(label) Item마다 대응하는 Product가 몇 개 있는지 센다. 그래프를 만들기 전에 "검색하면 결과가 나오는가"를 보는 용도.

각 Item에 대해 (02 전체 / 03 전체) Product 수를 센다:
  상분류 = 후보 조건(gate) / 하분류 일치 / 하분류+색 일치 / 하분류+속성(색·소재·패턴·디테일·핏) 1개 이상 일치
approx 값과 값이 없는 속성은 일치로 세지 않는다 (spec Step 8~9와 같은 규칙). 지원 밖 Item(category_major=null)은 건너뛴다.

실행:  python scripts/check_product_coverage.py [article_id ...]
입력:  eval/datasets/article_extraction/labels/*.json, data/processed/otpensource_all.jsonl,
       musinsa_snap_all.jsonl (없으면 03은 건너뛴다)
       (02 전체 파일은 `python scripts/build_otpensource.py`, 03은 `python scripts/build_musinsa_snap.py`로 만든다)
출력:  data/processed/product_coverage.txt 와 표준 출력
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.schemas.graph_schema import ProductRecord  # noqa: E402

LABELS = ROOT / "eval" / "datasets" / "article_extraction" / "labels"
PROC = ROOT / "data" / "processed"


def load(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        r = ProductRecord.model_validate_json(line)
        n = r.node
        out.append(dict(
            major=r.category_major.value if r.category_major else None,
            minor=n.category_minor.value if n.category_minor else None,
            gender=n.gender.value if n.gender else None,
            color={e.value for e in r.colors if e.value and not e.approx},
            material={e.value for e in r.materials if e.value and not e.approx},
            pattern={e.value for e in r.patterns if e.value and not e.approx},
            detail={e.value for e in r.details if e.value and not e.approx},
            fit=n.fit.value if n.fit else None,
        ))
    return out


def count(pool: list[dict], it: dict) -> tuple[int, int, int | None, int]:
    gate = [p for p in pool if p["major"] == it["major"]]
    minor = [p for p in gate if p["minor"] == it["minor"]] if it["minor"] else gate
    color = [p for p in minor if it["color"] & p["color"]] if it["color"] else None

    def hit(p: dict) -> bool:
        return any(it[k] & p[k] for k in ("color", "material", "pattern", "detail")) or (it["fit"] is not None and p["fit"] == it["fit"])

    return len(gate), len(minor), (len(color) if color is not None else None), sum(1 for p in minor if hit(p))


def main() -> None:
    all_path = PROC / "otpensource_all.jsonl"
    if not all_path.exists():
        sys.exit("otpensource_all.jsonl이 없다. 먼저 `python scripts/build_otpensource.py`")
    pools = {"02": load(all_path)}
    snap_path = PROC / "musinsa_snap_all.jsonl"
    if snap_path.exists():
        pools["03"] = load(snap_path)
    lines = ["Product: " + ", ".join(f"{k} {len(v)}" for k, v in pools.items()), "형식: (상분류 / 하분류 / 하분류+색 / 하분류+속성1개이상)", ""]
    ids = sys.argv[1:] or sorted(p.stem for p in LABELS.glob("*.json"))
    tot = {k: [0, 0, 0] for k in pools}
    for i in ids:
        st = json.loads((LABELS / f"{i}.json").read_text(encoding="utf-8"))["extraction"]["styles"][0]
        lines.append(f"=== {i} {st['name']}")
        for it in st["items"]:
            if it["category_major"] is None:
                lines.append(f"  {it['name']:<20} 지원 밖")
                continue
            x = dict(major=it["category_major"], minor=it["category_minor"], fit=it["fit"],
                     color={c["value"] for c in it["colors"] if c["value"]}, material={c["value"] for c in it["materials"] if c["value"]},
                     pattern={c["value"] for c in it["patterns"] if c["value"]}, detail={c["value"] for c in it["details"] if c["value"]})
            attrs = ",".join(f"{k}:{'/'.join(sorted(v)) if isinstance(v, set) else v}" for k, v in x.items() if k not in ("major", "minor") and v) or "속성 없음"
            cells = []
            for key, pool in pools.items():
                c = count(pool, x)
                tot[key][0] += 1
                tot[key][1] += c[1] > 0
                tot[key][2] += c[3] > 0
                cells.append(f"{key}{c}")
            lines.append(f"  {it['name']:<20} {attrs:<34} " + " ".join(cells))
        lines.append("")
    for key in pools:
        a, b, c = tot[key]
        lines.append(f"[{key}] 지원 범위 Item {a}개 중 하분류 후보가 있는 Item {b}, 속성이 1개 이상 맞는 후보가 있는 Item {c}")
    full = pools["02"]
    lines += ["", "02 하분류 분포: " + str(Counter(p["minor"] for p in full).most_common()),
              "02에서 핏이 있는 Product: " + str(sum(1 for p in full if p["fit"]))]
    if "03" in pools:
        lines.append("03에서 핏이 있는 Product: " + str(sum(1 for p in pools["03"] if p["fit"])))
    text = "\n".join(lines)
    (PROC / "product_coverage.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
