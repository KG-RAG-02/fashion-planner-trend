#!/usr/bin/env bash
# docs/diagrams/src/*.dot를 라이트·다크 SVG와 PNG로 그린다.
#
#   bash scripts/render_diagrams.sh
#
# 필요: Graphviz `dot`, `rsvg-convert`(librsvg), Pretendard 폰트(OFL, 로컬 설치. 레포에는 넣지 않는다).
# 결과: 원본 <name>.dot마다 docs/diagrams/<name>.svg, <name>-dark.svg, <name>.png, <name>-dark.png
# 원본의 색은 @TOKEN@ 자리표시자이고, 이 스크립트가 테마별 팔레트로 바꾼다.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=docs/diagrams/src
OUT=docs/diagrams
ROUTE='/v1/agents/trend/runs'

command -v dot >/dev/null || { echo "Graphviz 'dot'이 없다" >&2; exit 1; }
command -v rsvg-convert >/dev/null || { echo "'rsvg-convert'(librsvg)가 없다" >&2; exit 1; }
if command -v fc-list >/dev/null && ! fc-list : family | grep -i pretendard >/dev/null; then
  echo "경고: Pretendard 폰트를 찾지 못했다. 다른 폰트로 그려진다" >&2
fi

render() {  # render <name> <suffix> <bg> <fg> <svc_fill> <svc_line> <ext> <err>
  local name=$1 suffix=$2
  sed -e "s|@BG@|$3|g" -e "s|@FG@|$4|g" -e "s|@SVC_FILL@|$5|g" -e "s|@SVC_LINE@|$6|g" \
      -e "s|@EXT@|$7|g" -e "s|@ERR@|$8|g" -e "s|@ROUTE@|$ROUTE|g" "$SRC/$name.dot" \
    | dot -Tsvg \
    | sed -e "s|font-family=\"Pretendard\"|font-family=\"Pretendard, 'Noto Sans KR', 'Apple SD Gothic Neo', sans-serif\"|g" \
    > "$OUT/$name$suffix.svg"
  rsvg-convert -w 1600 "$OUT/$name$suffix.svg" -o "$OUT/$name$suffix.png"
}

for src in "$SRC"/*.dot; do
  name=$(basename "$src" .dot)
  render "$name" ""      "#ffffff" "#1f2328" "#eef2ff" "#4f46e5" "#6b7280" "#b91c1c"
  render "$name" "-dark" "#0d1117" "#e6edf3" "#1e1b4b" "#818cf8" "#6b7280" "#f87171"
  echo "그림: $name"
done
