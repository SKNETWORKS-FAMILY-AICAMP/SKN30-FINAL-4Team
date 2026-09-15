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

## 4. 후보 파이프라인 평가 원칙

- 동일한 원본 SHA-256과 동결 설정으로 실행한다.
- 실행 산출물은 Gold 디렉터리 밖 별도 output 경로에 쓴다.
- schema/exact-span/provenance 실패는 의미 비교 전에 실패 처리한다.
- 100건 aggregate뿐 아니라 위 6건의 교정 사유별 결과를 별도로 보고한다.
- 개선과 악화를 함께 기록한다. 한 공고 개선을 위해 다른 94건을 바꾸면 자동 승인하지 않는다.
- 의미 판정이 필요한 새 차이는 새 adjudication record를 만든 뒤 Gold 차기 버전에만 반영한다.

현재 `verify_existing_gold100.py`는 freeze 무결성 gate까지만 제공한다. 후보 pipeline을 실제로
100건 재실행하고 field/relationship 단위 diff를 내는 runner는 다음 구현 단계이며, OpenAI를
호출하는 경우 별도 승인·모델 pin·prompt bundle·token/latency 기록이 필요하다.
