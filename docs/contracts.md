# 계약

fashion-planner-trend는 Fashion Planner 오케스트레이터가 보내는 구조화된 검색 조건 **TrendQuery**를 받아 **TrendAnalysisResult**를 돌려준다. 결과에는
한국 패션 잡지 기사(Vogue Korea)에서 추출한 트렌드, 트렌드를 대표하는 의류(Item), 유사 상품, 그리고 각 내용의 기사 근거가 들어 있다. 이 문서는 HTTP 봉투(envelope)와 두
payload를 정의한다. 기준은 `src/fashion_planner_trend/schemas/`의 Pydantic 모델과 `src/fashion_planner_trend/api/server.py`다. 예시는
모두 `examples/stub/`과 `src/fashion_planner_trend/graph/fixture.py`의 합성 stub 데이터로 만들었고 실제 기사 문장은 없다.

- [HTTP API](#http-api)
- [TrendQuery](#trendquery)
- [TrendAnalysisResult](#trendanalysisresult)
- [근거 ID](#근거-id)
- [하류 서비스가 읽는 필드](#하류-서비스가-읽는-필드)
- [버전](#버전)

## HTTP API

서버는 표준 라이브러리 `ThreadingHTTPServer`다. 요청·헤더·본문은 로그에 남기지 않는다.

| 메서드 | 경로 | 인증 | 용도 |
|---|---|---|---|
| `GET` | `/health` | 없음 | 생존 확인. 항상 `200 {"status": "ok", "agent_id": "trend"}`를 돌려준다. |
| `POST` | `/v1/agents/trend/runs` | Bearer | 트렌드 검색 1회 실행 |

그 밖의 `GET`·`POST` 경로는 `404 not_found`다. 경로를 비교할 때 쿼리 문자열은 무시한다. 다른 HTTP 메서드에는 표준 라이브러리의 `501`(JSON 아님)이 나간다.

### 인증

`POST /v1/agents/trend/runs`에는 `Authorization: Bearer <token>` 헤더가 필요하다. 서버는 이 토큰을 `FASHION_PLANNER_SERVICE_TOKEN` 값과 상수 시간으로 비교한다. 스킴은 대소문자를
구분하고(`Bearer ` 뒤 공백 한 칸), 보낸 토큰의 앞뒤 공백은 무시한다. `FASHION_PLANNER_SERVICE_TOKEN`가 없거나 비어 있으면 **모든** `POST`가 `401`이다(fail closed). 인증에 실패한
요청은 본문을 읽기 전에 거부한다.

### 요청 봉투

| 필드 | 규칙 |
|---|---|
| `contract_version` | `trend-handoff`과 정확히 같아야 한다. |
| `request_id` | 공백만으로 된 값이 아닌 문자열, 최대 128자 |
| `input` | [TrendQuery](#trendquery) 객체. `input.request_id`는 봉투의 `request_id`와 같아야 한다. |

본문은 UTF-8로 인코딩한 JSON 객체이고, `Content-Length`가 1~1,000,000바이트(1 MB)여야 한다. `Content-Length` 없는 chunked 본문은 거부한다.
`Content-Type`은 검사하지 않지만 `application/json`을 보낸다. 모르는 봉투 필드는 무시한다. 전체 요청 예시는 [TrendQuery 예시](#예시)에 있다.

```bash
curl -s http://127.0.0.1:8768/v1/agents/trend/runs \
  -H "Authorization: Bearer $FASHION_PLANNER_SERVICE_TOKEN" -H "Content-Type: application/json" \
  --data-binary @examples/stub/trend-request.json
```

### 응답 봉투

검색이 끝나면 결과 상태와 관계없이 HTTP `200`이다. `no_results`는 오류가 아니라 정상 응답이다.

| 필드 | 값 |
|---|---|
| `contract_version` | `trend-handoff` |
| `request_id` | 요청의 `request_id` |
| `producer` | `{"agent_id": "trend", "schema_version": "1.0.0"}` |
| `status` | `result.status`와 같은 값: `ok`, `no_results`, `error` |
| `result` | [TrendAnalysisResult](#trendanalysisresult) 전체 |

응답은 공백 없는 UTF-8 JSON이고(비 ASCII 문자를 escape하지 않는다) 헤더는 `Content-Type: application/json; charset=utf-8`,
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`다.

### 오류

오류 응답도 같은 헤더를 쓰고 본문은 `{"error": {"code": "<code>", "message": "<text>"}}`다. `message`는 고정된 영어 문장이며 요청 값을 되풀이하지 않는다. 검사
순서는 경로 → 인증 → 본문 크기·JSON 파싱 → 봉투 → TrendQuery 검증 → 검색 → 결과 확인이다.

| HTTP | `code` | 조건 |
|---|---|---|
| 404 | `not_found` | 경로가 `/health`(GET)도 `/v1/agents/trend/runs`(POST)도 아니다. |
| 401 | `unauthorized` | 토큰이 없거나 형식이 틀리거나 값이 다르다. 또는 서버에 토큰이 설정되지 않았다. |
| 400 | `invalid_request` | `Content-Length`가 없거나 정수가 아니거나 `<= 0` 또는 `> 1000000`이다. 또는 본문이 JSON이지만 객체가 아니다. |
| 400 | `invalid_json` | 본문이 올바른 JSON이나 UTF-8이 아니다. |
| 400 | `unsupported_contract_version` | `contract_version`이 없거나 `trend-handoff`과 다르다. |
| 400 | `invalid_request_id` | `request_id`가 없거나 문자열이 아니거나 비었거나 128자를 넘는다. |
| 400 | `invalid_input` | `input`이 없거나 객체가 아니다. |
| 400 | `request_id_mismatch` | `input.request_id`가 `request_id`와 다르다. |
| 400 | `invalid_trend_query` | `input`이 TrendQuery 검증을 통과하지 못했다. 메시지에는 실패한 필드 경로만 나온다. 예: `input failed TrendQuery validation: requested.attributes.0, trend_terms.season.season` |
| 503 | `search_unavailable` | 검색 중 예외가 났다(예: Neo4j 연결 불가). 자세한 내용은 내보내지 않는다. |
| 500 | `invalid_search_result` | 검색 결과의 `request_id`나 `schema_version`이 맞지 않거나, `status`가 지원하지 않는 값이다. |

### 바인드 모드와 설정

서버는 패키지를 설치한 뒤(`pip install -e .`)
`python -m fashion_planner_trend.api.server [--mode local|service] [--host H] [--port P]`로 시작한다. 각 설정은 **CLI 옵션 > 프로세스
환경변수 > `.env` 파일 > 기본값** 순으로 정한다. 빈 값은 기본값을 쓴다는 뜻이다. 단, 명시적으로 비운 프로세스 환경변수도 `.env` 값을 덮어쓴다(토큰을 회수할 때 쓸 수 있다).

| 설정 | 옵션 | 환경변수 | 기본값 | 규칙 |
|---|---|---|---|---|
| 모드 | `--mode` | `TREND_API_MODE` | `local` | `local` 또는 `service` |
| 호스트 | `--host` | `TREND_API_HOST` | `127.0.0.1` | `local`: `127.0.0.1`, `::1`, `localhost` 중 하나. `service`: 정확히 `0.0.0.0` |
| 포트 | `--port` | `TREND_API_PORT` | `8768` | 1~65535 정수 |
| 토큰 | — | `FASHION_PLANNER_SERVICE_TOKEN` | 없음 | `service` 모드로 시작하려면 필수(공백만으로 된 값은 안 된다). 없으면 모든 `POST`가 401 |

- **local 모드**는 loopback에만 바인드하며 개발용이다. 토큰 없이도 시작하지만 그때는 `/health`만 쓸 수 있다.
- **service 모드**는 모든 IPv4 인터페이스에 바인드하고, `FASHION_PLANNER_SERVICE_TOKEN`가 없으면 시작하지 않는다. 컨테이너 안이나 직접 관리하는 네트워크 경계 뒤에서 쓴다.
- 설정이 잘못되면 어디에도 접속하기 전에 종료한다. 설정이 맞으면 Neo4j 연결을 확인하고, 연결할 수 없으면 종료한다.

## TrendQuery

`input`은 `TrendQuery`(`src/fashion_planner_trend/schemas/trend_query.py`)로 검증한다. 기획자의 질문을 이 구조로 바꾸는 것은 호출 측(보통
오케스트레이터)이고, 트렌드 서비스는 자연어를 해석하지 않는다. 모든 수준에서 모르는 필드는 무시한다.

### 모델

**TrendQuery**

| 필드 | 타입 | 기본값 | 의미 |
|---|---|---|---|
| `request_id` | string | 필수 | 봉투의 `request_id`와 같아야 한다. |
| `original_query` | string | 필수 | 기획자 질문 원문. 검색에는 쓰지 않는다. |
| `trend_terms` | TrendTerms | 필수 | 시즌과 트렌드명. Style을 고른다. |
| `requested` | Requested | 빈 값 | 카테고리·성별·속성 조건. Item과 상품을 거른다. |
| `unsupported` | UnsupportedCondition[] | `[]` | 다룰 수 없는 조건(브랜드, 가격, 신발 등). 기록용이다. |
| `missing_fields` | string[] | `[]` | 질문에 없던 검색 필드 이름. 기록용이다. |

**TrendTerms** — `season`(SeasonRequest, 필수), `style_names`(TermRef[], 기본 `[]`): 찾을 트렌드명, `item_terms`(TermRef[], 기본
`[]`): 질문에 나온 특정 상품. `item_terms`는 기록용이다.

**SeasonRequest** — `year`(int, 필수), `season`(`"SS"` | `"FW"`, 필수), `raw`(string | null, 기본 `null`): 사용자 표현,
`origin`(`"user_requested"` | `"default_next"`, 필수). 시즌은 항상 필수다. 사용자가 시즌을 말하지 않으면 호출 측이 다음 패션 시즌을
`origin: "default_next"`로 채운다(헬퍼 `SeasonRequest.default_next(today)`). 3~8월 → 같은 해 `FW`, 9~12월 → 다음 해 `SS`, 1~2월 → 같은 해 `SS`.

**TermRef** — `raw`(string, 필수): 원문 표현, `value`(string | null, 기본 `null`): 정규화 값 자리이며 지금은 쓰지 않는다.

**Requested** — `categories`(CategoryFilter, 기본 빈 값), `gender`(GenderRef | null, 기본 `null`),
`attributes`(RequestedAttribute[], 기본 `[]`).

**CategoryFilter / CategoryRef** — `include`와 `exclude`는 CategoryRef 목록이고 기본값은 `[]`다. `include`가 비면 상분류 4개 전체다.
CategoryRef는 `raw`(string, 필수), `major`(CategoryMajor | null), `minor`(CategoryMinor | null)를 가진다. `minor`가 있으면
`major`는 `minor`에서 정해진다(보낸 `major`는 덮어쓴다). 둘 다 없는 ref(예: 신발·가방)는 아무것도 고르지 않는다.

**RequestedAttribute** — `type`(`"color"` | `"material"` | `"pattern"` | `"detail"` | `"fit"`, 필수), `raw`(string, 필수):
사용자 표현(예: `핑크색`), `value`(string | null): 표준 값. `type`의 어휘에 없는 값이면 검증에 실패한다. `detail`은 디테일·넥라인·칼라 이름을 모두 받는다. `null`은
표준 값으로 바꾸지 못했다는 뜻이며, 이 속성은 필터에 쓰지 않고 `limitations`에 알린다.

**GenderRef** — `raw`(string | null), `value`(Gender | null), 둘 다 기본 `null`. **UnsupportedCondition** — `type`(예: `brand`,
`category`), `raw`, `reason`, 모두 필수 string.

### 표준 어휘

표준 값은 한국어 문자열이고 JSON에 아래 그대로 들어간다(`src/fashion_planner_trend/schemas/vocabulary.py`).

| Enum | 값 |
|---|---|
| Season | `SS`(봄·여름), `FW`(가을·겨울) |
| Gender | `남`, `여`, `공용` |
| CategoryMajor | `상의`, `하의`, `아우터`, `원피스` |
| CategoryMinor | `상의`: `탑`, `블라우스`, `티셔츠`, `니트웨어`, `셔츠`, `브라탑`, `후드티` / `하의`: `청바지`, `팬츠`, `스커트`, `레깅스`, `조거팬츠` / `아우터`: `코트`, `재킷`, `점퍼`, `패딩`, `베스트`, `가디건`, `짚업` / `원피스`: `드레스`, `점프수트` |
| Color (21) | `블랙`, `화이트`, `그레이`, `레드`, `핑크`, `오렌지`, `베이지`, `브라운`, `옐로우`, `그린`, `카키`, `민트`, `블루`, `네이비`, `스카이블루`, `퍼플`, `라벤더`, `와인`, `네온`, `골드`, `실버` |
| Fit (7) | `노멀`, `루즈`, `오버사이즈`, `타이트`, `스키니`, `와이드`, `벨보텀` |
| 그 밖 | Material(26), Pattern(19), 디테일 이름(디테일 40·넥라인 12·칼라 10), BodyLength(9), SleeveLength(5), Shape(10). `vocabulary.py` 참고 |

### 검색이 질의를 쓰는 방식

- **시즌** — 요청 시즌의 `"<year><season>"`(예: `2027SS`)이 Style의 `seasons`에 있으면 그 Style이 대상이다. 시즌이 확인되지 않은 Style은 보류하고
  `limitations`에 개수를 알린다.
- **트렌드명** — `style_names[].raw`와 Style 이름에서 공백을 지우고 소문자로 바꾼 뒤, 한쪽이 다른 쪽을 포함하면 일치다. 일치하는 Style이 없으면 `no_results`다.
- **카테고리** — `minor`가 있는 ref는 그 하분류와, `major`만 있는 ref는 상분류 전체와 일치한다. `exclude`는 `include` 다음에 적용한다. `include`가 비어 있지
  않은데 어떤 ref에도 `major`·`minor`가 없으면 전체로 넓히지 않고 바로 `no_results`를 돌려준다.
- **속성** — 같은 `type`의 값끼리는 OR, 서로 다른 `type`끼리는 AND다. Item은 요청과 *명시적으로* 다른 값이 있을 때만 빠진다(값이 없으면 남는다). 상품은 요청한 모든
  `type`에서 일치하는 값이 있어야 하며, 값이 없거나 다른 상품은 제외하고 `limitations`에 건수를 알린다.
- **성별** — `applied_filters.gender`에 담지만 필터로 쓰지 않고, 그 사실을 `limitations`에 알린다.
- `original_query`, `item_terms`, `TermRef.value`, `unsupported`, `missing_fields`는 결과에 영향을 주지 않는다.

### 예시

`examples/stub/trend-request.json`의 요청이다.

```json
{
  "contract_version": "trend-handoff",
  "request_id": "stub-trend-001",
  "input": {
    "request_id": "stub-trend-001",
    "original_query": "2027년 봄 발레코어에서 유행하는 핑크색 상의를 기획하고 싶어",
    "trend_terms": {
      "season": {"year": 2027, "season": "SS", "raw": "2027년 봄", "origin": "user_requested"},
      "style_names": [{"raw": "발레코어", "value": null}], "item_terms": []
    },
    "requested": {
      "categories": {"include": [{"raw": "상의", "major": "상의", "minor": null}], "exclude": []},
      "gender": null, "attributes": [{"type": "color", "raw": "핑크색", "value": "핑크"}]
    },
    "unsupported": [], "missing_fields": []
  }
}
```

## TrendAnalysisResult

`result`는 `TrendAnalysisResult`(`src/fashion_planner_trend/schemas/trend_analysis_result.py`)다. 구조는 **Style → Item →
유사 상품**이고, 근거 레코드는 별도의 평평한 목록으로 둔다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `request_id` | string | `TrendQuery.request_id` |
| `status` | `"ok"` \| `"no_results"` \| `"error"` | 아래 참고 |
| `applied_filters` | AppliedFilters \| null | 실제로 적용한 조건. 검색은 항상 채운다. |
| `trends` | Trend[] | Style id 순. `status`가 `ok`일 때만 비어 있지 않다. |
| `evidence` | EvidenceRecord[] | 반환한 Style·Item의 근거 레코드 전체(중복 제거) |
| `limitations` | string[] | 가정하거나 생략·제외한 내용을 알리는 문장 |
| `schema_version` | string | `1.0.0` |

### `status`

| 값 | 조건 |
|---|---|
| `ok` | 시즌·이름 조건을 통과한 Style이 하나 이상 있고, 그중 카테고리·속성 필터를 통과한 Item이 하나 이상 있다. Item의 `similar_products`는 비어 있을 수 있다. |
| `no_results` | 검색이 중간에 끝났다. 요청 카테고리가 모두 지원 밖이거나, 이름이 일치하는 Style이 없거나, 시즌에 맞는 Style이 없거나, 필터를 통과한 Item이 없는 경우다. `trends`·`evidence`는 비어 있고 사유는 `limitations`에 있다. |
| `error` | 스키마가 허용하고 API도 HTTP 200으로 그대로 전달하지만, 지금의 검색 코드는 이 값을 만들지 않는다. 검색 실패는 HTTP 503 `search_unavailable`로 나간다. |

### `limitations`

문장은 `graph/search.py`가 한국어로 만든다. 표시용 텍스트로 다루고 코드로 파싱하지 않는다. 주요 문장은 다음과 같다(`{…}`에는 값이 채워진다). "예"는 그 문장을 남기고 검색이 바로 `no_results`로 끝난다는 뜻이다.

| 문장 | 바로 `no_results` |
|---|---|
| `시즌 미지정 → {year} {season}로 해석했습니다.` (`origin`이 `default_next`일 때) | 아니오 |
| `성별 필터는 정책이 정해지지 않아 적용하지 않았습니다.` | 아니오 |
| `표준 값으로 바꾸지 못해 필터에 쓰지 않은 속성 요청: {raw, …}` | 아니오 |
| `요청한 카테고리가 모두 지원 범위(상의·하의·아우터·원피스) 밖입니다.` | 예 |
| `요청한 트렌드명({이름, …})과 일치하는 트렌드가 그래프에 없습니다.` | 예 |
| `시즌이 확인되지 않은 트렌드 {N}개는 시즌 조건 검색에서 보류했습니다.` (트렌드명을 요청했으면 이름도 붙는다) | 아니오 |
| `이름은 일치하지만 {key} 시즌이 아닌 트렌드 {N}개를 제외했습니다: {이름(시즌), …}` | 아니오 |
| `트렌드는 있지만 연결된 옷(Item)이 없어 상품 검색에서 뺀 트렌드 {N}개: {이름, …}` | 아니오 |
| `{key}에 유행한 트렌드가 그래프에 없습니다.` | 예 |
| `요청 속성과 명시적으로 달라 제외한 아이템 {N}개.` | 아니오 |
| `요청 속성을 충족한다는 근거가 없어 제외한 상품: 값 없음 {N}건, 다른 값 {M}건.` | 아니오 |
| `속성이 없는 아이템은 하분류가 같다는 것만으로 후보를 골랐습니다 (순위 근거가 약함): {Item 이름, …}` | 아니오 |
| `musinsa_snap 상품은 착장 사진 속 아이템이라 구매 가능한 상품이 아니며 색 정보가 없습니다 (kind=snap_garment).` | 아니오 |
| `조건을 통과한 트렌드·아이템이 없습니다.` | 예 |


### AppliedFilters

| 필드 | 타입 | 의미 |
|---|---|---|
| `season` | SeasonRequest | 받은 `trend_terms.season` 그대로 |
| `categories` | CategoryFilter | `requested.categories`(`minor`에서 정한 `major` 포함) |
| `gender` | Gender \| null | `requested.gender.value`. 담기만 하고 성별로 거르지 않는다. |
| `attributes` | `{type, value}[]` | 표준 값이 있는 요청 속성, 즉 실제로 필터에 쓴 속성 |

### Trend와 TrendStyle

`Trend`는 `{style: TrendStyle, items: TrendItem[]}`다. `items`에는 필터를 통과한 Item만 Item id 순으로 들어가며, 비어 있는 경우는 없다.

| TrendStyle 필드 | 타입 | 의미 |
|---|---|---|
| `id` | string | Style id, `<article_id>-s<n>`(예: `900003-s0`) |
| `name` | string | 원문 기사에 나온 트렌드명(대개 한국어) |
| `name_origin` | `"article"` \| `"analyst"` | 기사가 붙인 이름 / 사람 라벨러가 붙인 이름 |
| `seasons` | string[] | 기사가 이 트렌드가 유행한다고 말한 시즌. 예: `["2026FW", "2027SS"]` |
| `definition` | string | 한 줄 정의 |
| `description` | `{text, evidence_ids}[]` | 기획 참고 설명과 그 근거 id |
| `focus_core`, `focus_supporting` | string[] | 트렌드의 핵심·보조 속성(`category`, `color`, `material`, `pattern`, `detail`, `fit`). 점수 가중치를 높인다. |
| `focus_evidence_ids` | string[] | 핵심·보조 속성의 근거 |
| `aliases` | string[] | 기사 안에 나온 다른 이름(원문 그대로) |
| `article_id` | string \| null | 원문 기사 id |
| `example_images` | `{url, basis}[]` | 기사 사진 URL 최대 5장. `basis`는 `item_link`(Style의 Item에 연결된 사진), `section`(이 Style만 쓰는 기사 섹션의 사진), `article`(Style이 하나뿐인 기사의 사진) 중 하나 |

### 의류 속성 (TrendItem·ProductMatch 공통)

| 필드 | 타입 | 의미 |
|---|---|---|
| `category_major` | CategoryMajor \| null | 상분류 |
| `category_minor` | CategoryMinor \| null | 하분류 |
| `colors` | Color[] | 표준 색 |
| `materials`, `patterns` | string[] | 표준 소재·패턴 값 |
| `details` | `{type, name, raw, motif, placement}[]` | `type`은 `detail`·`neckline`·`collar`, `name`은 표준 값이다. `raw`, `motif`(모티프), `placement`(적용 위치)는 원문에서 온 선택 값이다. |
| `fit` | Fit \| null | 표준 핏 |
| `shape`, `length_body`, `length_sleeve` | enum \| null | 표시만 한다. 필터·점수에 쓰지 않는다. |
| `length_body_raw`, `length_sleeve_raw` | string \| null | 길이 원문 표현 |
| `approx` | string[] | 근사 매핑으로만 얻은 값. `color:<v>`, `material:<v>`, `pattern:<v>`, `detail:<type>:<name>` 또는 `fit` 같은 스칼라 필드 이름이다. 위 목록에도 들어 있지만 필터·점수에는 쓰지 않는다. |

**TrendItem**에는 다음 필드가 더 있다: `id`(`<style id>-i<n>`), `name`, `styling_raw`·`styling_accessory`(착용 방식과 함께 쓴 액세서리,
string | null), `gender`(Gender | null), `request_match`(MatchResult), `image_urls`(이 Item에 연결된 기사 사진 URL),
`evidence_ids`, `similar_products`(ProductMatch 최대 5개, 좋은 순). 기사 사진 URL(`image_urls`, `example_images`)은 **링크로만** 보여
준다. 임베드·핫링크·다운로드하지 않는다.

**ProductMatch**에는 다음 필드가 더 있다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `product_id` | string | 출처 접두사가 붙은 상품 id(예: `otpensource:p1`). 세트에서 나눈 구성품은 `<원 id>#<k>`다. |
| `name` | string \| null | 상품명 |
| `brand` | `"Musinsa"` | 고정값. 상품은 모두 무신사 데이터셋에서 온다. |
| `image_url` | string \| null | 카탈로그 이미지 URL |
| `source` | `"otpensource"` \| `"musinsa_snap"` | 출처 데이터셋 |
| `kind` | `"catalog_item"` \| `"snap_garment"` | 판매 상품 / 스냅 사진 속 착용 의류(구매 가능한 상품이 아니다) |
| `sub_colors` | Color[] | 서브 컬러. 표시만 한다. |
| `match` | MatchResult | Item과의 비교 결과 |
| `score`, `score_breakdown` | float, ScoreBreakdown | [score_breakdown](#score_breakdown) 참고. `score`는 소수 넷째 자리로 반올림한다. |
| `gender` | Gender \| null | 데이터셋에 있는 경우의 상품 성별 |
| `snap_id` | int \| null | 스냅 사진 id. 같은 사진의 의류는 같은 값이다. |
| `image_ref` | string \| null | 이미지 참조 단일 필드. 카탈로그 상품은 이미지 URL, 스냅 의류는 데이터셋 행 참조 `hf://datasets/<dataset>@<revision>#row=<row>`다. |
| `image_scope` | `"catalog"` \| `"full_snap"` \| `"full_set"` \| null | 상품 사진 / 자르지 않은 착장 사진 전체(그 사진의 의류가 함께 쓴다) / 세트 원 상품의 사진 |
| `image_status` | `"ok"` \| `"broken"` \| `"unchecked"` \| null | 이미지 점검 결과. 이미지 참조가 없으면 `null`이다. `ok`는 받을 수 있다는 뜻이지 사진이 그 의류를 보여 준다는 뜻은 아니다. |
| `parent_id` | string \| null | 세트에서 나눈 구성품의 원 상품 id |
| `multi_wearer_suspected` | bool \| null | 스냅 사진에 여러 명이 찍혔을 수 있으면 `true`, 그 밖에는 `null`(한 명이라는 증명이 아니다) |
| `has_image` | bool | `image_status`가 있으면 그 값이 `ok`인지, 없으면 `image_url`이나 `image_ref`가 있는지 |

### MatchResult

`{matched, mismatched, unknown}`이고 모두 string 목록이다. 한쪽에 값이 없는 속성은 **불일치가 아니라 미확인**(`unknown`)이다.

- `ProductMatch.match`(상품 vs Item): `matched`는 둘 다 가진 값, `mismatched`는 Item에는 있고 상품에는 없는 값이며 `<attr>:<value>` 형식이다.
  디테일은 자체 라벨 `<type>:<name>`을 쓴다(예: `neckline:<name>`). `unknown`은 Item에는 값이 있지만 상품에는 값이 없는 속성 이름이다.
- `TrendItem.request_match`(Item vs 요청): `matched`는 Item이 가진 요청 값(`<type>:<value>`), `unknown`은 Item에 값이 없는 요청 속성
  종류다. 요청과 명시적으로 다른 Item은 빠지므로 응답의 `mismatched`는 항상 비어 있다.

### score_breakdown

후보 상품은 Item과 상분류가 같고, 하분류·핏·색·소재·패턴·디테일 값 중 하나 이상을 공유한다. 점수 속성은 color, material, pattern, detail, fit이고, 속성 `a`는 Item과
상품이 모두 근사가 아닌 값을 가질 때만 *비교*한다.

```
weight_a   = base_weight_a × 배수                  (core 2.0, supporting 1.5, 그 밖 1.0)
ratio_a    = Σ 공통 값의 w(v) ÷ Σ Item 값의 w(v)    (값 가중치가 없으면 |공통 값| ÷ |Item 값|)
base_score = Σ weight_a × ratio_a ÷ Σ weight_a                         (비교한 속성이 없으면 0)
score      = base_score + Σ bonus
```

| 필드 | 의미 |
|---|---|
| `attributes` | 비교한 속성마다 `weight`(적용 가중치), `base_weight`(`configs/weights.yaml`의 기본 가중치: color 3, detail 3, pattern 2, material 2, fit 2), `level`(Style의 focus에서 온 `core`·`supporting`·`null`), `ratio`(0~1) |
| `base_score` | 가중 일치 비율, 0~1 |
| `bonus` | 하분류가 같으면 `{"category_minor": α × 배수}`, 다르면 키가 없다. α는 0.1이고, 배수는 Style의 focus에 `category`가 있을 때만 붙는다. |
| `unknown_core_weight` | 항상 `0.0`. 상품에 없는 속성은 분모에서 빠질 뿐 감점하지 않는다. 계약 필드라 남겨 둔다. |
| `value_weighted` | 값별 가중치 `w(v)`를 썼는가. API는 값 가중치를 넘기지 않으므로 `false`다. |

점수가 0인 상품은 내지 않는다. 나머지는 점수 → 비교한 속성 수 → 일치한 값 수 → `product_id` 순으로 정렬한다. Item마다 상위 5개를 돌려준다.

### EvidenceRecord

| 필드 | 타입 | 의미 |
|---|---|---|
| `evidence_id` | string | [근거 ID](#근거-id) 참고 |
| `article_id` | string | 원문 기사 id |
| `title`, `url`, `published_at` | string \| null | 기사 제목, URL, 게시일(예: `2026-10-01`) |
| `block_idx` | int | 크롤한 기사의 텍스트 블록 번호 |
| `quote` | string | 저장된 그대로의 근거 인용 |

`evidence`에는 Style 전체를 뒷받침하면서 어떤 id 목록에서도 가리키지 않는 레코드가 있을 수 있다.

### 예시

stub 요청을 합성 stub 그래프에 보낸 응답을 줄인 것이다. `definition`, `description`, `score`, 빈 값과 `null` 필드, 근거 레코드 4개 중 3개를 뺐다.
limitations 문장의 "다른 값 1건"은 요청 색과 다른 화이트 블라우스다. 상품의 `score`는 `base_score` + `bonus.category_minor` = 1.0 + 0.1이다.

```json
{
  "contract_version": "trend-handoff", "request_id": "stub-trend-001",
  "producer": {"agent_id": "trend", "schema_version": "1.0.0"}, "status": "ok",
  "result": {
    "request_id": "stub-trend-001", "status": "ok",
    "applied_filters": {
      "season": {"year": 2027, "season": "SS", "raw": "2027년 봄", "origin": "user_requested"},
      "categories": {"include": [{"raw": "상의", "major": "상의", "minor": null}], "exclude": []},
      "gender": null, "attributes": [{"type": "color", "value": "핑크"}]
    },
    "trends": [{
      "style": {
        "id": "900003-s0", "name": "발레코어", "name_origin": "article", "seasons": ["2027SS"],
        "focus_core": ["color"], "focus_evidence_ids": ["trend:900003:2:20a25667"], "article_id": "900003",
        "example_images": [{"url": "https://img.example.invalid/0.jpg", "basis": "item_link"},
                           {"url": "https://img.example.invalid/1.jpg", "basis": "article"}]
      },
      "items": [{
        "id": "900003-s0-i0", "name": "핑크 블라우스",
        "category_major": "상의", "category_minor": "블라우스", "colors": ["핑크"], "materials": ["시폰"],
        "request_match": {"matched": ["color:핑크"], "mismatched": [], "unknown": []},
        "image_urls": ["https://img.example.invalid/0.jpg"], "evidence_ids": ["trend:900003:3:d7db6a25"],
        "similar_products": [{
          "product_id": "otpensource:p1", "name": "otpensource:p1", "brand": "Musinsa",
          "source": "otpensource", "kind": "catalog_item", "image_url": "https://x/p1.jpg",
          "category_major": "상의", "category_minor": "블라우스", "colors": ["핑크"], "materials": ["시폰"],
          "match": {"matched": ["color:핑크", "material:시폰"], "mismatched": [], "unknown": []},
          "score_breakdown": {
            "attributes": {"color": {"weight": 6.0, "base_weight": 3.0, "level": "core", "ratio": 1.0},
                           "material": {"weight": 2.0, "base_weight": 2.0, "level": null, "ratio": 1.0}},
            "base_score": 1.0, "bonus": {"category_minor": 0.1}, "unknown_core_weight": 0.0, "value_weighted": false
          },
          "image_status": "ok", "has_image": true
        }]
      }]
    }],
    "evidence": [{"evidence_id": "trend:900003:3:d7db6a25", "article_id": "900003",
                  "title": "합성 기사 (발레코어)", "url": "https://example.invalid/900003",
                  "published_at": "2026-10-01", "block_idx": 3, "quote": "블라우스 인용"}],
    "limitations": ["요청 속성을 충족한다는 근거가 없어 제외한 상품: 값 없음 0건, 다른 값 1건."],
    "schema_version": "1.0.0"
  }
}
```

## 근거 ID

근거 레코드는 모두 `trend_analysis_result.py`의 `evidence_id()`가 만든 안정 id를 가진다.

```
trend:<article_id>:<block_idx>:<hash8>
hash8 = sha256(quote를 UTF-8로 인코딩한 바이트)의 16진수 앞 8자(소문자)
```

```python
import hashlib

def evidence_id(article_id: str, block_idx: int, quote: str) -> str:
    return f"trend:{article_id}:{block_idx}:{hashlib.sha256(quote.encode('utf-8')).hexdigest()[:8]}"

assert evidence_id("900003", 3, "블라우스 인용") == "trend:900003:3:d7db6a25"  # 합성 인용
```

- 인용은 저장된 문자열 그대로 해시한다. 앞뒤 공백 제거, 공백 정리, 유니코드 정규화를 하지 않는다. 같은 (기사, 블록, 인용)은 실행·질의가 달라도 같은 id이므로 여러 응답 사이에서 캐시하거나 조인할
  수 있다. id는 `^trend:[^:]+:\d+:[0-9a-f]{8}$` 형식이다.
- id는 `style.description[].evidence_ids`, `style.focus_evidence_ids`, `items[].evidence_ids`에서 가리키며, 각 id는
  `result.evidence`의 항목 하나에 대응한다.
- 하류 서비스는 id를 그대로 믿지 않고 `article_id`, `block_idx`, `quote`로 **다시 계산**한다. 근거를 저장하거나 전달하는 쪽은 `quote`를 바이트 단위로 바꾸지 않아야
  한다. 바뀌면 id 검증이 실패한다.

## 하류 서비스가 읽는 필드

오케스트레이터는 트렌드 결과를 바꾸지 않고 하류 서비스에 넘긴다. 하류 서비스는 먼저 `status`를 확인하고, 모르는 필드는 무시하며, 어휘 값은 한국어 문자열 그대로 비교하고, `limitations`는
표시용 텍스트로 다룬다. inventory 서비스는 다음 필드를 읽는다.

| 필드 | 타입 | 용도 |
|---|---|---|
| `applied_filters.season` | SeasonRequest | 대상 시즌(`year`, `season`) |
| `applied_filters.categories` | CategoryFilter | 요청한 상분류·하분류(`include` / `exclude`) |
| `applied_filters.attributes[]` 중 `type: "color"` | `{type, value}` | 요청한 표준 색 |
| `applied_filters.gender` | Gender \| null | 요청한 성별(트렌드 검색에서는 적용하지 않았다) |
| `trends[].items[].category_major`, `.category_minor` | CategoryMajor, CategoryMinor \| null | 트렌드 의류의 카테고리 |
| `trends[].items[].colors`, `.materials`, `.details` | 목록 | 의류 속성(값을 믿기 전에 `approx`를 확인한다) |
| `trends[].items[].gender` | Gender \| null | 알려진 경우의 의류 성별 |
| `trends[].items[].similar_products[].kind` | `catalog_item` \| `snap_garment` | `catalog_item`만 판매 상품이다. |
| `trends[].items[].similar_products[].score` | float | 트렌드 의류와의 유사도. 같은 Item 목록 안에서 비교한다. |
| `trends[].items[].similar_products[].image_url`, `.image_ref` | string \| null | 상품 이미지(`image_ref`는 스냅 사진까지 포함한다) |
| `trends[].items[].similar_products[].multi_wearer_suspected` | bool \| null | `true`면 스냅 사진에 다른 사람의 옷이 함께 보일 수 있다. |

## 버전

`contract_version`(`trend-handoff`)은 HTTP 봉투의 종류를 가리키는 이름이고 버전 숫자를 붙이지 않는다. 다른 값을 보낸 요청은
`400 unsupported_contract_version`으로 거부한다. `schema_version`(`1.0.0`)은 `producer.schema_version`과
`result.schema_version`에 함께 실리고 TrendAnalysisResult 스키마를 나타낸다. 서버는 버전이 다른 결과를 내보내지 않는다
(`500 invalid_search_result`). 기본값이 있는 선택 필드는 `schema_version`을 바꾸지 않고 추가할 수 있으므로 소비자는 모르는 필드를
무시해야 한다. 필드를 지우거나 이름·타입·의미를 바꾸거나 `status` 값을 추가하면 새 버전이 필요하다.
