# Pre-review DB 스키마 안내

2026-09-03 · 대상 `backend/app/db/schema.sql` · PostgreSQL 15+ / pgvector 0.7+

원본은 **`backend/app/db/schema.sql` 하나뿐이다.** Supabase 의 `sims` 스키마는 이 파일을 그대로 올린 것이고, 2026-09-03 기준으로 **테이블 37개가 파일과 실제 DB에서 정확히 일치**한다. 스키마를 바꿀 일이 생기면 DB를 먼저 고치지 말고 이 파일을 고친 뒤 반영한다.

테이블별 용도는 파일 안 `10.6 테이블 용도` 절에 `COMMENT ON TABLE` 로 달려 있다. 이 문서는 그 개별 설명이 답해주지 않는 것 — **어느 테이블이 어디에 매달리는지** — 를 정리한다.

## 1. 한눈에

| | |
|---|---|
| 테이블 | 37개 |
| 외래키 | 57개 |
| 인덱스 | 57개 |
| 트리거 | 12개 |
| 확장 | `vector`, `citext`, `pgcrypto`, `uuid-ossp` |

파일은 9개 구역으로 나뉜다.

| 구역 | 내용 | 테이블 수 |
|---|---|---|
| 1 | 인증과 검사 소유권 | 3 |
| 2 | 파일 자산과 삭제 아웃박스 | 3 |
| 3 | 파싱·OCR·청크 | 4 |
| 4 | 서식·추출 필드·누락 점검 | 6 |
| 5 | 데이터 소스·동기화·공고 버전 | 6 |
| 6 | 보관 전용 과거 자료 | 3 |
| 7 | 임베딩 모델·프로파일·벡터 | 5 |
| 8 | 검색 실행·후보·근거 | 3 |
| 9 | 불변 보고서·PDF 출력·결과 후 대화 | 4 |

## 2. 두 개의 축

이 스키마를 이해하는 가장 빠른 길은 **축이 둘이라는 것**을 먼저 보는 것이다.

```
[사용자 축]  app_user → inspection_case → 업로드 → 파싱 → 추출 → CPL 점검
                              |
                              +→ 벡터 → 검색 → 후보 → 보고서 → 대화
                                          ↑
[공고 축]    data_source → announcement → announcement_version → 벡터
                    ↑                              |
               api_sync_run                        +→ 첨부, 세부기간
```

**왼쪽 축은 사용자가 올린 요청서 하나를 따라간다.** 검사 건(`inspection_case`)이 중심이고, 파일·파싱·추출·점검·보고서·대화가 전부 여기 매달린다. 사용자가 이력에서 보는 한 줄이 이 행 하나다.

**오른쪽 축은 외부에서 긁어온 공고를 따라간다.** 사용자와 무관하게 동기화로 쌓인다.

**두 축이 만나는 곳이 `retrieval_run` 이다.** 요청서 벡터를 질의로, 공고 벡터를 대상으로 검색해서 후보를 뽑는다.

## 3. 검사 1건이 만드는 행들

업로드 한 번에 아래가 순서대로 생긴다. 화살표는 외래키 방향이 아니라 **생성 순서**다.

```
inspection_case          검사 건 (상태: UPLOADED → ... → COMPLETED)
  file_asset             올린 파일의 메타 (asset_scope = USER)
  uploaded_document      그 파일이 이 검사의 요청서라는 연결      [1:1]
  document_parse_run     파싱 시도 1회 (재시도는 attempt_no 로 누적)
    document_chunk_set   청킹 전략별 묶음
      document_chunk     조각들 (근거 인용 단위)
  request_extraction     항목 값 추출 실행                      [1:1]
    request_field_value  뽑아낸 값들 (+ 문서상 위치)
  missing_check_run      CPL 점검 실행
    missing_check_item   항목별 판정 (있음/누락/해당없음/확인필요/파싱실패)
  inspection_embedding   요청서 벡터
  retrieval_run          유사 공고 검색 실행
    retrieval_candidate  Top-N 후보 (+ 기관·기간 비교 결과)
      candidate_evidence 후보별 근거 조각
  inspection_report      결과 JSON 전체                         [1:1]
    output_artifact      PDF 등 출력물                          [1:1]
  chat_session           질의응답 세션                          [1:1]
    chat_message         메시지들
```

`[1:1]` 표시가 붙은 5개는 검사 건당 정확히 한 행이다 (`UNIQUE` 로 강제).

## 4. 알아두면 덜 헤매는 것

**공고는 신원과 내용이 나뉘어 있다.** `announcement` 는 기관이 준 `pblanc_id` 하나에 행 하나인 불변 신원이고, 실제 내용은 전부 `announcement_version` 에 있다. 내용이 바뀌면 새 버전이 생기고 이전 버전은 `is_current = false` 가 된다. 그래서 **공고를 조회할 때는 거의 항상 `announcement_version` 을 본다.** 현재 버전만 보는 뷰 `v_current_announcement` 와, 접수 중인 것만 거르는 `v_searchable_announcement` 가 준비돼 있다.

**파일은 두 종류다.** `file_asset.asset_scope` 가 `USER` 면 사용자가 올린 것이라 소유자와 검사 건이 반드시 있고, `SHARED` 면 공고 첨부나 서식처럼 전체가 공유하는 것이라 둘 다 비어 있다. 이 규칙은 `CHECK` 제약으로 강제돼 한쪽만 채우는 실수가 애초에 막힌다.

**파일을 지우면 흔적이 남는다.** `file_asset` 을 삭제하면 트리거가 `object_delete_outbox` 에 storage_key 를 넣고, 그 키는 **영구히 재사용이 금지된다** (다시 쓰려 하면 트리거가 예외를 던진다). 실제 저장소 삭제는 아웃박스를 읽는 쪽이 처리한다.

**화면에 보일 이름은 `app_user.display_name` 이다.** 비어 있으면 이메일의 `@` 앞부분을 대신 쓴다. 회원가입 API 가 없어 이름 없는 계정이 계속 생기므로 폴백을 둔다.

**비밀번호를 바꾸면 두 가지가 자동으로 일어난다.** 트리거가 `password_changed_at` 을 올리고 `password_change_history` 에 행을 남긴다. 앱 코드가 하는 일이 아니다. 토큰은 발급 당시의 `password_changed_at` 을 담고 있어서, 이 값이 바뀌면 그 전에 발급된 토큰이 전부 무효가 된다.

**검색 결과는 재현 가능하게 고정된다.** `retrieval_run.corpus_snapshot_at` 이 어느 시점의 공고 집합을 봤는지를 박아둬서, 나중에 공고가 늘어도 과거 결과가 흔들리지 않는다.

## 5. 벡터 구역 — 다른 작업과 겹치는 곳

`vector` 확장을 쓰는 테이블은 셋이다.

| 테이블 | 무엇의 벡터인가 | 매달린 곳 |
|---|---|---|
| `inspection_embedding` | 요청서 1건 | `inspection_case` |
| `announcement_embedding` | 공고 버전 1건 | `announcement_version` |
| `chunk_embedding` | 문서 조각 1개 | `document_chunk` |

셋 다 `embedding_profile_id` 를 함께 들고 있다. **이게 이 설계의 핵심이다.**

```
embedding_model      모델 자체 (provider, model_name, dimension, distance_metric)
       ↓
embedding_profile    모델 + 전처리 버전 + 대상 필드 + 청킹 프로파일
       ↓
세 벡터 테이블       (벡터, 입력 텍스트, 프로파일 id)
```

**같은 프로파일로 만든 벡터끼리만 비교가 성립한다.** 모델이 다르거나 전처리가 다르면 거리 값에 의미가 없기 때문에, 프로파일 id 를 함께 저장해두고 검색할 때 같은 프로파일로 거른다. 모델을 바꿔도 과거 벡터를 지우지 않고 새 프로파일로 나란히 쌓을 수 있다.

`embedding` 컬럼은 차원을 고정하지 않은 `vector` 타입이고, **트리거가 넣을 때마다 `embedding_model.dimension` 과 맞는지 검사한다.** 차원이 다른 모델을 같은 테이블에 섞어도 잘못된 행이 들어가지 않는다.

### HNSW 인덱스는 일부러 안 만들어져 있다

`schema.sql` 1029행에 이유와 템플릿이 적혀 있다. 요약하면, 모델 차원과 운영 프로파일이 정해지기 전에 인덱스를 만들면 쓸모가 없다. 정해진 뒤에 **프로파일별 부분 인덱스**를 만든다.

```sql
CREATE INDEX ix_announcement_embedding_profile_1_hnsw
ON sims.announcement_embedding
USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
WHERE embedding_profile_id = 1;
```

`ORDER BY` 식이 인덱스의 캐스트 식과 정확히 같아야 인덱스를 탄다. 질의 벡터는 바인드 파라미터나 스칼라 서브쿼리로 넘기고, JOIN 컬럼끼리의 거리 식으로 쓰면 인덱스를 못 쓴다.

### 다른 벡터 스키마와 합칠 때 볼 것

겹치는 개념은 이 셋일 가능성이 높다.

| 개념 | 이 스키마에서 | 확인할 것 |
|---|---|---|
| 모델 메타 | `embedding_model` | 차원·거리척도를 어디서 관리하는지. 두 곳에 있으면 하나로 |
| 벡터 저장 | 위 세 테이블 | 무엇의 벡터인지가 다르면 합치면 안 된다 |
| 청크 | `document_chunk` | 조각 단위와 위치 표현이 같은지 |

**합치기 전에 확인할 것 하나.** 여기 세 벡터 테이블은 각각 다른 것의 벡터다 (요청서 / 공고 / 문서 조각). 하나로 합치면 어느 것의 벡터인지를 구분하는 컬럼이 새로 필요해지고, 외래키로 강제되던 무결성이 사라진다. **테이블이 셋인 것은 중복이 아니라 대상이 셋이기 때문이다.**

## 6. 현재 적재 상태

2026-09-03 기준 `announcement` 6건. 대량 동기화 전이다.

## 7. 스키마를 바꿀 때

1. `backend/app/db/schema.sql` 을 고친다.
2. 테이블을 추가했으면 `10.6` 절에 `COMMENT ON TABLE` 도 같이 단다.
3. 한 객체에 `COMMENT` 는 하나뿐이다. 이미 `10.5` 에 설계 근거 주석이 있는 4개 테이블(`password_change_history`, `announcement_version`, `object_delete_outbox`, `missing_check_run`)에 다시 달면 기존 설명이 지워진다.
4. 반영 후 파일과 실제 DB의 테이블 목록이 일치하는지 확인한다.
