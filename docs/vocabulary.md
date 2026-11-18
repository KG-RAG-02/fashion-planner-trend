# 표준 어휘와 별칭 사전

Fashion Planner는 한국어 패션 매거진 기사에서 트렌드(Style → Item → 속성)를 LLM으로 추출하고, 공개 Hugging Face
데이터셋 두 개(`otpensource_dataset`, `musinsa-snap-new`)의 상품 레코드와 매칭한다. 양쪽의 모든 속성 값은 하나의
**표준 어휘**로 정규화한다. 이 문서는 표준 어휘의 값 목록과, 원문 문자열을 표준 값으로 바꾸는 **별칭 사전**을 설명한다.

- 값의 기준: `src/fashion_planner_trend/schemas/vocabulary.py` (파이썬 `str` Enum).
- 별칭 사전 코드: `src/fashion_planner_trend/ingestion/normalization/alias_map.py`, `text_match.py`.
- 별칭 사전 데이터: `configs/alias_map/*.yaml`. 테스트: `tests/test_alias_map.py`.
- 데이터에 들어가는 문자열은 Enum의 **값**(한글)이다. Enum **멤버 이름**(예: `Color.SKYBLUE`)은 코드에서만 쓴다.

## 출처와 라이선스

값 목록은 다음 문서의 속성 표에서 가져왔다.

**K-Fashion 이미지 AI 데이터 구축 가이드라인** (K-Fashion image AI data construction guideline), Ver 1.04
(2020-11-24), AI Hub 배포.

이 저장소에는 가이드라인의 본문이 들어 있지 않고, 코드가 쓰는 값 목록만 있다. 값 목록을 다시 쓰거나 배포하려면
가이드라인의 라이선스를 사용자가 직접 확인해야 한다. 이 저장소는 그 라이선스에 대해 아무것도 주장하지 않는다.
가이드라인 표와 다르게 쓰는 부분은 각 절에 적었다 (예: "알 수 없음"은 표준 값이 아니고, 정보가 없으면 `null`이다).

## 왜 표준 어휘인가

- **그래프 노드 공유.** 기사 Item과 상품은 같은 노드를 가리킬 때만 만난다. 기사가 "버터옐로우"라고 쓴 Item과
  `YELLOW` 라벨의 상품이 모두 `Color` 노드 `옐로우`에 연결되어야 탐색이 성립한다. 어휘가 하나가 아니면 비슷한
  노드가 여러 개 생기고 매칭을 놓친다.
- **비교 가능한 점수와 닫힌 선택지.** 점수는 Item과 Product의 값을 속성별로 비교하므로 양쪽이 같은 목록을 써야
  의미가 있다. 추출 LLM도 Enum 값(또는 `null`)만 고를 수 있어 새 카테고리나 색을 지어내지 못한다.
- **하위 서비스.** 색·카테고리 목록은 재고 서비스 fashion-planner-inventory가 받는 입력 규격과 같다.
- **원문 보존.** 표준 값 옆에 원문 표현을 항상 남긴다 (`raw`, `*_raw`, `category_raw`). 거친 어휘 때문에 잃는
  정보는 원문으로 보여 줄 수 있다.

## 한눈에 보기

| Enum | 개수 | 필드 | 그래프 표현 | 점수 |
|---|---:|---|---|---|
| `Color` | 21 | `colors[].value`, 상품 `sub_colors` | `Color` 노드, `HAS_COLOR {raw, approx}` | 사용 (`sub_colors`는 미사용) |
| `CategoryMajor` | 4 | `category_major` | `Category` 노드, `IN_CATEGORY` | 후보 조건 (상분류가 같아야 함) |
| `CategoryMinor` | 21 | `category_minor` | 속성 | 일치하면 보너스 |
| `Material` | 26 | `materials[].value` | `Material` 노드, `HAS_MATERIAL {raw, approx}` | 사용 |
| `Pattern` | 19 | `patterns[].value` | `Pattern` 노드, `HAS_PATTERN {raw, approx}` | 사용 |
| `DetailName` / `Neckline` / `Collar` | 40 / 12 / 10 | `details[]` (`type` 포함) | `Detail` 노드, `HAS_DETAIL {raw, motif, placement, approx}` | 사용 (`detail`로) |
| `BodyLength` | 9 | `length_body` | 속성 | 미사용 (저장·표시) |
| `SleeveLength` | 5 | `length_sleeve` | 속성 | 미사용 (저장·표시) |
| `Fit` | 7 | `fit` | 속성 | 사용 |
| `Shape` | 10 | `shape` | 속성 | 미사용 (저장·표시) |
| `Season` | 2 | Style `seasons`, 상품 `season` | Style 속성, 키 예: `2030SS` | 질의 필터 |
| `Gender` | 3 | `gender` | 속성 | 선택 필터 |

점수에 쓰는 속성은 `ScoredAttribute` Enum으로 고정되어 있다: `category`, `color`, `material`, `pattern`, `detail`,
`fit`. 근사 값([아래](#근사-매핑과-점수에서-빼는-이유))은 어떤 속성이든 점수에 쓰지 않는다.

## Color

21개. 가이드라인의 20색에 부록의 `실버`를 더했다.

| 값 | 멤버 | 값 | 멤버 | 값 | 멤버 |
|---|---|---|---|---|---|
| 블랙 | `BLACK` | 화이트 | `WHITE` | 그레이 | `GREY` |
| 레드 | `RED` | 핑크 | `PINK` | 오렌지 | `ORANGE` |
| 베이지 | `BEIGE` | 브라운 | `BROWN` | 옐로우 | `YELLOW` |
| 그린 | `GREEN` | 카키 | `KHAKI` | 민트 | `MINT` |
| 블루 | `BLUE` | 네이비 | `NAVY` | 스카이블루 | `SKYBLUE` |
| 퍼플 | `PURPLE` | 라벤더 | `LAVENDER` | 와인 | `WINE` |
| 네온 | `NEON` | 골드 | `GOLD` | 실버 | `SILVER` |

- 색 노드가 21개뿐이라 노드 하나에 상품이 많이 붙는다. 그래서 색은 탐색 시작점이 아니라 필터와 점수에 쓴다.
- 톤은 사라진다 (버터옐로우와 레몬이 모두 `옐로우`). 원문 색 이름은 `HAS_COLOR.raw`에 남는다.
- `sub_colors`(보조색, `musinsa-snap-new`에만 있음)는 같은 21색을 쓰지만 단순 속성이다. `Color` 노드에 연결하지
  않고 점수에도 쓰지 않는다.

## 카테고리: CategoryMajor, CategoryMinor

상분류(영역 4개)는 그래프 노드이고, 하분류(의류 종류 21개)는 Item·Product의 속성이다. `category_minor`가 있으면
코드가 `MINOR_TO_MAJOR` 표로 `category_major`를 정한다. LLM이 다른 상분류를 냈으면 코드 값을 쓰고 Item에
`category_major_mismatch` 검토 표시를 남긴다.

| `CategoryMajor` | `CategoryMinor` (값 `멤버`) |
|---|---|
| 상의 `UPPER` | 탑 `TOP`, 블라우스 `BLOUSE`, 티셔츠 `TSHIRT`, 니트웨어 `KNITWEAR`, 셔츠 `SHIRT`, 브라탑 `BRA_TOP`, 후드티 `HOODIE` |
| 하의 `LOWER` | 청바지 `JEANS`, 팬츠 `PANTS`, 스커트 `SKIRT`, 레깅스 `LEGGINGS`, 조거팬츠 `JOGGER_PANTS` |
| 아우터 `OUTER` | 코트 `COAT`, 재킷 `JACKET`, 점퍼 `JUMPER`, 패딩 `PADDING`, 베스트 `VEST`, 가디건 `CARDIGAN`, 짚업 `ZIP_UP` |
| 원피스 `ONEPIECE` | 드레스 `DRESS`, 점프수트 `JUMPSUIT` |

- 표기는 표와 똑같이 쓴다 (`스카이블루`, `점프수트`, `짚업`, `가디건`).
- 가이드라인의 "캐주얼상의"와 "수영복"은 이 어휘에 없다. 표에 없는 의류는 가장 가까운 종류에 둔다 (트렌치코트 →
  `코트`, 블레이저 → `재킷`). 원문은 `category_raw`에 남긴다.
- 영역만 알면 `category_minor = null`이고 `category_major`만 채운다. 영역도 모르면 둘 다 `null`이다.
- 신발·가방·모자·주얼리·액세서리·수영복도 수집하지만(`category_raw` 보존) `category_major = null`이다. 이런
  레코드는 그래프에 적재하지 않고 검색 결과에도 나오지 않는다.
- 상의와 아우터는 다른 영역이다. "하의 빼고"는 상의·아우터·원피스를 뜻한다.

## Material

26개. 가이드라인의 소재 25개에 부록의 `헤어니트`를 더했다.

| 값 | 멤버 | 값 | 멤버 | 값 | 멤버 |
|---|---|---|---|---|---|
| 퍼 | `FUR` | 무스탕 | `MOUTON` | 스웨이드 | `SUEDE` |
| 앙고라 | `ANGORA` | 코듀로이 | `CORDUROY` | 시퀸/글리터 | `SEQUIN_GLITTER` |
| 데님 | `DENIM` | 저지 | `JERSEY` | 트위드 | `TWEED` |
| 벨벳 | `VELVET` | 비닐/PVC | `VINYL_PVC` | 울/캐시미어 | `WOOL_CASHMERE` |
| 합성섬유 | `SYNTHETIC` | 니트 | `KNIT` | 레이스 | `LACE` |
| 린넨 | `LINEN` | 메시 | `MESH` | 플리스 | `FLEECE` |
| 네오프렌 | `NEOPRENE` | 실크 | `SILK` | 스판덱스 | `SPANDEX` |
| 자카드 | `JACQUARD` | 가죽 | `LEATHER` | 면 | `COTTON` |
| 시폰 | `CHIFFON` | 헤어니트 | `HAIR_KNIT` | | |

- 슬래시가 든 값 3개(`시퀸/글리터`, `비닐/PVC`, `울/캐시미어`)는 각각 **값 하나**다 (아래 복합값 보호).
- 다운·패딩은 소재가 아니다. 카테고리 `패딩`을 가리킨다.
- 기모·골지·우븐·테리·트윌 같은 가공·조직은 목록에 없다. 일부는 같은 소재 계열의 가장 가까운 값에 근사 매핑하고,
  나머지는 원문만 남기고 `null`로 둔다.

## Pattern

19개. 가이드라인의 프린트 18개에 부록의 `믹스`를 더했다.

| 값 | 멤버 | 값 | 멤버 | 값 | 멤버 |
|---|---|---|---|---|---|
| 체크 | `CHECK` | 스트라이프 | `STRIPE` | 지그재그 | `ZIGZAG` |
| 호피 | `LEOPARD` | 지브라 | `ZEBRA` | 도트 | `DOT` |
| 카무플라쥬 | `CAMOUFLAGE` | 페이즐리 | `PAISLEY` | 아가일 | `ARGYLE` |
| 플로럴 | `FLORAL` | 레터링 | `LETTERING` | 해골 | `SKULL` |
| 타이다이 | `TIE_DYE` | 그라데이션 | `GRADATION` | 무지 | `SOLID` |
| 그래픽 | `GRAPHIC` | 하운즈투스 | `HOUNDSTOOTH` | 깅엄 | `GINGHAM` |
| 믹스 | `MIX` | | | | |

`무지`는 "패턴이 없음이 **명시된** 경우"를 뜻하는 정식 값이다. 기본값이 아니다. 기사나 상품이 패턴을 말하지 않으면
패턴은 비어 있고(미확인) `무지`가 아니다. 기사는 패턴을 거의 언급하지 않으므로, 미언급을 `무지`로 보면 대부분
틀린 값이 된다.

## Detail, Neckline, Collar

가이드라인은 디테일·넥라인·칼라를 별도 속성으로 둔다. 이 저장소는 셋을 `Detail` 노드 하나에 두고 `type`
(`DetailType`: `detail`, `neckline`, `collar`)으로 구분한다. 노드 id는 `detail_node_id(type, value)`가 만드는
`<type>:<value>`다 (예: `detail:자수`, `neckline:브이넥`, `collar:셔츠칼라`).

접두어가 필요한 이유는 같은 이름이 여러 목록에 있기 때문이다. `detail:후드`와 `neckline:후드`는 다른 노드이고,
`detail:패딩`은 카테고리 `패딩`과 이름만 같다. `details[].value`는 그 `type`의 Enum 값이어야 하고, 아니면 스키마
검증에서 거부된다. 모티프("꽃")와 위치("가장자리")는 값이 아니라 관계 속성 `motif`, `placement`에 둔다.

`DetailName` (40):

| 값 | 멤버 | 값 | 멤버 | 값 | 멤버 | 값 | 멤버 |
|---|---|---|---|---|---|---|---|
| 비즈 | `BEADS` | 단추 | `BUTTONED` | 니트꽈배기 | `CABLE` | 체인 | `CHAIN` |
| 컷오프 | `CUT_OFF` | 더블브레스티드 | `DOUBLE_BREASTED` | 드롭숄더 | `DROP_SHOULDER` | 자수 | `EMBROIDERY` |
| 프릴 | `FRILL` | 프린지 | `FRINGE` | 플레어 | `FLARE` | 퀼팅 | `QUILTING` |
| 리본 | `RIBBON` | 롤업 | `ROLL_UP` | 러플 | `RUFFLE` | 셔링 | `SHIRRING` |
| 슬릿 | `SLIT` | 스팽글 | `SPANGLE` | 스티치 | `STITCH` | 스터드 | `STUD` |
| 퍼트리밍 | `FUR_TRIMMED` | 글리터 | `GLITTER` | 후드 | `HOOD` | 레이스업 | `LACE_UP` |
| 패딩 | `PADDING` | 패치워크 | `PATCHWORK` | 페플럼 | `PEPLUM` | 플리츠 | `PLEATS` |
| 포켓 | `POCKETS` | 폼폼 | `POMPOM` | 퍼프 | `PUFF` | 태슬 | `TASSEL` |
| 집업 | `ZIP_UP` | 띠 | `SASH` | 디스트로이드 | `DESTROYED` | 드롭웨이스트 | `LOW_WAIST` |
| 버클 | `BUCKLE` | 컷아웃 | `CUT_OUT` | X스트랩 | `X_STRAP` | 비대칭 | `UNBALANCED` |

`Neckline` (12), `Collar` (10):

| Neckline 값 | 멤버 | Collar 값 | 멤버 |
|---|---|---|---|
| 라운드넥 | `ROUND` | 셔츠칼라 | `SHIRT` |
| 유넥 | `U` | 보우칼라 | `BOW` |
| 브이넥 | `V` | 세일러칼라 | `SAILOR` |
| 홀터넥 | `HALTER` | 숄칼라 | `SHAWL` |
| 오프숄더 | `OFF_SHOULDER` | 폴로칼라 | `POLO` |
| 원숄더 | `ONE_SHOULDER` | 피터팬칼라 | `PETERPAN` |
| 스퀘어넥 | `SQUARE` | 너치드칼라 | `NOTCHED` |
| 노카라 | `COLLARLESS` | 차이나칼라 | `STANDUP` |
| 후드 | `HOOD` | 테일러드칼라 | `TAILORED` |
| 터틀넥 | `TURTLENECK` | 밴드칼라 | `BAND` |
| 보트넥 | `BOAT` | | |
| 스위트하트 | `SWEETHEART` | | |

## 기장: BodyLength, SleeveLength

기장과 소매 길이는 표준 값으로 저장하고 결과에 표시하지만 **점수에는 쓰지 않는다.** 소매 길이는 별도 필드이고
디테일이 아니다. 필드는 `length_body_raw` / `length_body`, `length_sleeve_raw` / `length_sleeve`다.

`BodyLength` (9)는 가이드라인의 영역별 기장 목록의 합집합이다.

| 값 | 멤버 | 값 | 멤버 | 값 | 멤버 |
|---|---|---|---|---|---|
| 크롭 | `CROP` | 노멀 | `NORMAL` | 하프 | `HALF` |
| 롱 | `LONG` | 미니 | `MINI` | 니렝스 | `KNEE` |
| 미디 | `MIDI` | 발목 | `ANKLE` | 맥시 | `MAXI` |

| 영역 | 가이드라인이 그 영역에 둔 기장 |
|---|---|
| 상의 | 크롭, 노멀, 롱 |
| 하의, 원피스 | 미니, 니렝스, 미디, 발목, 맥시 |
| 아우터 | 크롭, 노멀, 하프, 롱, 맥시 |

영역 표는 참고용이고 제약이 아니다. 스키마는 어느 영역이든 9개 값을 모두 받고, 표와 맞지 않는 상품 값도 원천
그대로 둔다. 기사에서는 원문이 표준 값과 같은 말이면 그 값을 쓴다. "롱"·"긴"은 스커트나 드레스여도 `롱`이고
`맥시`로 바꾸지 않는다.

`SleeveLength` (5): 민소매 `SLEEVELESS`, 반팔 `SHORT`, 캡 `CAP`, 7부소매 `THREE_QUARTER`, 긴팔 `LONG`.

## Fit

`Fit`은 **몸과의 여유**다. 노드가 아니라 속성이다.

| 값 | 멤버 | 가이드라인이 둔 영역 |
|---|---|---|
| 노멀 | `NORMAL` | 전체 |
| 루즈 | `LOOSE` | 전체 |
| 오버사이즈 | `OVERSIZED` | 상의, 아우터, 원피스 |
| 타이트 | `TIGHT` | 상의, 아우터, 원피스 |
| 스키니 | `SKINNY` | 하의 |
| 와이드 | `WIDE` | 하의 |
| 벨보텀 | `BELL_BOTTOM` | 하의 |

- `슬림`은 표준 값이 아니다. `타이트`로 **근사** 매핑하므로 저장은 하지만 점수에는 쓰지 않는다.
- A라인·펜슬·부츠컷·스트레이트 같은 형태 어휘는 핏이 아니라 `Shape`다. 사이즈(S/M/L)는 쓰지 않고 `size`는 항상
  `null`이다.

## Shape

`Shape`는 의류의 전체 형태다. 저장·표시만 하고 검색·필터·점수에는 쓰지 않는다. 가이드라인의 "알 수 없음"은 넣지
않았다. 값(10): 페플럼 `PEPLUM`, 머메이드 `MERMAID`, 비대칭 `ASYMMETRICAL`, 벨보텀/플레어 `BELL_FLARE`, 부츠컷
`BOOTS_CUT`, 펜슬 `PENCIL`, 테이퍼드 `TAPERED`, A라인 `A_LINE`, 스트레이트 `STRAIGHT`, H라인 `H_LINE`.

겹치는 이름은 값이 들어온 필드로 구분한다. `벨보텀`은 핏이고 `벨보텀/플레어`는 세이프다. `비대칭`과 `페플럼`은
디테일에도 세이프에도 있다.

## Season, Gender

둘 다 가이드라인에서 온 값이 아니다 (가이드라인의 원천 이미지는 여성복이고 시즌 정보가 없다).

- `Season`: `SS`(봄·여름), `FW`(가을·겨울). Style의 시즌은 `SeasonRef {year, season}`이고, 그래프 키는 둘을 이은
  문자열이다 (예: `2030SS`).
- `Gender`: `남`, `여`, `공용`. 상품의 "성별무관"은 `공용`, 빈 값은 `null`이다. 기사에서는 명시된 경우만 채운다.

## 별칭 사전

별칭 사전은 원문 문자열(상품 라벨, 상품명, LLM의 `raw`)을 표준 값으로 바꾼다. 보수적으로 동작한다. 사전에 없는
문자열은 `null`로 두고 원문을 남긴다. 가장 비슷한 값에 억지로 넣지 않는다.

### 파일 구성

YAML 파일마다 `attribute:` 키로 어느 속성의 사전인지 밝힌다 (로더는 파일 이름이 아니라 이 키를 쓴다).

| 파일 | 내용 | 쓰는 섹션 |
|---|---|---|
| `color.yaml` | 색 | `modifiers`, `aliases`, `null_values` |
| `category.yaml` | 하분류 | `aliases`, `null_values`, `hint` |
| `category_otpensource.yaml` | `otpensource_dataset` 소분류 → `[하분류, 상분류, 기장 원문, 소매 원문]`. 셔츠/블라우스·스커트/원피스는 상품명으로 정하고, 소분류가 빈 행은 대분류로 상분류만 정한다 | `subcategories`, `blouse_shirt_keywords`, `big_category_fallback`, `skirt_dress_name_fallback` |
| `material.yaml` | 소재 | `aliases`, `approx`, `null_values`, `route`, `hint` |
| `pattern.yaml` | 패턴 | `aliases`, `approx`, `null_values`, `route` |
| `detail.yaml`, `neckline.yaml`, `collar.yaml` | 디테일·넥라인·칼라 | `aliases`, `approx`, `null_values`, `route` |
| `fit.yaml` | 핏 | `aliases`, `approx`, `null_values`, `route` |
| `shape.yaml`, `body_length.yaml`, `sleeve_length.yaml` | 세이프·기장 | `aliases`, `approx`, `null_values` |
| `text_guard.yaml` | 텍스트 검색 오탐 가드 단어 | `guards` |
| `set_components.yaml` | 어휘가 아니다. 상품명의 세트·셋업을 구성품으로 나누는 규칙 | 의류 키, 표지어, 가드 |
| `set_overrides.yaml` | 어휘가 아니다. 사람이 검토한 상품별 세트 분해 결과 | `split`, `clear_candidate` |

### 항목 종류

| 종류 | YAML 형식 | 뜻 | 결과 (`Normalized`) |
|---|---|---|---|
| 표준 | 적지 않음 | Enum 값은 모두 자동으로 색인된다 | `matched_by="standard"`, `approx=False` |
| `aliases` | `표준 값: [원문, …]` | 같은 뜻의 다른 표기·언어 | `matched_by="alias"`, `approx=False` |
| `approx` | `표준 값: [원문, …]` | 같은 것은 아니지만 같은 계열에서 가장 가까운 값 | `matched_by="approx"`, `approx=True` |
| `null_values` | `[원문, …]` | 값 없음이 명시된 것 (미상, 해당 없음, 노이즈, 의류가 아닌 말) | `matched_by="null_value"`, 값 `null` |
| `route` | `원문: {to: 속성, value: 값}` | 다른 속성의 값이 잘못 들어온 것. 속성만 알면 `value`는 `null` | `route_to` 설정, 호출한 쪽이 옮긴다 |
| `hint` | `원문: {필드: 값}` | 이 속성의 값은 아니지만 같은 레코드의 다른 필드에 주는 힌트 | `hint` 설정, 호출한 쪽이 판단한다 |

예: `아이보리`는 `화이트`의 별칭이다. `기모`는 `플리스`, `슬림`은 `타이트`로 근사한다. 디테일 필드에 들어온
`스트라이프`는 패턴으로, 핏 필드에 들어온 `부츠컷`은 세이프로 옮긴다. 소재 `다운`은 `category_minor = 패딩`
힌트다. 상품 빌드는 하분류가 아직 비어 있고 영역이 미정이거나 아우터일 때만 이 힌트를 적용하고, 하분류가 비어 있는
다른 영역에는 대신 디테일 `패딩`을 붙이며, 이미 정해진 하분류는 덮어쓰지 않는다. `pattern.yaml`에서 "없음"류
단어는 `무지`의 별칭이다. `musinsa-snap-new`의 프린트 라벨이 그 단어로 "패턴 없음"을 명시하기 때문이다.

### 단일 값 매칭 규칙

`AliasMap.normalize(attribute, raw)`:

1. 키를 만든다. 소문자로 바꾸고 공백·`-`·`_`를 뺀다 (`Sky Blue` → `skyblue`).
2. 색인에서 찾는다. 색인은 표준 값, 별칭, 근사 순으로 채워지고, 같은 키가 두 번 나오면 먼저 들어간 종류가 이긴다.
3. 없으면 `route`, `hint`, `null_values` 순으로 찾는다.
4. 그래도 없으면 `matched_by="none"`이다. 값은 `null`이고 원문은 남는다.

### 복수 값과 복합값 보호

`split_tokens`는 필드를 `,` `/` `+` `&` `:`로 나누고, 리스트는 원소마다 나눈다. 표준 값 3개는 값 자체에 슬래시가
있으므로 나누기 전에 보호한다 (대소문자 무시): `시퀸/글리터`, `비닐/PVC`, `울/캐시미어`. 그래서 `울/캐시미어`는
토큰 하나로 남고 `울/폴리`는 두 토큰이 되어 `울/캐시미어`와 `합성섬유`로 매핑된다. 토큰이 아무것에도 걸리지
않으면 꼬리표 "블렌드"·"혼방"을 떼고 한 번 더 찾는다 (`울 블렌드` → `울/캐시미어`).

### 색 규칙

`normalize_color`는 토큰마다 다음 순서로 처리한다.

1. 숫자만 있는 값과 "2color" 같은 색 개수 표기는 `null`이다.
2. 위의 단일 값 매칭을 한다.
3. 걸리지 않으면 `modifiers`(다크·라이트·딥·워시드·멜란지·미디엄·pale·heather 등)를 떼고 다시 찾는다. 이렇게
   찾은 값은 정확 일치로 본다 (`다크 네이비` → `네이비`).
4. `멜란지`만 있으면 `그레이`다.
5. 토큰이 2글자 이상의 표준 색이나 별칭으로 **끝나면** 그 색을 쓰되 **근사**로 표시한다 (`sage green`·`차콜 그레이`
   → 근사). `블루그린`처럼 틀릴 수 있는 조합이 있어서다.
6. 나머지는 `null`이다. 목록에 없는 색(`오트밀` 등)은 추측하지 않는다.

### 상품명 텍스트 매칭 (`text_match.py`)

`otpensource_dataset`에는 패턴·디테일·세이프 열이 없어 상품명과 짧은 `feature` 텍스트에서 찾는다. 두 텍스트를
따로, 토큰 단위로 찾는다.

- **토큰 경계.** 공백·괄호와 `/ , + & | · • : ; _ " ! ? ~ * # @ =`로 나눈다. 하이픈은 토큰 안에 둔다 (`A-LINE`).
- **한국어 키**는 토큰 안 부분 일치를 허용한다 (`반집업`에서 `집업`). 단 가드 단어가 있으면 버린다.
- **영문 키**는 토큰 전체가 키이거나 키 + 굴절 접미사(`s`, `es`, `d`, `ed`, `ded`, `ered`, `y`, `ie`, `ies`,
  `ing`)일 때만 맞다. 복수형 키는 단수 어간도 맞다. `pockets`는 `POCKET`에, `stud`는 `STUDDED`에 맞지만 `STUDIO`에는
  맞지 않고, `puff`는 `PUFFER`에 맞지 않는다.
- **이웃한 두 토큰**은 이어 붙인 값이 키와 완전히 같을 때만 맞다 (`BOOTS CUT` → `부츠컷`). 한국어 키는 앞 토큰의
  끝 + 뒤 토큰 **전체**를 덮을 때도 맞다 (`라쿤퍼 트리밍` → `퍼트리밍`). 뒤 토큰의 앞부분만 걸치면 오탐으로 버린다
  (`빈티지 브라운`에서 `지브라`를 찾지 않는다).
- **`text_guard` 오탐 가드.** 한국어 키에만 쓴다. 토큰 안에 가드 단어가 있으면 그 키를 버린다. 예: `비즈니스` 안의
  `비즈`, `무지개` 안의 `무지`, `커스터드` 안의 `스터드`, `지오그래픽` 안의 `그래픽`.
- 정확 키를 근사 키보다 먼저, 긴 키를 먼저 검사하고 값마다 한 번만 낸다. 한 글자 키, `플레어`(디테일과 세이프에
  모두 있음), "없음"류 단어는 텍스트에서 찾지 않는다. `후드`·`집업`·`패딩`은 하분류가 이미 정해진 상품에서는
  의류 종류 이름이므로 디테일로 쓰지 않는다.

### 근사 매핑과 점수에서 빼는 이유

근사 매핑은 "같은 것"이 아니라 "가장 가까운 표준 값"이라는 뜻이다. 근사 값은 `approx` 섹션과 끝 기본색 규칙에서
나오고, 어디서나 표시된다. 관계 `HAS_COLOR`·`HAS_MATERIAL`·`HAS_PATTERN`·`HAS_DETAIL`에는 `approx: true`로,
스칼라 필드(`fit`, `length_body`, `length_sleeve`, `shape`)는 레코드의 `approx` 목록에 남는다. LLM 추출 Item은
그래프 로더가 `raw`를 사전으로 다시 판정해, 고른 값이 근사로만 나오면 `approx`를 표시한다 (정답 라벨은 라벨러의
표시를 그대로 쓴다).

점수는 근사 값을 **미확인**으로 다룬다. 분자와 분모에서 모두 빠지므로 일치로도 불일치로도 세지 않고, 그래프 후보
탐색도 근사 관계를 쓰지 않는다. 근사를 일치로 세면 추측(기모 → 플리스, 슬림 → 타이트, 롱스커트 → 맥시)이 정확한
일치처럼 보인다. 근사 매핑은 데이터에서 차지하는 비율이 작아 빼도 잃는 것이 적다. 값은 저장·표시되므로 평가 결과에
따라 나중에 켤 수 있다.

### 사전이 쓰이는 곳

- 상품 빌드: `src/fashion_planner_trend/ingestion/normalization/otpensource.py`, `musinsa_snap.py`, `set_split.py`.
- LLM 추출 후처리 (`src/fashion_planner_trend/ingestion/extraction/postprocess.py`): 정확·별칭 일치는 LLM 값을
  사전 값으로 바꾸고(`alias_override`), 값 없음은 값을 지우고(`alias_null`), 근사는 바꾸지 않고 표시만 한다
  (`approx`, `alias_disagree`). 속성 이동과 힌트도 표시만 한다 (`alias_route`, `alias_hint`).
- 그래프 적재 (`src/fashion_planner_trend/graph/loader.py`): LLM 추출 Item의 근사 표시.

## 사전을 안전하게 바꾸는 법

다른 서비스와 이미 저장된 데이터가 이 사전에 기대므로 변경은 최소로 한다. 사전으로 처리되지 않는 표현은 억지로
메우지 않고 한계로 기록한다.

규칙:

1. 사전 작업으로 표준 값 집합을 바꾸지 않는다. 표준 값의 추가·삭제·개명은 `vocabulary.py`, 이 문서, 그래프 초기
   노드, LLM 스키마를 함께 바꾸는 별도 결정이다.
2. 기존 항목을 바꾸거나 지우지 않는다. 항목의 종류도 바꾸지 않는다 (예: `슬림`을 `approx`에서 `aliases`로 올리지
   않는다).
3. 추가할 수 있는 것은 이미 있는 표준 값과 **뜻이 같은 표기 변형**(띄어쓰기, 붙여쓰기, 영문 표기, 흔한 약칭)뿐이고,
   `aliases`에만 넣는다.
4. 추가하는 표기는 지금 아무 항목에도 걸리지 않아야 한다:
   `AliasMap().normalize(attribute, text).matched_by == "none"`. 이미 걸리면 추가가 곧 기존 동작 변경이다.
5. 다른 속성의 표준 값과 같은 단어는 넣지 않는다. 예: `후드`는 디테일·넥라인의 표준 값이므로 카테고리 `후드티`의
   별칭으로 넣지 않는다.
6. 뜻이 같은지 애매하면 넣지 않는다. 근사 매핑은 이 절차로 추가하지 않고, 근거를 남긴 별도 결정으로만 추가한다.

절차:

1. 후보를 확인한다:
   `python -c "from fashion_planner_trend.ingestion.normalization.alias_map import AliasMap; print(AliasMap().normalize('fit', '<표기>'))"`
2. `configs/alias_map/<속성>.yaml`의 `aliases`에 추가하고 `tests/test_alias_map.py`에 테스트를 더한다.
3. 테스트를 돌린다: `pytest tests/test_alias_map.py tests/test_normalization_fixes.py tests/test_musinsa_snap.py tests/test_set_split.py tests/test_extraction.py`, 이어서 전체 `pytest`.
4. 상품을 다시 만든다 (처음 실행하면 Hugging Face에서 원천 데이터를 받는다): `python scripts/build_otpensource.py`,
   `python scripts/build_musinsa_snap.py`, `python scripts/build_musinsa_dataset.py`.
5. 그래프를 다시 적재한다: `python scripts/load_graph.py`. 상품의 관계는 적재할 때마다 지우고 다시 만든다. 빠진
   상품이 있으면 `--prune-products`, 처음부터 다시 만들려면 `--reset`을 쓴다. 이미 저장된 기사 추출 결과는 추출할
   때 정한 값을 그대로 가지므로, 값이 바뀌어야 하는 기사는 추출을 다시 한다.
6. 바꾼 표기·이유·날짜를 변경 이력에 남기고, 이 어휘를 쓰는 서비스(fashion-planner-inventory 등)의 관리자에게 알린다.

## 한계

- 21색은 거칠다. 톤과 색감이 사라진다 (원문 색 이름은 보존). 소재에는 혼방·가공·충전재가 없어 일부는 근사,
  상당수는 `null`이다.
- 가이드라인은 2020년의 여성복 기준이다. 런웨이·스트리트·신규 어휘(블레이저, 트렌치코트, 카고 등)는
  `category_raw`에만 남거나 `null`이 된다.
- 세이프는 저장만 하고 검색에 쓰지 않는다. 실루엣 필드는 아직 없다.
- 사전과 근사 매핑은 개발자가 만들었고 패션 전문가의 검수를 받지 않았다.
