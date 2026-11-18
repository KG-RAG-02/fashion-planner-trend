# 설계 결정 기록

이 서비스의 모양을 정한 결정을 짧게 남긴다(MADR-lite: 배경, 결정, 결과, 검토한 대안).
상태 값은 *채택*, *NNNN으로 대체*다.

| 번호 | 결정 | 상태 |
|---|---|---|
| [0001](0001-neo4j-property-graph.md) | Neo4j 속성 그래프 + Cypher 후보 검색 + 결정론적 점수 | 채택 |
| [0002](0002-extraction-model-and-prompt.md) | 기사마다 OpenAI 구조화 출력 1회, 프롬프트와 모델 기준선 고정 | 채택 |
| [0003](0003-one-style-per-article.md) | 기사마다 트렌드별 Style 노드를 따로 둔다(기사 간 자동 병합 없음) | 채택 |
| [0004](0004-approximate-values-not-scored.md) | 근사 어휘 매핑은 저장하되 후보·점수에 쓰지 않는다 | 채택 |
| [0005](0005-k-fashion-vocabulary.md) | 표준 어휘는 K-Fashion 속성 목록, 상품 데이터는 별칭 재료로만 쓴다 | 채택 |
| [0006](0006-labels-stay-out-of-graph.md) | 정답 라벨은 평가에만 쓰고 그래프에 올리지 않는다 | 채택 |
| [0007](0007-strict-season-matching.md) | 시즌 질의는 기사에 적힌 시즌만 맞춘다 | 채택 |
