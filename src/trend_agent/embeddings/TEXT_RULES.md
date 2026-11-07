# C1 표준 텍스트 규칙 c1-v1

이 규칙의 구현은 `text.py`다. 원문에 없는 속성을 채우지 않는다. UTF-8 텍스트는 Unicode NFC와 연속 공백 1개로 정리하고 SHA256을 계산한다. `null`, 빈 문자열·목록은 생략한다. 상품 id·이미지·가격·성별·시즌·인용 출처는 임베딩 문장에 넣지 않는다.

- Product: `{category_major}>{category_minor} | 색 {colors} | 소재 {materials} | 패턴 {patterns} | 디테일 {details} | 핏 {fit} | 기장 {length_body}/{length_sleeve} | {name}`.
- Item: Product 문장에 ` | {styling_raw}`를 붙인다.
- Style: `name | aliases | definition | description`. 별칭은 중복 제거·정렬하고 description은 원래 순서를 유지한다. description 객체에서는 `text`만 사용하고 evidence 인용은 넣지 않는다.
- Term: `text`. keyword inventory에서는 `text`, `keyword`, `term` 열 중 첫 번째로 존재하는 열을 사용한다.

ProductRecord는 `node`의 스칼라와 레코드 루트의 관계 목록을 함께 읽는다. Item은 추출 JSON의 평평한 구조를 읽는다. 색·소재·패턴은 표준 `value`만, 디테일은 `type:value`만 사용한다. `value:null`의 raw를 표준 값으로 대체하지 않는다. 목록은 문자열 기준 중복 제거 후 정렬한다. 관계의 `approx:true`, 또는 node.approx에 이름이 있는 스칼라는 값 앞에 `~`를 붙인다. 근사 표시는 의미 검색 참고용이며 1차 필터·점수의 사용 허가가 아니다.

카테고리는 있는 값만 `>`로 잇는다. 기장은 몸통/소매 위치를 유지한다 (`기장 /반팔`, `기장 미니/`). 빈 전체 필드는 파이프 구분자와 함께 생략한다. `shape`, `sub_colors`, `raw_text`, focus 가중치는 지정 템플릿에 없으므로 추가하지 않는다.

입력은 `data/processed/v2/*.jsonl`이 있으면 v2, 없으면 v1이다. 추출은 `--extraction-version` 지정 또는 자연 정렬상 가장 최신 디렉터리를 사용한다. 라벨은 같은 article_id의 자동 추출 전체를 대체한다. 미지원 category_major=null Product/Item은 그래프 적재와 동일하게 제외한다. Style id는 `<article_id>-s<0-based index>`, Item id는 `<style_id>-i<original index>`로 graph/loader.py와 일치시킨다. 같은 id에 상충하는 텍스트는 실패한다.

Term map은 명시적 `(id,text)` 또는 T3 제안의 `(norm,term)`을 받는다. 후자는 `id=term:<norm>`, `text=term`으로 변환한다. 정규화 알고리즘을 C1에 복제하지 않으며 T3가 기록한 norm을 그대로 쓴다. 같은 Term id에 상충하는 표기 텍스트가 있으면 중지하고 입력 정리를 요청한다. keyword inventory도 명시 id 또는 norm을 우선 사용한다. 이 둘이 없는 키워드만 `keyword:<text_sha>`로 구분하며 그래프 노드 id가 아님을 manifest에 기록한다. 어휘가 없는 경우 빈 Term parquet을 만들되 미존재 사유·건수를 남긴다.

모델 카드 확인(2026-11-08 KST): [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3/raw/main/README.md)의 `license: mit`, dense 1024차원, 최대 8192 토큰과 질의 prefix 불필요를 확인했다. [intfloat/multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large)는 MIT, 1024차원, 최대 512 토큰이며 문서 `passage: `·질의 `query: `를 적용한다. Prefix는 text_sha에 포함하지 않으며 문서 캐시와 질의 인코딩을 분리한다. 실행 시 모델 revision과 토큰 절단 건수는 manifest에 기록한다.
