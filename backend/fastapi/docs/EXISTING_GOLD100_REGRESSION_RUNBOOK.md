# Existing Profile Gold 100건 회귀 검증 안내

## 1. 자료의 역할

두 100건 자료는 원문 집합은 같지만 역할이 다르다.

| 자료 | 역할 | 정답으로 사용 가능 여부 |
|---|---|---|
| `structured-profiles-100.zip` | 자동 생성 Profile·selection과 원본 공고 묶음. 단, Common IR에는 수동 교정 블록이 포함됨 | 불가 |
| `frozen_existing_profile_gold_100_20260909_v5` | baseline의 의미 오류를 사람이 판정·교정하고 검증 기록까지 동결한 Gold oracle | 가능 |

baseline의 Profile JSON을 최신 정답처럼 재사용하면 안 된다. 원문 attachment와 metadata는
파이프라인 재실행 입력으로 사용할 수 있다. 그러나 2026-09-16 감사에서 baseline ZIP의
Common IR에도 `adj:` 식별자와 `manual_gold`/adjudication 계열 provenance가 들어 있음을
확인했다. 따라서 이 ZIP의 Common IR은 모델 입력용 자동 원본이 아니며, 원본 attachment에서
다시 생성한 무교정 Common IR만 canary 입력으로 사용할 수 있다. freeze v5의 Common IR·
source-selection·Profile은 후보 실행 결과를 평가하는 외부 oracle로만 사용한다. Gold JSON을
production prompt나 repair payload에 넣거나 정답 값을 복사하는 것은 금지한다.

교정 6건에 대해서는 원본에서 다시 생성한 별도 모델 입력 `I`가 준비돼 있다. 이 입력은
`.runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.zip`이며,
SHA-256은 `24d70a944f43446563a257958fc5b97a5c484e28b5ac653ccf3f2792880d7800`이다.
저장소의 `backend/baselines/existing_profile/pristine_hard6_common_ir.v1.json`을 공개 trust
root로 사용한다. 구성은 PDF native-only 5건과 HWP 1건이다.

아래 94/6 수치는 두 동결 corpus의 비교 기준점으로는 유효하다. 다만 baseline Profile과
selection도 수동 교정 블록이 포함된 Common IR을 바탕으로 생성됐으므로, 이를 “완전히 사람 검토
전인 parser→Profile baseline”이라고 해석해서는 안 된다.

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
2. 고정된 pristine Common IR(`I`) → source-selection → Existing Profile
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
기대값이 하나라도 달라지면 보고서를 쓰지 않고 실패한다. 이 결과의 `unchanged=94`는 §1의
Common IR 오염 단서를 포함한 동결 baseline 중 94건을 Gold가 그대로 승인했다는 뜻이고,
`changed=6`은 사람이 의미 오류를 추가 교정한 공고 수다. 이는 새 후보 파이프라인의 품질 점수가
아니라 비교기 자체의 기준점이다.

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
8절의 B/G/I/C semantic gate는 새 후보를 ID·set-like 순서 변화에 독립적으로 평가한다.
교정 6건의 입력·비교 계약은 구현됐으며, 다음 단계는 승인된 pristine archive를 사용해 실제
Terra canary를 실행하고 의미 결과를 측정하는 것이다. OpenAI를 호출하는 경우 별도 승인·모델
pin·prompt bundle·token/latency 기록이 필요하다. strict canonical 94/6 기준은 semantic gate
결과로 덮어쓰지 않는다.

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

모델 입력은 원본 attachment에서 두 번 독립 재생성해 결정성을 확인한 pristine Common IR `I`다.
로더는 archive·member·원본 SHA-256, 정확한 6건 목록, Common IR schema, production producer와
parser version을 검증한다. 수동 교정 provenance, 비정상 JSON 수치, 임의 manifest 교체는 모델
클라이언트를 만들기 전에 차단한다. Gold와 historical baseline은 dry-run에서 읽지 않는다.

현재 archive는 PDF 5건을 Existing 전용 `pdf_native_only` 경로로, HWP 1건을 `rhwp` 경로로
생성했다. PDF의 Surya/ODL OCR 의미 근거나 표·다이어그램 fusion이 포함됐다는 뜻은 아니다.
archive를 다시 확인하려면 다음 명령을 사용한다.

```bash
backend/.venv/bin/python backend/scripts/freeze_pristine_hard6_common_ir.py \
  --source-inventory .runtime/evaluations/pristine-common-ir-rebuild-20260916/input_manifest.json \
  --pdf-replay-root-a .runtime/evaluations/pristine-hard6-pdf-native-20260917-v1 \
  --pdf-replay-root-b .runtime/evaluations/pristine-hard6-pdf-native-20260917-v2 \
  --hwp-common-ir-a .runtime/evaluations/pristine-common-ir-rebuild-20260916/runs-final/PBLN_000000000117175/common_ir_v1/PBLN_000000000117175.hwp.json \
  --hwp-common-ir-b .runtime/evaluations/pristine-common-ir-rebuild-20260916/diagnostic-hwp-freetype/common_ir_v1/PBLN_000000000117175.hwp.json \
  --archive .runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.zip \
  --manifest .runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.json \
  --check
```

정상 결과는 `status=valid`와 고정 archive SHA-256이다.

먼저 무호출 계획을 확인한다.

```bash
cd /path/to/SKN30-FINAL-4Team

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/run_existing_a_routing_canary.py \
  --input-zip .runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.zip
```

현재 pristine 입력에서 확인한 계획은 `announcement_section_scope_v1` 2회와
`announcement_block_router_v03` 6회, 합계 8회다.

실제 호출은 비공개 Common IR을 외부 OpenAI API에 전송하므로 무교정 input pin 검토와 자료
전송 승인을 모두 받은 뒤에만 명시적인 `--execute-openai`로 실행한다. `OPENAI_API_KEY`는
출력하거나 보고서에 쓰지 않고 환경 파일에서만 읽는다. 모델은 이 canary에 고정된
`gpt-5.6-terra`만 허용한다.

```bash
REPORT_DIR="$(mktemp -d /tmp/existing-a-routing-canary.XXXXXX)"

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --env-file backend/.env --project backend \
  python backend/scripts/run_existing_a_routing_canary.py \
  --input-zip .runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --execute-openai \
  --model gpt-5.6-terra \
  --timeout-seconds 120 \
  --output-dir "$REPORT_DIR"

jq . "$REPORT_DIR/existing-a-routing-canary.v2.json"
```

SDK 자동 재시도는 0이며 호출 budget은 provider에 제어를 넘기기 전에 차감한다. 따라서 timeout
또는 전송 실패도 `calls.attempted`에 포함되고 8회를 넘지 않는다. provider가 응답한 호출의
token 사용량은 원문·응답·request ID 없이 숫자만 `calls.usage`에 기록한다.

보고서의 세 상태는 서로 다른 의미다.

- `execution_status`: 고정된 routing 호출 계획 자체의 성공 여부
- `routing_retention_status`: `stable_table_relation/v1`로 고정한 Gold 기대 관계 12개가 실제
  A routing 이후에도 모두 남았는지 여부
- `semantic_profile_status`: source-selection과 Profile 생성을 실행하지 않으므로 항상 `not_run`

통과 기준은 표 block/cell, primary/header 역할, 원래 순서와 문장부호를 보존해 정규화한 셀
내용으로 만든 안정 관계 12개의 `12/12` 보존이다. 분리된 occurrence는 CandidatePack의 안정된
source 순서로 다시 조립한다. HWP generator `1.0.1 → 1.1.0`에서 같은 셀이 여러 occurrence로 분리돼 raw
occurrence 직접 비교는 `4/30`이지만, 이는 `raw_occurrence_diagnostic`에만 남고 pass/fail에는
사용하지 않는다. stable 12/12도 전체 PDF/HWP 추출 품질이나 최종 Profile 의미 품질을 뜻하지
않으며, 최종 의미는 8절의 semantic gate로 따로 검증한다.

2026-09-17 실제 pristine I를 native-as-routed로 사용한 오프라인 audit은 stable 기대 12,
all-native 12, routed 12, 집합 동일, `status=passed`였다. 이 검사는 OpenAI를 호출하지 않은
구조·결속 검증이다.

2026-09-15 당시 오염된 historical baseline과 v1 보고서로 실행한 결과는 다음과 같다. 이후 입력
Common IR의 수동 교정 블록 혼입이 확인됐으므로 이는 **비블라인드 legacy 참고 진단**이며 현재
routing release gate가 아니다.

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

이 표는 당시 실행 경로와 호출량을 재현하는 기록일 뿐, 깨끗한 입력에서의 routing 보존이나
source-selection/Profile 의미 정확도의 통과 기록이 아니다. 비용은 실행 계정에 적용되는
`gpt-5.6-terra` 단가가 별도로 확인되지 않았으므로 token 사용량만 고정한다.

관련 테스트:

```bash
UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend pytest -q \
  backend/tests/test_freeze_pristine_hard6_common_ir.py \
  backend/tests/test_pristine_common_ir.py \
  backend/tests/test_existing_a_routing_canary.py \
  backend/tests/test_existing_composite_shadow_integration.py \
  backend/tests/test_worker_core_contract.py
```

### 7.1 교정 6건 Existing Profile full canary

`run_existing_profile_canary.py`는 같은 고정 6건에서
`section scope → block router → source-selection → final Profile assembly`를 실행하고, 각
공고의 Profile과 그 Profile을 만든 **동일한 finalized source-selection artifact**를 후보 ZIP에
남긴다. native exact mode는 `lines+continuations`, composite은 `shadow`로 고정한다.

pristine archive를 사용한 실제 Terra full canary는 아직 실행하지 않았다. 아래 dry-run과
오프라인 gate가 통과한 것은 입력·계약·결속이 준비됐다는 뜻이지 의미 품질 개선이 입증됐다는
뜻이 아니다. 2026-09-15 실행은 7.2절의 오염된 legacy 진단으로만 취급한다.

기본 명령은 Gold와 historical baseline을 읽지 않고, input ZIP의 SHA·Common IR 범위·수동
교정 metadata 부재와 prompt pin·호출 상한만 확인하는 dry-run이다.

```bash
cd /path/to/SKN30-FINAL-4Team

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/run_existing_profile_canary.py \
  --input-zip .runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.zip
```

실제 실행은 pin된 무교정 Common IR을 OpenAI에 전송한다. 따라서 새 input archive pin 검토와
자료 전송·비용 승인을 받은 뒤에만 `--execute-openai`를 붙인다. `OPENAI_LOG=debug`는 원문 또는
SDK 진단 노출 위험 때문에 허용하지 않는다. API key는 환경에서만 읽고 후보 ZIP·보고서·표준
출력에 기록하지 않는다.

```bash
REPORT_DIR="$(mktemp -d /tmp/existing-profile-canary.XXXXXX)"

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --env-file backend/.env --project backend \
  python backend/scripts/run_existing_profile_canary.py \
  --input-zip .runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.zip \
  --baseline-zip /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --execute-openai \
  --model gpt-5.6-terra \
  --timeout-seconds 90 \
  --output-dir "$REPORT_DIR"

jq . "$REPORT_DIR/existing-profile-canary.v1.json"
```

`--output-dir`은 비어 있는 Gold 밖 디렉터리여야 한다. provider 실행과 후보 생성이 완료되고
semantic 비교 보고서까지 게시되면 gate 통과 여부와 관계없이 정확히 다음 세 최상위 파일을
만든다. 의미 불일치라면 세 파일을 남기고 종료 코드 `2`를 반환한다.

```text
existing-profile-canary.candidates.v1.zip
existing-profile-canary.v1.json
existing-profile-semantic-bgic.v1.json
```

후보 ZIP은 공고마다 정확히 세 파일만 가진다.

```text
PBLN_…/pipeline/structured_profile.v0.2.json
PBLN_…/pipeline/source_selection.json
PBLN_…/pipeline/common_ir_v1/PBLN_….{pdf|hwp|hwpx}.json
```

ZIP member 순서, JSON key 순서, ZIP timestamp/권한은 고정되어 동일한 artifact 입력이면
결정적 bytes를 만든다. canary 요약 보고서는 raw Common IR·model response·prompt·secret을 넣지
않고 hash, 호출 수, token 수, 단계 상태만 기록한다. 함께 쓰는
`existing-profile-semantic-bgic.v1.json`은 비교기의 opaque hash/count/공고별 gate 진단만
담는 상세 semantic 보고서다.

provider SDK 재시도는 0이다. source-selection은 초기 선택과 서버 검증에 따른 최대 한 번의
repair만 허용한다. 호출을 provider에 넘기기 전에 task별 상한을 차감한다: section scope 2,
block router 6, source-selection 12, anchor correction 12, 총 32회다. 응답 길이도 task별로
고정한다: scope/correction 4,096, router 16,384, source-selection 32,768 completion tokens. timeout은
120초 이하만 허용한다. reasoning effort는 `medium`으로 고정하고 temperature는 지정하지 않는다.
첫 공고 실패 시 나머지 공고 호출은 중단한다.

Gold와 historical baseline은 모든 OpenAI 호출이 끝나기 전에는 열거나 hash하지 않는다. input
Common IR 자체도 수동 교정 metadata가 없어야 한다. 호출이 모두 끝난 뒤에만 freeze manifest
pin을 검증하고 `compare_existing_profile_semantics.py`의 B/G/I/C semantic gate를 이 6건 후보
ZIP에 로컬 실행한다. B는 역사 기준과 94/6 calibration, G는 사람 검토 oracle, I는 실제 pristine
모델 입력, C는 이번 실행 후보다. 배포용 `document.provenance.source_location`을 제외하면 C의
Common IR은 I와 같아야 한다.

성공 판정은 `execution_status=succeeded`와 `semantic_gate.status=passed`가 모두 성립하는 경우다.
`semantic_gate.status=failed`는 모델 호출 성공과 별개로 후보 의미 graph가 Gold와 같지 않거나
로컬 provenance/입력 검증이 실패했다는 뜻이며, 결과를 Gold에서 복사해 보정해서는 안 된다.
먼저 해당 공고의 gate 진단과 일반화 가능한 source-selection/assembly 규칙을 검토하고 fixture와
회귀 테스트를 추가한다.

### 7.2 2026-09-15 full canary 실제 실행 결과와 2026-09-16 감사 정정

고정 baseline Common IR 6건을 OpenAI `gpt-5.6-terra`에 전송해 실제 실행했다. 당시에는 Gold
root를 모든 provider 호출 뒤에만 열었으므로 Gold 디렉터리의 Profile·selection을 직접 읽어
payload에 넣지는 않았다. 그러나 2026-09-16 감사에서 **전송한 baseline Common IR 자체에**
`adj:` 및 `manual_gold`/adjudication provenance를 가진 수동 교정 블록이 포함됐고, 그중 일부가
모델 후보에 노출됐음을 확인했다. 따라서 “Gold와 완전히 격리된 blind canary”라는 종전 설명을
철회한다.

OpenAI 호출과 6건의 Profile·source-selection 생성은 모두 완료됐지만 Gold 의미 회귀 gate는
0/6으로 실패했다. 이 실행은 전송·직렬화·산출물 생성 경로를 확인하고 후속 문제를 찾는
**비블라인드 진단 자료**로만 보존한다. Existing Profile 의미 품질 통과, 회귀 gate 기준점,
배포 승인 결과로 사용할 수 없다. 당시 CLI 종료 코드는 의미 불일치를 뜻하는 `2`였다.

| 항목 | 결과 |
|---|---:|
| 실행 상태 | `succeeded` |
| Profile 생성 | 6 / 6 |
| 의미 gate | 선택 6 / 통과 0 / 실패 6 |
| 호출 | 시도 17 / provider 응답 17 |
| 호출 구성 | scope 2 / router 6 / source-selection 8 / anchor correction 1 |
| token | prompt 546,984 / completion 51,179 / 합계 598,163 |
| 누적 provider 지연시간 | 460,946 ms |
| 후보 ZIP SHA-256 | `c46e51f648f8ac0d94d791f2e1ecc8bef18d4cb73309d4195f3dbb164fc6e5b3` |

로컬 진단 산출물은 아래 Git 제외 경로에 보존했다. 이 자료는 오염 사실을 포함한 역사적
진단·감사용이며 재검증 baseline, 배포 또는 Existing KB bootstrap 입력이 아니다.

```text
.runtime/evaluations/existing-profile-canary-20260915/
├── existing-profile-canary.candidates.v1.zip
├── existing-profile-canary.v1.json
└── existing-profile-semantic-bgc.v1.json
```

위 `existing-profile-semantic-bgc.v1.json`은 당시 계약으로 생성한 legacy B/G/C 보고서명이다.
현재 pristine 실행의 보고서명은 `existing-profile-semantic-bgic.v1.json`이다.

후보 ZIP을 당시 B/G/C 비교 코드로 다시 오프라인 검증해도 선택 6 / 통과 0 / 실패 6과 종료
코드 `2`가 재현됐다. 현재 B/G/I/C gate에는 이 오염 후보를 입력하지 않는다. 여섯 공고 모두
승인된 Gold 추가 atom 회수는 0이었다. 원문이 없어서 실패한 경우보다
다음과 같은 source-selection 및 assembly 문제가 공통적으로 확인됐다.

- 표의 행·열 축과 병합 문맥을 완전한 명제 및 지원 컴포넌트로 조립하지 못함
- 연속 목록·잘린 문장을 결합하지 못하고 수량 한정어와 조건을 누락함
- 지원 대상과 수혜자, 지원 방식과 지원 내용, 일정 단계와 지원 패키지를 혼동함
- 공고 수준 사실과 컴포넌트 소속 사실, 금액·기간·부담률 관계를 잘못 연결하거나 누락함
- 근거보다 많은 facet·지원 규모 projection을 파생함

후속 수정은 Gold 문구를 복사하지 않고 native Common IR에서 일반화 가능한 표 축 조립, 목록
연속성, 컴포넌트 후보 생성, 전 항목 coverage 검사를 결정적 전처리·검증 규칙으로 추가한 뒤
최소 fixture와 이 6건 회귀 gate로 검증한다.

## 8. ID·순서 비의존 Profile 의미 회귀 비교

`compare_existing_profile_semantics.py`는 자동 historical baseline(`B`), 사람 검토 Gold(`G`),
고정 pristine 모델 입력(`I`), 새 후보(`C`)를 함께 검증하고 비교한다. 후보의
**Profile + source-selection + Common IR** 세 산출물을 검사하며 OpenAI·DB·Storage를 호출하지
않는다. 생성할 때마다 달라질 수 있는 Fact·component ID와 set-like 배열 순서는 비교에서
제외하지만, 원문 값·상태·역할·component membership·방향성 관계·지원 규모 projection은
보존한다.

이 명령은 입력 파일명이나 경로를 신뢰하지 않는다. 실행 전에 B, G, I의 SHA-256을 검증하고,
후보 ZIP도
`--expected-candidate-sha256`을 주면 같은 방식으로 검증한다. pin 불일치는 비교나
보고서 기록 전에 종료 코드 `1`로 실패한다. B/G pin은 Gold v5 calibration trust root이고,
I pin은 현재 hard-6 모델 입력 trust root다.

최종 통과 조건은 승인 delta를 부분적으로 세는 휴리스틱이 아니라 의미 multigraph의
`C == G`다. 보고서에는 원문 대신 atom SHA-256과 종류·개수만 기록한다. 입력 ZIP 파일명이나
Gold 디렉터리명도 복사하지 않고 corpus SHA-256·건수·역할만 남긴다.

후보 ZIP은 공고마다 다음 세 JSON을 반드시 포함해야 한다.

```text
PBLN_<15자리>/pipeline/structured_profile.v0.2.json
PBLN_<15자리>/pipeline/source_selection.json
PBLN_<15자리>/pipeline/common_ir_v1/<한 개의 JSON>
```

후보의 Common IR은 Gold나 historical baseline에서 복사하지 않는다. 후보 Profile을 생성한
고정 pristine `I`를 후보 ZIP에도 포함해야 한다. 배포 경로인
`document.provenance.source_location`만 달라질 수 있으며, 나머지 Common IR이 I와 다르면 의미
비교 전에 실패한다. B는 모델 입력이 아니라 역사 delta와 94/6 calibration 전용이다.

후보의 출처 정보도 후보가 스스로 주장한 문자열만으로 신뢰하지 않는다. parent lineage가 없는
RunPod `0.1.4` 호환 후보는 I Common IR에서 기본 projection → PDF inspector의 native
table occurrence → native line atom → 최대 3개 native continuation composite 순으로
**결정적 source universe**를 다시 만들며, 후보는 그 ID·본문이 정확히 같은 block의 부분집합만
쓸 수 있다. 반면 `semantic_structuring.native_exact_transform`의 parent lineage가 기록된 현재
worker 후보는 source-selection의 atomic ID 집합만 라우팅 결과로 받아들이고, 각 block의
본문·locator를 I Common IR에서 다시 만든 production A-pack에 같은 transform variant를 적용한다.
이 경우 current/parent pack identity와 전체 `source_block_texts` map까지 정확히 일치해야 한다.
historical baseline 전용 legacy block이나 Gold 수동 교정
`adj:*` block을 후보가 복사하는 것은 허용하지 않는다. source-selection의 선택값, materialized
evidence, Profile fact/component도 서로 일치해야 한다. 지원 규모 수치는 locator의 원문 토큰을
다시 해석해 measure 종류·단위·역할·값과 맞는지 확인한다. 새 후보의 component 이름은 해당
component source block 안에 정확히 한 번 존재해야 하며, 숫자 locator는 더 큰 숫자의 일부가
아니라 서버의 complete-token 규칙으로 독립 재열거된 span이어야 한다. 과거 B/G의 검토 delta는
별도 calibration으로 읽되 이 새 후보 admission 규칙을 우회해 후보 합격으로 취급하지 않는다.

parent lineage가 없는 RunPod `0.1.4` 호환 source universe는 그 파이프라인의
section-scope·block-router 선택을 그대로 재현하는 CandidatePack parity 검사가 아니다. 실제
router가 어떤 block을 노출했는지는 별도의 production parity 테스트 대상이다. 여기서는 후보가
사용한 값과 근거가 고정 Common IR에서 결정적으로 재생성 가능한지만 fail-closed로 검증한다.
parent lineage가 있는 현재 worker 후보는 위 production A-pack replay 규칙이 추가 적용된다.
보고서의
`normalization.candidate_source_admission.scope`도 이 범위를 명시한다.

후보의 evidence/context occurrence locator ID는 source admission 단계에서 I와 결속해 엄격히
검증한다. 다만 검증을 마친 locator ID 자체는 C와 G의 의미 동등성 atom에는 넣지 않는다. 수동
adjudication 과정에서 새 occurrence ID를 만든 Gold를 후보가 복제하게 하지 않기 위해서다.
대신 검증된 CandidatePack 근거 원문의 SHA-256을 `evidence`와 `context_evidence`에 각각 남기므로,
UUID만 달라지고 근거 내용이 같으면 통과하지만 다른 문단을 근거로 고르면 실패한다.
CandidatePack block/offset과 Common IR block/cell/section도 admission 단계의 locator로 검증하고,
component 의미 자체인 kind·이름·상태·소속 fact는 비교한다. B/G calibration은 기존의
provenance-sensitive 94/6 비교를 그대로 유지한다.

의미를 버릴 위험이 있는 아직 지원하지 않는 구조는 조용히 무시하지 않고 실패한다. 현재
`table_catalog`, `unresolved_relations`, 두 경계를 하나의 locator로 표현한 `range` measure가
여기에 해당한다. `unresolved_observations[].reason`은 무시 대상이 아니라 보존되는 의미다.

현재 transform은 원문에 없는 구두점을 삽입하거나 bullet을 제거해 새 문장을 만들지 않고,
여러 표 셀의 관계를 임의의 문장으로 합성하지도 않는다. 예를 들어 `117175`의 주관/공동기관
eligibility 교정값은 label과 body 사이의 `: ` 삽입 및 두 번째 bullet 제거가 필요하므로 현재
계약으로는 exact source를 만들 수 없다. 이런 값은 `adj:*`를 예외 허용하지 않고, 향후
`derived_text_basis + source_spans`와 표 관계를 포함한 versioned production transform 계약을
정한 뒤 지원한다. 따라서 이 단계의 비교기는 B/G/I/C 의미 gate와 출처 검증 도구를 제공하지만,
현 production pipeline이 Gold 100건을 전부 생성할 수 있다고 주장하지 않는다.

현재 I loader 계약은 교정 hard-6 정확히 6건이다. 후보 C도 같은 6건이어야 한다. 검사 예시는
다음과 같다.

```bash
cd /path/to/SKN30-FINAL-4Team

REPORT_DIR="$(mktemp -d /tmp/existing-semantic-bgic.XXXXXX)"

UV_CACHE_DIR=/tmp/prereview-uv-cache \
uv run --project backend python backend/scripts/compare_existing_profile_semantics.py \
  --baseline-zip /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --gold-root /path/to/frozen_existing_profile_gold_100_20260909_v5 \
  --input-zip .runtime/evaluations/pristine-hard6-common-ir-20260917-v1/pristine-hard6-common-ir.v1.zip \
  --candidate-zip /path/to/candidate-existing-profile-hard6.zip \
  --output-dir "$REPORT_DIR" \
  --expected-reference-count 100 \
  --expected-candidate-count 6 \
  --expected-baseline-sha256 6649f1a5aab36f659d688634103950d3b73f8a5903a453aabdbbd9f5bc0f7f0d \
  --expected-gold-freeze-manifest-sha256 a2c35fb4c98c92c23ff34faa045a16ec4e8ed1ea4bae5397caab547cb3db6987 \
  --expected-input-sha256 24d70a944f43446563a257958fc5b97a5c484e28b5ac653ccf3f2792880d7800 \
  --expected-candidate-sha256 <후보_ZIP_SHA256>

jq . "$REPORT_DIR/existing-profile-semantic-bgic.v1.json"
```

전체 hard-6를 검사할 때는 `--notice-id`가 필요 없다. 부분 진단이 필요할 때만 아래 ID 중
`--notice-id`를 반복하며, 중복·미등록 ID는 허용하지 않는다.

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
실행한다. calibration은 B/G 전용이므로 `--input-zip`, `--input-manifest`,
`--expected-input-sha256`을 주면 실패한다.

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
