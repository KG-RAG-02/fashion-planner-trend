# Trend Analysis Agent

패션 MD가 신상품을 기획할 때 **"요즘 어떤 스타일이 유행하고, 우리가 살 수 있는 비슷한 상품은 무엇인가?"** 에 답해 주는 에이전트입니다.
패션 매거진 기사에서 트렌드를 추출해 Knowledge Graph(Neo4j)로 만들고, 실제 상품과 속성(색상·소재·패턴·디테일 등)으로 연결해 근거와 함께 보여줍니다.

> 조직: [KG-RAG-02](https://github.com/KG-RAG-02) · 상태: **개발 중 — 1차 프로토타입.** 정규화 → Neo4j 적재 → 검색·점수 → 결과 출력과 **기사 추출 LLM(OpenAI)·검증·비교 도구**가 구현돼 있습니다. 150건 배치 추출과 자동 수집·Style 병합은 아직 없습니다. 자세한 내용은 [진행 상황](#진행-상황)

## 빠른 길잡이

| 지금 상황 | 가야 할 곳 |
|---|---|
| Docker만 있고 **이미지 파일을 전달받았다** (코드만 고쳐 보고 싶다) | [실행 방법 A](#a-이미지로-전달받았다면-docker만-있으면-된다) |
| 레포를 받아 **처음부터 환경을 만든다** (Python + Docker) | [실행 방법 B](#b-처음부터-환경을-만들기) |
| 스크립트가 무엇을 하는지, 어떤 파일이 어디에 있는지 | [스크립트 한눈에](#스크립트-한눈에), [파일과 경로](#파일과-경로) |
| 다른 Agent에 작업을 넘긴다 / 이어받는다 | [docs/pipeline.md §0.1](docs/pipeline.md) (이어받기 요약), [docs/spec.md](docs/spec.md) §0.2·§13 |
| **다음에 무엇을 만들어야 하는지** (자동 수집·LLM 추출 작업 계획) | [docs/pipeline.md](docs/pipeline.md) |
| 무신사 Product v2(이미지 주소 통일·통합본)의 경위와 검수할 발견 사항 | [docs/product_v2_rebuild.md](docs/product_v2_rebuild.md) |

## 이 레포의 역할

전체 프로젝트(VOGUE)는 5개 에이전트로 나뉘며, 이 레포는 그중 **1번 에이전트**를 담당합니다.

```
사용자 쿼리
   ↓
① 트렌드 분석   ← 이 레포
   ↓
② 자사 상품·재고
   ↓
③ 새 상품 기획
   ↓
④ 상품성·실행 가능성
   ↓
⑤ 기획안 생성·MD 검토
```

| 단계 | 입력 | 출력 |
|---|---|---|
| 트렌드 분석 (GraphRAG) | `TrendQuery` — 시즌, 트렌드명(발레코어 등), 특정 상품, 요청 카테고리(포함/제외)·성별·속성(색상 등), 지원하지 않는 조건(`unsupported`) | `TrendAnalysisResult` — 조건에 맞게 걸러진 트렌드(정의·설명·핵심 속성) → 대표 Item(속성) → 유사 상품(일치/불일치/미확인 속성, 점수·근거), 기사·상품 출처, 한계 |

예시 검색 조건: 시즌 2027 SS, 트렌드명 발레코어, 요청 카테고리 블라우스 → 해당 트렌드의 블라우스 Item 속성을 분석하고 비슷한 Product를 찾는다.

> 유사 상품은 무신사 기반 데이터셋(아래 [데이터](#데이터))에서 찾으므로 Brand는 전부 `Musinsa`입니다. 검색 조건에서 브랜드(예: H&M)를 지정하면 처리하지 않고 `unsupported`로 기록해 응답의 한계로 알립니다. 조건에 시즌이 없으면 다음 패션 시즌(예: 2026-11 → 2027 SS)으로 해석합니다.

## 동작 방식

**1) 데이터 구축 (사전 작업)**

```
Vogue Korea 기사 크롤링
 → LLM으로 Style / Item / 속성 추출 (Structured JSON, 1회 호출 기준선)        ← 구현됨 (`scripts/extract_articles.py`). 정답 라벨은 평가 전용이라 그래프에 올리지 않는다 (docs/pipeline.md)
 → 속성값 정규화·검증 (K-Fashion 표준 어휘 + 별칭 사전)
 → Neo4j Knowledge Graph 적재
 → 무신사 기반 Product 데이터 연결 (02·03 전체를 같은 사전으로 정규화)
```

**2) 검색**

```
검색 조건(TrendQuery) → Style 탐색(시즌·트렌드명) → 카테고리·속성 필터 → Item 속성 조회
 → Product 후보 검색(공유 속성 노드) → 속성별 가중치 일치도 계산 → TrendAnalysisResult 반환
```
(지금은 `scripts/run_trend_search.py`의 인자로 검색 조건을 줍니다.)

### 핵심 개념

- **Style**: 기사에서 추출한 트렌드/스타일 (예: 절제된 맥시멀리즘). 한 줄 정의(`definition`), 기획 참고 설명(`description`), 이 트렌드에서 중요한 핵심·보조 속성을 가진다.
- **Item**: 스타일의 대표 아이템. 개별 의류 한 벌 (예: 꽃 자수 재킷). 한 Style에 여러 Item이 붙는다.
- **Product**: 실제 상품 데이터. 두 데이터셋에서 온다 (아래 [데이터](#데이터)).
- Item과 Product는 **같은 속성 노드(Category 상분류, Color, Material, Pattern, Detail)를 공유**하고, 공유하는 속성으로 유사 상품을 찾아 일치도를 계산합니다. 하분류(`category_minor`)·핏·길이·세이프·스타일링은 노드가 아니라 속성입니다.

```
Style -[HAS_ITEM]-> Item -[HAS_COLOR / HAS_MATERIAL / HAS_PATTERN / HAS_DETAIL]-> 속성 노드 <-[...]- Product -[BELONGS_TO]-> Brand
                      └-[IN_CATEGORY]-> Category(상의·하의·아우터·원피스) <-┘
```

### 설계 원칙

- 기사에 **명시된 속성만** 추출하고, 없으면 `null`/`[]`로 둡니다. (추정 금지) 미언급 속성은 점수 계산에서 빠지며 불일치로 감점하지 않습니다.
- Pattern(프린트) / Detail(자수 등 장식) / Styling(착용 방식)을 구분합니다. 기사의 `raw` 표현은 보존하고, 표준 값은 K-Fashion 표에 있을 때만 채웁니다.
- 검증은 LLM이 아니라 코드가 합니다. 근사 매핑(`approx`) 값은 1차 검색(필터·점수)에 쓰지 않고 표시만 합니다.
- 추천 결과에는 **일치·불일치·미확인 속성**, 점수 분해, 기사·상품 출처를 함께 제공해 순위를 재현·설명할 수 있게 합니다.

## 데이터

| 구분 | 출처 | 비고 |
|---|---|---|
| 트렌드 (기사) | Vogue Korea 패션 트렌드 기사 | 크롤러 구현 완료. 수집 150건(기존 20건 + 130건). 이미지는 URL만 저장. **원문은 git에 없고 팀 내부로 전달** |
| 정답 라벨 | 위 기사 중 5건을 Claude가 `silver`로 라벨링하고 사용자가 검수해 **gold**로 확정(2026-11-10, 이미지 연결 포함) | Style 5개(절제된 맥시멀리즘·스커트·보라색·시럽코어·슬렌더 맨), Item 34개 + 지원 밖 5개(gold 검수 뒤, 이미지 연결 51개). **방침상 평가 전용이다. 지금의 임시 그래프만 이 라벨로 만들었다. git에 없고 팀 내부로 전달** |
| Product 02 | `hateslopacademy/otpensource_dataset` | 상품 카탈로그(8,827행, 상품명·이미지 URL 있음). 샘플링 없이 전체를 정규화 |
| Product 03 | `disl/musinsa-snap-new` | 착장 사진 속 아이템 라벨(24,532 슬롯). 구매 가능한 상품이 아니라 `kind=snap_garment`로 구분, 색 정보 없음, 핏은 있음 |
| 표준 어휘 | K-Fashion 가이드라인 (AI Hub, CC0) | 색 21, 카테고리 상분류 4 + 하분류 21, 소재 26, 패턴 19 등 |

## 진행 상황

기준일 2026-11-09. 현재 범위는 **구현해서 결과를 출력하는 데까지**이고, 검증·평가는 후속입니다.

| 영역 | 상태 |
|---|---|
| Vogue 크롤러, 샘플 수집 스크립트 | ✅ 구현 (150건 수집) |
| 스키마 코드 (`vocabulary`, `trend_query`, `article_extraction`, `graph_schema`, `trend_analysis_result`) | ✅ 작성 (문서는 사용자 검수 전) |
| 별칭 사전 + Product 정규화 v2: 02 8,819행(image_url 없는 빈 행 65 제외·세트 분해 포함), 03 24,532 슬롯(`snap_id`·`image_ref`), 통합본 `musinsa_dataset_v2.jsonl` 33,351행 (같은 사전) | ✅ 재빌드 완료 2026-11-10 ([D-33](docs/pipeline.md), 기록 [product_v2_rebuild.md](docs/product_v2_rebuild.md)). 세트 분해 검토·그래프 재적재는 남음 |
| Neo4j: 운영 그래프 = **AuraDB Free(클라우드)**, 개발·테스트 = 로컬 Docker (`docker-compose.yml`, 5.26 LTS) | ✅ Aura 적재·검색 확인 2026-11-10 ([D-35](docs/pipeline.md)): Product v2 33,203 + 라벨 5건(Style 5·Item 36), 검증 불일치 0/36. 인증서는 `neo4j_driver()`가 처리(D-35 ⑦). 로컬은 실행·접속 확인 |
| Neo4j 적재 (`scripts/load_graph.py`) | ✅ Product와 **기사 추출 결과**를 적재한다. 다시 실행해도 같은 그래프. 정답 라벨은 `--include-labels`일 때만(스키마 확인용 임시 옵션) |
| 유사도 가중치 설정 (`configs/weights.yaml`)·점수 계산 (`retrieval/scoring.py`) | ✅ 구현 (값은 임의 시작값) |
| 검색·결과 출력 (`scripts/run_trend_search.py`) | ✅ 구현. 그래프 검색이 전체 Product 직접 점수 계산과 36/36 일치 |
| 데이터를 구워 넣은 Docker 이미지 (`docker/demo/build.sh`) | ✅ 구현. CPU별로 `amd64`(Windows·Intel)와 `arm64`(Apple Silicon) 두 파일. arm64는 Apple Silicon에서 `docker compose`로 확인, amd64는 Apple Silicon 에뮬레이션에서 `docker run`으로만 확인 (**Windows·Intel PC 실행은 미확인**). 전달용, 팀 내부 |
| 테스트 | ✅ 366개 통과, 12개 건너뜀 (Neo4j 테스트 인스턴스가 필요한 것과 Windows에서 symlink를 못 만드는 것). 테스트 인스턴스(7688)를 붙이면 376개 통과, 2개 건너뜀(2026-11-10). Neo4j 통합 테스트 10개 통과 ([pipeline §13 R-9](docs/pipeline.md)) |
| LLM 호출·기사 추출·검증·비교 (WP-A) | ✅ 구현 (`llm/`, `ingestion/extraction/`, `scripts/extract_articles.py`, `compare_extraction.py`). ⬜ 150건 배치 실행(OpenAI로 기사 원문이 나가므로 실행 전 확인), 시즌 후보 대조, Item 단위 제외 ([docs/pipeline.md §1.1](docs/pipeline.md)) |
| 추출 프롬프트·모델 실험 | 🔶 진행 중. 프롬프트 v3(합성 예시 + 시즌 후보)·v4(Item 규칙 완화), 후처리 P1·P2 채택, 라벨 5건으로 gpt-4o-mini·gpt-4.1 비교 완료. LLM 호출 구조 정리(모델 계열 모듈 + 모델 등록표 `configs/llm_models.yaml`) 완료. 다음은 추론 모델 gpt-5-mini 실험 ([pipeline §6.7·§6.8, D-24~D-30](docs/pipeline.md)) |
| 검색 과정·점수 사유 출력(trace) | 🔶 점수 분해·근거는 나오지만 "무엇이 왜 빠졌는지"는 아직. 추가 여부 확인 대기 |
| 자동화 파이프라인(주 1회 수집 → 추출 → Style 해소 → 적재, 스케줄러) (WP-B) | ⬜ 설계 완료, 구현 전 |
| 자동화 파이프라인 설계와 작업 계획 | ✅ [docs/pipeline.md](docs/pipeline.md) (결정, 설계, 작업 패키지, 확인 대기 질문) |
| 평가셋 (기사 추출 정답 라벨링) | 🔶 150건 분류 완료, 라벨링 기준 v0.4 초안, **라벨 5건 gold**(2026-11-10). manifest에는 silver 18건이 더 적혀 있으나 파일은 이 PC에 없다(R-10). [eval/README.md](eval/README.md), [spec §13.2](docs/spec.md) |

## 폴더 구조

```
trend-analysis-agent/
├── src/trend_agent/
│   ├── config.py              # .env 읽기 (Neo4jSettings, 비밀번호는 출력하지 않음)                              ✅
│   ├── filelock.py            # 프로세스 간 파일 잠금 (POSIX·Windows 공통)                                      ✅
│   ├── llm/                   # OpenAI 호출 단일 진입점 (structured·call, 호출 기록·캐시)                         ✅
│   ├── embeddings/            # 로컬 BAAI/bge-m3 `embed_texts()` (Style 해소에 쓸 예정)                          ✅
│   ├── schemas/               # TrendQuery, ArticleExtraction, graph_schema, TrendAnalysisResult, vocabulary    ✅
│   ├── ingestion/
│   │   ├── crawler/           #   Vogue Korea 기사 수집 (vogue_kr.py)                                           ✅
│   │   ├── extraction/        #   LLM 기반 Style/Item/속성 추출·후처리·검증·일괄 실행 (프롬프트는 prompts/extraction/)  ✅
│   │   └── normalization/     #   alias_map.py(별칭 사전 적용), otpensource.py(02), musinsa_snap.py(03)           ✅
│   ├── graph/                 # loader.py(Neo4j 적재), search.py(검색·결과 조립)                                  ✅
│   ├── retrieval/             # weights.py(가중치 로더), scoring.py(점수·요청 속성 필터)                          ✅
│   └── api/                   # 에이전트 진입점 (다른 에이전트가 호출)                                            (미구현)
├── configs/
│   ├── alias_map/             # 속성별 별칭 사전 (yaml)
│   └── weights.yaml           # 유사도 가중치 기본값
├── scripts/                   # 실행 스크립트 (아래 "스크립트 한눈에")
├── prompts/extraction/        # 기사 추출 프롬프트 (v1, v2 = 동결한 스크립트 기본값, v3 = 합성 예시+시즌 후보, v4 = v3에서 Item 규칙 완화. 기본값은 실험 뒤 결정)
├── tests/                     # pytest 344개 (스키마·정규화·점수·LLM·추출·적재·비교·실험 등)
├── eval/                      # 평가 기준·정답 데이터 (라벨·검수 문서·결과는 git 제외)
├── docs/                      # spec.md(명세·결정·현재 상태), schemas/(데이터 계약·표준 어휘·별칭 사전·근거 자료)
├── data/                      # raw/ (수집 원본), processed/ (가공 결과) — 모두 git 제외
├── notebooks/                 # 검증 기록 (01 크롤 검증, 02·03 Product 후보 데이터셋 탐색)
├── docker-compose.yml         # 로컬 Neo4j 실행 파일 하나. 기본 = 개발용(빈 DB), `neo4j-demo`+`app` = 데이터가 구워진 이미지(전달받은 사람용)
├── docker/demo/               # 위 이미지 재료: Dockerfile.neo4j, Dockerfile.app, requirements.txt, build.sh
├── .github/                   # Issue / PR 템플릿
├── .env.example               # 환경변수 예시
├── pytest.ini, requirements.txt
└── README.md
```

## 파일과 경로

**git에 있는 것**: 코드(`src`, `scripts`, `tests`), 설정(`configs`), 문서(`docs`, `eval/README.md`, `eval/guidelines`, `eval/datasets/article_extraction/manifest.yaml`), Docker 정의(`docker-compose.yml`, `docker/demo/*` 중 Dockerfile·requirements·build.sh), `.env.example`.

**git에 없는 것** (`.gitignore`) — 어디서 오는지가 중요합니다.

| 경로 | 내용 | 어떻게 생기나 |
|---|---|---|
| `.env` | 비밀번호·API 키 | `.env.example`을 복사해 직접 입력 ([환경변수](#환경변수)) |
| `data/raw/vogue_kr/articles_sample.jsonl` | 기사 원문 20건 (사용자가 검수 중이라 덮어쓰지 않는다) | 크롤러 또는 **팀 내부 전달** |
| `data/raw/vogue_kr/articles_pool_150.jsonl` | 기사 원문 150건 | 크롤러(`--limit 150`) 또는 **팀 내부 전달** |
| `data/processed/v2/otpensource_all.jsonl` | 02 정규화 결과 v2 (ProductRecord. 원천 8,827행 → image_url 없는 빈 행 65 제외·세트 분해 후 8,819행) | `scripts/build_otpensource.py` |
| `data/processed/v2/musinsa_snap_all.jsonl` | 03 정규화 결과 v2 24,532 슬롯 (`snap_id`·`image_ref`로 parquet 행의 사진에 연결) | `scripts/build_musinsa_snap.py` |
| `data/processed/v2/musinsa_dataset_v2.jsonl` | **02·03 통합본** 33,351행 (같은 필드, 출처 `node.source`, 이미지 주소 `image_ref`). 그래프 적재의 기본 입력 | `scripts/build_musinsa_dataset.py` |
| `data/processed/v2/*_report.json`, `audit_v1_v2.md`, `product_coverage.txt` | 정규화·통합·v1↔v2 비교·커버리지 리포트 | 위 스크립트, `audit_products.py`, `check_product_coverage.py` |
| `data/processed/v1/` | 이전 v1 산출물 보관 (빌드 스크립트가 쓰지 못한다) | — |
| `data/processed/images_preview/` | 이미지 조회 확인용 jpg | `scripts/resolve_image.py` |
| `data/processed/results/*.json` | 검색 결과 (`TrendAnalysisResult`) | `scripts/run_trend_search.py` |
| `data/processed/extractions/<prompt_version>/<기사id>.json` | **기사 추출 결과** (검증 통과분. 실패는 `_review/`, 목록은 `review_queue.jsonl`) | `scripts/extract_articles.py` |
| `data/processed/llm/` | LLM 호출 기록(`calls.jsonl`, 원문 없음)과 응답 캐시(`cache/`, 원문 포함 — git 제외) | `llm` 모듈 |
| `data/processed/experiments/<vN>_<모델>_…/`, `INDEX.md` | **추출 실험 결과** — 결과와 함께 사용한 프롬프트·코드(patch·해시)·모델·비용·라벨 비교를 저장. 이름 앞은 프롬프트 버전(`v3_` 등). `embedding_<시각>/`은 Style 임베딩 유사도 측정 | `scripts/run_experiment.py`, `measure_style_similarity.py` |
| `eval/datasets/article_extraction/labels/<기사id>.json` | **정답 라벨** (Vogue 인용 포함, 평가용) | **팀 내부 전달**. `load_graph.py --include-labels`일 때만 그래프 입력이 된다 |
| `eval/datasets/article_extraction/review/<기사id>.md` | 라벨 검수용 문서 | `scripts/render_review.py` |
| Docker 볼륨 `trend-agent_neo4j_data` | **Neo4j 그래프 데이터** (git 밖, 이 PC의 Docker 안) | `docker compose up -d` 후 `scripts/load_graph.py` |
| `docker/demo/dump/neo4j.dump`, `docker/demo/out/*.tar.gz` | 그래프 덤프, 전달용 이미지 파일 | `bash docker/demo/build.sh` |

**팀 내부로 전달받는 파일**: Vogue 원문과 그 라벨에는 Vogue 본문 인용이 들어 있어 커밋하지 않습니다. 같은 그래프를 만들려면 받아서 **위 표의 같은 위치**에 둡니다. Product(02·03)는 공개 데이터라 스크립트가 직접 만들므로 전달이 필요 없습니다. 이미지(`trend-agent-demo-images-amd64.tar.gz`, `-arm64.tar.gz`)에도 인용문이 들어 있어 내부로만 전달합니다.

## 환경변수

`.env.example`을 `.env`로 복사해 채웁니다. **`.env.example`에는 값을 채우지 않습니다** (주소·포트·모델명·비밀번호 모두 각자 `.env`에만 씁니다). `.env`는 커밋하지 않습니다.

| 변수 | 필수 | 설명 |
|---|---|---|
| `NEO4J_URI` | 선택 | 비워 두면 로컬 Neo4j 기본 주소를 씁니다 (코드 기본값). **Aura를 쓸 때는 `neo4j+s://<인스턴스 id>.databases.neo4j.io`** (Aura가 만들 때 내려 주는 접속 정보 파일의 값). 로컬 포트를 바꿨을 때도 채웁니다 |
| `NEO4J_USER` | 선택 | 비워 두면 `neo4j`를 씁니다 (Docker Neo4j의 사용자는 항상 `neo4j`). Aura 접속 정보 파일에서는 이름이 **`NEO4J_USERNAME`** 이므로 그 값을 여기에 옮겨 적습니다 |
| `NEO4J_PASSWORD` | ✅ | 로컬: `docker compose up -d`가 이 값으로 Neo4j 비밀번호를 정합니다. 8자 이상을 권장합니다 (compose가 최소 길이를 5자로 낮춰 두었음). Aura: 인스턴스를 만들 때 한 번만 보여 주는 비밀번호 |
| `LLM_API_KEY` | 기사 추출에서 | OpenAI 키. `scripts/extract_articles.py`가 읽는다 (실행하면 기사 원문이 OpenAI로 전송된다) |
| `LLM_MODEL` | 기사 추출에서 | 사용할 모델명. **코드에 기본값이 없어** 비어 있으면 오류다. `configs/llm_models.yaml`에 등록된 모델만 쓸 수 있다(기준선 `gpt-4o-mini`, 모델은 실험으로 고른다 — [pipeline §6.8, D-30](docs/pipeline.md)) |
| `LLM_TIMEOUT_S` | 선택 | 호출당 제한 시간(초). 기본 600 |
| `LLM_CACHE_DIR`, `LLM_CALL_LOG` | 선택 | LLM 응답 캐시와 호출 기록의 위치. 기본 `data/processed/llm/` |
| `HF_HOME` | 선택 | 임베딩 모델(`BAAI/bge-m3`) 캐시 위치. 기본은 Hugging Face 기본 위치 |
| `NEO4J_BOLT_PORT`, `NEO4J_HTTP_PORT` | 선택 | 이미 쓰는 Neo4j와 포트가 겹칠 때 (기본 7687 / 7474). 바꾸면 `NEO4J_URI`의 포트도 같이 바꿉니다 |
| `DEMO_BOLT_PORT`, `DEMO_HTTP_PORT` | 선택 | 이미지 방식(demo 서비스)의 포트 (기본 7687 / 7474) |

## 실행 방법

> **compose 파일은 `docker-compose.yml` 하나**이고, **기본은 개발용(빈 DB)** 입니다. `docker compose up -d`는 개발용 Neo4j를 시작합니다. 전달받은 이미지(demo)는 서비스 이름을 적어 시작합니다: `docker compose up -d neo4j-demo app`. (끝낼 때는 `docker compose --profile demo down`. `docker compose down neo4j-demo app`은 Compose 버전에 따라 `unknown command`가 난다)

### A. 이미지로 전달받았다면 (Docker만 있으면 된다)

그래프 데이터(정답 라벨 5건 + 무신사 Product 02·03)와 파이썬 환경을 **Docker 이미지에 구워 넣은** 전달 방식입니다. 가상환경 설치·정규화·적재·`.env`가 필요 없고, **Docker와 이 레포의 코드 폴더**만 있으면 됩니다.

```bash
# 1. 이미지 가져오기 (처음 한 번. tar.gz 파일은 전달받는다). 내 CPU에 맞는 파일 하나만 쓴다
docker load -i trend-agent-demo-images-amd64.tar.gz     # Windows / Intel PC
docker load -i trend-agent-demo-images-arm64.tar.gz     # Apple Silicon Mac

# 2. 시작 (Neo4j에는 이미 데이터가 들어 있다)
#    개발용 Neo4j(방법 B)가 떠 있으면 같은 포트(7474/7687)라 시작에 실패한다 → 먼저 `docker compose stop neo4j`
docker compose up -d neo4j-demo app

# 3. 검색 실행
docker compose exec app python scripts/run_trend_search.py --include 스커트 --top-k 3
docker compose exec app python -m pytest -q

# 4. 호스트에서 코드를 고치고(src/trend_agent/graph/search.py, retrieval/scoring.py, configs/weights.yaml …) 3번을 다시 실행하면 바로 반영된다

# 5. 끝낼 때 (데이터는 전달받은 시점으로 돌아간다)
docker compose --profile demo down
```

- 5번을 `docker compose down neo4j-demo app`으로 쓰면 `unknown command "neo4j-demo"`가 나오는 Compose 버전이 있다 (v2.15.1에서 확인). `--profile demo down`은 어느 버전에서나 된다.
- 개발용 Neo4j를 멈춰 두었다면 demo를 끝낸 뒤 `docker compose up -d`(또는 `docker compose start neo4j`)로 다시 켠다. 개발용 데이터는 볼륨에 그대로 있다.
- 메모리는 Docker Desktop에 **4GB 이상** 할당한다 ([방법 B의 메모리 안내](#b-처음부터-환경을-만들기)와 같다).

- 코드(`src`, `scripts`, `configs`, `tests`)는 이미지에 없고 **호스트 폴더를 그대로 연결**합니다. 고친 파일이 곧바로 쓰입니다. 검색 결과 JSON은 호스트의 `data/processed/results/`에 저장됩니다.
- **확인할 수 있는 수정**: `search.py`, `scoring.py`, `weights.yaml`, 결과 출력. **확인할 수 없는 수정**: `loader.py`·그래프 구조처럼 **데이터가 바뀌는 수정**입니다. 이미지의 데이터는 고정된 스냅샷이므로 [실행 방법 B](#b-처음부터-환경을-만들기)로 처음부터 적재해야 합니다.
- 이미지의 Neo4j는 인증을 끄고(비밀번호 없음) 이 PC의 `127.0.0.1`에만 열립니다. 브라우저는 http://localhost:7474 입니다. 포트가 겹치면 `DEMO_BOLT_PORT`, `DEMO_HTTP_PORT`를 바꿉니다.
- 이미지에는 `numpy`·`requests`가 없어서 임베딩과 OpenAI 백엔드 테스트 일부는 건너뜁니다 (`docker compose exec app python -m pytest -q`). 이 이미지는 라벨 기반 임시 그래프를 담고 있다 (pipeline.md D-17).
- **CPU 아키텍처에 맞는 파일을 받아야 합니다.** 이미지는 CPU마다 따로 만들고, 파일 이름 끝이 아키텍처입니다: **Windows·Intel PC → `-amd64`, Apple Silicon Mac(M1 이상) → `-arm64`**. 맞지 않는 파일은 에뮬레이션으로 돌아가 매우 느리고(검색이 10초 안팎에서 약 50초로), Apple Silicon에서 amd64를 `docker compose`로 돌리면 healthcheck(`cypher-shell`)가 쌓여 메모리 부족으로 Neo4j가 종료될 수 있습니다 (exit 137). 맞는 파일을 쓰면 이 문제가 없습니다.

**이미지를 만들어 전달하는 사람**: 그래프가 적재돼 있는 상태에서 `bash docker/demo/build.sh` (Windows는 Git Bash). 지금 그래프를 덤프해 `trend-agent-demo-neo4j`(데이터 포함)와 `trend-agent-demo-app`(파이썬 환경) 이미지를 만들고 `docker/demo/out/trend-agent-demo-images-<arch>.tar.gz`(각 약 370MB)로 저장합니다 (`--no-save`로 파일 저장은 생략). 그래프나 라벨을 바꾼 뒤에는 `python scripts/load_graph.py`를 하고 다시 실행해 새 스냅샷을 만듭니다. 덤프하는 동안 개발용 Neo4j가 잠깐 멈췄다가 자동으로 다시 시작됩니다.

**받는 사람의 PC가 섞여 있으면 CPU별로 두 번 만듭니다.** 아무것도 안 주면 만드는 PC의 CPU용이 만들어지고, `PLATFORM`으로 다른 CPU용을 만듭니다 (덤프는 CPU와 무관):

```bash
bash docker/demo/build.sh                           # 이 PC의 CPU용 (Apple Silicon이면 arm64)
PLATFORM=linux/amd64 bash docker/demo/build.sh      # Windows·Intel용. Apple Silicon에서는 에뮬레이션이라 느리다 (아래 시간 참고)
```

- 이미지 이름(태그)은 두 CPU 모두 `:latest`라서 **마지막에 만든 것이 이 PC의 `:latest`가 됩니다.** 파일 이름에만 `-amd64`, `-arm64`가 붙습니다.
- Apple Silicon에서 amd64를 만든 실측: 이미지 빌드 약 1분, tar.gz 약 374MB. 에뮬레이션 Neo4j는 약 20초에 뜨고 검색·테스트(당시 74개)가 통과했습니다. 다만 `docker compose`의 healthcheck가 쌓여 OOM(exit 137)이 날 수 있어, **amd64 파일은 Windows·Intel에서 쓰고 Mac에서는 arm64를 씁니다.**
- **전달할 것: ① 받는 사람 CPU에 맞는 tar.gz ② 레포 코드** (`.env`와 `data`는 필요 없음).

만든 뒤에는 **개발용 Neo4j를 멈추고**(`docker compose stop neo4j`) 위 방법 A의 2~3번이 되는지 확인합니다. 그 상태에서 `docker compose --profile demo down`으로 끝낸 다음 개발용을 다시 켭니다. 이 절차로 확인한 결과: 그래프는 Style 5 · Item 36 · Product 33,323(옛 v1 Product)이고, 검색이 되며, 테스트가 통과합니다(2026-11-06 확인 당시 74개) (arm64는 Apple Silicon에서 `docker compose`로 확인, amd64는 `docker run`으로 확인. Windows·Intel PC에서의 실행은 이 PC에 없어 확인하지 못했습니다).

### B. 처음부터 환경을 만들기

준비물: Python 3.12 (3.9에서도 의존성 설치와 테스트(당시 74개)가 통과함을 확인했지만 3.12가 기준), Docker Desktop, Git. **Docker Desktop에는 메모리를 4GB 이상 할당**하고 다른 컨테이너는 정리해 둡니다 (Neo4j가 힙 1GB + 페이지캐시 512MB 등 약 2GB를 씁니다. 메모리가 부족하면 컨테이너가 OOM으로 계속 재시작합니다 → [자주 막히는 곳](#자주-막히는-곳)). 아래는 **이 순서대로** 실행합니다 (명령은 레포 루트에서). Windows 콘솔에서 한글이 깨지면 `PYTHONIOENCODING=utf-8`을 앞에 붙입니다.

```bash
# 1. 받기 + 가상환경 + 의존성
git clone https://github.com/KG-RAG-02/trend-analysis-agent.git && cd trend-analysis-agent
python -m venv .venv
.venv\Scripts\activate           # Windows   (macOS / Linux: source .venv/bin/activate)
pip install -r requirements.txt

# 2. 환경변수: .env.example을 복사해 NEO4J_PASSWORD(필수)를 입력. 나머지는 비워 두면 기본값을 쓴다
cp .env.example .env

# 3. 테스트 (Neo4j 없이 도는 단위 테스트)
python -m pytest -q                       # 366 passed, 12 skipped (Neo4j 테스트 인스턴스가 필요한 것과 symlink 권한이 필요한 것은 건너뜀)

# 4. Product 정규화: 공개 데이터를 내려받아 표준 어휘로 바꾼다 (처음에는 수 분 걸릴 수 있다)
python scripts/build_otpensource.py       # 02 → data/processed/v2/otpensource_all.jsonl (8,819행)
python scripts/build_musinsa_snap.py      # 03 → data/processed/v2/musinsa_snap_all.jsonl (24,532 슬롯)
python scripts/build_musinsa_dataset.py   # 02·03 통합 → data/processed/v2/musinsa_dataset_v2.jsonl (33,351행, 그래프 적재 입력)

# 5. Neo4j 시작 (처음에는 이미지를 내려받는다. 받은 뒤 healthy까지 약 30초). healthy가 되면 사용 가능
#    .env의 NEO4J_PASSWORD가 비어 있으면 컨테이너가 Restarting을 반복한다. 방법 A의 demo가 떠 있으면 포트가 겹친다
docker compose up -d
docker compose ps
python scripts/check_neo4j.py             # "성공: Neo4j Kernel 5.26.x" 가 나오면 된다

# 6. 기사 추출 (선택). .env에 LLM_API_KEY·LLM_MODEL을 넣은 뒤. OpenAI로 기사 원문이 전송되므로 처음에는 소량으로 확인한다
python scripts/extract_articles.py --limit 3
#    스키마만 확인하려면 팀 내부로 받은 라벨을 eval/datasets/article_extraction/labels/ 에 두고 7번에 --include-labels를 붙인다 (임시, 기본 아님)

# 7. 그래프 적재 (약 15초). 여러 번 실행해도 같은 그래프가 나온다
python scripts/load_graph.py              # 표준 값 + 02·03 Product + 기사 추출 결과

# 8. 검색
python scripts/run_trend_search.py --season 2027SS --include 스커트 --top-k 3
python scripts/verify_graph_search.py     # (선택) 그래프 검색 = 전체 Product 직접 점수 계산인지 확인: "불일치 0개"
```

- 추출 결과가 없으면 7번은 Product까지만 적재하고 "추출 결과 없음"을 알립니다. 이때 8번은 `no_results`입니다.
- Neo4j 브라우저: http://localhost:7474 (Bolt `bolt://localhost:7687`).
- **Aura(클라우드)를 쓸 때** ([D-35](docs/pipeline.md)): 5번의 `docker compose`는 건너뛰고 `.env`에 Aura 접속 정보(위 [환경변수](#환경변수))를 넣은 뒤 `check_neo4j.py`부터 같은 순서로 실행합니다. 콘솔·브라우저는 https://console.neo4j.io . 적재는 네트워크를 타므로 로컬보다 오래 걸립니다. **`pytest -m neo4j`는 그래프를 비우므로 Aura에 돌리지 않습니다** (로컬 7688 테스트 인스턴스만, `tests/conftest.py`가 막음). 접속은 모두 `trend_agent.config.neo4j_driver()`를 거치며, Aura 인증서를 운영체제 저장소와 certifi 묶음으로 함께 검증합니다 (Windows에서 `ServiceUnavailable: Unable to retrieve routing information`이 나던 원인, D-35 ⑦).
- 중지는 `docker compose down`, **그래프 데이터까지 삭제**는 `docker compose down -v`입니다. 그래프만 비우고 다시 만들려면 `python scripts/load_graph.py --reset`.
- 기사를 새로 수집하려면 `python scripts/crawl_vogue_sample.py --limit 20 --delay 1.0` (`--out`으로 저장 위치 지정, 기본 `data/raw/vogue_kr/articles_sample.jsonl`. **기존 파일을 덮어쓰니** 사용자가 검수 중인 파일은 `--out`을 바꿉니다).

### 코드를 고친 뒤 무엇을 다시 해야 하나

| 고친 것 | 다시 할 일 |
|---|---|
| `retrieval/scoring.py`, `retrieval/weights.py`, `configs/weights.yaml`, `graph/search.py` (점수·검색·결과 출력) | 없음. `run_trend_search.py`를 다시 실행하면 된다 |
| `graph/loader.py`, 그래프 구조·속성 | `python scripts/load_graph.py` (기사만 바꾸면 `--articles-only`) |
| 라벨 JSON (`eval/.../labels/*.json`) | `python scripts/validate_labels.py` → `python scripts/render_review.py` (검수 문서 갱신). 그래프에는 올리지 않는다 |
| 기사 추출 결과 (`data/processed/extractions/…`) | `python scripts/load_graph.py --articles-only` |
| 별칭 사전 (`configs/alias_map/*.yaml`), `ingestion/normalization/*` | `build_otpensource.py`, `build_musinsa_snap.py` → `build_musinsa_dataset.py` → `load_graph.py` |
| `src` 안 어느 코드든 | `python -m pytest -q` |

### 스크립트 한눈에

`docker/demo/build.sh`만 빼고 모두 레포 루트에서 `python scripts/<이름>`으로 실행합니다 (.venv의 파이썬).

| 스크립트 | 하는 일 | 입력 → 출력 | 선행 |
|---|---|---|---|
| `crawl_vogue_sample.py` | Vogue 기사 수집 (`--limit`, `--delay`, `--out`) | 웹 → `data/raw/vogue_kr/*.jsonl` | 인터넷 |
| `build_otpensource.py` | 02를 표준 어휘로 정규화 (전체) | HuggingFace 02 → `data/processed/v2/otpensource_all.jsonl` + 리포트 | 인터넷(처음) |
| `build_musinsa_snap.py` | 03을 같은 사전으로 정규화 (전체) | HuggingFace 03 → `data/processed/v2/musinsa_snap_all.jsonl` + 리포트 | 인터넷(처음) |
| `build_musinsa_dataset.py` | 02·03 v2를 통합본 하나로 합침 (출처·id·필드 검사) | `v2/*_all.jsonl` → `data/processed/v2/musinsa_dataset_v2.jsonl` + 리포트 | 위 두 스크립트 |
| `resolve_image.py` | Product id·`image_ref` → 이미지 (03은 jpg 저장, 02는 URL만 출력, 외부 요청 없음) | 통합본 + HF 캐시 parquet → `data/processed/images_preview/` | 03 parquet 캐시 |
| `check_neo4j.py` | Neo4j 접속 확인 (비밀번호는 출력 안 함) | `.env` | `docker compose up -d` |
| `extract_articles.py` | 기사를 LLM으로 추출·검증해 저장 (`--limit`, `--ids`, `--report-only`). **OpenAI로 원문이 전송된다** | `data/raw/vogue_kr/articles_pool_150.jsonl` → `data/processed/extractions/<prompt_version>/` | `.env`의 `LLM_API_KEY`·`LLM_MODEL` |
| `compare_extraction.py` | 추출 결과를 정답 라벨과 **건수**로 비교 | extractions + labels → `eval/results/` | 추출, 라벨 |
| `run_experiment.py` | 추출 실험 1조건(프롬프트 버전 × 모델) 실행·기록: 코드·프롬프트·비용·라벨 비교까지 저장 (`--prompt-version`, `--model`, `--reasoning-effort`, `--ids`, `--no-dedupe`, `--no-name-fix`, `--note`). **OpenAI로 원문이 전송된다**(같은 조건은 캐시로 재사용) | 기사 + labels → `data/processed/experiments/<vN>_…/` | `.env`의 `LLM_API_KEY` |
| `check_llm_model.py` | 새 LLM 모델을 등록표에 넣기 전 합성 문장 1건으로 요청 형식 확인 (`--model`, `--profile standard\|reasoning`, `--reasoning-effort`). 기사는 보내지 않는다 | 합성 문장 → 통과/실패 + `configs/llm_models.yaml`에 넣을 항목 | `.env`의 `LLM_API_KEY` |
| `measure_style_similarity.py` | Style 이름·정의의 임베딩 유사도 측정 (로컬 BGE-M3, 외부 호출 없음) | labels + 실험 결과 → `data/processed/experiments/embedding_<시각>/` | `.venv`에 `torch`·`sentence-transformers` |
| `load_graph.py` | 표준 값·Product·기사 추출 결과를 Neo4j에 적재. `--articles-only`, `--include-labels`(임시), `--prompt-version`, `--prune-products`, `--reset`, `--products`(기본 통합본), `--products-dir`(출처별 폴더, 예: v1) | `data/processed/v2/musinsa_dataset_v2.jsonl`, `data/processed/extractions/` → Neo4j | Neo4j, 정규화 |
| `run_trend_search.py` | 조건으로 트렌드·유사 상품 검색 | Neo4j → 요약 + `data/processed/results/<id>.json` | 적재 |
| `verify_graph_search.py` | 그래프 검색이 전체 Product 직접 계산과 같은지 검증 (`--top-k`) | Neo4j + `data/processed/*_all.jsonl` | 적재 |
| `validate_labels.py` | 라벨 JSON 검증 (스키마·인용 위치·raw 존재) | labels (+ `data/raw`) | |
| `render_review.py` | 라벨 → 검수용 md (원본 URL 포함) | labels → `eval/.../review/<id>.md` | |
| `check_product_coverage.py` | 정답 Item마다 대응 Product가 몇 개 있는지 | labels + `data/processed/*_all.jsonl` → `product_coverage.txt` | 정규화 |
| `docker/demo/build.sh` | 데이터를 구워 넣은 전달용 이미지 생성 (`bash docker/demo/build.sh`) | 현재 그래프 → `docker/demo/out/*.tar.gz` | 적재, Docker |
| `audit_products.py` | Product 정규화 전·후(이전·새 산출물 폴더) 수치표 (`--v1 data/processed/v1 --v2 data/processed/v2`) | 두 폴더 → `audit_v1_v2.md` | 정규화 |
| `check_image_urls.py` | 02 이미지 URL을 스트리밍 GET으로 점검 (이미지는 내려받지 않음) | `--shared` 폴더 → 점검 기록 | 인터넷. **기본 경로에서 쓰지 않는다** |
| `fetch_snap_images.py`, `catalog_content.py`, `finalize_catalog_content.py`, `set_components_review.py` | 스냅 원본 이미지 내려받기, 이미지 내용 판정(비전·CLIP), 세트 구성품 검토 — 연구용 | `--shared` 폴더 구조가 전제 | **기본 경로에서 쓰지 않는다** (pipeline.md F-12: 지울지 사용자 확인 대기) |

`run_trend_search.py` 옵션:

| 옵션 | 의미 | 예 |
|---|---|---|
| `--season` | 시즌 (없으면 다음 패션 시즌) | `2027SS`, `2026FW` |
| `--include`, `--exclude` | 카테고리(상분류·하분류) 포함·제외 | `--include 상의`, `--exclude 후드티`, `--include 스커트` |
| `--color` `--material` `--pattern` `--detail` `--fit` | 요청 속성(표준 값). 같은 속성의 여러 값은 OR | `--color 퍼플`, `--fit 스키니` |
| `--style` | 트렌드명으로 좁히기 | `--style 시럽코어` |
| `--top-k` | 아이템당 유사 상품 수 (기본 5) | `--top-k 3` |
| `--json` | 결과 JSON 저장 경로 | `--json out.json` |

결과 읽는 법: 상품마다 `점수  출처:이름  일치 [...]  불일치 [...]  미확인 [...]`이 나옵니다. JSON의 `score_breakdown`에 속성별 적용 가중치·일치 비율·하분류 보너스가 있고, `evidence`에 기사 원문 인용(URL 포함)이 있습니다. 화면 맨 아래 "한계·알림"에는 시즌 해석, 보류·제외한 건수, 순위 근거가 약한 아이템이 나옵니다.

### 자주 막히는 곳

| 증상 | 원인과 해결 |
|---|---|
| `ModuleNotFoundError: pydantic` | 시스템 파이썬으로 실행했다. `.venv`를 활성화하거나 `.venv/Scripts/python.exe`로 실행 |
| 전달받은 이미지인데 `docker compose up -d`를 했더니 비밀번호 오류로 Neo4j가 종료된다 | 그냥 `up -d`는 **개발용**(빈 DB)을 시작한다. 이미지(demo)는 `docker compose up -d neo4j-demo app` |
| `docker compose up -d neo4j-demo app`이 `pull access denied for trend-agent-demo-app`로 실패 | 이미지가 이 PC에 없다. 전달받은 파일로 `docker load -i trend-agent-demo-images-<amd64 또는 arm64>.tar.gz`를 먼저 한다 (Docker Hub에는 없다) |
| demo 시작이 `Bind for 127.0.0.1:7687 failed: port is already allocated`로 실패 | 개발용 Neo4j가 같은 포트를 쓰고 있다. `docker compose stop neo4j` 하거나 `DEMO_BOLT_PORT`, `DEMO_HTTP_PORT`를 바꾼다 |
| 개발용 컨테이너가 시작 직후 종료되거나 `Restarting`을 반복한다 (`docker compose ps`) | `.env`의 `NEO4J_PASSWORD`가 비었거나 없다 (`Invalid value for NEO4J_AUTH: 'neo4j/'`). `docker logs trend-agent-neo4j`에서 확인 |
| 컨테이너가 `Restarting`을 반복하고 `healthy`가 되지 않는다. 로그에는 오류 없이 "Started."만 반복된다 | Docker Desktop 메모리 부족(OOM). `docker inspect -f '{{.State.OOMKilled}}' trend-agent-neo4j`가 `true`면 확정이다. Docker Desktop 메모리를 4GB 이상으로 올리거나(Settings → Resources) 다른 컨테이너를 멈춘다 (`docker stats --no-stream`으로 확인) |
| Neo4j 기본 포트가 이미 사용 중 | 다른 Neo4j가 쓰고 있다. `.env`에 `NEO4J_BOLT_PORT`, `NEO4J_HTTP_PORT`를 정하고 `NEO4J_URI` 포트도 맞춘다 |
| `check_neo4j.py` 실패 (인증) | 이미 만든 볼륨은 처음 비밀번호를 기억한다. 비밀번호를 바꿨다면 `docker compose down -v` 후 다시 시작 (그래프 삭제) |
| 검색이 `no_results` | 그래프가 비었거나 기사가 없다. `load_graph.py` 출력의 "추출 결과 …" 줄과 Style 개수를 확인 |
| 한글이 깨진다 (Windows) | `PYTHONIOENCODING=utf-8`을 앞에 붙인다 (출력만의 문제, 데이터는 정상) |
| 이미지 방식에서 포트 충돌 | `DEMO_BOLT_PORT=7688 DEMO_HTTP_PORT=7475 docker compose up -d neo4j-demo app` |

## 개발 규칙 (GitHub)

> 브랜치 정책·리뷰·CI 규칙은 저장소 현황 확인 후 확정 예정(TBD)입니다. 아래는 제안안입니다.

- **브랜치**: `main`, `develop`, `feature/*`, `fix/*`, `docs/*`
- **Issue**: 배경, 요구사항, 완료 기준, 라벨/우선순위를 작성합니다. (`.github/ISSUE_TEMPLATE`)
- **Commit**: 변경 의도가 드러나게 작성합니다. (세부 convention TBD)
- **PR**: 연결 Issue, 변경 요약, 테스트, 위험/스크린샷을 작성합니다. (`.github/PULL_REQUEST_TEMPLATE.md`)
- **Definition of Done**: 수용 기준 충족 · 관련 테스트/문서 갱신 · 리뷰 및 Issue/PR 연결
- **충돌 해결**: 최신 기본 브랜치를 가져와 작업 브랜치에서 해결하고, 관련 테스트 후 PR에 해결 내용을 기록합니다.
- **커밋하지 않는 것**: `.env`, `data/`, 정답 라벨·검수 문서(`eval/datasets/**/labels`, `review`, `*.zip`), 이미지 재료(`docker/demo/dump`, `out`). Vogue 인용이 들어 있거나 비밀 값입니다.

## 다른 에이전트와의 연동

이 레포는 **이 레포 안의 문서만 기준**으로 스키마를 정의하며, 상위 Project Workflow의 공통 응답 envelope·클래스명(`PlanningRequest`, `TrendResult` 등)은 현재 사용하지 않습니다. 연결이 필요해지면 그때 매핑표를 만듭니다. 04 stock-agent로 보내는 필드(`trend_colors`, `trend_categories`)도 아직 정하지 않았고, 이 레포의 스키마를 나중에 전달합니다. 근거가 없는 값은 추정하지 않고 미확인으로 표시하는 원칙은 동일합니다. 이 에이전트의 호출 진입점(API)은 아직 없다 ([docs/pipeline.md](docs/pipeline.md) F-7).

## 로드맵

1. ✅ 데이터 계약(스키마·표준 어휘·별칭 사전)과 Product 02·03 정규화
2. ✅ 정답 라벨 5건으로 임시 Neo4j 그래프 구축(전달용 이미지에 들어 있음), 검색·점수·결과 출력
2-1. ✅ LLM 호출 모듈, 기사 추출·검증·비교 도구 (OpenAI `gpt-4o-mini`)
3. **다음 (설계: [docs/pipeline.md](docs/pipeline.md))**: ① **LLM 규격화 마무리** — 150건 배치 실행, 시즌 후보 대조, Item 단위 제외
4. ② **기사 workflow** — 주 1회 자동 수집 → 추출 → Style 해소(이름 일치 → 임베딩 임계값 → 신규) → 적재, 스케줄러(Airflow 추천), 그래프를 LLM 결과로 재구축
5. 후속: Pantone 공식 자료, LLM 기반 Style 병합 판정, 평가셋 확대, 가중치·점수 튜닝

## 문서와 한계

- 개발 명세와 진행 상태: [docs/spec.md](docs/spec.md) (다른 Agent에 넘길 때 가장 먼저 읽는 문서)
- **자동화 파이프라인 설계와 작업 계획(이어받는 개발자용)**: [docs/pipeline.md](docs/pipeline.md)
- 데이터 계약(스키마, 표준 어휘, 별칭 사전): [docs/schemas/README.md](docs/schemas/README.md). 문서 이름이 코드 모듈 이름이다.
- 표준 어휘의 근거: K-Fashion 가이드라인 ([원문 발췌](docs/schemas/sources/kfashion_guideline.md))
- 데이터셋 설명: [docs/Dataset-Description.md](docs/Dataset-Description.md)
- 평가 데이터셋·라벨링 기준: [eval/README.md](eval/README.md)
- **한계와 향후 과제**: 전체 요약은 [docs/spec.md §15](docs/spec.md), 스키마별 상세는 각 스키마 문서 맨 아래 표에 있다. 주요 한계는 다음과 같다.
  - 크롤링 허용 여부(robots.txt·약관)와 Vogue 이미지 사용 권한을 검토하지 않았다.
  - 정답 라벨이 5건(gold)뿐이고, 점수 식·가중치는 정답으로 검증하지 않은 임의 시작값이다. 정보가 적은 Product가 만점을 받고 하분류 보너스(α 0.1)가 작은 약점이 첫 실행에서 보였다 ([spec §15 K-9](docs/spec.md)).
  - 상품은 무신사 한 곳이고 브랜드·가격·URL이 없어 **구매 가능한 상품 추천이 아니라 기획 참고용**이다. 03은 착장 속 아이템이라 구매할 수 없다.
