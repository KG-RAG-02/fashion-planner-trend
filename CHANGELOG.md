# 변경 기록

이 프로젝트의 주요 변경을 적는다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를,
버전은 [유의적 버전](https://semver.org/lang/ko/)을 따른다.

## [1.0.0] - {{RELEASE_DATE}}

첫 공개 버전.

### 추가
- HTTP 서비스: `GET /health`, 인증이 필요한 `POST /v1/agents/trend/runs`(TrendQuery → TrendAnalysisResult,
  전송 이름 `trend-handoff`, `schema_version` `1.0.0`). local(루프백)·service 바인드 모드.
- 기사·트렌드(Style)·옷(Item)·상품·공유 속성 어휘로 이루어진 Neo4j 그래프와, 다시 실행해도 같은 결과를 내는 적재기.
- 트렌드 검색: 시즌·이름 일치, 카테고리·속성 필터, 속성 노드에서 출발하는 후보 탐색, 결정론적 가중 점수,
  안정 근거 id, 이유를 적는 `limitations`.
- LLM 추출 파이프라인: 엄격한 JSON 스키마 출력, 별칭 재매핑, 검증(V1~V6), 1회 재시도, 검토 대기열, 로컬 응답 캐시.
  프롬프트는 `article-extraction` 하나이고 보낸 본문의 sha256을 함께 기록한다.
- 공개 상품 데이터셋 두 개의 정규화: 별칭 사전, 키워드 오탐 방지, 여러 벌 세트 분해.
- 프로젝트를 밝히는 User-Agent와 robots.txt 확인을 갖춘 기사 수집기(교육·연구용 예시).
- 오프라인 stub용 합성 그래프와 적재 스크립트.
- 문서: 아키텍처, 계약, 그래프 스키마, 추출, 어휘, 라벨링 가이드, 데이터 카드, 문제 해결, 설계 결정, 다이어그램.

### 참고
- 공개 전 히스토리 정리 때 커밋 메시지의 옛 이름(VOGUE, P06~P08, 옛 레포·패키지 이름)을 현재 이름이나 중립 표현으로 바꿨다. 과거 커밋의 코드와 메시지 속 이름이 다를 수 있다.
