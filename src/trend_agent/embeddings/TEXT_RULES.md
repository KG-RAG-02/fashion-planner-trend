# C1 임베딩 입력 규칙 c1-v2

구현은 `text.py`, 회귀 검증은 `tests/test_embeddings.py`다. 원본 JSONL·추출·라벨·term_map을 읽기만 하며 임베딩에 넣는 문장만 정리한다. 문장은 UTF-8 SHA256으로 식별한다. 원문에 없는 속성은 채우지 않는다.

## 공통 정리

HTML 엔티티(`&#039;`, `&amp;` 등)를 해제하고 br/span 등의 HTML 태그를 공백으로 바꾼다. 이모지·장식 기호(Unicode So/Sk, variation selector, joiner, keycap·bullet)를 제거한 뒤 Unicode NFKC와 공백 정리를 적용한다. ™가 NFKC로 TM이 되기 전에 제거한다. 패션 의미가 있는 `/`, `+`, `-`, 괄호와 근사 표시 `~`는 유지한다. null·빈 필드는 구분자와 함께 생략한다. 상품 ID·원문 evidence 인용·이미지 URL/ref/status/scope·가격·성별·시즌은 문장에 넣지 않는다.

## 02 상품명 정제

상품명 정리는 임베딩 이름에만 적용한다. 다음 순서로 처리하고 제거 횟수와 영향받은 상품 수를 따로 센다.

1. `[]`, `()`, `{}` 안의 판촉·배송 문구를 제거한다. 예: `[무료배송]`, `[무료반품]`, `[단독]`, `(당일발송)`. `[단독 와이드]`는 `와이드`가 남는다. `[SET]`·`(빈티지)` 같은 패션 의미는 보존한다.
2. FREE/ONE SIZE, S/M/L/XL처럼 크기가 증가하는 선택지, 단독 사이즈 토큰과 `사이즈: 90/95/100`을 제거한다. `L/S`, `S/S` 소매 표기, `오버사이즈`·`오버 사이즈`·`빅사이즈`는 보존한다.
3. 숫자가 4개 이상 있는 ASCII 품번 토큰을 제거한다. `KLTW0153`, `Q321-3152-1MG`, `[5597]`은 제거하지만 `MA-1`, `1980년대`, `1000g` 같은 짧은 모델명·시대·단위 표기는 보존한다.
4. 품번에 붙은 `_WHITE`, `Polo_`, `BODYSUIT_` 같은 색·의류 단어는 보존한다. 같은 색의 중복 코드(BK/BLK/BLACK/블랙 등)는 대표 표기 1개를 남긴다. 색상이 다른 이름과 원래의 정규화 색 관계는 유지한다. `_` 구분자와 제거 후 남은 빈 괄호·구두점은 정리한다.

기본 보호어는 와이드·오버핏·크롭·빈티지·셋업·투피스 등이다. `$SHARED/keywords/inventory.csv`의 keyword/text/term 표기도 보호어로 더한다. 보호어가 포함된 품번 후보·사이즈 구간을 지우지 않는다. 전·후 보호어 집합 차이가 생기면 배치를 실패시킨다. 이 검사는 전체 의미 동등성을 증명하지 않으므로 `preprocess_report.md`의 다양한 규칙 조합 표본 30개를 함께 검토한다.

## 라벨별 문장

- Product: `{category_major}>{category_minor} | 색 {colors} | 소재 {materials} | 패턴 {patterns} | 디테일 {details} | 핏 {fit} | 기장 {length_body}/{length_sleeve} | {정제된 name}`. `parent_id` 또는 `split_from`가 있으면 ` | 세트 구성품 {category_minor}`를 추가한다. 부모 ID 자체는 넣지 않는다.
- Item: 같은 속성 템플릿에 `name`, `styling_raw`를 사용한다. 상품 판촉·품번 제거는 추출 Item 이름에 적용하지 않는다.
- Style: `name | aliases | definition | description`. 별칭은 중복 제거·정렬한다. description 순서를 유지하고 객체에서는 text만 읽는다.
- Term: `text | aliases`. term_map의 같은 계약 ID에 속하는 표기와 aliases를 중복 제거·정렬해 대표 표기 뒤에 붙인다. 프로필 summary·수치·evidence는 넣지 않는다.

ProductRecord는 node 스칼라와 루트의 표준 속성 관계를 읽는다. 색·소재·패턴은 value만, 디테일은 type:value만 쓴다. null value의 raw를 대체 값으로 쓰지 않는다. 관계의 approx=true 또는 node.approx의 스칼라는 `~`를 붙인다. 기장은 몸통/소매 자리를 유지한다(`기장 /반팔`). shape·sub_colors·raw_text·focus는 템플릿에 없다.

## 입력 준비·ID·증분

각 단계에서 BOARD §1·§2·USER·외부 @C1/@ALL 지시와 입력 준비 상태를 다시 확인한다.

- Product는 T2 `T2-T3-products-v2.md`와 v2 상품 파일이 모두 있어야 준비다. 실제 `musinsa_snap_all.jsonl`, `otpensource_all.jsonl`만 읽고 `otpensource_split_parents.jsonl` sidecar를 상품으로 읽지 않는다. v1은 명시적 과거 재현 옵션으로만 읽는다.
- Style/Item은 T1 `T1-T3,T4-extractions-ready.md`와 해당 prompt_version 추출 디렉터리가 있어야 준비다. 같은 기사에서는 라벨이 자동 추출 전체를 대체한다. Style ID는 `<aid>-s<n>`, Item은 `<aid>-s<n>-i<원래 index>`다. category_major=null Product/Item은 그래프와 같이 제외하고 제외 수를 기록한다.
- Term은 T3 `terms/term_map.jsonl`이 있어야 준비다. `from trend_agent.schemas.graph_schema import normalize_term, term_id`를 직접 쓴다. 주어진 id/norm과 계약 계산값이 다르면 실패한다. T3 그래프처럼 추출 Style aliases도 합치고 alias→translation→trend_word, label→extraction→manual→term_mentions→llm 우선으로 대표 표기를 고른다. 최신 profile이 recognized=false이고 profile 이외 근거가 없는 Term은 제외한다. keyword inventory는 보호어에만 쓰고 별도 Term을 만들지 않는다.

준비되지 않은 소스는 pending이고 해당 라벨은 비어 있다. 셋 다 준비되지 않으면 모델을 로드하지 않고 pending_manifest/wait_state와 전처리 규칙만 남긴다. 임베딩 중 입력 추가·삭제·해시·준비 상태가 바뀌면 기존 ready snapshot을 유지하고 중지한다.

문서 캐시 키는 `(text_sha256, model)`이고 revision·차원·prefix·max_length 설정을 검증한다. 2단계는 1단계와 같은 `BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181`, 1024차원으로 고정한다. 다른 모델로 자동 전환하지 않는다. MPS 실패 시 같은 모델을 CPU에서 재시도한다. 이전 run은 변경하지 않고 새 run에는 현재 입력 ID만 내보낸다. 인터페이스 `embed_texts`·`search`는 그대로다.
