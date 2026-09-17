# ML 모델 CPU 상주 결정 — 실제 Existing 공고 벤치마크

> 측정일: 2026-09-17 · 기준: `backend-rebuild` `52a1758eee4c` + 미커밋 CPU sidecar 변경
>
> 기계 판독 가능한 원자료: [`benchmarks/ml_cpu_residency_20260917.json`](benchmarks/ml_cpu_residency_20260917.json)

## 결론

현재 PoC의 기본 배치는 Model 1·2·3을 RunPod GPU로 옮기지 않고 **백엔드 EC2의 CPU 프로세스에 상주시키는 것**으로 정한다. 실제 공고 1건의 세 모델 합산 지연은 호출마다 새 프로세스를 만드는 현재 방식의 약 **14.15초**에서 상주 재사용 시 약 **1.06초**로 줄었다(약 **13.4배**, 13.09초 절감). 병목은 추론 자체보다 Python/ML 라이브러리 import, artifact 검증·로드 및 프로세스 종료의 반복이었다. RunPod GPU는 Surya OCR·Gemma·향후 로컬 LLM처럼 GPU 이득이 큰 작업에 우선 배정한다.

현재는 **Model 1 sidecar와 Model 2·3 결합 sidecar가 모두 구현되어 실제 worker 연결,
인증, runtime manifest, health/readiness 검증까지 완료**됐다. Model 2·3은 pandas/NumPy와
serving import cache를 공유하는 단일 CPU 프로세스에서 bundle과 reference pool을 시작 시
한 번만 적재한다. worker는 기존 공개 결과 계약을 유지한 채 내부 HTTP adapter를 사용하고,
host 직접 실행을 위한 기존 one-shot subprocess는 fallback으로만 남긴다.

## 실험 입력과 환경

- Existing KB bootstrap 원본 공고: `PBLN_000000000117383` — 「2026년 스포츠산업 예비선도기업 육성 지원사업 참여기업 모집 공고」
- current Profile: `547950af-4470-433c-b557-16df451c421e`, `existing_program_profile/v0.2`
- artifact SHA-256: source `41e41a28…`, Common IR `b0439504…`, Profile `a6104e6d…`
- 실제 근거: 목적·대상·지원내용과 `기업당 최대 1억원`, `총사업비 70%/자부담 30%`, `지원기간 최대 3년`
- Model 1 입력 SHA-256: `83c6750ec78047a08a44e44120aa92377e517a7555afd8342d4a5de4eee82add`
- 장비: WSL2, Intel Core i5-7200U(2코어/4스레드), RAM 11 GiB, CPU-only
- Model 1 sidecar의 warm idle 메모리 snapshot: 약 400 MiB(`docker stats` 참고값)
- 방법: DB/Storage 조회와 입력 조립은 측정에서 제외했다. 각 추론은 직렬로 정확히 5회 실행하고, 최소·최대 1개씩을 제외한 중간 3회의 산술평균을 대표값으로 사용했다. 상주 호출은 출력 hash가 5회 동일한지도 확인했다.
- 재측정: Model 1은 `backend/scripts/benchmark_model1_resident.py`가 5회 원자료와 중간 3회 평균을 출력한다. Model 2·3 구현 검증은 실제 worker 이미지와 인증 HTTP sidecar를 사용했으며, 동일 실제 공고 payload를 기존 subprocess와 resident adapter에 각각 5회 입력했다.
- 주의: Existing KB의 production ML 경로는 Model 1 분류다. Existing Profile/Common IR로 Model 2·3을 실행한 값은 실제 공개 데이터를 사용한 **request 분석 지연 proxy**이며 품질평가가 아니다.

## 결과

| 모델 | 실행 방식 | 5회 측정값(ms) | 중간 3회 평균 | 개선 |
|---|---|---:|---:|---:|
| Model 1, KLUE-BERT | 매번 subprocess | 9,530.758 / 8,545.764 / 8,071.610 / 8,555.494 / 8,524.914 | **8,542.057 ms** | 기준 |
| Model 1, KLUE-BERT | CPU sidecar HTTP | 667.766 / 692.292 / 800.275 / 761.338 / 531.632 | **707.132 ms** | **12.1배**, 91.7% 감소 |
| Model 2, XGBoost | 매번 subprocess | 3,068.129 / 3,003.634 / 3,124.811 / 3,141.938 / 3,101.102 | **3,098.014 ms** | 기준 |
| Model 2, XGBoost | 로드 후 프로세스 재사용 | 331.661 / 263.568 / 339.947 / 266.923 / 261.226 | **287.384 ms** | **10.8배**, 90.7% 감소 |
| Model 3, 거리 기반 | 매번 subprocess | 2,528.082 / 2,490.133 / 2,949.178 / 2,207.343 / 2,498.312 | **2,505.509 ms** | 기준 |
| Model 3, 거리 기반 | pool 로드 후 프로세스 재사용 | 125.451 / 61.702 / 61.975 / 61.041 / 62.528 | **62.068 ms** | **40.4배**, 97.5% 감소 |

실제 출력도 회귀 기준과 일치하거나 정상 생성됐다. Model 1은 DB 기준값과 동일한 `사업화 / 신뢰 / 0.8816877`, Model 2는 `149,971,984원 / 참고 / partial`, Model 3은 유효 3축으로 `과거 사업 패턴과 차이가 큼`, 주요 차이 축 `지원비율`을 반환했다.

### 구현 완료 후 동일 입력 Docker 검증

상주 구현을 worker에 연결한 뒤 같은 실제 공고의 Profile/Common IR로 payload를 다시 만들고,
기존 subprocess와 resident HTTP를 정확히 같은 입력으로 비교했다. 이 검증의 Model 2
evidence는 CPL snapshot이 없는 Existing 자료의 전체 Common IR 원문을 사용했으므로 위 최초
proxy와 입력 hash·절대시간은 다르다. 같은 행 안의 방식끼리만 비교한다.

| 모델 | 기존 subprocess 5회(ms) | 기존 중간 3회 평균 | resident HTTP 5회(ms) | resident 중간 3회 평균 | 개선 |
|---|---:|---:|---:|---:|---:|
| Model 2 | 7,714.460 / 7,517.233 / 7,648.684 / 7,685.278 / 7,244.414 | **7,617.065 ms** | 398.331 / 271.834 / 310.175 / 292.040 / 263.484 | **291.350 ms** | **26.1배**, 96.2% 감소 |
| Model 3 | 7,186.867 / 7,068.361 / 6,499.768 / 7,195.269 / 6,440.783 | **6,918.332 ms** | 70.898 / 111.051 / 84.128 / 73.157 / 106.083 | **87.789 ms** | **78.8배**, 98.7% 감소 |

두 모델 모두 각 방식의 5회 출력 hash가 안정적이었고, subprocess와 resident HTTP의 최종
정규화 출력 SHA-256도 모델별로 정확히 일치했다. 두 모델 합산 추론 지연은 이 입력에서
약 **14.54초 → 0.38초**로 줄었다. `model23-cpu`는 host port 없이 전용 internal network에서
실행되며 DB·Supabase·Storage·OpenAI 자격증명을 받지 않는다.

## 로드·종료 비용

| 구간 | 5회 측정값(ms) | 중간 3회 평균 |
|---|---:|---:|
| Model 1 artifact 검증 + 422 MiB weight 로드 + warmup | 19,886.033 / 19,978.406 / 19,762.551 / 19,492.515 / 19,706.293 | **19,784.959 ms** |
| Model 2 라이브러리 import | 1,918.821 / 2,114.980 / 1,809.877 / 1,882.559 / 1,794.406 | **1,870.419 ms** |
| Model 2 19.87 MiB bundle 로드 | 602.093 / 647.980 / 616.338 / 580.056 / 692.431 | **622.137 ms** |
| Model 3 라이브러리 import | 1,900.711 / 2,091.483 / 1,757.761 / 1,899.236 / 1,731.494 | **1,852.569 ms** |
| Model 3 9.67 MiB reference pool 준비 | 217.779 / 212.126 / 250.997 / 228.766 / 222.889 | **223.145 ms** |
| Model 2 process 종료·회수 proxy | 264.504 / 364.644 / 264.723 / 264.638 / 265.774 | **265.045 ms** |
| Model 3 process 종료·회수 proxy | 214.954 / 214.789 / 314.675 / 265.128 / 265.270 | **248.451 ms** |

Model 2·3에는 명시적 unload API가 없어 마지막 두 행은 stdout 완료 후 child process 회수까지의 시간이다. 상주 서비스에서는 요청별 unload를 하지 않고, 배포·재시작 때 OS가 메모리를 회수한다.

Artifact는 Model 1 weight `8fa1522c…`(442,551,356 bytes), Model 2 bundle `0c5b93e2…`(20,830,778 bytes), Model 3 pool `79649c09…`(10,139,727 bytes)로 고정했다. 이 수치는 저사양 노트북·WSL·warm page cache·단일 공고의 아키텍처 비교값이다. EC2에서는 실제 Request Profile 표본, 동시성, queue 대기, RSS를 포함해 같은 5회 절차로 재측정한다. 목표 지연·메모리를 넘으면 Model 2·3 process 수 또는 GPU 배치를 다시 결정하되, 요청마다 로드를 반복하는 현재 방식으로는 돌아가지 않는다.
