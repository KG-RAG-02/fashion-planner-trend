# 다이어그램 근거표

`docs/diagrams/src/*.dot`의 모든 노드와 엣지를, 그것을 뒷받침하는 코드 위치와 함께 적는다.
원본을 고친 뒤에는 `bash scripts/render_diagrams.sh`로 다시 그리고 이 표도 고친다. 코드를 고쳐 줄 번호가 바뀌면
이 표의 줄 번호도 함께 고친다.

확인 방법: **읽기** = 그 줄을 열어 본다, **테스트** = 오프라인 테스트가 단언한다, **실행** = 명령을 돌려 확인한다.
경로의 `src/…`는 `src/fashion_planner_trend/…`를 줄인 것이다.

## pipeline.dot

| 요소 | 근거 (file:line) | 확인 방법 |
|---|---|---|
| Vogue Korea → 1 수집 ("HTML + 목록 API") | `src/…/ingestion/crawler/vogue_kr.py:25-46`(기본 URL, 목록 페이지, `admin-ajax.php` 액션), `:117` `fetch_article_list`, `:432` `crawl`, robots.txt 확인 `:85-110` | 읽기, `tests/test_crawler_incremental.py`(HTTP 대역, 오프라인) |
| 1 수집 = `crawl_vogue_sample.py` | `scripts/crawl_vogue_sample.py:21,50` | 읽기 |
| 수집 → 원문 기사 JSONL(텍스트 블록, 이미지 캡션) | `scripts/crawl_vogue_sample.py:38`(`--out` JSONL), 이미지 파일은 받지 않고 URL·캡션만: `vogue_kr.py:1-12` | 읽기 |
| 원문 → 2 LLM 추출 ("텍스트 블록만") | `src/…/ingestion/extraction/prompt.py:1-11`(이미지 픽셀·URL은 보내지 않음), `:79` `render_blocks`, `:132` `render_prompt` | 읽기, `tests/test_extraction.py`의 프롬프트 렌더링 테스트 |
| 2 LLM 추출 = `extract_articles.py`, 프롬프트 + JSON 스키마 | `scripts/extract_articles.py`, `src/…/ingestion/extraction/batch.py`, `extractor.py:89-120`, `llm_schema.py:62` | 읽기 |
| LLM 추출 ↔ OpenAI API | `src/…/llm/backends.py:51`(chat completions 엔드포인트) | 읽기, `tests/test_llm.py`(대역) |
| 추출 → 후처리 + 검증 | `extractor.py:57-65`(`postprocess` 다음 검증), `postprocess.py:1-16`(별칭 재매핑), `validate.py:1-16`(V1–V6, N3) | 읽기, `tests/test_extraction.py` |
| 검증 → 추출 ("오류 목록을 붙여 1회 재시도") | `extractor.py:91` `max_retries=1`, `:101-110` 반복과 `render_retry_suffix` | 읽기, `tests/test_extraction.py`의 재시도 테스트 |
| 검증 → `extractions/<article_id>.json` ("통과") | `batch.py:3`, `:103` | 읽기, `tests/test_extraction_batch.py` |
| 검증 → `_review/` + `review_queue.jsonl` ("재시도 후에도 실패") | `batch.py:3-5`, `:49`, `:103` | 읽기, `tests/test_extraction_batch.py` |
| Hugging Face 데이터셋 → 3 정규화 ("고정 리비전") | `src/…/ingestion/normalization/otpensource.py:26-27,35-39`, `musinsa_snap.py:25-26,40-43` | 읽기 |
| 3 정규화 + 별칭 = `build_otpensource.py`, `build_musinsa_snap.py`, 세트 분해, 통합 | `scripts/build_otpensource.py`, `scripts/build_musinsa_snap.py`, `normalization/alias_map.py`, `set_split.py`, `combine.py:20` | 읽기, `tests/test_alias_map.py`, `test_set_split.py`, `test_combine.py` |
| 정규화 → 상품 데이터셋 JSONL(ProductRecord) | `combine.py:1,20-21`, `scripts/build_musinsa_dataset.py:51` | 읽기, `tests/test_combine.py` |
| extractions, 상품 데이터셋 → 4 그래프 적재 | `scripts/load_graph.py:32-36,78`, `src/…/graph/loader.py:182-219`(`read_products`, `load_products`), `:378-406`(기사) | 읽기 |
| 원문 → 그래프 적재 ("이미지 맵", 점선) | `loader.py:308-326`(`read_crawl`, `image_map_of`), `:338-376`(Style 예시 사진) | 읽기, `tests/test_style_example_images.py` |
| 그래프 적재 → Neo4j ("Cypher MERGE", 다시 실행해도 같은 결과) | `loader.py:1-5`, `:154-178`, `:231-248` | 읽기, `tests/test_graph_neo4j.py`(`NEO4J_TEST_URI` 필요) |
| Neo4j 내용(Article, Style, Item, Product, 어휘 노드) | `loader.py:41` `LABELS`, `:118-132` `seed_vocabulary` | 읽기 |
| Neo4j → 5 검색 + 점수 ("Cypher 조회") | `src/…/graph/search.py:61-79`(Cypher), `:134-144` `TrendSearcher` | 읽기 |
| 5 검색 + 점수 = `TrendSearcher`, `rank_products` | `search.py:263-368`, `src/…/retrieval/scoring.py:134-151` | 읽기, `tests/test_scoring.py`, `test_scoring_options.py`, `test_search_itemless.py` |
| 검색 → 6 HTTP API ("TrendAnalysisResult") | `src/…/api/server.py:113-131` | 읽기, `tests/test_api_server.py` |
| 6 HTTP API: `POST /v1/agents/trend/runs`, Bearer 인증 | `server.py:24-26`, `:84-96` | 읽기, `tests/test_api_server.py`, `test_api_handler.py` |
| orchestrator ↔ HTTP API ("TrendQuery") | `server.py:38-56`(봉투의 `input` = TrendQuery). 호출하는 쪽은 orchestrator 레포 | 읽기 |

## graph-schema.dot

| 요소 | 근거 (file:line) | 확인 방법 |
|---|---|---|
| 노드 라벨(Article, Style, Item, Product, Brand, Category, Color, Material, Pattern, Detail)과 고유 `id` | `src/…/graph/loader.py:41-42` | 읽기 |
| Article 속성 | `loader.py:250-259` `article_props` | 읽기, `tests/test_stub_fixture.py` |
| Style 속성, id `<article_id>-s<n>` | `src/…/schemas/graph_schema.py:74-103`, id는 `loader.py:277` | 읽기, `tests/test_contract.py` |
| Item 속성, id `<style_id>-i<m>` | `graph_schema.py:106-127`, id는 `loader.py:286` | 읽기 |
| Product 속성(`source`, `kind`, 이미지 필드) | `graph_schema.py:130-162` | 읽기, `tests/test_build_output.py` |
| Category 4, Color 21, Material 26, Pattern 19, Detail 62(detail 40, neckline 12, collar 10), Brand 1 | `loader.py:118-132`, 열거형 크기는 `src/…/schemas/vocabulary.py` | 실행: `python -c "from fashion_planner_trend.schemas import vocabulary as V; print(len(V.Color))"` 등 |
| Category 값 상의 · 하의 · 아우터 · 원피스 | `vocabulary.py`의 `CategoryMajor` | 읽기 |
| Style -[HAS_ITEM]-> Item | `loader.py:240` | 읽기 |
| Style -[EVIDENCED_BY {block_idx, quote, about, idx}]-> Article | `loader.py:242-243` | 읽기 |
| Item -[EVIDENCED_BY {block_idx, quote, about='item'}]-> Article | `loader.py:245-246` | 읽기 |
| Item/Product -[IN_CATEGORY]-> Category | `loader.py:164-165`(두 라벨이 같은 Cypher를 씀, `:177-178`) | 읽기 |
| Item/Product -[HAS_COLOR/HAS_MATERIAL/HAS_PATTERN {raw, approx}]-> | `loader.py:168-170` | 읽기 |
| Item/Product -[HAS_DETAIL {raw, motif, placement, approx}]-> Detail | `loader.py:171-173` | 읽기 |
| Product -[BELONGS_TO]-> Brand | `loader.py:155`, `:177`(Product만 `with_brand=True`) | 읽기 |
| Style 예시 사진은 링크만 저장 | `graph_schema.py:102-103`, `loader.py:338-376` | 읽기 |

## request-flow.dot

| 요소 | 근거 (file:line) | 확인 방법 |
|---|---|---|
| `POST /v1/agents/trend/runs`, 본문 `{contract_version, request_id, input}` | `src/…/api/server.py:24-26,38-50,90-92` | 읽기, `tests/test_api_server.py` |
| Bearer 토큰 확인(상수 시간 비교) → 401 | `server.py:84-88`(`hmac.compare_digest`), `:94-96` | 읽기, `tests/test_api_server.py` |
| 봉투 + TrendQuery 검증 → 400(`invalid_request`, `unsupported_contract_version`, `invalid_trend_query`) | `server.py:38-56`, 본문 크기 `:27,97-104` | 읽기, `tests/test_api_handler.py` |
| 1 Style: 모든 Style을 읽고 시즌 일치, 이름이 있으면 이름 일치 | `src/…/graph/search.py:73` `STYLES_CYPHER`, `:283-297` | 읽기, `tests/test_search_itemless.py` |
| Style → `no_results`(이름·시즌 불일치, Item 없음) | `search.py:286-310`, 지원 밖 카테고리는 `:279-281`에서 먼저 끝남 | 읽기 |
| 2 Style별 Item: 카테고리 포함·제외, 요청 속성 필터 | `search.py:312-329` | 읽기 |
| 3 상품 후보: 상분류가 같고 (하분류, fit, 공유 속성 노드) 중 하나 일치, approx 제외 | `search.py:156-185`, `:79` `_ATTR_CANDIDATES`(fit은 `WHERE NOT 'fit' IN p.approx`) | 읽기, `tests/test_graph_neo4j.py` |
| 점수 = Σ w·ratio ÷ Σ w + α, 상품에 없는 속성은 분모에서 빠짐 | `src/…/retrieval/scoring.py:84-131` | 읽기, `tests/test_scoring.py` |
| w = 기본 가중치 × 트렌드 배수(핵심 2.0, 보조 1.5), α = 0.1 | `configs/weights.yaml:13-27`, `src/…/retrieval/weights.py`의 `applied_weight`, `applied_alpha` | 읽기, `tests/test_weights.py` |
| 점수 → `no_results` ("통과한 것 없음") | `search.py:355-357` | 읽기 |
| 4 근거: `EVIDENCED_BY` → `evidence_id = trend:<article>:<block>:<sha8>` | `search.py:74,228-239,358-367`, `src/…/schemas/trend_analysis_result.py:31-36` | 읽기, `tests/test_contract.py` |
| 200 `{contract_version, request_id, producer, status, result}` | `server.py:125-131` | 읽기, `tests/test_api_server.py` |
| 검색 예외 → 503 `search_unavailable` | `server.py:113-117`(Style 단계만이 아니라 `search()` 호출 전체를 감쌈) | 읽기, `tests/test_api_handler.py` |
