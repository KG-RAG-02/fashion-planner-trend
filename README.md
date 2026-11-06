# Trend Analysis Agent

패션 MD가 신상품을 기획할 때 **"요즘 어떤 스타일이 유행하고, 우리가 살 수 있는 비슷한 상품은 무엇인가?"** 에 답해 주는 에이전트입니다.
패션 매거진 기사에서 트렌드를 추출해 Knowledge Graph(Neo4j)로 만들고, 실제 브랜드 상품과 속성(색상·소재·패턴·디테일 등)으로 연결해 근거와 함께 보여줍니다.

> 조직: [KG-RAG-02](https://github.com/KG-RAG-02) · 상태: 초기 세팅 (폴더 구조만 준비됨)

## 이 레포의 역할

전체 프로젝트(VOGUE)는 5개 에이전트로 나뉘며, 이 레포는 그중 **1번 에이전트**를 담당합니다.

```
사용자 쿼리
   ↓
① 쿼리 전처리 + 트렌드 분석   ← 이 레포
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
| 쿼리 전처리 | 사용자 원문 쿼리 | `PlanningRequest` — 시즌, 트렌드명(고프코어, 발레코어 등), 상품군(숏패딩 등), 요청 속성(상의, 핑크색 등) |
| 트렌드 분석 (GraphRAG) | `PlanningRequest` | `TrendResult` — 트렌드명·정의·설명, 속성, 이미지(선택), 타 브랜드 유사 상품 목록(브랜드명, 이미지, 속성) |

예시 질의: *"2027 SS 발레코어 스타일에서 유행하는 블라우스의 특징을 분석하고, 비슷한 H&M 상품을 찾아줘."*

## 동작 방식

**1) 데이터 구축 (사전 작업)**

```
패션 기사 크롤링 (Vogue, Elle 등)
 → LLM으로 Style / Item / 속성 추출 (Structured JSON)
 → 속성값 정규화·검증
 → Neo4j Knowledge Graph 적재
 → 브랜드별 실제 Product 데이터 연결
```

**2) 사용자 질의 처리**

```
질의 분석 → Style 탐색 → Item·속성 추출 → Product 후보 검색 → 속성 일치도 계산 → 결과 반환
```

### 핵심 개념

- **Style**: 기사에서 추출한 트렌드/스타일 (예: 발레코어)
- **Item**: 스타일을 구현하는 기준 아이템 (예: 핑크 시폰 리본 블라우스)
- **Product**: 브랜드에서 실제로 판매하는 상품
- Item과 Product는 **같은 속성 노드(Category, Color, Material, Pattern, Detail)를 공유**하며, 공유하는 속성으로 유사 상품을 찾고 일치도를 계산합니다.

```
Style -[HAS_ITEM]-> Item -[HAS_COLOR / HAS_MATERIAL / ...]-> 속성 노드 <-[...]- Product -[BELONGS_TO]-> Brand
```

### 설계 원칙

- 기사에 **명시된 속성만** 추출하고, 없으면 `null`로 둡니다. (추정 금지)
- Pattern(프린트) / Detail(자수 등 장식) / Styling(착용 방식)을 구분합니다.
- 추천 결과에는 **일치·불일치·미확인 속성**과 기사·상품 출처를 함께 제공합니다.

## 폴더 구조

```
trend-analysis-agent/
├── src/trend_agent/
│   ├── schemas/               # PlanningRequest, TrendResult 등 입출력 스키마
│   ├── query_preprocessing/   # 사용자 쿼리 분석 및 검색 조건 추출
│   ├── ingestion/             # 데이터 구축 파이프라인
│   │   ├── crawler/           #   기사·상품 수집
│   │   ├── extraction/        #   LLM 기반 Style/Item/속성 추출
│   │   └── normalization/     #   속성값 정규화·검증
│   ├── graph/                 # Neo4j 스키마, 적재, 쿼리
│   ├── retrieval/             # Style/Item 검색, Product 후보 탐색, 일치도 계산
│   └── api/                   # 에이전트 진입점 (다른 에이전트가 호출)
├── data/
│   ├── raw/                   # 수집 원본 (git 제외)
│   ├── processed/             # 가공 결과 (git 제외)
│   └── sample/                # 테스트용 샘플
├── configs/                   # 설정 파일
├── docs/                      # 문서
├── scripts/                   # 실행 스크립트
├── tests/
├── .github/                   # Issue / PR 템플릿
├── .env.example               # 환경변수 예시
├── requirements.txt
└── README.md
```

## 시작하기

```bash
# 1. clone
git clone https://github.com/KG-RAG-02/trend-analysis-agent.git
cd trend-analysis-agent

# 2. 가상환경 (Python 3.12)
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 3. 의존성 설치
pip install -r requirements.txt

# 4. 환경변수
cp .env.example .env            # Neo4j, LLM 키 입력
```

## 개발 규칙 (GitHub)

> 브랜치 정책·리뷰·CI 규칙은 저장소 현황 확인 후 확정 예정(TBD)입니다. 아래는 제안안입니다.

- **브랜치**: `main`, `develop`, `feature/*`, `fix/*`, `docs/*`
- **Issue**: 배경, 요구사항, 완료 기준, 라벨/우선순위를 작성합니다. (`.github/ISSUE_TEMPLATE`)
- **Commit**: 변경 의도가 드러나게 작성합니다. (세부 convention TBD)
- **PR**: 연결 Issue, 변경 요약, 테스트, 위험/스크린샷을 작성합니다. (`.github/PULL_REQUEST_TEMPLATE.md`)
- **Definition of Done**: 수용 기준 충족 · 관련 테스트/문서 갱신 · 리뷰 및 Issue/PR 연결
- **충돌 해결**: 최신 기본 브랜치를 가져와 작업 브랜치에서 해결하고, 관련 테스트 후 PR에 해결 내용을 기록합니다.

## 공통 계약 (다른 에이전트와의 연동)

모든 에이전트는 동일한 응답 envelope(`schema_version`, `request_id`, `agent_id`, `status`, `summary`, `findings`, `evidence`, `limitations`, `errors`)를 사용합니다.
`status`는 `ok / partial / no_results / error` 중 하나이며, 근거가 없는 값은 추정하지 않고 미확보로 표시합니다.
공통 계약 파일은 5개 레포에 동일한 경로·내용·버전으로 포함하며, 위치는 확정되면 이 문서에 추가합니다.

## 로드맵

1. 기사에서 Style·Item 추출
2. 속성 스키마 구조화 및 정규화
3. Neo4j Knowledge Graph 구축
4. Graph Traversal 기반 Product 후보 검색
5. 속성 일치도 계산 및 근거 반환
