# 로컬 텍스트 임베딩

C1은 로컬 BAAI/bge-m3로 1024차원 dense 벡터를 만든다. 전처리 규칙은 [TEXT_RULES.md](../src/trend_agent/embeddings/TEXT_RULES.md)다. 원본 데이터는 변경하지 않는다. 공개 `embed_texts`·`search` 인터페이스와 parquet 스키마는 1단계와 같다.

## 실행과 재개

`VOGUE`를 팀 작업 루트로 설정한 셸에서 실행한다. Python·모델은 팀이 준비한 공유 venv·HF 캐시를 사용한다.

```sh
export SHARED="$VOGUE/_shared"
export HF_HOME="$SHARED/hf_cache"
cd "$VOGUE/worktrees/c1"
export PYTHONPATH="$PWD/src"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
"$VOGUE/_venv/bin/python" -m trend_agent.embeddings.batch --inventory-only
"$VOGUE/_venv/bin/python" -m trend_agent.embeddings.batch --product-version v2 --extraction-version extract-v2 --batch-size 8
```

재개는 같은 명령이다. 준비된 T2 v2·T1 추출·T3 term_map만 읽고, 아직 없는 소스는 manifest.pending에 기록한다. 전부 없으면 모델 없이 `pending_manifest.json`·`wait_state.json`을 쓴다. 기존 ready manifest는 마지막으로 검증 완료된 snapshot으로 유지한다. 소스가 생기면 재실행한다.

문서 캐시 SQLite 키는 `(model,text_sha)`이고 배치마다 트랜잭션으로 저장한다. 같은 문장을 공유하는 ID는 벡터 1개를 재사용한다. 변경되거나 새로 생긴 문장만 계산하며 삭제된 ID는 새 parquet에서 빠진다. 검증·인계까지 끝난 completion_state와 입력이 같으면 재계산·재게시 없이 snapshot을 검증한다. `checkpoint.json`·`run.log`에 진행률이 남는다.

2단계 모델 revision은 1단계의 `5617a9f61b028005a4858fdac845db406aefb181`로 고정한다. CPU/MPS 선택과 MPS→CPU 재시도는 같은 모델을 유지한다. bge-m3 로딩 실패 시 체크포인트를 남기고 중지한다. 외부 임베딩 API를 호출하지 않는다. CPU 스레드는 `C1_TORCH_THREADS`(기본 4), 질의 배치는 `C1_EMBED_BATCH_SIZE`(기본 8)로 설정한다.

## 산출물과 검증

루트는 `$SHARED/embeddings`다.

- `runs/<run_id>/manifest.json`: ready 상태, c1-v2, 입력 파일별 SHA256, v2 합성 SHA, prompt_version, term_map SHA, pending, 라벨 건수, 캐시 재사용·계산 수, 시간·장치·검증 결과.
- `{product,item,style,term}_BAAI--bge-m3_1024.parquet`: `id:string, text_sha:string, vector:fixed_size_list<float32>[1024]`.
- `text_index.parquet`: `text_sha:string, text:string`.
- `preprocess_report.md`: 규칙별 적용 횟수·영향받은 상품 수, 정제 전·후 30개 표본, 보호어 손실 검사.
- `probe_report.md`·`probe_comparison.json`: 20개 질의 × 4개 라벨의 상위 5, 같은 10개 질의의 v1/v2 상위 5 비교와 ID 증감·문장 변경 수. 기본 v1 run은 `20261107T171500Z-9e039290`이며 `--baseline-run <id>`로 지정할 수 있다.
- `final_validation.json`·`completion_state.json`·`pytest.log`: 최종 공개 인터페이스 검증과 인계 완료 근거.
- `document_cache.sqlite3`: 재개용 문서 벡터 캐시. 과거 문장도 보관하되 새 snapshot에는 현재 입력만 쓴다.

새 immutable run의 parquet을 다시 열어 입력 ID·SHA·건수, 고정 float32 차원, NaN/Inf 0, L2 norm, 자기검색 cosine 1.0을 확인한다. 원본 파일 집합·해시와 BOARD 계약·결정·대상 지시도 단계마다 확인한다. 검증 후 manifest를 원자적으로 게시한다. 루트 parquet·리포트는 최신 snapshot을 가리키는 symlink다. 검색은 manifest의 immutable 경로를 읽으며 1단계 run은 보존한다.

## 질의와 검색

```python
from trend_agent.embeddings import embed_texts, search

query_vec = embed_texts(["보라색 니트"])[0]
hits = search("Product", query_vec, k=5)
# [(node_id, cosine_score), ...]
```

기본 디렉터리는 `$SHARED/embeddings`, 별도 snapshot 루트는 `VOGUE_EMBEDDINGS_DIR`로 지정한다. label은 Product/Item/Style/Term 대소문자를 구분하지 않는다. 유한한 1024차원 0이 아닌 질의 벡터를 사용한다. 점수는 numpy cosine이고 동점은 ID 오름차순이다. 파일·manifest 부재와 차원 오류는 예외, 존재하는 빈 라벨은 빈 목록이다. import만으로 모델을 로드하지 않는다.

프로브 비교는 입력 변화와 전처리 효과를 함께 보여준다. 관련성 정답 평가나 τ/δ 보정 근거로 단독 사용하지 않는다. 근사 속성의 1차 점수 제외·중복 상품과 이미지 제한·검색 품질 평가는 T3/T4가 담당한다.

## 테스트

```sh
cd "$VOGUE/worktrees/c1"
PYTHONPATH="$PWD/src" "$VOGUE/_venv/bin/python" -m pytest -q
```

fixture encoder로 공통 정리·상품명 규칙·보호어·원본 불변·세트 구성품·준비 상태 8가지 조합·Term ID/별칭·증분/삭제·캐시 재개·모델 고정·입력 변경 차단·검색 오류를 검증한다. fixture 벡터는 임시 디렉터리에만 쓰며 실제 모델 품질로 보고하지 않는다. 실데이터·벡터·캐시·인용은 git에 넣지 않는다.
