# RunPod Gemma Request Profile 실험 결과와 개선 방향

- 작성일: 2026-09-09
- 대상 브랜치: `이동욱`
- 범위: 요청서 파싱 이후 Request Profile 생성 단계와 백엔드 E2E 영향
- 기준 문서: `Pre-review_백엔드_코드베이스_재설계_초안_v0.2_260907.md`

## 1. 결론

백엔드의 내부 배선은 다음 경로까지 연결되어 있다.

```text
업로드 → Storage → DB 작업 큐 → 파서 → Common IR → Request Profile
       → CPL → FIT → Retrieval/SIM → 결과 DB/RPC → PDF → 다운로드
```

Fake 외부 서비스와 실제 PostgreSQL을 사용한 회귀 테스트는 통과한다. 그러나 실제 RunPod LLM과 실제 Supabase를 모두 사용한 단일 백엔드 E2E는 아직 완료되지 않았다.

현재 가장 먼저 드러난 외부 E2E 병목은 Request Profile 생성이다. Gemma 12B가 현재 입력과 출력 계약을 처리할 수는 있지만, 실행 시간과 서버 검증 통과 여부가 안정적이지 않다.

따라서 현재 상태를 다음과 같이 구분해야 한다.

| 구분 | 상태 |
| --- | --- |
| 백엔드 내부 코드 배선 | 완료에 가까움 |
| Fake + 실제 PostgreSQL 회귀 | 통과 |
| 실제 RunPod Request Profile 생성 | 일부 성공, 재현 안정성 부족 |
| 실제 요청서 한 건의 업로드→PDF 전체 E2E | 미완료 |
| 실제 Supabase Storage/JWT/RLS/PostgREST/Realtime | 미검증 |
| 실제 공고 ExistingProfile 기반 Retrieval/SIM | 준비 미완료 |

## 2. 실험 환경

### 2.1 모델과 서버

| 항목 | 값 |
| --- | --- |
| vLLM 모델 ID | `gemma-12b` |
| 원본 모델 | `google/gemma-4-12B-it-qat-w4a16-ct` |
| 양자화 표기 | QAT W4A16 |
| vLLM 버전 | `0.28.1` |
| 최대 컨텍스트 | 32,768 |
| 로컬 터널 | `http://127.0.0.1:8010/v1` |
| 온도 | `0` |

실험 시점에 `/v1/models`, `/v1/responses`, `/v1/chat/completions`, `/metrics`가 응답했다.

### 2.2 Request Profile 출력 계약

초기 측정 당시 `RequestSourceSelectionV012`의 JSON Schema 특성은 다음과 같았다.

| 항목 | 값 |
| --- | --- |
| 스키마 문자열 길이 | 약 8,157자 |
| `$defs` | 15개 |
| 최대 관찰 깊이 | 7 |

모델은 CandidatePack과 읽기 전용 문맥을 받고 사실 근거, 정량 근거, 수행체계 관계 등을 하나의 selection으로 반환한다. 서버는 그 결과를 다시 exact-span, provenance, CandidatePack 소속, 관계 컨테이너 규칙으로 검증하고 최종 프로필을 조립한다.

## 3. 최초 실험 결과

표본은 `samples/hwpx` 계열 요청서 10건이었다.

| 결과 | 건수 | 설명 |
| --- | ---: | --- |
| `request_type` 컨테이너 부재·모호성 | 4 | LLM 선택 단계에 도달하지 못함 |
| 선택 단계 도달 | 6 | CandidatePack 생성 후 모델 선택 가능 |
| 실제 LLM 호출 | 4 | 최초 측정에서는 모두 실패 |

실제 호출 상세:

| 표본 | 시간 | 관찰 결과 |
| --- | ---: | --- |
| `mockup_01` | 20.3초 | legacy anchor에 `source_block_id`와 `anchor_text`가 함께 필요하다는 검증 실패 |
| `mockup_04` | 17.5초 | `value_span_candidate_id`와 `anchor_text`를 함께 반환해 검증 실패 |
| `mockup_05` | 15.3초 | legacy anchor의 필수 좌표 누락 |
| `mockup_03` | 455.2초 | JSON 파싱 실패, 긴 생성 또는 컨텍스트 소진 가능성 |

## 4. 최초 장애의 직접 원인

### 4.1 JSON Schema와 서버 검증 계약의 차이

vLLM guided JSON은 JSON Schema를 강제한다. 그러나 다음 anchor 불변조건은 Pydantic `model_validator`에 구현되어 있어 생성용 JSON Schema만으로 완전히 강제되지 않는다.

```text
value_span_candidate_id 사용:
  anchor_text와 함께 사용 금지

legacy anchor 사용:
  source_block_id와 anchor_text가 모두 필요
```

따라서 모델은 JSON Schema에는 맞지만 서버의 교차 필드 검증에는 실패하는 응답을 만들 수 있었다.

### 4.2 기존 보완 루프에 도달하지 못함

팀원 패키지의 기존 repair는 파싱된 selection이 서버 재료화에서 실패한 경우를 대상으로 한다. 반면 어댑터에서 Pydantic 검증에 실패하면 `LLMInvalidResponseError`가 먼저 발생해 기존 재료화 repair에 도달하지 못했다.

즉 최초 anchor 오류는 보완 가능성이 있어도 재호출 0회로 종료됐다.

## 5. 적용한 수정

커밋:

```text
cbda84b fix(worker): repair invalid source selections
```

수정 내용:

1. OpenAI Responses 어댑터에서 JSON 파싱과 Pydantic 검증을 분리했다.
2. JSON은 읽혔지만 스키마가 틀린 경우에만 원본 dict를 메모리 내 예외에 전달한다.
3. 원본 응답은 예외 문자열, 로그, 진단 JSON에 노출하지 않는다.
4. `value_span_candidate_id`가 권위 있는 경우 중복 `anchor_text`를 제거하는 팀원 원격 호환 정규화를 재사용한다.
5. 정규화 후에도 교차 필드 검증에 실패할 때만 오류를 정제해 최대 1회 replacement를 요청한다.
6. JSON 파손, 타임아웃, 서비스 장애, 두 번째 스키마 오류는 반복 호출하지 않는다.
7. vLLM의 명시적 비정상 `finish_reason`은 불완전 응답으로 구분한다.
8. 기본 LLM timeout을 30초에서 60초로 조정했다.

검증 결과:

```text
714 passed, 12 skipped
714 passed, 12 skipped
```

실제 PostgreSQL DB를 초기화하지 않고 연속 두 번 실행했으며 실패와 에러는 없었다. 수정 전 기준선 707건 대비 신규 회귀 7건이 추가됐다.

## 6. 수정 후 실제 RunPod 실험

### 6.1 성공 사례

`mockup_04`를 실제 RunPod `gemma-12b`와 활성 Responses 어댑터, `make_vllm_selector`, 팀원 서버 재료화 경로로 실행했다.

| 항목 | 결과 |
| --- | --- |
| 상태 | `OK` |
| selection 시도 | 1회 |
| 진단 | 0건 |
| 경과 시간 | 27.28초 |

최초 실험에서 `value_span_candidate_id + anchor_text` 충돌로 실패했던 표본이 수정 후 실제 프로필 생성에 성공했다. 다만 이 성공 snapshot은 확인용 임시 메모리에만 존재했고 파일로 보존하지 못했다.

### 6.2 `mockup_01` 재료화 실패

수정 후 `mockup_01`은 최초 handoff의 Pydantic 스키마 오류를 넘어 서버 재료화까지 진행했다.

기본 보완 1회 결과:

| 시도 | 서버 진단 |
| ---: | --- |
| 1 | paragraph delivery relation members가 같은 relation container block에 있지 않음 |
| 2 | `value_span_candidate_id`의 `source_block_id` hint가 CandidatePack과 불일치 |

결과는 `FAILED / REPAIR_BUDGET_EXHAUSTED`, 총 49.09초였다.

측정 목적으로만 보완을 2회 허용했을 때:

| 시도 | 서버 진단 |
| ---: | --- |
| 1 | delivery relation container 불일치 |
| 2 | CandidatePack source block hint 불일치 |
| 3 | CandidatePack source block hint 불일치 반복 |

결과는 다시 실패했으며 61.99초가 걸렸다. 세 번째 시도에서 같은 오류가 반복됐으므로 보완 횟수를 늘리는 것만으로 해결되지 않는다는 근거가 됐다. 제품 기본값은 1회로 유지했다.

### 6.3 결과 파일 저장 재시도

사용자가 직접 볼 수 있는 검증 완료 프로필 JSON을 남기기 위해 `mockup_04`를 다시 실행했다.

- 첫 export 실행은 모델 호출 후 임시 export 코드의 진단 직렬화 오류로 상태를 기록하지 못했다. 모델 성공 여부의 근거로 사용하지 않는다.
- 수정 후 60초 제한 실행은 `LLM_TIMEOUT`이었다.
- 결과 확인용으로만 120초 제한을 사용한 추가 실행도 `LLM_TIMEOUT`이었다.

최신 실패 진단은 `tmp/actual_request_profile_mockup04_latest_failed_attempt.json`에 남아 있다. 이는 성공 프로필이 아니라 타임아웃 진단 파일이다.

## 7. vLLM 누적 지표

2026-09-09 확인 시점의 `/metrics`:

| 지표 | 값 |
| --- | ---: |
| 현재 실행 중 요청 | 0 |
| 현재 대기 요청 | 0 |
| 누적 완료 요청 | 69 |
| `finish_reason=stop` | 67 |
| `finish_reason=length` | 2 |
| abort | 0 |
| error | 0 |
| 누적 queue time | 약 0.366초 |

지연 분포:

| 구간 | 누적 건수 | 구간별 해석 |
| --- | ---: | --- |
| 30초 이하 | 64 | 대부분의 요청 |
| 120~240초 | 65 | 1건 |
| 240~480초 | 69 | 4건 |

대기열 시간은 69건 전체에서 약 0.366초에 불과했다. 따라서 긴 지연을 팀원 요청과의 큐 경합으로 설명할 근거는 없다. 일부 요청 자체의 생성 시간이 매우 길었다.

abort가 0인 반면 클라이언트에서는 timeout을 관찰했다. 이 조합은 클라이언트가 기다리기를 중단한 뒤에도 서버 생성이 계속됐을 가능성을 시사한다. 서버 로그와 요청 ID를 연결하지 않았으므로 확정 사실이 아닌 추론으로 남긴다.

## 8. 현재 문제의 분류

### 문제 A. 긴 꼬리 지연과 timeout

관찰 사실:

- 같은 모델과 유사한 계약에서 30초 이내 완료와 240~480초 완료가 함께 존재한다.
- 대기열 시간은 거의 없다.
- 출력 길이 종료가 2건 있다.

가능성이 높은 이유:

- 큰 CandidatePack과 깊은 구조화 출력 계약을 한 번에 처리한다.
- 모델이 많은 사실과 관계를 한 응답에서 생성한다.
- 일부 입력에서 긴 출력 또는 반복 생성 경로에 들어간다.

아직 확인하지 못한 것:

- 표본별 prompt token과 completion token의 정확한 값
- 긴 실행별 실제 `finish_reason`
- 서버가 클라이언트 연결 종료를 감지해 생성을 취소하는지
- GPU 사용률, KV cache 압력, 배치 설정과의 상관관계

따라서 “프롬프트가 커서만 발생했다”고 단정하지 않는다. 현재 근거로는 요청 단위 생성 부담이 주요 후보이며, 팀원 요청 대기는 원인으로 보이지 않는다.

### 문제 B. 서버 재료화 계약 불일치

관찰 사실:

- JSON/Pydantic 검증을 통과한 뒤에도 delivery relation container가 어긋난다.
- CandidatePack ID와 `source_block_id` hint가 불일치한다.
- 같은 진단을 추가 보완에서도 반복했다.

이 문제는 속도 문제가 아니라 선택 정확성 문제다. timeout을 늘려도 해결되지 않는다.

### 문제 C. `request_type` 컨테이너 부재

10개 중 4개는 요청유형 옵션 컨테이너가 없거나 모호해 LLM 단계에 도달하지 못했다. 이는 모델 성능과 별개다.

확인할 사항:

- 해당 목업이 현재 요청서 서식 계약을 실제로 충족하는지
- 파서가 존재하는 옵션 컨테이너를 놓친 것인지
- 실제 부재를 실패로 둘지 별도 미확인 상태로 둘지

### 문제 D. 실제 전체 E2E 미완료

Request Profile 한 번의 성공은 확인했지만 같은 실행에서 업로드부터 결과 저장과 PDF 다운로드까지 이어지는 실제 외부 E2E는 아직 실행하지 않았다.

또한 실제 공고 ExistingProfile 성공 데이터가 준비되지 않으면 Retrieval/SIM은 후보 0건 또는 보류 결과가 될 수 있다.

## 9. 아키텍처 평가

### 9.1 유지해야 하는 복잡성

다음은 결과 신뢰성과 데이터 무결성을 위해 필요하다.

- 원문 Evidence와 CandidatePack 소속 검증
- 정상 결과 보존과 실패 단위 격리
- DB 큐 claim, lease, fencing
- CPL/FIT/SIM 결과 계약 분리
- LLM과 임베딩의 교체 가능한 포트
- 결과 DB와 PDF의 동일한 불변 분석 내용 사용

이 검증을 느슨하게 만들어 모델 출력을 통과시키는 것은 해결책이 아니다.

### 9.2 정리 가능한 과도기 복잡성

- OpenAI Responses 어댑터와 vLLM Chat Completions 어댑터 병존
- `OPENAI_*`와 `VLLM_*` 설정 병존
- legacy 분석 실행과 새 DB 큐 실행 병존
- FastAPI에서 Supabase Edge Function 경로를 임시 제공하는 구조
- 기존 `sims.*`와 새 `kb/workspace/result` 저장 경계의 과도기 병존

현재 모델 장애의 직접 원인은 이 전체 구조가 아니라 한 번의 모델 호출이 담당하는 선택 범위가 넓다는 점이다. 다만 과도기 경로가 많아 실제 운영 경로와 테스트 경로를 혼동할 위험은 있다.

## 10. 예상 해결 방법

### 10.1 1순위: 관측과 실행 경계 확정

먼저 한 요청마다 다음을 같은 call ID로 보존해야 한다.

- 모델 ID와 프롬프트 버전
- 시작·종료 시각과 경과 시간
- prompt/completion/total token
- `finish_reason`
- timeout 값
- 최초 호출인지 repair인지
- 서버 검증 reason code

원문과 raw 모델 응답을 일반 로그에 남기지 않는다.

클라이언트 timeout 뒤 서버 생성이 계속되는지도 확인해야 한다. 취소가 지원된다면 요청 중단을 연결하고, 지원되지 않는다면 전체 deadline 안에서 중복 호출하지 않도록 제한한다.

### 10.2 2순위: 모델의 선택 범위 축소

가장 명확한 코드 개선 방향은 하나의 거대한 selection을 더 작은 닫힌 단위로 나누는 것이다.

제안 단위:

1. 일반 의미 facts
2. 정량 facts
3. delivery relations

각 호출은 자기 후보와 허용 필드만 받고, 서버가 결과를 합쳐 기존 `RequestSourceSelectionV012`와 팀원 materializer로 최종 검증한다.

기대 효과:

- 한 호출의 입력·출력 길이 감소
- delivery 관계 오류가 일반 사실 결과를 무효화하지 않음
- 실패한 묶음만 한 번 보완 가능
- 정상 facts 보존
- 원인과 지연을 호출 단위로 측정 가능

주의:

- 호출 수가 늘 수 있으므로 전체 deadline과 동시성 제한이 필요하다.
- 분할 전후의 총 지연과 프로필 정확성을 같은 표본으로 비교해야 한다.
- 분할을 새로운 범용 멀티에이전트 프레임워크로 만들지 않는다.

### 10.3 3순위: 결정적 후보 제한

서버가 이미 아는 닫힌 제약은 모델이 틀린 조합을 만들 수 없도록 입력에서 제한한다.

- delivery relation은 같은 relation container의 actor/role 후보만 제공
- `value_span_candidate_id`를 사용하면 실제 block은 CandidatePack에서 서버가 확인
- 모델에게 임의 원문이나 좌표를 생성하게 하지 않음
- 정량값은 기존 Rule 정규화를 유지

단, 잘못된 `source_block_id`를 조용히 덮어써 오류를 숨기는 방식은 팀원 provenance 계약과 먼저 대조해야 한다.

### 10.4 4순위: 모델 교체 비교

포트 경계가 있으므로 더 큰 자체 운영 instruct 모델이나 다른 양자화 모델로 교체 시험할 수 있다.

같은 CandidatePack, 프롬프트, schema, timeout, 표본으로 다음을 비교해야 한다.

- 서버 검증 통과율
- exact-span과 relation 정확성
- p50/p95 지연
- 출력 길이 종료 수
- repair 횟수

모델을 키우는 것은 가장 빠른 운영 실험일 수 있지만, 현재 계약을 반드시 해결한다고 보장할 수는 없다.

## 11. 피해야 할 대응

- timeout만 계속 120초, 300초, 600초로 올리기
- 동일 오류에 무제한 repair 호출
- 성공률을 높이기 위해 exact-span/provenance 검증 완화
- 원문에 없는 anchor나 관계를 서버가 추측해 생성
- 실패한 profile을 빈 정상 profile로 저장
- 모델 교체 전후를 서로 다른 표본과 프롬프트로 비교

## 12. 권장 진행 순서

1. 현재 `cbda84b`의 제한 복구와 엄격한 검증 유지
2. call ID, token, `finish_reason`, 경과 시간, repair 진단 연결
3. 서버가 유휴 상태일 때 독립 표본으로 실제 Request Profile 결과 파일 보존
4. `request_type` 미도달 4건을 서식 문제와 파서 문제로 분리
5. delivery relation 후보 제한 실험
6. 필요하면 selection을 facts/quantitative/delivery로 최소 분할
7. 같은 표본으로 현재 12B와 대안 자체 운영 모델 비교
8. 안정된 프로필 생산 후 업로드→PDF 실제 백엔드 E2E 실행
9. 별도로 공고 ExistingProfile과 실제 Supabase 경계를 검증

## 13. 완료 판정 기준

Request Profile 단계는 최소한 다음을 만족해야 완료로 볼 수 있다.

- 같은 실제 입력과 같은 배포 artifact로 반복 실행 가능
- 프로필 `status=OK`
- 모든 Evidence가 CandidatePack과 원문에 접지
- delivery relation의 멤버가 계약상 같은 컨테이너에 속함
- 실패 시 raw 원문 비노출
- 동일 결함에 무제한 repair 없음
- timeout 뒤 중복 원격 실행 방지
- `request_type` 부재와 모델 실패가 서로 다른 진단으로 남음

백엔드 E2E 완료는 그 뒤 실제 요청서 한 건에 대해 다음 경로가 하나의 실행으로 이어져야 한다.

```text
실제 업로드
→ 실제 Storage 읽기
→ DB 큐 claim
→ 실제 파서
→ 실제 자체 운영 LLM 프로필
→ CPL/FIT
→ Retrieval/SIM 또는 정직한 부분 실패
→ 결과 DB/RPC
→ PDF 생성·저장
→ 다운로드
```

실제 Supabase를 사용하지 않은 경로는 PostgreSQL 회귀가 통과해도 Supabase 연동 완료로 보고하지 않는다.

