# 기사 추출

추출 단계는 크롤링한 패션 매거진 기사(Vogue Korea, 한국어)를 구조화된 트렌드 **Style → Item → 속성 관계**로 바꾼다. 모든 주장에는 기사에서 글자 그대로 옮긴 인용이 근거로 붙는다. 기사 1건마다 strict JSON 스키마를 지정해 OpenAI API를 한 번 호출한다. 그 출력을 코드가 후처리하고 검증한다. 검증 오류가 있으면 오류 목록을 프롬프트에 붙여 한 번 더 호출한다. 검증을 통과한 결과는 [graph-schema.md](graph-schema.md)대로 Neo4j에 적재한다.

> **개인정보·데이터 반출.** 추출을 실행하면 기사 원문이 OpenAI로 전송된다. 어떤 기사든 실행하기 전에 [개인정보·데이터](#개인정보데이터)를 먼저 읽는다.

코드 위치는 다음과 같다. 추출 단계는 `src/fashion_planner_trend/ingestion/extraction/`(`prompt.py`, `llm_schema.py`, `extractor.py`, `postprocess.py`, `validate.py`, `batch.py`), LLM 클라이언트는 `src/fashion_planner_trend/llm/`, 출력 모델은 `src/fashion_planner_trend/schemas/article_extraction.py`, 실행 스크립트는 `scripts/extract_articles.py`다.

```text
크롤 JSONL ──► 프롬프트 렌더링 ──► OpenAI (strict JSON 스키마) ──► 후처리 ──► pydantic + V1–V6
                                         ▲                                              │
                                         └──── 오류 목록을 붙여 1회 재시도 ◄── 오류 ──┤
                                                                                        ├─ 통과 ─► <article_id>.json
                                                                                        └─ 실패 ─► _review/ + review_queue.jsonl
```

## 입력

입력은 크롤러가 만든 JSONL이고 한 줄이 기사 하나다. 추출은 다음 필드를 쓴다.

| 필드 | 쓰임 |
|---|---|
| `article_id`, `title`, `published_at`, `tags` | 프롬프트 머리말. `published_at`은 앞 10자만 쓴다 |
| `season`, `season_candidates` | 프롬프트 머리말. 기사의 시즌 태그와, 크롤러가 찾은 시즌 표현(출처·횟수 포함)이다. 힌트일 뿐이다 |
| `blocks[]` | 읽는 순서대로 놓인 본문. 블록마다 `idx`(블록 번호), `type`(`paragraph`, `heading`, `image`), `text`가 있다 |
| `type`이 `image`인 `blocks[]` | `image_idx`, `run_idx`(이어진 사진 묶음), `layout`, `caption`, `caption_parsed`(`type`, `brand`, `season`) |
| `images[]` | `image_idx`, `block_idx`, `url`, `section_idx`. 코드만 쓰고 모델에는 보내지 않는다 |

**모델이 보는 것은 텍스트 블록과 이미지 캡션뿐이다.** 블록 하나가 `[<idx>] <종류>: <내용>` 한 줄이 된다. 이미지 줄에는 캡션과 구조화된 브랜드·시즌이 들어가고, 모델은 이 블록 번호로 사진과 Item을 잇는다. **이미지 픽셀과 이미지 URL은 보내지 않는다.** 기사 URL, 작성자, `images[]`도 보내지 않는다.

## 프롬프트

프롬프트 템플릿은 `prompts/article_extraction.md`다. 프롬프트 id는 `article-extraction`이고, 결과마다 id와 `prompt_sha256`을 함께 기록한다. 맨 앞의 YAML front matter는 보내기 전에 떼어 낸다. front matter에 `season_candidates: true`가 있으면 크롤러의 시즌 후보를 기사 머리말에 넣는다. 코드는 템플릿 뒤에 기사 머리말, 번호가 붙은 블록, "JSON 객체 하나만 출력하라"는 마지막 지시를 붙인다. 프롬프트가 모델에게 요구하는 내용을 요약하면 다음과 같다.

- **기본 규칙.** 도구를 쓰지 않는다. 스키마에 맞는 JSON 객체 하나만 출력한다. 점수·가중치를 만들지 않는다. 모르는 것은 `null` 또는 `[]`로 둔다. 자리표시자 문구를 쓰지 않는다.
- **Style.** 기사가 지금 또는 다음 시즌의 유행으로 소개한 트렌드만 넣는다. 이름 붙은 트렌드와, 기사가 시즌 트렌드라고 선언한 컬러·소재·아이템·핏·패턴 트렌드가 여기에 해당한다. 코디·조합 팁, 한 브랜드의 컬렉션 콘셉트, 쇼 총평, 이름 없는 팁, 끝났다고 말한 트렌드, 지나가는 언급은 빼고, 이름도 새로 짓지 않는다. `name`은 기사 표현 그대로 쓰되 소개하는 말·감싼 따옴표·덧붙인 "트렌드"는 뺀다. `name_origin`은 항상 `article`이다.
- **Style 필드.** `definition`은 한 문장이다. `description`은 항목 하나에 사실 하나이고 항목마다 근거를 붙인다. `seasons`에는 기사가 그 트렌드에 결부한 시즌만 넣는다. 시즌 후보에는 과거 컬렉션이 섞여 있을 수 있고, 발행일로 시즌을 추정하지 않는다. Style 근거는 이름, 정의의 각 주장, 각 시즌을 모두 받쳐야 한다.
- **Item.** 기사가 트렌드의 예로 든 독립된 옷 한 벌이다. 남이 입었다고 나열만 한 옷, 같이 입으라고 권한 옷, 역사적 배경, 일반 범주, 착용 법칙(`description`으로 보낸다), 트렌드와 대비시킨 옷은 넣지 않는다. 확실하지 않으면 넣지 않는다. 신발·가방·모자 같은 지원 범위 밖 아이템도 기준에 맞으면 내되, `category_raw`만 채우고 두 카테고리 값은 `null`로 둔다.
- **속성.** 모든 `raw`는 그 Item의 근거 블록에서 글자 그대로 복사한다. 표준 값은 스키마 enum에서만 고른다. 언급이 없는 패턴은 `[]`이지 `무지`가 아니다. 성질만 말한 소재는 `raw`만 남기고 `value: null`로 둔다.
- **근거.** `quote`는 인용한 텍스트 블록의 연속된 부분 문자열이다. 이미지 블록은 인용하지 않는다. Item은 서로 인접한 텍스트 블록 1~2개만 인용한다.
- **이미지 연결.** `basis`는 `caption_brand`, `adjacent`, `order` 중 하나다. `image_block_idx`에는 이미지 블록의 번호를 쓴다.

## 출력 모델

`ArticleExtraction`은 `src/fashion_planner_trend/schemas/article_extraction.py`에 정의되어 있다. 표준 값은 `src/fashion_planner_trend/schemas/vocabulary.py`의 한글 문자열이다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `article_id` | string | 모델이 아니라 코드가 채운다 |
| `styles` | `ExtractedStyle[]` | 트렌드가 없는 기사(흔하다)는 빈 목록이다 |

**`ExtractedStyle`**

| 필드 | 타입 | 의미 |
|---|---|---|
| `name` | string | 기사에 나온 트렌드명 그대로 |
| `name_origin` | `article` \| `analyst` | LLM 출력은 `article`이어야 한다(`V1`) |
| `aliases` | string[] | 기사에 나온 다른 이름. 원문 그대로 쓴다 |
| `seasons` | `{year: int, season: "SS" \| "FW"}[]` | 중복할 수 없다 |
| `definition` | string | 한 문장 |
| `description` | `{text, evidence: Evidence[≥1]}[]` | 상품 기획에 참고할 사실 |
| `focus_attributes` | `{attribute, level, evidence: Evidence[≥1]}[]` | `attribute`는 `category`, `color`, `material`, `pattern`, `detail`, `fit` 중 하나이고 `level`은 `core` 또는 `supporting`이다. 같은 속성은 한 번만 쓸 수 있고 `core`는 최대 3개다 |
| `evidence` | `Evidence[]` | 이름·정의·시즌의 근거 |
| `items` | `ExtractedItem[]` | 트렌드의 예로 든 옷 |

**`Evidence`**: `block_idx`(int, 텍스트 블록 번호)와 `quote`(그 블록에서 글자 그대로 옮긴 인용).

**`ExtractedItem` 스칼라 필드**(이름이 그래프의 Item 속성과 같다)

| 필드 | 타입 | 의미 |
|---|---|---|
| `name`, `raw_text` | string | 짧은 이름과 그 옷을 서술한 구절 |
| `gender` | `남` / `여` / `공용`? | 기사가 밝힌 경우에만 채운다 |
| `size` | null | 항상 `null`. 모델에게 보이지 않는다 |
| `category_raw` | string? | 카테고리 원문 |
| `category_minor` | enum? | 하분류 21개 중 하나. 예: `재킷`, `팬츠` |
| `category_major` | enum? | `상의`, `하의`, `아우터`, `원피스`. **코드가 `category_minor`로 정한다.** 모델 값과 다르면 코드 값을 쓰고 `category_major_mismatch:<llm>-><derived>`를 표시한다. `null`이면 지원 범위 밖 Item이다. JSON에는 남지만 그래프에는 적재하지 않는다 |
| `fit_raw`, `fit` | string?, enum? | 핏 7개. 예: `와이드` |
| `shape_raw`, `shape` | string?, enum? | 실루엣 10개 |
| `length_body_raw`, `length_body`, `length_sleeve_raw`, `length_sleeve` | string?, enum? | 기장과 소매 길이 |
| `styling_raw`, `styling_accessory` | string? | 착용 방식과 함께 쓴 액세서리 |
| `review_flags` | string[] | **코드만 채운다**([후처리](#후처리) 참고). 모델에게 보이지 않는다 |

**`ExtractedItem` 속성 관계와 연결**

| 필드 | 원소 | 의미 |
|---|---|---|
| `colors[]` | `{raw, value: Color?}` | 표준 21색 |
| `materials[]` | `{raw, value: Material?}` | 표준 소재 26개 |
| `patterns[]` | `{raw, value: Pattern?}` | 표준 패턴 19개 |
| `details[]` | `{raw, type, value?, motif?, placement?}` | `type`은 `detail`, `neckline`, `collar` 중 하나이고, `value`는 그 `type`의 표에서 골라야 한다 |
| `evidence[]` | `Evidence` | Item과 그 모든 `raw`의 근거 |
| `image_links[]` | `{image_idx, basis}` | `image_idx`는 크롤러의 `images[].image_idx`이며 블록 번호가 아니다. `basis`는 `caption_brand`, `adjacent`, `order` 중 하나다 |

`value`가 `null`이면 `raw`는 남지만 그래프에 관계는 만들지 않는다.

## LLM 호출

**스키마.** `llm_schema.py`가 `ArticleExtraction`을 strict JSON 스키마로 바꾼다. 모든 속성이 필수이고, 선택 필드는 `anyOf [..., null]`이 되며, `additionalProperties`는 `false`이고, `$ref`는 모두 펼친다. 추출용으로 세 가지를 바꾼다.

- `article_id`, `size`, `review_flags`는 모델에게 보이지 않는다.
- 이미지 연결은 `{image_block_idx, basis}`가 된다. 모델은 블록 번호만 쓰고, 후처리가 `image_block_idx`를 크롤러의 `image_idx`로 바꾼다.
- `details`는 `detail`·`neckline`·`collar` 세 가지 `anyOf` 변형이 된다. 변형마다 값 enum이 따로 있어서 스키마가 `type`↔`value` 대응을 강제한다.

`evidence`에 걸린 `minItems: 1` 제약 두 개는 strict 모드에서 쓸 수 없어 스키마에서는 빠진다. 대신 pydantic이 검사한다.

**요청.** 클라이언트는 프롬프트를 사용자 메시지 하나로 담아 OpenAI Chat Completions에 보내고, `response_format: {type: "json_schema", strict: true}`를 지정한다. 모델은 `configs/llm_models.yaml`에 등록된 것만 쓸 수 있고, 코드에는 모델명을 두지 않는다.

- `standard` 모델에는 `temperature: 0`을 보낸다.
- `reasoning` 모델에는 등록된 목록에서 고른 `reasoning_effort`를 선택적으로 보낸다. 일반 모델에 추론 강도를 주면 오류다.

| 설정 | 출처 |
|---|---|
| API 키 | `LLM_API_KEY` |
| 모델 | `--model`, 없으면 `LLM_MODEL`(필수) |
| 추론 강도 | `--reasoning-effort`, 없으면 `LLM_REASONING_EFFORT`. 환경변수 값은 모델도 환경변수에서 올 때만 쓴다 |
| 시간 제한 | `--timeout`, 없으면 `LLM_TIMEOUT_S`, 그것도 없으면 600초 |
| 캐시 폴더 / 호출 로그 | `LLM_CACHE_DIR` / `LLM_CALL_LOG`. 없으면 `data/processed/llm/cache/`와 `data/processed/llm/calls.jsonl` |

설정은 프로세스 환경변수나 저장소의 `.env` 파일에서 읽고, 둘 다 있으면 프로세스 환경변수가 이긴다.

**응답 검사.** 호출은 다음 오류 중 하나로 실패한다.

- `LLMBackendError`: 200이 아닌 응답, 키 없음, 등록되지 않은 모델.
- `LLMTimeout`: 시간 초과.
- `LLMOutputError`: 거절, 빈 출력, JSON이 아닌 출력, 스키마 불일치. 출력 전체를 감싼 `json` 코드 블록 하나는 벗겨 내고 파싱한다.

**캐시.** 캐시 키는 다음 다섯 가지를 합친 것이다.

- **입력 sha256** = `sha256(렌더링한 프롬프트 + "\n" + 스키마 sha256)`. 템플릿과 기사가 모두 반영된다.
- **backend**
- **model**
- **reasoning effort**
- **프롬프트 id**

캐시 파일은 `<cache_dir>/<backend>/<model>/<effort>/<prompt id>/<input_sha256>.json`에 둔다. **성공했고 스키마에 맞는 응답만 캐시한다.** 실패한 호출은 캐시하지 않으며, 캐시 적중도 쓰기 전에 현재 스키마로 다시 검사한다. 스키마는 통과했지만 검증(`V1`–`V6`)에서 걸린 응답은 캐시에 남는다. 그래서 같은 프롬프트를 다시 실행하면 같은 결과가 나오고, 새 응답을 받으려면 `--no-cache`를 준다. 재시도 프롬프트는 원래 프롬프트와 다르므로 캐시 항목도 따로 생긴다. `--no-cache`이면 캐시를 읽지도 쓰지도 않는다.

**호출 로그.** 실패한 호출과 캐시 적중을 포함해 모든 호출이 호출 로그에 JSONL 한 줄을 더한다. 필드는 `ts`, `purpose`(`article_extraction`), `prompt_version`, `backend`, `model`, `reasoning_effort`, `profile`, `schema_sha256`, `input_sha256`, `prompt_chars`, `thread_id`(OpenAI 응답 id), `latency_s`, `usage`, `cache_hit`, `ok`, `error`, `error_type`이다. **프롬프트와 응답 원문은 넣지 않는다.**

## 후처리

`postprocess.py`는 pydantic 검증 전에 모델 출력의 사본을 다음 순서로 고친다.

1. **이미지 연결.** `image_block_idx`를 크롤러의 `image_idx`로 바꾼다. 이미지 블록이 아니거나 크롤러 이미지 범위 밖이면 그 연결을 지우고 `V6` 오류로 돌려준다.
2. **별칭 사전 재매핑.** Item의 스칼라 `raw`(카테고리·핏·세이프·기장·소매 길이)와 색·소재·패턴·디테일의 `raw`를 `configs/alias_map/`의 별칭 사전으로 다시 판정한다. 사전과 모델이 다를 때 값을 바꾸는 것은 사전이 정확히 일치한 경우뿐이다. 결과는 Item의 `review_flags`에 남긴다.

   | 표시 | 조건 | 값 |
   |---|---|---|
   | `alias_override:<field>:<raw>:<llm>-><dict>` | 사전의 표준 값·별칭 정확 일치가 모델 값과 다르다 | 사전 값으로 바꾼다 |
   | `alias_null:<field>:<raw>:<llm>` | 사전은 표준 값이 없는 표현이라고 하는데 모델이 값을 냈다 | `null`로 바꾼다 |
   | `approx:<field>:<raw>-><value>` | 사전이 근사로 매핑하고 모델 값과 같다 | 그대로 둔다 |
   | `alias_disagree:<field>:<raw>:<llm>\|<dict>` | 근사이거나 여러 값으로 갈리는 사전 결과가 모델 값과 다르다 | 그대로 둔다 |
   | `alias_route:<field>:<raw>-><attribute>:<value>` | 다른 속성에 속하는 표현이다 | 그대로 둔다 |
   | `alias_hint:<field>:<raw>-><key>=<value>` | 다른 필드를 암시하는 표현이다. 예: 충전재를 뜻하는 소재 표현은 하분류 `패딩`을 암시한다 | 그대로 둔다 |

   `<field>`는 `category_minor`, `fit`, `shape`, `length_body`, `length_sleeve`, `colors`, `materials`, `patterns`, `details.detail`, `details.neckline`, `details.collar` 중 하나다. 사전에 없는 표현은 모델 값을 그대로 두고 표시하지 않는다. `--length-area`(기본 꺼짐)를 켜면 하의·원피스의 `롱`을 `맥시`로 바꾸고 `length_area:length_body:<old>-><new>`를 남긴다.
3. **중복 합치기(`P1`, 선택).** Style 안에서 같은 `focus_attributes`가 여럿이면 하나로 합친다. 처음 나온 자리를 유지하고, `core`가 `supporting`보다 우선하며, 근거는 `(block_idx, quote)` 기준 합집합이다. 같은 `seasons`도 하나로 합친다. `core`가 3개를 넘는 경우는 고치지 않으므로 `V1` 오류로 남는다.
4. **이름 고치기(`P2`, 선택).** Style 이름을 기사 원문 표현으로 되돌린다.
   - **소개하는 말.** 이름이 `일명`·`이른바`·`소위`로 시작하면 그 말과 감싼 따옴표를 뗀다.
   - **끝의 일반어.** 이름이 기사에 그대로 없고 끝의 `트렌드`·`trend`를 떼면 기사에 있을 때, 그 말을 한 번 뗀다.

   두 경우 모두 남은 이름이 2글자 이상이고, 기사에 있고, 같은 기사의 다른 Style 이름과 겹치지 않아야 고친다. 다른 Style과 겹치는 것만 걸리면 고치지 않고 `N3` 기록만 남긴다. `definition`, `description`, `evidence`, `aliases`는 건드리지 않으며, 두 번 적용해도 결과가 같다.

`P1`·`P2`가 고친 내역은 코드로 시작하는 경고로 남는다. 실행 스크립트는 둘 다 기본으로 켜고 `--no-dedupe`·`--no-name-fix`로 끈다. 라이브러리 함수(`postprocess`, `extract_article_outcome`, `BatchConfig`)의 기본값은 꺼짐이다. 어떤 단계를 실행했는지는 `label_meta`의 `postprocess`에 기록한다.

## 검증

`validate.py`는 pydantic을 통과한 결과를 코드로 검사하고 아무것도 자동으로 고치지 않는다. 문자열 비교는 모두 공백을 무시한다. 오류·경고 문구는 `V3 Item[…]: …`처럼 코드로 시작하고 뒤에 사람이 읽는 설명이 붙는다. 프로그램에서는 앞의 코드만 믿는다. 코드별 건수는 `label_meta.validation.counts`에 있다.

| 코드 | 등급 | 검사 |
|---|---|---|
| `V1` | 오류 | **구조와 정책.** pydantic 검증 실패: enum 값, `evidence` 1개 이상, focus 속성·시즌 중복 금지, `core` 최대 3개, 디테일 값과 `type` 표의 일치. pydantic 오류는 20건까지 기록한다. 그 밖에 `name_origin`이 `article`이 아닌 경우, 별칭이 제목·텍스트 블록에 그대로 없는 경우, Style 이름에 자리표시자 토큰(`dummy`, `placeholder`, `removeme`, `todo`, `tbd`, `lorem`, `xxx`, 대소문자 무시)이 있고 그 이름이 기사에 없는 경우도 `V1`이다 |
| `V2` | 오류 | **근거 존재.** Style·설명·focus·Item의 `quote`가 비어 있거나, `block_idx`가 텍스트 블록(문단·소제목)이 아니거나, 인용이 그 블록의 부분 문자열이 아니다 |
| `V3` | 오류 | **속성 존재.** Item의 `*_raw`, `styling_accessory`, 색·소재·패턴·디테일의 `raw` 가운데 하나가 그 Item 근거 블록들을 이은 텍스트의 부분 문자열이 아니다 |
| `V4` | 오류 | **근거 결합.** Item에 근거가 없거나, 서로 다른 블록을 2개 넘게 인용하거나, 텍스트 블록 순서에서 `--max-text-gap`(기본 1)보다 멀리 떨어진 두 블록을 인용했다. 간격을 셀 때 이미지 블록은 건너뛴다 |
| `V5` | 경고(`W5`로 센다) | **분류 혼동 의심.** Style에 `seasons`가 없다. `raw_text`가 근거 블록의 부분 문자열이 아니다. 패턴 `raw`에 자수·비즈·리본 같은 디테일 낱말이 있다. 디테일 `raw`에 프린트·체크·스트라이프·도트·플로럴 같은 패턴 낱말이나, 두르다·매치·레이어드 같은 스타일링 표현이 있다. 키워드 목록은 `validate.py`에 있다 |
| `V6` | 오류 | **이미지 연결.** 이미지 블록이 아닌 블록을 가리키거나 `image_idx`가 크롤러 이미지 범위 밖이다 |
| `N3` | 경고 | **이름 충실성.** Style 이름이 제목·텍스트 블록에 그대로 없다(대소문자 무시). 문구에 끝의 일반어(트렌드·스타일·패션·코디·룩 또는 영문 trend·style·fashion·look), 괄호 부분, 바뀌었거나 지어낸 이름 가운데 짐작되는 사유를 적는다. 값은 바꾸지 않는다 |
| `P1`, `P2` | 경고 | 위 후처리가 고친 내역 |

오류가 없으면 그 기사는 **통과**다. 경고는 통과 여부에 영향을 주지 않는다. `V2`·`V3` 판정 함수는 `src/fashion_planner_trend/ingestion/extraction/evidence.py`에 있고 라벨 검사 스크립트 `scripts/validate_labels.py`도 같은 함수를 쓰므로 LLM 출력과 사람이 만든 라벨이 같은 기준으로 판정된다.

## 재시도와 검토 대기열

오류가 있으면 프롬프트 끝에 검증 오류 목록(최대 30건, 나머지는 건수만)을 붙여 **한 번 더** 보낸다. 덧붙인 부분은 처음부터 다시 추출하라고 하면서, 인용과 `raw`를 글자 그대로 복사하고, Item 근거는 인접 블록 1~2개만 쓰고, 근거를 댈 수 없는 것은 빼라고 지시한다. 그 뒤 처리는 다음과 같다.

- **오류가 없으면** `<out>/<article_id>.json`에 쓴다.
- **재시도 후에도 오류가 남으면** 두 번째 결과를 같은 형식(`validation.passed = false`)으로 `<out>/_review/<article_id>.json`에 쓰고, `review_queue.jsonl`에 한 줄(`ts`, `article_id`, `prompt_version`, `path`, `n_errors`, 앞의 오류 10건)을 더한다. pydantic이 거부한 경우에는 검토할 수 있도록 후처리한 원형 dict를 남긴다.
- **LLM 호출 자체가 실패하면**(백엔드·시간 초과·출력 오류) 같은 실행 안에서 재시도하지 않고 **파일도 쓰지 않는다.** `run.log`에 `failed` 기록만 남기고, 다음 실행에서 다시 시도한다. 호출 실패가 3번 연속되면 새 기사를 더 시작하지 않는다. 대개 사용 한도나 인증 문제다.

## 출력 파일

```text
data/processed/extractions/
├── review_queue.jsonl            # 검토로 보낸 기사마다 한 줄
└── article-extraction/                # --out
    ├── <article_id>.json         # 검증 통과 (scripts/load_graph.py가 읽는다)
    ├── _review/<article_id>.json # 재시도 후에도 실패 (적재하지 않는다)
    ├── run.log                   # JSONL, 처리한 기사마다 한 줄
    └── run_report.md             # 집계 보고서, 실행할 때마다 다시 쓴다
data/processed/llm/
├── cache/…                       # 응답 캐시 (프롬프트, 즉 기사 원문이 들어 있다)
└── calls.jsonl                   # 호출 로그 (원문 없음)
```

파일은 원자적으로 쓴다. 기사가 통과하면 `_review/`에 남아 있던 사본을 지우고, 반대의 경우도 마찬가지다. 결과 파일은 `{"label_meta": {...}, "extraction": ArticleExtraction}` 형식이고, 사람이 만든 라벨 파일과 같아서 적재기가 둘 다 읽는다. `label_meta`는 다음과 같다.

| 필드 | 값 |
|---|---|
| `article_id`, `url`, `title` | 크롤 레코드에서 가져온다 |
| `published` | `published_at[:10]` 또는 `null` |
| `status` | `"auto"`. 라벨 파일은 `silver` 또는 `gold`다 |
| `labeler` | `"llm:<backend>/<model>"`. 예: `llm:openai_api/<model>` |
| `prompt_version`, `prompt_sha256` | `"article-extraction"`와 함께 기록하는 해시 |
| `extracted_at` | `+09:00` 오프셋이 붙은 ISO 8601 시각 |
| `postprocess` | `{alias_map, length_area, dedupe, name_fix}` 불리언. 실행한 후처리 단계 |
| `validation` | `{passed, attempts, errors[], warnings[], counts{}}`. `counts`의 키는 `V1`–`V4`, `V6`, `W5`, `N3`, `P1`, `P2`다 |
| `llm` | `{backend, model, reasoning_effort, thread_ids[], latency_s, cache_hit[], schema_sha256, usage[], tool_events}`. 배열은 시도마다 한 원소다. `usage`는 OpenAI usage 객체이고 캐시 적중이면 `null`이다. `latency_s`는 시도들의 합이다. `tool_events`는 항상 0이다 |

`run.log`에는 기사마다 `status`(`passed`, `review`, `failed`), `attempts`, 소요 시간, 호출별 지연, `cache_hit`, `usage`, `counts`, 오류·경고 수, Style·Item 수가 남는다. 호출이 실패한 기사는 대신 `error_type`과 잘라 낸 오류 문구가 남는다.

## 실행

```bash
python scripts/extract_articles.py --limit 3            # 먼저 3건만 시험
python scripts/extract_articles.py --ids 1001,1002      # 지정한 기사만, 이 순서대로
python scripts/extract_articles.py                      # --in의 기사 전체
python scripts/extract_articles.py --report-only        # LLM 호출 없이 run_report.md만 다시 만든다
```

| 옵션 | 의미 |
|---|---|
| `--in` | 크롤 JSONL(기본 `data/raw/articles.jsonl`) |
| `--out` | 출력 폴더(기본 `data/processed/extractions/article-extraction/`) |
| `--prompt-version` | 프롬프트 id(기본 `article-extraction`) |
| `--model`, `--reasoning-effort`, `--timeout` | [LLM 호출](#llm-호출) 참고 |
| `--ids`, `--limit` | 기사를 고른다. 입력에 없는 id를 주면 멈춘다 |
| `--concurrency` | 1 또는 2(기본 2) |
| `--no-cache` | 응답 캐시를 읽지도 쓰지도 않는다 |
| `--retry-review` | `_review/`의 기사를 다시 처리한다 |
| `--reprocess` | 모든 출력을 캐시된 응답으로 다시 만든다. 캐시에 없을 때만 모델을 호출한다 |
| `--no-dedupe`, `--no-name-fix` | `P1`·`P2`를 끈다 |
| `--max-text-gap`, `--length-area`, `--review-queue` | `V4`의 간격, 선택 기장 규칙, 검토 대기열 경로 |

**이어서 하기.** 결과 파일이나 `_review/` 파일이 이미 있는 기사는 건너뛰므로, 중단된 실행은 멈춘 곳부터 이어진다. 호출이 실패한 기사는 파일이 없으므로 다시 시도한다. 캐시를 켠 채 `--retry-review`를 주면 모델을 부르지 않고 캐시된 응답으로 후처리·검증만 다시 하는데, 검증기를 바꾼 뒤에 유용하다. 새 응답을 받으려면 `--no-cache`를 함께 준다.

**종료 코드.** `0`은 실패한 호출이 없다는 뜻이다. `2`는 실패한 호출이 남았다는 뜻이고, 다시 실행하면 이어서 한다. `3`은 연속 실패로 일찍 멈췄다는 뜻이다.

## 비용과 보고서

`run_report.md`는 실행할 때마다 출력 폴더의 모든 파일로 다시 만든다. **집계만 있고 기사 원문이나 인용은 없다.** 담기는 내용은 다음과 같다.

- 통과·검토 대기·호출 실패(파일 없음) 기사 수
- 쓴 모델과 추론 강도
- 이번 실행 결과
- `V1`–`V4`·`V6` 코드별 실패 기사 수
- 시도 횟수 분포
- 추출 분포: Style·Item 수, Style이 없는 기사, 카테고리, focus 속성, `review_flags` 종류
- 호출 통계: 호출 수, 캐시 적중, 지연(평균·중앙값·최대), 기사당 소요 시간

**토큰 합계**(입력·출력)는 기사마다 실제로 API를 부른 마지막 기록에서 더하므로 캐시 적중은 0이다. 보고서는 금액이 아니라 토큰을 보여 준다. 비용을 어림하려면 합계에 지금 쓰는 모델의 공식 단가를 곱한다. 추론 토큰은 출력 토큰(`completion_tokens`)에 들어 있다.

## 예시

아래는 지어낸 트렌드, 지어낸 시즌, 가상의 브랜드로 만든 합성 예시다. 입력 레코드는 다음과 같다(일부 생략).

```json
{"article_id": "demo-0001", "url": "https://example.invalid/articles/demo-0001",
 "title": "2030 S/S, 피스타치오 테일러링이 온다", "published_at": "2029-10-02T09:00:00+09:00",
 "tags": ["트렌드"], "season": {"value": "2030SS", "source": "title", "raw": "2030 S/S"},
 "blocks": [
  {"idx": 0, "type": "heading", "text": "피스타치오 테일러링"},
  {"idx": 1, "type": "paragraph", "text": "디자이너들은 2030 S/S 시즌의 새 흐름을 피스타치오 테일러링(Pistachio Tailoring)이라고 부른다. 부드러운 피스타치오 그린으로 재단한 날렵한 수트가 중심이다."},
  {"idx": 2, "type": "image", "image_idx": 0, "run_idx": 0, "layout": "single", "caption": "Brand A 2030 S/S RTW",
   "caption_parsed": {"type": "collection", "brand": "Brand A", "season": "2030SS"}},
  {"idx": 3, "type": "paragraph", "text": "핵심 아이템은 피스타치오색 더블브레스티드 린넨 재킷과 피스타치오색 와이드 팬츠다."}],
 "images": [{"image_idx": 0, "block_idx": 2, "url": "https://img.example.invalid/0.jpg", "section_idx": 1}]}
```

모델에게는 블록이 다음과 같이 간다. 이미지 URL은 들어가지 않는다.

```text
[0] 소제목: 피스타치오 테일러링
[1] 문단: 디자이너들은 2030 S/S 시즌의 새 흐름을 피스타치오 테일러링(Pistachio Tailoring)이라고 부른다. 부드러운 피스타치오 그린으로 재단한 날렵한 수트가 중심이다.
[2] 이미지 (run 0 · single): 캡션 "Brand A 2030 S/S RTW" → 브랜드 Brand A · 시즌 2030SS
[3] 문단: 핵심 아이템은 피스타치오색 더블브레스티드 린넨 재킷과 피스타치오색 와이드 팬츠다.
```

후처리를 거친 결과의 `extraction` 부분은 다음과 같다(두 번째 Item의 null 필드는 생략).

```json
{"article_id": "demo-0001", "styles": [{
  "name": "피스타치오 테일러링", "name_origin": "article", "aliases": ["Pistachio Tailoring"],
  "seasons": [{"year": 2030, "season": "SS"}],
  "definition": "2030 S/S 런웨이에서 나타난, 부드러운 피스타치오 그린 수트를 중심으로 한 테일러링 트렌드.",
  "description": [{"text": "재킷과 팬츠를 같은 피스타치오색으로 맞춘다.",
                   "evidence": [{"block_idx": 3, "quote": "피스타치오색 더블브레스티드 린넨 재킷과 피스타치오색 와이드 팬츠"}]}],
  "focus_attributes": [{"attribute": "color", "level": "core",
                        "evidence": [{"block_idx": 1, "quote": "부드러운 피스타치오 그린으로 재단한 날렵한 수트"}]}],
  "evidence": [{"block_idx": 1, "quote": "2030 S/S 시즌의 새 흐름을 피스타치오 테일러링(Pistachio Tailoring)이라고 부른다"}],
  "items": [
   {"name": "피스타치오 린넨 재킷", "raw_text": "피스타치오색 더블브레스티드 린넨 재킷",
    "gender": null, "size": null, "category_raw": "재킷", "category_major": "아우터", "category_minor": "재킷",
    "colors": [{"raw": "피스타치오색", "value": "그린"}], "materials": [{"raw": "린넨", "value": "린넨"}], "patterns": [],
    "details": [{"raw": "더블브레스티드", "type": "detail", "value": "더블브레스티드", "motif": null, "placement": null}],
    "styling_raw": null, "styling_accessory": null, "fit_raw": null, "fit": null, "shape_raw": null, "shape": null,
    "length_body_raw": null, "length_body": null, "length_sleeve_raw": null, "length_sleeve": null,
    "evidence": [{"block_idx": 3, "quote": "피스타치오색 더블브레스티드 린넨 재킷"}],
    "image_links": [{"image_idx": 0, "basis": "adjacent"}], "review_flags": []},
   {"name": "피스타치오 와이드 팬츠", "raw_text": "피스타치오색 와이드 팬츠",
    "category_raw": "팬츠", "category_major": "하의", "category_minor": "팬츠",
    "colors": [{"raw": "피스타치오색", "value": "그린"}], "fit_raw": "와이드", "fit": "와이드",
    "evidence": [{"block_idx": 3, "quote": "피스타치오색 와이드 팬츠"}], "image_links": [], "review_flags": []}]}]}
```

모델이 낸 `image_block_idx: 2`는 후처리에서 크롤러 `image_idx: 0`이 됐다. 두 `category_major`는 모두 `category_minor`에서 정해졌다. 이 출력은 경고 없이 첫 시도에 통과하므로 `validation`은 `{"passed": true, "attempts": 1, "errors": [], "warnings": [], "counts": {}}`다.

실패하는 경우도 보자. 재킷의 `evidence`가 블록 3이 아니라 블록 1(`"날렵한 수트"`)을 인용했다고 하자. 그러면 `재킷`·`피스타치오색`·`린넨`·`더블브레스티드`가 근거 블록에 없으므로 `V3` 오류가 4건 생긴다. `raw_text`도 근거 블록에 없으므로 `V5` 경고(`W5`)가 1건 생긴다. 재시도 프롬프트에 이 오류들이 실리고, 두 번째 답도 같으면 `attempts: 2`로 `_review/`에 들어간다.

## 개인정보·데이터

- **밖으로 나가는 것.** 실제 실행(`--report-only`가 아닌 모든 실행)은 기사마다 제목, 발행일, 태그, 시즌 태그와 후보, 모든 문단·소제목의 전문, 이미지 캡션을 `LLM_API_KEY`로 OpenAI API(`api.openai.com`)에 보낸다. 재시도하면 한 번 더 보낸다. OpenAI가 데이터를 어떻게 다루는지는 계정의 약관에 따른다. 실행하기 전에 그 기사를 제3자에게 보내도 되는지 저작권과 원 사이트 이용약관 양쪽에서 확인한다.
- **나가지 않는 것.** 이미지 픽셀과 이미지 URL, 기사 URL과 작성자, 그리고 렌더링한 프롬프트에 없는 모든 것은 보내지 않는다.
- **디스크에 남는 것.** 응답 캐시에는 기사 원문을 담은 프롬프트 전체가 저장된다. 결과 파일과 `_review/` 파일에는 원문 인용이 있다. `review_queue.jsonl`과 오류 문구에는 짧은 발췌가 들어간다. 모두 git이 무시하는 `data/processed/` 아래에 있으니 공개하지 않는다. 호출 로그, `run.log`, `run_report.md`에는 기사 원문이 없다.
- **키.** API 키는 환경변수나 `.env`에서 읽고, 로그·예외·결과 파일에 쓰지 않는다.
- **테스트.** `tests/test_extraction.py`, `tests/test_extraction_batch.py`, `tests/test_llm.py`는 합성 기사와 가짜 백엔드를 쓰며 API를 호출하지 않는다.
