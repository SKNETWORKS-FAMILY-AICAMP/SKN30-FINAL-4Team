# Existing Profile Gold 100건 회귀 검증 안내

## 1. 자료의 역할

두 100건 자료는 원문 집합은 같지만 역할이 다르다.

| 자료 | 역할 | 정답으로 사용 가능 여부 |
|---|---|---|
| `structured-profiles-100.zip` | 사람 검토 전 자동 생성 baseline과 원본 공고 묶음 | 불가 |
| `frozen_existing_profile_gold_100_20260909_v5` | baseline의 의미 오류를 사람이 판정·교정하고 검증 기록까지 동결한 Gold oracle | 가능 |

baseline의 Profile JSON을 최신 정답처럼 재사용하면 안 된다. 원문 attachment와 metadata는
파이프라인 재실행 입력으로 사용할 수 있고, freeze v5의 Common IR·source-selection·Profile은
후보 실행 결과를 평가하는 외부 oracle로만 사용한다. Gold JSON을 production prompt나
repair payload에 넣거나 정답 값을 복사하는 것은 금지한다.

확인된 동일성·차이는 다음과 같다.

- 공고 ID: 100/100 동일
- 원본 파일명과 원본 SHA-256: 100/100 동일
- 원본 형식: PDF 47, HWP 48, HWPX 5
- 주요 metadata: 100/100 동일
- Common IR canonical JSON: 95 동일, 5 교정
- source-selection semantic JSON: 94 동일, 6 교정
- Profile JSON: `notice_id` namespace만 정규화하면 94 동일, 6 의미 교정

의미 교정 공고는 다음 6건이다.

```text
PBLN_000000000103645
PBLN_000000000112425
PBLN_000000000117175
PBLN_000000000121019
PBLN_000000000121309
PBLN_000000000122023
```

## 2. 검증 계층

회귀 검증은 물리 추출과 의미 구조화를 분리한다.

1. 원본 attachment → Common IR
   - native text·표 구조·section 경계·provenance·원본 SHA-256을 비교한다.
   - PDF OCR/ODL/Surya sidecar는 아직 Gold의 native 근거를 대체하지 않는다.
2. 동결 Common IR → source-selection → Existing Profile
   - Fact field, exact span, component/관계, support scale measure를 비교한다.
   - 이 계층은 parser 차이 없이 의미 선택 변경만 평가할 때 사용한다.

두 결과가 다르면 바로 Gold 값을 복사하지 않는다. 먼저 Gold의 adjudication/governance 기록에서
교정 사유를 확인하고, 일반화 가능한 규칙인지 판단한 뒤 최소 fixture와 회귀 테스트를 추가한다.
특정 공고 ID나 Gold 문구를 production 코드에 하드코딩하지 않는다.

## 3. freeze 자체 무결성 확인

검증기는 읽기 전용이며 `--gold-root`를 반드시 명시해야 한다. DB·Storage·OpenAI API를
호출하지 않고 corpus를 수정하지 않는다.

```bash
cd /path/to/SKN30-FINAL-4Team

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/verify_existing_gold100.py \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5
```

정상 출력 예시는 다음과 같다.

```json
{
  "artifact_count": 387,
  "dataset_version": "frozen_existing_profile_gold_100_20260909_v5",
  "fact_count": 2409,
  "notice_count": 100,
  "status": "valid"
}
```

검증 범위는 freeze pin, artifact SHA-256·크기·경로, 100건 목록, Common IR/Profile
provenance 결속, CandidatePack text basis, CSV 역할 매핑, v4/v5 governance 결과다. 이 명령이
성공해도 현재 production 파이프라인이 Gold와 의미상 동일하다는 뜻은 아니다. 현재 단계에서는
Gold corpus 자체가 훼손되지 않았음을 확인한다.

검증기 자체 테스트:

```bash
UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend pytest -q backend/tests/test_existing_gold100_verifier.py
```

## 4. 자동 baseline과 Gold 비교

아래 비교기는 baseline ZIP을 풀지 않고 Profile JSON만 읽는다. JSON 객체 키 순서와 최상위
`notice_id`의 `bizinfo:` namespace 차이만 정규화하며, 배열 순서·문자열·Fact ID·근거 span·
`processing_metadata`를 포함한 나머지 값은 그대로 비교한다. 유사도나 LLM으로 차이를
자동 승인하지 않는다.

```bash
cd /path/to/SKN30-FINAL-4Team

REPORT_DIR="$(mktemp -d /tmp/existing-gold100-compare.XXXXXX)"

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/compare_existing_profile_candidates.py \
  --baseline-zip /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --output-dir "$REPORT_DIR" \
  --expected-profile-count 100 \
  --expected-shared 100 \
  --expected-unchanged 94 \
  --expected-changed 6 \
  --expected-baseline-sha256 6649f1a5aab36f659d688634103950d3b73f8a5903a453aabdbbd9f5bc0f7f0d \
  --expected-gold-freeze-manifest-sha256 a2c35fb4c98c92c23ff34faa045a16ec4e8ed1ea4bae5397caab547cb3db6987 \
  --expected-changed-id PBLN_000000000103645 \
  --expected-changed-id PBLN_000000000112425 \
  --expected-changed-id PBLN_000000000117175 \
  --expected-changed-id PBLN_000000000121019 \
  --expected-changed-id PBLN_000000000121309 \
  --expected-changed-id PBLN_000000000122023

jq . "$REPORT_DIR/existing-profile-comparison.v1.json"
```

건수·변경 ID뿐 아니라 baseline ZIP과 Gold freeze manifest의 SHA-256도 함께 고정한다.
기대값이 하나라도 달라지면 보고서를 쓰지 않고 실패한다. 이 결과의 `unchanged=94`는 자동
baseline 94건을 사람이 검수한 Gold가 그대로 승인했다는 뜻이고, `changed=6`은 사람이 의미
오류를 교정한 공고 수다. 이는 새 후보 파이프라인의 품질 점수가 아니라 비교기 자체의 기준점이다.

비교기 테스트:

```bash
UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend pytest -q backend/tests/test_existing_profile_gold_comparator.py
```

## 5. 후보 파이프라인 평가 원칙

- 동일한 원본 SHA-256과 동결 설정으로 실행한다.
- 실행 산출물은 Gold 디렉터리 밖 별도 output 경로에 쓴다.
- schema/exact-span/provenance 실패는 의미 비교 전에 실패 처리한다.
- 100건 aggregate뿐 아니라 위 6건의 교정 사유별 결과를 별도로 보고한다.
- 개선과 악화를 함께 기록한다. 한 공고 개선을 위해 다른 94건을 바꾸면 자동 승인하지 않는다.
- 의미 판정이 필요한 새 차이는 새 adjudication record를 만든 뒤 Gold 차기 버전에만 반영한다.

4절의 strict canonical 비교기는 동결된 자동 baseline과 Gold의 원본 JSON 차이를 재현하고,
8절의 semantic gate는 새 후보를 ID·set-like 순서 변화에 독립적으로 평가한다. 다음 구현 단계는
공유 production transform을 확정한 뒤 새 pipeline으로 100건 후보 ZIP을 실제 생성해 semantic
gate에 넣는 것이다. OpenAI를 호출하는 경우 별도 승인·모델 pin·prompt bundle·token/latency
기록이 필요하다. strict canonical 94/6 기준은 semantic gate 결과로 덮어쓰지 않는다.

## 6. Composite 후보의 오프라인 shadow 검사

표 값에 행·열 문맥을 결속하거나 같은 셀 안의 잘린 문장을 보존하는
`CompositeCandidate`는 아직 Profile 입력이 아니다. 기본 모드는 `off`이며 `shadow`도
기존 LLM payload, Profile v0.2, DB/API/retrieval 계약을 바꾸지 않고 원문 없는 집계 진단만
남긴다.

현재 polling worker에는 Existing Profile producer 호출점이 없으므로 환경변수를 `shadow`로
바꾸는 것만으로 운영 로그가 생성되지는 않는다. 지금 구현은 향후 producer 연결을 위한
prepared/dormant seam이며, 실제 연결과 활성화는 별도 변경으로 다룬다.

아래 검사는 Gold를 수정하거나 OpenAI·DB·Storage를 호출하지 않는다. 동결 Common IR의
모든 exact native occurrence를 합성 A pack으로 투영하므로 실제 LLM A routing 결과가 아니라
구조 후보의 **상한선**을 보는 검사다.

먼저 3절의 무결성 검증을 통과해야 한다. 이 검사기는 `freeze_manifest.json`을 trust root로
삼아 그 안의 `profile_manifest_sha256`과 각 선택 Common IR의 manifest SHA-256을 다시
확인하지만, freeze manifest 자체의 외부 고정 SHA까지 pin하지는 않는다.

```bash
cd /path/to/SKN30-FINAL-4Team

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/evaluate_existing_composite_shadow.py \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --expected-notice-count 100
```

공고별 집계까지 필요할 때만 `--include-notices`를 추가한다. 출력에는 원문,
Candidate/atom ID가 포함되지 않는다. 반드시 확인할 불변식은
`invariants.cross_common_ir_block_candidate_count == 0`과
`invariants.fatal_diagnostic_notice_count == 0`이다. 잘못된 Common IR identity나
문서·CandidatePack 전체 자원 상한 위반처럼 결과 전체를 신뢰할 수 없는 진단이 한 건이라도
있으면 검사는 `status: invalid`와 종료 코드 1을 반환한다.

현재 표의 행·열 영역은 explicit cell geometry 위에서 좌상단 셀의 span을 사용하는 shadow
가설이다. Common IR v1에는 semantic header 표시가 없으므로, 이 가설은 Gold 의미 평가나
upstream의 명시적 header-region 계약 없이 Profile evidence로 승격하면 안 된다. 검사 결과의
candidate 수가 많다는 사실도 의미 품질 향상을 뜻하지 않는다.

검사기 자체 테스트:

```bash
UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend pytest -q \
  backend/tests/test_existing_composite_shadow_evaluator.py \
  backend/tests/test_existing_composite_candidates.py \
  backend/tests/test_existing_composite_shadow_integration.py
```

## 7. 교정 6건 A-routing canary

`run_existing_a_routing_canary.py`는 사람 검토로 의미 교정된 6건에 대해 production
파이프라인의 `section scope → block router` 구간만 실행한다. source-selection, Profile 생성,
DB·Storage 적재, embedding은 실행하지 않는다. 기본 동작은 API를 호출하지 않는 계획 검증이다.

모델에 전달되는 자료는 SHA-256으로 고정된 자동 baseline ZIP의 Common IR뿐이다. 실행 전에는
Gold의 고정된 `freeze_manifest.json` 메타데이터만 확인한다. Gold Common IR,
source-selection, Existing Profile과 adjudication 자료는 모든 모델 호출이 끝난 뒤 로컬
감사 단계에서 처음 읽으므로 prompt나 repair payload에 들어가지 않는다.

먼저 무호출 계획을 확인한다.

```bash
cd /path/to/SKN30-FINAL-4Team

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/run_existing_a_routing_canary.py \
  --baseline-zip /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5
```

정상 계획은 `announcement_section_scope_v1` 2회와
`announcement_block_router_v03` 6회, 합계 8회다. 자료·prompt·모델 pin 또는 이 호출 계획이
달라지면 실행하지 않고 실패한다.

실제 호출은 비공개 Common IR을 외부 OpenAI API에 전송하므로 자료 전송 승인을 받은 뒤에만
명시적인 `--execute-openai`로 실행한다. `OPENAI_API_KEY`는 출력하거나 보고서에 쓰지 않고
환경 파일에서만 읽는다. 모델은 이 canary에 고정된 `gpt-5.6-terra`만 허용한다.

```bash
REPORT_DIR="$(mktemp -d /tmp/existing-a-routing-canary.XXXXXX)"

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --env-file backend/.env --project backend \
  python backend/scripts/run_existing_a_routing_canary.py \
  --baseline-zip /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --execute-openai \
  --model gpt-5.6-terra \
  --timeout-seconds 120 \
  --output-dir "$REPORT_DIR"

jq . "$REPORT_DIR/existing-a-routing-canary.v1.json"
```

SDK 자동 재시도는 0이며 호출 budget은 provider에 제어를 넘기기 전에 차감한다. 따라서 timeout
또는 전송 실패도 `calls.attempted`에 포함되고 8회를 넘지 않는다. provider가 응답한 호출의
token 사용량은 원문·응답·request ID 없이 숫자만 `calls.usage`에 기록한다.

보고서의 세 상태는 서로 다른 의미다.

- `execution_status`: 고정된 routing 호출 계획 자체의 성공 여부
- `routing_retention_status`: generator v2가 현재 표현할 수 있는 Gold 기대 12개가 실제 A routing
  이후에도 모두 남았는지 여부
- `semantic_profile_status`: source-selection과 Profile 생성을 실행하지 않으므로 항상 `not_run`

현재 Gold의 multi-occurrence 기대는 30개지만 generator v2로 표현 가능한 범위는 12개다.
따라서 `generator_expressibility=12/30`, `routed_a_retention=12/12`가 나오더라도 전체 6건의
의미 구조화가 성공했다는 뜻은 아니다. 나머지 18개와 최종 Profile 의미 품질은 8절의
ID·순서 비의존 semantic diff로 따로 검증해야 한다.

2026-09-15 고정 자료·모델로 실제 실행한 결과는 다음과 같다.

| 항목 | 결과 |
|---|---:|
| 실행 상태 | `succeeded` |
| routing 보존 상태 | `passed` |
| semantic Profile 상태 | `not_run` |

| 호출 | 계획 8 / 시도 8 / provider 응답 8 |
| 호출 구성 | section scope 2 / block router 6 |
| token | prompt 107,768 / completion 18,526 / 합계 126,294 |
| 누적 provider 지연시간 | 114,390 ms |
| 표현 가능 기대 | 12 / 30 |
| 실제 routing 보존 | 12 / 12 |
| fatal 진단 | 0 |
| Common IR block 경계 위반 후보 | 0 |

이 표는 routing canary 결과이며 source-selection이나 Profile 의미 정확도의 통과 기록이 아니다.
비용은 실행 계정에 적용되는 `gpt-5.6-terra` 단가가 별도로 확인되지 않았으므로 token 사용량만
고정한다.

관련 테스트:

```bash
UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend pytest -q \
  backend/tests/test_existing_a_routing_canary.py \
  backend/tests/test_existing_composite_shadow_integration.py \
  backend/tests/test_worker_core_contract.py
```

## 8. ID·순서 비의존 Profile 의미 회귀 비교

`compare_existing_profile_semantics.py`는 자동 baseline(`B`), 사람 검토 Gold(`G`), 새
후보(`C`)의 **Profile + source-selection + Common IR** 세 산출물을 함께 검증하고 비교한다.
OpenAI·DB·Storage를 호출하지 않는 오프라인 검사다. 생성할 때마다 달라질 수 있는 Fact·
component ID와 set-like 배열 순서는 비교에서 제외하지만, 원문 값·상태·역할·Common IR
occurrence 근거·component membership·방향성 관계·지원 규모 projection은 보존한다.

이 명령은 입력 파일명이나 경로를 신뢰하지 않는다. 실행 전에 baseline ZIP과 Gold
`freeze_manifest.json`의 SHA-256을 검증하고, 후보 ZIP도
`--expected-candidate-sha256`을 주면 같은 방식으로 검증한다. pin 불일치는 비교나
보고서 기록 전에 종료 코드 `1`로 실패한다. 아래 B/G pin은 이 Gold v5 릴리스의 trust
root이므로, 새 Gold 릴리스에서는 pin·calibration 기대값을 같은 리뷰 변경으로 교체한다.

최종 통과 조건은 승인 delta를 부분적으로 세는 휴리스틱이 아니라 의미 multigraph의
`C == G`다. 보고서에는 원문 대신 atom SHA-256과 종류·개수만 기록한다. 입력 ZIP 파일명이나
Gold 디렉터리명도 복사하지 않고 corpus SHA-256·건수·역할만 남긴다.

후보 ZIP은 공고마다 다음 세 JSON을 반드시 포함해야 한다.

```text
PBLN_<15자리>/pipeline/structured_profile.v0.2.json
PBLN_<15자리>/pipeline/source_selection.json
PBLN_<15자리>/pipeline/common_ir_v1/<한 개의 JSON>
```

후보의 Common IR은 Gold에서 복사하지 않는다. baseline의 동결 Common IR을 후보 Profile
생성 입력으로 사용하고 후보 ZIP에도 그대로 포함해야 한다. 배포 경로인
`document.provenance.source_location`만 달라질 수 있으며, 나머지 입력이 baseline과 다르면
의미 비교 전에 실패한다.

후보의 출처 정보도 후보가 스스로 주장한 문자열만으로 신뢰하지 않는다. baseline Common IR에서
기본 projection → PDF inspector의 native table occurrence → native line atom → 최대 3개 native
continuation composite 순으로 **결정적 source universe**를 다시 만들며, 후보는 그 ID·본문이
정확히 같은 block의 부분집합만 쓸 수 있다. baseline 전용 legacy block이나 Gold 수동 교정
`adj:*` block을 후보가 복사하는 것은 허용하지 않는다. source-selection의 선택값, materialized
evidence, Profile fact/component도 서로 일치해야 한다. 지원 규모 수치는 locator의 원문 토큰을
다시 해석해 measure 종류·단위·역할·값과 맞는지 확인한다. 새 후보의 component 이름은 해당
component source block 안에 정확히 한 번 존재해야 하며, 숫자 locator는 더 큰 숫자의 일부가
아니라 서버의 complete-token 규칙으로 독립 재열거된 span이어야 한다. 과거 B/G의 검토 delta는
별도 calibration으로 읽되 이 새 후보 admission 규칙을 우회해 후보 합격으로 취급하지 않는다.

이 source universe는 RunPod `0.1.4`의 section-scope·block-router 선택을 그대로 재현하는
CandidatePack parity 검사가 아니다. 실제 router가 어떤 block을 노출했는지는 별도의 production
parity 테스트 대상이다. 여기서는 후보가 사용한 값과 근거가 고정 Common IR에서 결정적으로
재생성 가능한지만 fail-closed로 검증한다. 보고서의
`normalization.candidate_source_admission.scope`도 이 범위를 명시한다.

의미 비교에서 원문 위치의 공통 식별자는 `source_sha256 + occurrence_ids`다. 수동 교정 Gold가
새 grouping block과 section을 만들었더라도 같은 원문 occurrence라면 새 파이프라인 결과와
동일하게 비교하기 위해서다. CandidatePack block/offset과 Common IR block/cell/section은 버리는
정보가 아니라 위 admission 단계에서 엄격히 검증하는 locator이며, 의미 atom에는 넣지 않는다.
component의 local source locator도 현재 frozen raw-occurrence alias가 없어 검증 전용이고,
component 의미 자체인 kind·이름·상태·소속 fact는 비교한다.

의미를 버릴 위험이 있는 아직 지원하지 않는 구조는 조용히 무시하지 않고 실패한다. 현재
`table_catalog`, `unresolved_relations`, 두 경계를 하나의 locator로 표현한 `range` measure가
여기에 해당한다. `unresolved_observations[].reason`은 무시 대상이 아니라 보존되는 의미다.

현재 transform은 원문에 없는 구두점을 삽입하거나 bullet을 제거해 새 문장을 만들지 않고,
여러 표 셀의 관계를 임의의 문장으로 합성하지도 않는다. 예를 들어 `117175`의 주관/공동기관
eligibility 교정값은 label과 body 사이의 `: ` 삽입 및 두 번째 bullet 제거가 필요하므로 현재
계약으로는 exact source를 만들 수 없다. 이런 값은 `adj:*`를 예외 허용하지 않고, 향후
`derived_text_basis + source_spans`와 표 관계를 포함한 versioned production transform 계약을
정한 뒤 지원한다. 따라서 이 단계의 비교기는 B/G/C 의미 gate와 출처 검증 도구를 제공하지만,
현 production pipeline이 Gold 100건을 전부 생성할 수 있다고 주장하지 않는다.

100건 전체 후보를 검사하는 예시는 다음과 같다.

```bash
cd /path/to/SKN30-FINAL-4Team

REPORT_DIR="$(mktemp -d /tmp/existing-semantic-bgc.XXXXXX)"

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/compare_existing_profile_semantics.py \
  --baseline-zip /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --candidate-zip /path/to/candidate-existing-profile-100.zip \
  --output-dir "$REPORT_DIR" \
  --expected-reference-count 100 \
  --expected-candidate-count 100 \
  --expected-baseline-sha256 6649f1a5aab36f659d688634103950d3b73f8a5903a453aabdbbd9f5bc0f7f0d \
  --expected-gold-freeze-manifest-sha256 a2c35fb4c98c92c23ff34faa045a16ec4e8ed1ea4bae5397caab547cb3db6987 \
  --expected-candidate-sha256 <후보_ZIP_SHA256>

jq . "$REPORT_DIR/existing-profile-semantic-bgc.v1.json"
```

교정 6건만 담은 후보 ZIP은 `--expected-candidate-count 6`과 아래 여섯
`--notice-id`를 함께 준다. 요청 ID의 중복·누락은 허용하지 않는다.

```text
PBLN_000000000103645
PBLN_000000000112425
PBLN_000000000117175
PBLN_000000000121019
PBLN_000000000121309
PBLN_000000000122023
```

후보 gate의 종료 코드는 입력/계약 오류 `1`, 의미 불일치 `2`, 전건 통과 `0`이다. B/G의
검토 delta를 재현하는 기준점은 후보 모드의 우회 조건이 아니라 명시적인 calibration 모드로만
실행한다.

```bash
REPORT_DIR="$(mktemp -d /tmp/existing-semantic-calibration.XXXXXX)"

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/compare_existing_profile_semantics.py \
  --baseline-zip /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --calibrate-baseline \
  --output-dir "$REPORT_DIR" \
  --expected-reference-count 100 \
  --expected-baseline-sha256 6649f1a5aab36f659d688634103950d3b73f8a5903a453aabdbbd9f5bc0f7f0d \
  --expected-gold-freeze-manifest-sha256 a2c35fb4c98c92c23ff34faa045a16ec4e8ed1ea4bae5397caab547cb3db6987
```

calibration 결과는 정확히 `evaluation_kind=baseline_gold_calibration`, `selected=100`,
`passed=94`, `failed=6`, 아래 여섯 PBLN ID여야 한다. 이 기대값과 정확히 맞으면
`status=calibration_matched`, 종료 코드 `0`이다. 다르면 보고서는 남기되
`status=calibration_mismatch`, 종료 코드 `2`다. 이는 후보 gate 통과가 아니라 **동결된
B/G 기준점이 그대로 재현됐다는 별도 성공 상태**다.

`--expected-calibration-passed`, `--expected-calibration-failed`,
`--expected-calibration-failed-notice-id`, `--notice-id`로 다른 기대 집합을 지정하는 기능은
합성 fixture와 부분집합 진단용이다. 그 경우의 `calibration_matched`는 **사용자가 지정한
진단 기대값과 일치했다**는 뜻일 뿐 Gold v5 release gate 통과를 뜻하지 않는다. Gold v5
릴리스 검증은 위 명령처럼 해당 옵션을 재정의하거나 공고를 부분 선택하지 않고 기본
100건·94/6·고정 6개 ID를 사용해야 한다.

```text
PBLN_000000000103645
PBLN_000000000112425
PBLN_000000000117175
PBLN_000000000121019
PBLN_000000000121309
PBLN_000000000122023
```

실제 후보 비교는 항상 `evaluation_kind=candidate_gold_gate`이며 엄격한 source admission을
우회하지 않는다. 이 94/6은 비교기 회귀 기준일 뿐 새 후보의 합격 결과가 아니다. 앞 절의
A-routing canary는 source-selection과 Profile을 만들지 않으므로 이 검사기의 후보 ZIP으로
사용할 수 없다.

### 개발자·릴리스 CI의 실제 corpus 테스트

`test_existing_profile_semantic_diff.py`의 실제 Gold100 테스트는 저장소 밖 자료를 임의의
절대경로에서 찾지 않는다. 개인 개발 환경에서 두 환경변수가 **모두 미설정**이면 해당 실제
corpus 테스트만 skip할 수 있다.

```bash
export PREREVIEW_EXISTING_GOLD100_BASELINE_ZIP=/path/to/structured-profiles-100.zip
export PREREVIEW_EXISTING_GOLD100_GOLD_ROOT=/path/to/frozen_existing_profile_gold_100_20260909_v5
```

한 변수만 설정했거나, 명시한 경로가 없으면 skip하지 않고 테스트 실패다. release/CI에서는
`PREREVIEW_REQUIRE_EXISTING_GOLD100=1`을 반드시 설정해 실제 corpus 테스트의 skip을 금지하고,
`-rs` 출력에 `SKIPPED`가 없는지 확인한다. CI는 이 환경변수 없이 통과한 unit-test 결과를
Gold100 회귀 통과로 표기해서는 안 된다.

```bash
PREREVIEW_REQUIRE_EXISTING_GOLD100=1 \
PREREVIEW_EXISTING_GOLD100_BASELINE_ZIP=/secure/input/structured-profiles-100.zip \
PREREVIEW_EXISTING_GOLD100_GOLD_ROOT=/secure/input/frozen_existing_profile_gold_100_20260909_v5 \
UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend pytest -q -rs \
  backend/tests/test_existing_profile_semantic_diff.py
```

검사기 테스트:

```bash
UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend pytest -q \
  backend/tests/test_existing_profile_semantic_diff.py \
  backend/tests/test_existing_profile_gold_comparator.py \
  backend/tests/test_existing_gold100_verifier.py
```
