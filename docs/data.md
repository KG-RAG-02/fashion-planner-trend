# 데이터 카드

이 레포에는 **코드와 작은 합성 fixture만** 있다. 원래 그래프를 만들 때 쓴 기사 원문, 상품 레코드, 이미지,
LLM 출력, 정답 라벨은 하나도 들어 있지 않으며, 이 레포를 통해 재배포해서도 안 된다.

> **MIT 라이선스는 이 레포의 코드에만 적용된다.** 아래 제3자 데이터에 대한 어떤 권리도 주지 않는다.
> 데이터를 받거나 가공하거나 그 결과를 공개하기 전에 각 출처의 이용 조건을 직접 확인한다.

## 출처

| 출처 | 코드가 쓰는 것 | 가져오는 곳 | 라이선스·이용 조건 | 이 레포에 포함 |
|---|---|---|---|---|
| Vogue Korea 패션 트렌드 기사 | 제목, 발행일, URL, 텍스트 블록, 이미지 URL과 캡션(이미지 파일은 받지 않음) | `src/fashion_planner_trend/ingestion/crawler/`의 수집기 | 사이트 이용약관은 **검토하지 않았다.** 공개본 크롤러는 실행할 때 robots.txt를 확인해 금지된 주소면 멈추고, 이 프로젝트를 밝히는 User-Agent를 쓴다. 기사 저작권은 발행처에 있다 | **없음** |
| `hateslopacademy/otpensource_dataset` (Hugging Face) | 카탈로그 상품 행: 이름, 카테고리, 색, 소재, 패턴, 디테일, 이미지 URL | `ingestion/normalization/otpensource.py`에 고정한 리비전 | 데이터셋 카드에 CC-BY-4.0 표기(작성자 확인). 원 데이터는 쇼핑몰 상품 목록이고, 그 쇼핑몰의 약관은 **확인하지 않았다** | **없음** |
| `disl/musinsa-snap-new` (Hugging Face) | 착장 사진 속 옷의 라벨(카테고리, 기장, 핏, 디테일). 이미지는 복사하지 않고 parquet 행 번호로만 가리킨다 | `ingestion/normalization/musinsa_snap.py`에 고정한 리비전 | 데이터셋 카드에 **라이선스 표기 없음**(작성자 확인) | **없음** |
| K-Fashion 이미지 AI 데이터 구축 가이드라인 Ver 1.04 (AI Hub) | 표준 어휘로 쓰는 *속성 값 목록*(색, 카테고리, 소재 등)만 | `src/fashion_planner_trend/schemas/vocabulary.py`에 값 목록으로 적음 | 가이드라인 이용 조건은 직접 확인한다. 가이드라인 본문은 들어 있지 않다 | 값 이름만 |
| OpenAI API | 기사마다 구조화된 추출 결과를 만든다 | `src/fashion_planner_trend/llm/` | 사용자 자신의 OpenAI 계정 약관을 따른다. 추출을 실행하면 **기사 텍스트가 OpenAI로 전송된다** | — |

이 프로젝트는 Vogue, Condé Nast, 두산매거진, 무신사, H&M과 관련이 없다.

### 결과에 들어가는 제3자 이미지 링크

API 결과에는 제3자 이미지 **URL**이 들어간다. 이 서비스는 이미지를 내려받거나 저장하지 않는다.

- Style 예시 사진(`trends[].style.example_images[].url`)과 Item 사진(`image_urls`)은 기사 페이지의 이미지 URL이다.
- 상품 사진은 카탈로그 상품의 `image_url` 또는 데이터셋 행을 가리키는 `image_ref`(`hf://…#row=<n>`)다.

이 링크를 화면에 보여 주거나 내려받는 것은 결과를 쓰는 쪽의 책임이다. 원 출처의 권리를 확인하고,
내려받은 이미지는 재배포하지 않는다.

## 레포에 들어 있는 것

| 경로 | 내용 |
|---|---|
| `src/fashion_planner_trend/graph/fixture.py` | 합성 그래프(가상의 기사·트렌드·옷·상품, id `9000xx`). Neo4j 테스트와 오프라인 stub이 쓴다 |
| `scripts/seed_fixture_graph.py` | 위 합성 그래프를 비어 있는 로컬 Neo4j에 적재하고 질의를 한 번 돌린다 |
| `examples/stub/` | 합성 요청과 기대 응답 요약 |
| `configs/alias_map/*.yaml` | 원문 표현을 표준 어휘로 바꾸는 별칭 사전 |
| `configs/alias_map/set_overrides.yaml` | 여러 벌로 된 카탈로그 상품을 사람이 검토해 나눈 결과. 원 데이터셋의 상품 id를 키로 쓴다 |
| `tests/` | 손으로 만든 작은 합성 입력을 쓰는 단위 테스트 |

`data/` 아래는 모두 git에서 제외된다.

## 데이터를 직접 받고 만드는 법

경로는 스크립트 기본값이다. 모든 스크립트는 `--help`를 지원한다.

1. **기사** (선택, 위의 라이선스 주의를 먼저 읽는다):
   `python scripts/crawl_vogue_sample.py --limit 20 --out data/raw/articles.jsonl`
   기사 하나를 JSON 한 줄로 쓴다(`article_id`, `title`, `url`, `published_at`, `blocks[]`, `images[]`).
2. **추출** (기사 텍스트가 OpenAI로 가고 비용이 든다):
   `python scripts/extract_articles.py --in data/raw/articles.jsonl --limit 3`
   결과는 `data/processed/extractions/article-extraction/<article_id>.json`, 실패는 `_review/`에 쓴다.
   [extraction.md](extraction.md) 참고.
3. **상품** (두 Hugging Face 데이터셋을 고정 리비전으로 Hugging Face 캐시에 받는다):
   ```bash
   python scripts/build_otpensource.py
   python scripts/build_musinsa_snap.py
   python scripts/build_musinsa_dataset.py   # → data/processed/musinsa_dataset.jsonl
   ```
   스크립트마다 입력·출력 SHA-256과 원 데이터 리비전을 `data/processed/BUILD_INFO.json`에 남긴다.
4. **그래프**: `python scripts/load_graph.py`가 `.env`의 Neo4j에 상품과 추출 결과를 적재한다.
   [graph-schema.md](graph-schema.md) 참고.

## 스키마

| 산출물 | 스키마 |
|---|---|
| 수집한 기사 JSONL | [extraction.md](extraction.md)의 입력 절 |
| 추출 파일(`label_meta` + `extraction`) | `src/fashion_planner_trend/schemas/article_extraction.py`의 `ArticleExtraction`, 설명은 [extraction.md](extraction.md) |
| 상품 JSONL(`ProductRecord`) | `src/fashion_planner_trend/schemas/graph_schema.py`, 설명은 [graph-schema.md](graph-schema.md) |
| 그래프 | [graph-schema.md](graph-schema.md) |
| API 입출력 | [contracts.md](contracts.md) |

## 원래 배포본과 재현할 수 없는 부분

작성자들의 그래프(호스팅 Neo4j, 2026년 11월에 마지막으로 다시 적재)에는 다음이 들어 있었다.

| 노드 | 개수 | 출처 |
|---|---:|---|
| Article | 193 | 2026-01-01부터 2026년 11월까지 발행된 기사(발행일 기준으로 고름) |
| Style | 240 | 기사마다 트렌드별 Style 하나(기사 간 병합 없음) |
| Item | 528 | 지원하는 상분류가 있는 Item(지원 밖 131개는 제외) |
| Product | 32,212 | 카탈로그 상품 7,857 + 착장 사진 속 옷 24,355(상분류가 없는 148행 제외) |

이 기사들은 지금 `prompts/article_extraction.md`로 남긴 프롬프트와 OpenAI 모델 `gpt-6-sol`(추론 강도 `none`)로
추출했다. 그때 만든 Article 노드의 `prompt_version` 속성에는 예전 식별자 `extract-v5`가 들어 있다. 새로 실행하면
`article-extraction`와 `prompt_sha256`이 기록된다.

재현의 한계:

- **원래 그래프의 상품 데이터는 사람이 검수한 것이라 이 코드로 다시 만들 수 없다.** 빌드 스크립트는 자동
  정규화한 데이터셋(약 33,000행)을 만든다. 팀은 그 뒤 레포 밖에서 색·소재·패턴·디테일 값을 손으로 검수하고
  고쳤고, 그 과정에서 약 1,000행이 빠지고 많은 속성 값이 바뀌었다. 검수본은 공개하지 않는다.
- **기사 원문은 바뀐다.** 나중에 다시 수집하면 블록 번호가 달라질 수 있다. 근거(`block_idx` + `quote`)는
  추출할 때 쓴 수집본에 묶여 있다.
- **LLM 출력은 공급자나 모델이 바뀌면 달라진다.** `data/processed/llm/cache/`의 캐시가 같은 입력의 재실행을
  로컬에서 재현 가능하게 한다.
- **정답 라벨**(프롬프트와 모델을 고를 때 쓴, 사람이 검수한 기사 23건)은 공개하지 않는다. 평가 스크립트는
  직접 만든 라벨(`data/labels/`, git 제외)로 동작한다. [annotation-guide.md](annotation-guide.md) 참고.

## 개인정보

파이프라인은 개인정보를 일부러 모으지 않는다. 다만 기사 본문과 착장 사진에는 실제 인물(디자이너, 유명인,
거리에서 찍힌 사람)이 나오거나 이름이 적힐 수 있다. 가공한 데이터를 공개하기 전에 확인한다.
