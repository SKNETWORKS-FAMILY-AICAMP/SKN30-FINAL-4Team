# Pre-review `report_json` v0.1 프론트 계약 전달안

## 1. 문서 목적

이 문서는 Pre-review 알파 결과 화면과 PDF에서 공통으로 사용할
`report_json` v0.1 계약을 프론트와 합의하기 위한 초안이다.

프론트 화면과 PDF는 동일한 `report_json`을 데이터 원천으로 사용한다.
프론트 협의 결과를 반영하고 사용자에게 최종 확인받은 후
`alpha-report-v0.1`로 고정한다. 이후 Composer, 불변 저장, PDF,
조회·다운로드 API를 Slice 10에서 순서대로 구현한다.

## 2. 이번에 확정한 사항

1. CPL 확인율은 `(PRESENT + NOT_APPLICABLE) / 13 * 100`이다.
2. CPL·FIT·SIM 판정 상태는 한글 문구가 아니라 코드값으로 전달한다.
3. 사업명 추출 기능은 새로 만들지 않는다. `case.title`은 원본 파일명을
   fallback으로 사용한다.
4. BEN 참고정보 생성은 알파에서 제외한다. 다만 구현기준서의 최상위 계약을
   유지하기 위해 `ben_references`는 예약 필드로 두고 항상 빈 배열을 반환한다.
5. 분석 건 사용자 삭제 기능은 알파에서 제외한다.
6. 화면 목업의 T1/T2 심각도, 승인·거절 선례, 평균 대비 수치는 데이터 근거가
   없으므로 계약에 포함하지 않는다.
7. `report_download_url`은 불변 `report_json`에 저장하지 않는다. 결과 조회
   API가 PDF 생성 상태에 따라 동적으로 추가한다.
8. 후보별 차이 정보의 기준 원천은 `similar_candidates[].axes[].differences`다.
   최상위 `differences`는 예약 필드로 두고 v0.1에서는 항상 빈 배열을 반환한다.

## 3. 저장되는 `report_json` v0.1

```ts
type ReportJsonV01 = {
  schema_version: "alpha-report-v0.1";

  case: {
    case_id: number;
    title: string;              // 원본 파일명 fallback
    created_at: string;         // ISO 8601
    completed_at: string;       // ISO 8601
  };

  ui_status: "COMPLETED";

  self_check: {
    confirmed_count: number;
    total_count: 13;
    confirmation_rate: number;

    items: Array<{
      field_code: string;
      status:
        | "PRESENT"
        | "MISSING"
        | "NOT_APPLICABLE"
        | "NEEDS_CONFIRMATION"
        | "PARSE_FAILED";
      reason_code: string | null;
      explanation: string | null;
      occurrences: ReportEvidence[];
    }>;

    ruleset_version: string;
    prompt_version: string;
    model_profile: string;
    warnings: string[];
  };

  structural_consistency: {
    module_status: "AVAILABLE" | "UNAVAILABLE";

    score: {
      value: number | null;
      numerator: number;
      denominator: number;
      assessable_count: number;
      total_count: 7;
      scoring_version: string;
    };

    relations: Array<{
      relation_id:
        | "FIT-1"
        | "FIT-2"
        | "FIT-3"
        | "FIT-4"
        | "FIT-5"
        | "FIT-6"
        | "FIT-7";
      status: "FIT" | "NEEDS_REVIEW" | "CONFLICT" | "INSUFFICIENT";
      score: number | null;
      summary: string;
      reason_code: string | null;
      left_evidence: ReportEvidence[];
      right_evidence: ReportEvidence[];
      rule_version: string;
      prompt_version: string;
    }>;

    ruleset_version: string;
    prompt_version: string;
    scoring_version: string;
    model_profile: string;
    warnings: string[];
  };

  review_issues: Array<{
    issue_id: string;
    source: "CPL" | "FIT" | "SIM";
    reference_id: string;
    status:
      | "MISSING"
      | "NEEDS_CONFIRMATION"
      | "PARSE_FAILED"
      | "NEEDS_REVIEW"
      | "CONFLICT"
      | "INSUFFICIENT"
      | "FOCUS_REVIEW"
      | "GENERAL_REVIEW";
    summary: string;
    reason_code: string | null;
    evidence: ReportEvidence[];
  }>;

  similar_candidates: SimCandidate[];

  ben_references: [];           // 알파 v0.1 예약 필드, 항상 빈 배열
  differences: [];              // 알파 v0.1 예약 필드, 항상 빈 배열

  warnings: string[];
};
```

`structural_consistency.module_status`의 의미는 다음과 같다.

- `AVAILABLE`: 유효한 `FitResult`가 생성된 상태다. 일부 관계가
  `INSUFFICIENT`이거나 LLM 경고가 있어도 여기에 해당한다.
- `UNAVAILABLE`: FIT 모듈 자체의 실행 오류로 `FitResult`를 생성하지 못한
  상태다.

관계별 정보 부족과 모듈 실행 실패를 같은 상태로 취급하지 않는다.

## 4. 공통 Evidence 계약

```ts
type ReportEvidence = {
  evidence_ref: string;
  source_side: "REQUEST" | "ANNOUNCEMENT";
  source_id: string;

  field_code: string | null;
  axis_code: string | null;
  source_role: string | null;

  excerpt: string;
  normalized_value: unknown | null;

  block_id: string | null;
  page_no: number | null;
  section_path: string[];
  source_locator: Record<string, unknown>;

  extraction_method: "RULE" | "LLM" | "SOURCE";
  extraction_version: string;
};
```

현재 알파 HWP/HWPX 파서는 페이지 번호를 제공하지 않으므로 `page_no`는 항상
`null`이다. 프론트는 페이지 번호 노출을 전제로 분기를 만들거나 번호를
임의로 생성하지 않는다. 근거 위치는 `section_path`와 `source_locator`를
사용해 표시한다.

## 5. SIM 후보 계약

```ts
type SimCandidate = {
  rank: number;
  announcement_id: string;
  announcement_version_id: number;
  title: string;
  source_url: string;

  semantic_similarity: number;
  semantic_similarity_display: number;

  weighted_score: number | null;
  assessable_axis_count: number;
  review_grade:
    | "FOCUS_REVIEW"
    | "GENERAL_REVIEW"
    | "LOW_PRIORITY"
    | "ON_HOLD";

  comparison_summary: string;

  axes: {
    purpose: SimAxisResult;
    target: SimAxisResult;
    content: SimAxisResult;
    delivery: SimAxisResult;
  };

  warnings: string[];
  ruleset_version: string;
  prompt_version: string;
  scoring_version: string;
  model_profile: string;
};

type SimAxisResult = {
  axis_id: "SIM-1" | "SIM-2" | "SIM-3" | "SIM-4";
  status: "SIMILAR" | "PARTIAL" | "DIFFERENT" | "INSUFFICIENT";
  score: number | null;
  summary: string;
  common_points: string[];
  differences: string[];
  request_evidence: ReportEvidence[];
  candidate_evidence: ReportEvidence[];
  reason_code: string | null;
};
```

`semantic_similarity_display`는 코사인 값을 0~100으로 변환한 표시값이며
중복 확률이 아니다. 화면에서 `% 중복 확률`처럼 표시하지 않는다.

## 6. 결과 조회 API 응답

DB에 저장되는 `report_json`에는 `report_download_url`이 없다. 결과 조회 API는
저장된 JSON에 다음 필드를 동적으로 추가해 응답한다.

```ts
type ReportResponse = ReportJsonV01 & {
  report_download_url: string | null;
};
```

PDF가 준비되지 않았으면 `null`, 준비됐으면 다음과 같은 상대 URL을 제공한다.

```json
{
  "report_download_url": "/api/v1/cases/123/report.pdf"
}
```

## 7. 검토 쟁점 파생 기준안

심각도 등급을 새로 만들지 않고 원본 상태 코드를 그대로 제공한다.

- CPL: `MISSING`, `NEEDS_CONFIRMATION`, `PARSE_FAILED`
- FIT: `NEEDS_REVIEW`, `CONFLICT`, `INSUFFICIENT`
- SIM: `FOCUS_REVIEW`, `GENERAL_REVIEW` 후보

SIM의 `DIFFERENT`는 곧바로 문제가 있다는 뜻이 아니므로 자동 쟁점으로 만들지
않고 `differences`에 기록한다.

여기서 `differences`는 후보별 `similar_candidates[].axes[].differences`를
뜻한다. 최상위 예약 필드 `differences`는 v0.1에서 비워 둔다.

## 8. 분석 진행 상태 API

현재 백엔드의 다음 API는 한글 상태값을 반환한다.

```text
GET /api/v1/cases
GET /api/v1/cases/{case_id}/status
```

두 API의 화면 상태는 구현기준서에 따라 다음 세 한글 값으로 유지한다.

```text
분석 중 / 분석 완료 / 분석 실패
```

CPL·FIT·SIM 판정 상태를 코드값으로 전달한다는 결정은 분석 건의 진행 상태와
별개다. CPL·FIT·SIM 코드값만 프론트가 한글 문구·색상·아이콘으로 변환한다.

## 9. Retrieval 결과 표현

Retrieval 실행 실패와 정상적인 후보 0건을 구분한다.

- Retrieval 실패: 분석 건이 `분석 실패`가 되며 `report_json`은 생성하지 않는다.
- Retrieval 성공·후보 없음: 완료 보고서에서 `similar_candidates: []`를 반환한다.
- Retrieval 성공·후보 있음: 완료 보고서에서 후보 목록을 반환한다.

프론트는 `similar_candidates: []`를 오류로 표시하지 않고 `유사사업 후보 없음`
상태로 표시한다.

## 10. 프론트 확인 요청

아래 항목에 대해 승인 또는 수정 의견을 전달해주기 바란다.

1. 최상위 필드명과 중첩 구조
2. nullable 필드 처리 방식
3. `ReportEvidence`를 CPL·FIT·SIM에서 공통으로 사용하는 방식
4. 판정 상태 코드의 한글 표시 매핑
5. `report_download_url`을 결과 조회 응답에서 동적으로 제공하는 방식
6. `ben_references`와 최상위 `differences`를 v0.1 예약 빈 배열로 유지하는 방식
7. Retrieval 성공 후 후보가 0건일 때 `유사사업 후보 없음`으로 표시하는 방식
8. BEN 블록과 분석 건 삭제 UI의 알파 제외 반영

프론트 협의 결과를 반영하고 사용자에게 최종 확인받은 후 Composer,
불변 저장, PDF, 조회·다운로드 API를 Slice 10에서 순서대로 구현한다.
