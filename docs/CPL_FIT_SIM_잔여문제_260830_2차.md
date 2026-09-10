# CPL·FIT·SIM 잔여 문제 (2026-08-30, 2차)

`docs/CPL_FIT_문제정리_260830.md`(1차)의 문제 1~4는 해소됐다. 이 문서가 1차를 대체한다.

측정 기준: 실제 파이프라인 실행 결과와 `samples/golden/mockup_08.json`(사람 판독 정답 79건) 대조.
채점 도구: `backend/app/services/cpl/golden_evaluator.py`

---

## 0. 현재 상태

| 문서 | CPL 확인율 | FIT | 판정 가능 |
|---|---|---|---|
| mockup_08 (CPL 전항목) | 12/13 | 90.0 | 5/7 |
| 사전협의요청서_우수사례 | 8/13 | 87.5 | 4/7 |
| mockup_03 (보통사례) | 11/13 | 0.0 | 1/7 |
| mockup_05 (미흡사례) | 10/13 | 0.0 | 1/7 |

골든셋 대비(mockup_08): 필드 상태 12/13, occurrence 회수 46/79(58.2%),
정밀도 46/65(70.8%), **정규값 정확도 24/24(100%)**.

테스트 149 passed. 실패 1건·에러 53건은 실제 PostgreSQL이 필요한 통합 테스트로 기존과 동일하다.

mockup_03·05의 낮은 FIT은 상당 부분이 **의도된 결함**이다. 아래는 그것을 제외한 잔여 문제만 담았다.

---

## A. Rule 결함 — 좁고 확실함

### A-1. 기간 파서가 무효 날짜에서 종료일을 시작일로 승격시킨다

```
_normalize_period("2026. 02. 30. ~ 2028. 12. 31.")
  → {'start': '2028-12-31', 'end': None, 'continuing': False, 'single_year': True}
```

2월 30일이 무효라 버려지는 것까지는 맞다. 그런데 **종료일이 시작일 자리로 올라가
3년 사업이 단년도로 기록된다.** 실패가 아니라 틀린 값을 저장하므로 우선순위가 높다.

기대: 한쪽 토큰이 무효면 전체를 `None`으로 두거나 `start=None`으로 남긴다.
어느 쪽이든 **다른 토큰을 그 자리에 채우지 않는다.**

정상 동작하는 표기(회귀 방지용):

```
2026. 01. ~ 2028. 12.                   → start 2026-01,    end 2028-12
2026. 01. 01. ~ 2028. 12. 31.           → start 2026-01-01, end 2028-12-31
2026-01-01 ~ 2028-12-31                 → 동일
2026.1.1.~2028.12.31.                   → 동일
2026년 1월 1일부터 2028년 12월 31일까지   → 동일
'21년 ~ '25년(5년)                      → start 2021, end 2025, two_digit_year_policy ASSUME_2000S
2026년~계속                              → start 2026, end None, continuing true
```

### A-2. 법령 fallback이 한정어를 법령명으로 만든다

```
_legal_citations("관련 법령에 따라 추진")
  → [('관련 법', {'law_name': '관련 법', 'article': None})]
```

CPL 판별기준 §8.2는 `관련 법령에 따라 추진`을 **근거 언급은 있으나 특정 불가**로 두라고 한다.
지금은 `관련 법`이라는 법령이 존재하는 것처럼 나와 거짓 `PRESENT`가 된다.

기대: 인용부호가 없는 fallback에서 `관련`, `해당`, `상위`, `기타` 같은 한정어로 시작하는
구간은 법령으로 잡지 않는다. 정상 케이스는 유지한다.

```
「중소기업진흥에 관한 법률」 제62조의2, 「부산광역시 중소기업 육성 및 지원 조례」 제9조
  → 2건, article 제62조의2 / 제9조         (현재 정상)
대구광역시 수출 중소기업 육성 조례
  → 1건, article None                      (현재 정상)
```

### A-3. `IMPLEMENTATION_PLAN`이 근거를 가진 채 `MISSING`이 된다

case 212 실제 출력:

```
status = MISSING / IMPLEMENTATION_PLAN_REQUIRED
  PROGRAM_LEVEL            '내역사업'
  SUBPROGRAM_PLAN_CONTENT  '1단계(2026~2027년): 시제품 제작 및 성능검증 중심 지원, 연 40개사'
  SUBPROGRAM_PLAN_CONTENT  '2단계(2028년): 인증·판로 연계 사업화 지원으로 전환, 40개사'
```

`MISSING`은 근거 없음을 뜻하는데 근거가 3건 있다. mockup_08의 유일한 필드 상태 불일치이며
CPL 상태 계약 위반이다.

`_resolve_implementation_plan`이 다년도 사업인데 `ANNUAL_PLAN_CONTENT`가 없다는 이유로
`MISSING`을 찍는다. 기대: **grounded occurrence가 하나라도 남아 있으면 `MISSING`이 아니라
`NEEDS_CONFIRMATION`**이어야 한다. `MISSING`은 근거가 0건일 때만 쓴다.

근본 원인은 B-3(축 오분류)이지만, 상태 계약은 원인과 무관하게 지켜져야 한다.

### A-4. 라벨 포함 여부만 다른 중복 occurrence

case 212 실제 출력:

```
DELIVERY_METHOD_TYPE  DELIVERY_METHOD  '시 출연기관 위탁(보조)'
DELIVERY_METHOD_TYPE  DELIVERY_METHOD  '수행방식: 시 출연기관 위탁(보조)'
```

같은 사실이 두 건이다. `raw_text`가 달라 dedup에 걸리지 않는다.
FIT-5의 범위 조건 오탐과 같은 계열이므로 지금 정리해두는 편이 안전하다.

기대: 같은 필드·축·구역에서 한쪽 `raw_text`가 다른 쪽을 포함하면 **긴 쪽만 남긴다.**
Slice 6의 Rule/LLM tie-break(동일 위치·원문·축·구역이면 Rule 우선)와는 별개 규칙이므로
그 계약은 건드리지 않는다.

---

## B. LLM 축 태깅 — 프롬프트 영역

**주의: B 항목은 A를 고치고 재측정한 뒤에 손대야 한다.** 지금 프롬프트를 또 바꾸면
어느 변경이 어디에 기여했는지 다시 분리할 수 없게 된다.
(이번 회차에서 scope 수정과 프롬프트 v0.9가 함께 들어가 이미 한 번 섞였다.)

### B-1. 사업목적에서 축이 하나만 나온다 — 영향이 가장 크다

```
mockup_08  PURPOSE_TARGET_CONDITION 등 3축      (정상)
mockup_05  PURPOSE_SPECIFIC_OBJECTIVE 만
mockup_03  PURPOSE_PROBLEM_DOMAIN 만
```

FIT-1은 좌측에 `PURPOSE_TARGET_CONDITION`, FIT-2·3은 `PURPOSE_DIRECTION`을 요구한다.
우측(지원대상·지원활동·기대효과)은 두 문서 모두 정상 추출됐는데 좌측이 비어서
**세 관계가 한꺼번에 죽는다.**

문서 탓이 아니다. mockup_03의 사업목적 원문에는 대상 한정과 방향이 모두 있다.

```
전북 도내 지역 자원 및 문화 특성을 활용하는 청년 로컬크리에이터의 시제품 제작 및
온·오프라인 판로 개척 지원을 통한 지역 정착 활성화
   대상 한정: 청년 로컬크리에이터
   방향:     활성화
```

**회귀 포함**: mockup_05의 `PURPOSE_TARGET_CONDITION`은 이전 실행(case 165)에서는 나왔다.
그때 FIT-1이 `FIT`이었고 지금은 `INSUFFICIENT`다.

한 문장이 여러 목적 축을 동시에 지지할 수 있고, 각 축을 별도 occurrence로 남겨야 한다는 점을
프롬프트에서 분명히 해야 한다. 지원조건에 대해서는 이미 그런 안내가 있다.

### B-2. 축 오분류

```
DELIVERY_STEP_ROLE  ← '부산테크노파크(주관), 부산상공회의소(협력)'   (수행기관 줄. 단계별 역할이 아님)
SUPPORT_INSTRUMENT  ← '기업당 한도: 최대 5,000만원'                (한도 금액은 전달수단이 아님)
```

서버는 축이 필드 허용 목록 안에 있으므로 통과시킨다. 축의 의미 구분을 프롬프트에서 좁혀야 한다.

### B-3. 연차별 계획을 내역사업 계획으로 태깅한다

```
'1단계(2026~2027년): …'  → SUBPROGRAM_PLAN_CONTENT   (기대: ANNUAL_PLAN_CONTENT)
'2단계(2028년): …'       → SUBPROGRAM_PLAN_CONTENT   (기대: ANNUAL_PLAN_CONTENT)
```

라벨이 `연차별·내역사업별 추진계획`으로 둘을 겸해서 구분이 흐리다.
시간·단계를 나타내면 `ANNUAL_PLAN_CONTENT`, 내역사업 단위 구성이면
`SUBPROGRAM_PLAN_CONTENT`임을 프롬프트에서 갈라줘야 한다. A-3의 원인이다.

### B-4. 회수율이 낮은 필드

mockup_08 골든 대비: `DELIVERY_SYSTEM` 3/13, `EXPECTED_EFFECTS` 8/15,
`SUPPORT_CONTENT_AND_SCALE` 5/11. 대부분 B-1·B-2와 같은 계열이다.

참고: 같은 문구가 문서 안 여러 scope에 나타나면 서버가 모호한 인용으로 거부한다.
`시제품 제작`(3회), `시제품 제작비`(2회)가 여기 해당한다.
**이 거부는 의도된 동작이므로 우회하지 않는다.** 골든셋 쪽에서 더 긴 고유 구간을
쓰도록 조정하는 편이 맞다.

---

## C. FIT

### C-1. FIT-5가 '좁힘'과 '모순'을 구분하지 못한다 — 오탐

mockup_03(보통사례)에서 `CONFLICT`가 나왔다.

```
left:  TARGET_GROUP  전라북도 내 만 39세 이하 청년 창업기업 (업력 5년 이내)
right: COND_OTHER    전북 특산물/문화자원 연계 비즈니스 모델 보유 기업
```

조건이 대상을 좁히는 정상 구조인데 모순으로 판정했다.
이 목업이 지목한 결함은 FIT-2 범위차이와 FIT-6 정보부족이지 조건 충돌이 아니다.

이전에 mockup_07(모순 없도록 설계)에서도 같은 계열 오탐이 있었다(범위 조건 분할이 원인,
그쪽은 해소됨). 지금 것은 순수 의미 판단 오류다.

기대: 조건이 대상을 **좁히는 것**은 `FIT` 또는 `NEEDS_REVIEW`이고,
동시에 성립할 수 없어야 `CONFLICT`다. 프롬프트에 판정 기준을 명시한다.

### C-2. FIT-7 구역 설계와 실제 양식의 불일치 — 계약 결정 필요

실제 [서식 1]은 지원기업수·단가를 **지원내용 안에** 쓰게 하고 지원규모를 별도 구역으로 두지 않는다.
따라서 이 양식을 따른 문서에서 FIT-7은 항상 단측이 되고
`INSUFFICIENT / SINGLE_SIDED_NO_CONFLICT`가 나온다. mockup_08이 그렇다.

**이것은 현재 계약에서 올바른 결과다. CPL 수정으로 `SUPPORT_SCALE` 구역을 임의 생성해
통과시키면 안 된다.** FIT-7 자체를 바꿀지는 별도 계약 논의 사안이다.

참고로 mockup_05의 주석이 기대한 `기업당 5,000만원 × 20개사 = 10억 vs 예산 3억` 산술충돌은
지금 FIT-7 계약(같은 축 값 집합 비교)의 범위 밖이다. 예산과의 대조는 다른 관계가 필요하다.

---

## D. SIM

### D-1. 공고 프로필 하위키 복제 — 정정기록 CR-08

case 212 rank1 실제 출력:

```
SIM-1  [problem_domain, direction, specific_objective] ← 같은 문장 하나가 세 키에
       "부산경영자총협회에서는 고용노동부와 부산광역시가 지원하는 2026년…"
SIM-2  [target_group, company_type, region]            ← 같은 문장이 세 키에
SIM-3  [activity, instrument, item]                    ← 같은 문장이 세 키에
```

공고 원문 한 덩어리를 하위 키마다 그대로 복제한다.
게다가 SIM-1의 저 문장은 사업목적이 아니라 공고 안내 상투구다.
구조화된 것처럼 보이지만 비교 토대가 만들어지지 않았고, 지금 나오는 `PARTIAL` 판정도
그 상투구를 근거로 삼은 것이라 신뢰하기 어렵다.

정정기록 CR-08: 원문 근거별 의미 매핑, 실패 시 빈 값과 warning.
SIM Slice 9 미구현 구간이라 예정된 상태다.

### D-2. 후보 하나가 통째로 실패한다

case 213 rank2에서 4축 전부 `INSUFFICIENT / LLM_INVALID_RESPONSE`,
등급 `ON_HOLD`. 다른 4건은 정상이므로 후보 단위 격리는 동작한다.
CPL·FIT에서 본 LLM 응답 계약 위반과 같은 계열로 보인다.

### 참고 — 좋아진 부분

이전(case 167) 대비 요청 측 근거가 채워지면서 평가 가능 축이 2/4 → 4/4가 됐고,
유사도가 5건 모두 61%로 뭉쳐 있던 것이 73/71/69/68/68로 흩어졌다.
CPL 수정의 효과가 SIM까지 전달됐다.

---

## E. 계약 공백 — 코드가 아니라 결정이 필요함

1. **사업목적의 정량 표현** — 판별기준 §3.1이 확인 항목으로 두지만
   (`지원기업 100개사`) `PURPOSE_GOAL`에 담을 축이 없다.
2. **성과지표의 측정시점** — 판별기준 §15.1의 구조화 후보인데 축이 없다.
   `측정시점 사업 종료 후 1년`을 담을 자리가 없다.
3. **occurrence 묶음** — `CplOccurrence`에 그룹 키가 없어 `목표값 30%`가
   어느 지표의 것인지 표현할 수 없다. 수행절차 7단계와 단계별 역할 3건도 짝지어지지 않아
   FIT-6이 단계별 역할을 제대로 대조하지 못한다.
   축 44개를 다시 만들 필요는 없고 **개체 차원 하나를 추가**하는 문제다.
4. **FIT-7 구역 설계** — C-2 참조.

---

## F. 검증 체계

**골든셋이 mockup_08 하나뿐이라 회귀가 자동으로 잡히지 않는다.**
이번에 발견한 mockup_05 FIT-1 회귀(B-1)와 mockup_03 FIT-5 오탐(C-1)은 모두
사람이 손으로 이전 실행과 비교해서 찾았다.

최소 한 건 더 필요하다. mockup_03이 적합하다 — 결함이 FIT-2·FIT-6에 있어
mockup_08(전항목 정상)과 성격이 겹치지 않는다.
작성 방식과 검증 절차는 `samples/golden/mockup_08.json`과 동일하게 가면 되고,
`golden_evaluator.py`가 그대로 재사용된다.

SIM도 공고 프로필 정답을 만들어두면 CR-08 작업 시 즉시 채점된다.

---

## 권장 순서

**알파 전 (오늘)**

1. A-1 기간 파서 — 틀린 값을 저장하는 경로
2. A-3 `IMPLEMENTATION_PLAN` 상태 — 계약 위반
3. A-2 법령 fallback — 거짓 `PRESENT`
4. A-4 중복 occurrence

전부 Rule 영역이라 LLM 호출 없이 단위 테스트로 검증된다. 골든셋에 기대값이 이미 있다.

**알파 후**

5. mockup_03 골든셋 추가 (F) → 회귀 자동 탐지 확보
6. 재측정 후 B-1 → B-3 → B-2 순으로 프롬프트
7. C-1 FIT-5 판정 기준
8. D-1 SIM Slice 9 (CR-08·CR-09)
9. E 계약 결정 (묶음 차원, 축 2개 추가, FIT-7)

## 재현

```bash
bash scripts/e2e_up.sh
cd backend && python -m uvicorn main:app --host 127.0.0.1 --port 8000
python scripts/e2e_run.py "samples/hwpx/mockup_08_CPL전항목_스마트기술사업화.hwpx"
```

채점:

```bash
cd backend && python -m app.services.cpl.golden_evaluator \
  ../samples/golden/mockup_08.json <리포트 JSON 경로>
```
