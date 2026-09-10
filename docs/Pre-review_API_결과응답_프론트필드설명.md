## 1. CPL: `self_check`

요청서 필수 항목 13개의 자체 점검 결과다.

```json
{
  "confirmed_count": 8,
  "total_count": 13,
  "confirmation_rate": 61.538,
  "items": []
}
```

| 필드 | 뜻 |
|---|---|
| `confirmed_count` | `PRESENT`와 `NOT_APPLICABLE` 상태 항목 수 |
| `total_count` | CPL 전체 점검 항목 수. 항상 `13` |
| `confirmation_rate` | `confirmed_count / 13 * 100` |
| `items` | 프론트에 보여 줄 항목별 결과 |

### `self_check.items[]`

```json
{
  "field_code": "TARGET_AND_CONDITIONS",
  "status": "NEEDS_CONFIRMATION",
  "evidence": [
    { "excerpt": "지원 대상·조건의 원문 근거" }
  ]
}
```

| 필드 | 뜻 |
|---|---|
| `field_code` | CPL 항목 코드. 프론트에서 한글 항목명으로 매핑 |
| `status` | 항목 점검 상태 |
| `display` | 요청 유형처럼 별도 화면 표현이 필요한 항목에만 제공 |
| `evidence` | 확인 필요·누락 항목의 원문 근거. 분석용 세부 조각은 합쳐 최대 1개로 제공 |

### CPL 상태

| 값 | 뜻 |
|---|---|
| `PRESENT` | 필요한 내용 확인 |
| `MISSING` | 필요한 내용 누락 |
| `NOT_APPLICABLE` | 원문에 해당 없음이 명시됨 |
| `NEEDS_CONFIRMATION` | 원문은 있으나 확정하기 어려워 확인 필요 |

`PARSE_FAILED` 항목은 프론트 응답의 `items`와 해당 CPL 검토 이슈에서 제외한다. 내부 보고서에는 보존한다.

### CPL 항목 코드

| `field_code` | 화면 항목명 |
|---|---|
| `REQUEST_TYPE` | 사전협의 요청 유형 |
| `PURPOSE_GOAL` | 사업 목적·목표 |
| `IMPLEMENTATION_PLAN` | 추진계획 |
| `BUSINESS_PERIOD` | 사업 기간 |
| `NEW_OR_CHANGED_CONTENT` | 신설·변경 내용 |
| `BUSINESS_NEED` | 사업 필요성 |
| `LEGAL_BASIS` | 법적 근거 |
| `LINKED_POLICY` | 연계 정책·계획 |
| `BUDGET` | 예산 |
| `TARGET_AND_CONDITIONS` | 지원 대상·조건 |
| `SUPPORT_CONTENT_AND_SCALE` | 지원 내용·규모 |
| `DELIVERY_SYSTEM` | 수행 체계 |
| `EXPECTED_EFFECTS_AND_PERFORMANCE` | 기대효과·성과지표 |

### 요청 유형 표시 데이터: `display`

`REQUEST_TYPE`에만 제공한다.

```json
{
  "display": {
    "type": "checkbox_group",
    "summary": "선택된 요청 유형이 없습니다.",
    "options": [
      {
        "code": "SUBPROGRAM_NEW",
        "label": "내역사업 신설",
        "selected": false
      }
    ]
  }
}
```

| 필드 | 뜻 |
|---|---|
| `type` | 화면 표시 형식 |
| `summary` | 표시용 한 줄 요약 |
| `options[].code` | 요청 유형 코드 |
| `options[].label` | 표시 라벨 |
| `options[].selected` | 선택 여부 |

## 2. FIT: `structural_consistency`

요청서 내부 항목 간 논리적 연결 관계 7개의 점검 결과다.

```json
{
  "module_status": "AVAILABLE",
  "availability": {
    "assessable_count": 3,
    "total_count": 7
  },
  "relations": []
}
```

| 필드 | 뜻 |
|---|---|
| `module_status` | FIT 모듈 결과 생성 여부. `AVAILABLE` 또는 `UNAVAILABLE` |
| `availability.assessable_count` | 실제 비교 결과를 낼 수 있었던 관계 수 |
| `availability.total_count` | 전체 관계 수. 항상 `7` |
| `relations` | 관계별 상세 결과 |

### `relations[]`

```json
{
  "relation_id": "FIT-1",
  "status": "NEEDS_REVIEW",
  "summary": "목적과 대상 범위의 연결이 명확하지 않습니다.",
  "left_evidence": [{ "excerpt": "왼쪽 원문 근거" }],
  "right_evidence": [{ "excerpt": "오른쪽 원문 근거" }]
}
```

| 필드 | 뜻 |
|---|---|
| `relation_id` | FIT 관계 코드 |
| `status` | 관계 점검 상태 |
| `summary` | 관계 설명 |
| `left_evidence` | 비교 왼쪽 항목의 원문 근거 |
| `right_evidence` | 비교 오른쪽 항목의 원문 근거 |

원문 근거는 분석용 세부 조각을 합쳐 각 배열에 최대 1개로 제공한다.

### FIT 관계 코드

| 코드 | 비교 내용 |
|---|---|
| `FIT-1` | 목적의 대상 조건 ↔ 지원 대상 |
| `FIT-2` | 목적 방향 ↔ 지원 활동·수단 |
| `FIT-3` | 목적 방향 ↔ 기대효과·성과지표 |
| `FIT-4` | 사업 계층 간 비교 |
| `FIT-5` | 대상군 ↔ 지원 조건 |
| `FIT-6` | 수행기관 ↔ 절차·역할 |
| `FIT-7` | 지원 내용 ↔ 지원 규모 정량값 |

### FIT 상태

| 값 | 뜻 |
|---|---|
| `FIT` | 연결 관계 확인 |
| `NEEDS_REVIEW` | 근거는 있으나 추가 검토 필요 |
| `CONFLICT` | 관계 충돌 확인 |
| `INSUFFICIENT` | 비교 정보 부족 |

## 3. 통합 검토 이슈: `review_issues`

CPL·FIT·SIM 결과 중 검토 필요 항목.

```json
{
  "source": "FIT",
  "status": "NEEDS_REVIEW",
  "evidence": [
    { "excerpt": "검토 근거 원문" }
  ]
}
```

| 필드 | 뜻 |
|---|---|
| `source` | 이슈 출처: `CPL`, `FIT`, `SIM` |
| `status` | 출처 모듈의 검토 상태 |
| `evidence` | 검토 근거 원문. 중복 조각은 합쳐 최대 1개로 제공 |


## 4. SIM: `similar_candidates`


```json
{
  "rank": 1,
  "title": "2026년 ICT 미래시장 선점 R&D 지원사업 공고",
  "source_url": "https://...",
  "relevance_score": 84,
  "comparison_summary": "후보 전체 비교 요약",
  "axes": {}
}
```

| 필드 | 뜻 |
|---|---|
| `rank` | 후보 표시 순서 |
| `title` | 후보 공고 제목 |
| `source_url` | 공고 전체 보기 링크 |
| `relevance_score` | 후보 탐색 참고값. 화면 표기는 `후보 관련도 84` |
| `comparison_summary` | 후보 전체 비교 요약 |
| `axes` | 목적·대상·내용·수행체계 4개 축 비교 결과 |

### `axes`

| 필드 | 화면 이름 |
|---|---|
| `purpose` | 사업 목적 |
| `target` | 지원 대상 |
| `content` | 지원 내용 |
| `delivery` | 수행 체계 |

### 축별 구조

```json
{
  "status": "PARTIAL",
  "summary": "일부 공통점이 확인되었습니다.",
  "common_points": ["공통점"],
  "differences": ["차이점"],
  "request_evidence": [{ "excerpt": "현재 요청서 원문" }],
  "candidate_evidence": [{ "excerpt": "후보 공고 원문" }]
}
```

| 필드 | 뜻 |
|---|---|
| `status` | 축별 비교 상태 |
| `summary` | 축별 비교 설명 |
| `common_points` | 확인된 공통점 |
| `differences` | 확인된 차이점 |
| `request_evidence` | 현재 요청서 원문 근거 |
| `candidate_evidence` | 후보 공고 원문 근거 |


### SIM 상태

| 값 | 뜻 |
|---|---|
| `SIMILAR` | 공통점 확인 |
| `PARTIAL` | 일부 공통점 확인 |
| `DIFFERENT` | 차이 확인 |
| `INSUFFICIENT` | 비교 정보 부족 |
