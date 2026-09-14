# PDF 문서 융합 및 원격 GPU 실행 설계

- 상태: 리뷰 반영 설계 v0.2
- 기준일: 2026-09-15
- 대상: Existing 공고와 Request 요청서가 공유하는 PDF 물리 추출·근거 계층
- 리뷰: Opus 5 xhigh, Sol xhigh 독립 리뷰 및 Grok 레드팀 검토 반영
- 구현 판정: 0~2단계만 GO. 원격 GPU·표 승격·Request PDF 개방은 각 gate 통과 전 NO-GO

## 1. 결정 요약

PDF 처리는 하나의 parser 결과를 정답으로 간주하지 않는다. 문자와 구조의 권한을
분리하고 모든 파생 결정을 재생 가능한 artifact로 남긴다.

| 관측/산출물 | 권한 | 초기 용도 |
|---|---|---|
| pdf-inspector native text | 유일한 v1 문자 근거 | `value_raw`, CandidatePack canonical exact span |
| OpenDataLoader JSON | 독립 구조 제안 | 초기에는 cached JSON 기반 table-only shadow |
| Surya HTML/TableRec/layout | 시각 구조 제안·진단 | 표·도형 bbox, native 누락 페이지 탐지 |
| fusion plan sidecar | 파생·감사 가능 | alignment 승인·거절·충돌·소유권 ledger |
| context group sidecar | 선택 불가 문맥 | 후속 A/B에서만 LLM 주변 문맥으로 사용 |
| Common IR occurrence/cell | 선택 가능한 근거 | Profile Fact와 evidence가 역추적하는 원자 단위 |

현재 Common IR `1.1.0`의 PDF semantic text는 native text만 허용한다. OCR/ODL 문자열을
`native_text`로 위장하거나 Common IR v1의 `conflicts`에 넣지 않는다. OCR 문자로
image-only PDF를 자동 구조화하는 기능은 provenance 의미가 바뀌므로 별도 Common IR v2
RFC·schema·migration·UI 계약으로 분리한다.

### 1.1 이번 설계에서 확정한 선택

1. conflict, context group, cross-page table 후보와 fusion ledger는 Common IR v1 밖의
   versioned sidecar로 둔다.
2. ODL은 EC2의 격리된 CPU subprocess에서 실행한다. RunPod는 GPU가 필요한 Surya만
   실행한다.
3. RunPod Serverless의 비동기 `/run`을 사용하고 EC2 worker가 `/status/{job_id}`를
   polling한다. 외부 callback endpoint는 만들지 않는다.
4. 원격 계산 key와 DB fence를 분리한다. `logical_compute_key`는 attempt 독립이고,
   `processing_run_pk`는 현재 DB attempt의 commit 권한만 나타낸다.
5. native capture는 항상 PDF 전체를 한 번에 수행한다. page range는 render/Surya에만
   적용한다.
6. 최초 explicit table 승격은 Surya HTML grid, Surya TableRec grid와 ODL logical grid의
   독립 합의 및 native ownership 검증을 모두 요구한다. 그 전에는 shadow/partial이다.
7. 첫 구현 단위에는 paragraph context, table promotion, Request PDF, OCR semantic v2를
   넣지 않는다.

## 2. 목표 구조와 신뢰 경계

```text
source PDF (SHA-256, EC2 Storage)
  ├─ full-document native capture (EC2 CPU)
  ├─ OpenDataLoader table proposal (EC2 isolated CPU subprocess)
  └─ canonical render + coordinate manifest (EC2 CPU)
       └─ immutable page images + short-lived signed GET
            └─ RunPod Surya GPU job
                    ↓
             result artifact upload
                    ↓
        EC2 polling + size/hash/schema/geometry verification
                    ↓
        deterministic fusion-plan sidecar (shadow first)
                    ↓
           Common IR 1.1.0 + CandidatePack
              ├─ Existing adapter
              └─ Request adapter
```

EC2 backend worker가 DB queue claim, heartbeat, fence, Storage, artifact 검증과 최종 DB
commit을 소유한다. RunPod에는 `DATABASE_URL`, Supabase service-role key, 사용자 JWT를
전달하지 않는다. RunPod 결과는 DB row가 아니라 검증 전 artifact일 뿐이며, live fence를
가진 EC2 worker만 최종 반영할 수 있다.

## 3. Artifact와 lineage 계약

### 3.1 영속 계보

```text
source PDF SHA-256
→ native JSON SHA-256 + exact pdf-inspector version/config
→ ODL JSON SHA-256 + parser/image/config digest
→ page image SHA-256[] + coordinate manifest SHA-256
→ Surya artifact SHA-256 + model weights/revision + worker image digest
→ page coverage manifest SHA-256
→ fusion-plan SHA-256 + generator version
→ Common IR SHA-256
→ CandidatePack SHA-256
→ Profile JSON SHA-256
```

기존 `workspace.source_artifact`와 `workspace.artifact_lineage`를 우선 재사용한다. 새 enum을
즉시 늘리지 않고 다음처럼 구분한다.

| artifact_type | artifact_logical_id / schema_version 예시 |
|---|---|
| `source` | 원본 PDF |
| `parser_raw` | `pdf_native`, `opendataloader`, `surya_layout` |
| `format_ir` | `pdf_coordinate_manifest`, `pdf_page_coverage`, `pdf_fusion_plan`, `pdf_context_groups` |
| `common_ir` | `common_ir/1.1.0` |
| `candidate_pack` | 실제 generator/version |
| `structured_profile` | Existing 또는 Request Profile version |

Storage object key는 전역 hash 하나가 아니라
`{analysis_run_id}/{artifact_role}/{content_sha256}.{ext}`처럼 run-scoped로 만든다. 같은
문서를 다른 사용자가 올려도 row/object가 충돌하거나 존재 여부가 누출되지 않아야 한다.

page image와 RunPod 임시 출력은 요청 workspace의 삭제 정책을 따른다. 최종 재생에 필요한
원시 JSON·manifest·fusion ledger는 terminal manifest에 묶고, 임시 GPU 파일은 검증 완료
후 삭제 가능하다. 무엇을 삭제해도 terminal manifest가 참조하는 필수 artifact가
사라져서는 안 된다.

### 3.2 결속 검증

모든 sidecar는 다음을 직접 포함하고 consumer가 비교한다.

- source PDF SHA-256
- page count와 처리 page 목록
- producer 이름·버전·config digest·worker image digest
- 입력 artifact SHA-256 목록
- coordinate manifest SHA-256
- artifact schema version, MIME, size, content SHA-256

호출자가 source hash를 주입했다고 신뢰하지 않는다. producer 실행 manifest가 실제 입력
artifact hash를 직접 증명해야 한다. 하나라도 다르면 해당 sidecar를 거절하고 reason을
fusion ledger에 남긴다.

## 4. 좌표계와 렌더 권위

첫 구현의 최우선 계약은 `pdf_coordinate_manifest/v1`이다.

- page number: 1-based
- source document `page_count`; 모든 page manifest가 같은 값을 가지며 document render
  manifest는 `1..page_count`를 빠짐없이 포함
- canonical space: `pdf_user_space`
- page별 `media_box`, `crop_box`, `rotation`, `user_unit`
- 회전과 `user_unit`을 반영한 `canonical_width_pt`, `canonical_height_pt`
- `render_scale_px_per_point`와 rendered width/height
- PDF·pixel origin과 x/y axis 방향
- `pdf_user_space → rendered_page_px` affine matrix와 inverse
- renderer 이름·정확한 version·config digest
- source SHA-256와 각 page image SHA-256

EC2가 한 번 렌더한 immutable page image를 RunPod에 전달한다. RunPod가 PDF를 다시
렌더링하지 않으며, 결과마다 입력 page image SHA-256와 coordinate manifest SHA-256를
echo한다. 네 모서리 및 bbox의 `user → pixel → user` 왕복 오차가 0.5pt를 넘거나 page
bounds를 벗어나면 거절한다.

`pdf_coordinate_manifest/v1`과 `pdf_render_manifest/v1` JSON Schema를 package에 함께
배포한다. 순수 validator는 선언값의 상호 일관성과 실제 PDF/PNG SHA·size·PNG dimensions,
그리고 초기 renderer 계약의 non-interlaced 8-bit RGB/RGBA PNG IDAT/DEFLATE·scanline filter를
bounded decode로 검증한다. 다만 PDF page box·rotation·UserUnit 자체는 임의 호출자 입력을 신뢰하지 않고,
후속 renderer integration이 pinned PDF engine에서 직접 읽어 manifest를 생성해야 한다.

ODL 고유 bbox도 producer coordinate space와 canonical affine transform을 manifest에
명시한다. ODL 2.5.7의 origin/axis/rotation convention을 실제 fixture로 확정하기 전에는
bbox 기반 결과를 production 표로 승격하지 않는다.

## 5. Native 문자와 occurrence 불변조건

1. native capture는 항상 전체 문서로 실행하며 page-range capture를 금지한다.
2. pdf-inspector의 정확한 package/version/config를 고정한다. version을 얻지 못하면 capture
   단계에서 실패한다.
3. ID 문자열에서 page를 추론하지 않고 occurrence의 provenance.page를 사용한다. ID page와
   provenance가 다르면 실패한다.
4. parser/version/config가 바뀐 artifact의 기존 occurrence binding을 재사용하지 않는다.
5. native text가 정상인 영역에서는 native 문자열이 항상 우선한다.
6. ODL/OCR 정규화 문자열은 후보 검색에만 사용하며 evidence text를 교체하지 않는다.
7. 동일 문자열이 여러 위치에 있으면 geometry와 occurrence identity로 유일성이 해소되지
   않는 한 거절한다.

여기서 `exact span`은 PDF 파일 byte substring이 아니다. pdf-inspector occurrence에서
결정적으로 만들어진 CandidatePack canonical text의 span이며, 원시 occurrence와 적용된
deterministic transform을 역추적할 수 있어야 한다. 공백 교정·생성형 재작성·normalized-only
일치는 selectable evidence가 아니다.

## 6. Mixed·image-only·비가시 native text

Common IR v1 schema를 바꾸지 않고 `pdf_page_coverage/v1` sidecar가 페이지별로 다음을
기록한다.

- substantive native occurrence 수
- visual text/layout region 유무
- 완전 blank/장식-only 판정과 reason
- page geometry와 입력 sidecar hash

degradation 정책은 다음과 같다.

| 상태 | 처리 |
|---|---|
| 모든 substantive page에 native text | native-only 진행 가능 |
| visual text region은 있으나 native text가 없는 page 존재 | `partial_source_coverage`; Existing activation 차단/검토, Request는 거절 |
| 전체 image-only | v1 `excluded_image_only`, semantic block 0 |
| ODL/Surya 실패·hash/schema/좌표 불일치 | sidecar 거절; native coverage가 완전할 때만 native-only fallback |
| table gate 실패 | atomic native만 유지하고 table은 partial |

흰색/초소형/off-page/CropBox 밖/겹친 숨은 native text는 authoritative라는 이유만으로
LLM 입력에 넣지 않는다. 검출 가능한 항목은 `non_semantic` diagnostic으로 격리하고 검출할
수 없는 색상 정보는 명시적인 한계로 기록한다. 모든 LLM prompt는 문서 본문을 신뢰할 수
없는 데이터로 구획하고 문서 안 지시를 따르지 않도록 한다. exact-span 검증은 provenance를
보장할 뿐 문서가 악의적이지 않음을 보장하지 않는다.

## 7. 문단 융합

문단은 초기 구현 범위가 아니다. 기존 실험에서 큰 context가 missing axis를 복구하지
못했고 일부 scope의 과다 선택과 token/latency 증가가 있었으므로 별도 shadow A/B가
필요하다.

### 7.1 Context group

- Common IR v1 밖 `pdf_context_groups/v1` sidecar에 둔다.
- LLM의 scope·문맥 입력에만 사용하며 selector가 evidence anchor로 선택하면 실패한다.
- 같은 page, column, layout region의 연속 원자 블록만 제한적으로 묶는다.
- 최종 `value_raw`는 반드시 구성 occurrence의 CandidatePack exact span으로 재결속한다.

공유 물리 계층의 hard boundary는 page, column, table/cell, heading, list/bullet,
checkbox/선택지와 붙임·별첨이다. Request field label은 Request adapter가 추가로 group을
더 쪼개는 의미 경계이며, 공유 core가 판단하지 않는다. adapter는 공유 group을 더
분할할 수만 있고 다시 합칠 수 없다.

### 7.2 Lossless native composite

extractor 때문에 한 문장이 잘린 경우에만 소수의 연속 native occurrence를 결합한다.
구성 ID, 원본 offset, canonical 순서와 삽입 separator를 전부 보존한다. occurrence 순서는
입력 배열이 아니라 `(page, source text-item index)`로 검증한다. separator 없는 단순 문자열
붙이기나 정규화-only match는 금지한다.

## 8. ODL alignment와 표 복원

### 8.1 ODL 초기 범위

현재 bundle의 ODL adapter는 top-level table 중심이고 paragraph/heading/list grouping은
구현돼 있지 않다. 따라서 첫 integration은 pinned OpenDataLoader 2.5.7 cached JSON의
table-only shadow로 한정한다.

- exact string match도 cell bbox와 occurrence bbox의 geometry가 맞아야 한다.
- composition은 ordered occurrence IDs, offsets와 separator를 보존한다.
- 하나의 occurrence를 여러 logical cell이 소유할 수 없다.
- normalized-only, ambiguous, wrong-geometry와 duplicate-ownership을 서로 다른 reason으로
  거절한다.
- ODL raw 문자열은 Common IR에 복사하지 않고 raw artifact와 문자열 hash를 ledger에 둔다.

### 8.2 명시적 표 승격 predicate

초기 production 승격은 다음을 모두 만족해야 한다.

1. Surya high-accuracy HTML table과 TableRec source bbox IoU가 최소 0.95다.
2. 세 결과(Surya HTML logical grid, TableRec physical grid를 logical cell로 접은 결과,
   ODL logical grid)의 row/column extent가 같다.
3. 세 결과의 logical cell address 집합 `(row0, col0, rowspan, colspan)`이 같다.
4. canonical geometry에서 대응 cell들이 동일한 native occurrence ID 집합을 소유한다.
5. 각 substantive occurrence는 정확히 한 logical cell만 소유한다.
6. table bbox 안 substantive native occurrence orphan이 0이다.
7. blank structural cell은 빈 상태를 허용하되 nonblank cell은 exact native occurrence 또는
   lossless ordered composition에 결속된다.
8. table-to-block matching은 IoU 하한과 1:1 exclusive assignment를 만족한다. 상대적
   `max(IoU)`만으로 선택하지 않는다.

하나라도 실패하면 `table_candidate/partial` sidecar로 유지하고 atomic native occurrence만
검색·선택 대상으로 노출한다. Surya/ODL HTML 또는 OCR 문자열은 evidence text와 diagram
relation에 복사하지 않는다. 페이지 간 table continuation은 Common IR v1에 표현하지 않고
이번 구현 전체에서 제외한다.

## 9. Existing과 Request의 공유 경계

공유 물리 계층:

- PDF capture와 manifest/hash 검증
- 좌표계 변환과 page coverage
- native/ODL/Surya alignment
- table grid·occurrence ownership validator
- duplicate/conflict/reject ledger
- exact-span materialization
- resource cap, 실패·재시도·telemetry

adapter 전용 의미 계층:

- Existing: 공고 본문·첨부 범위, purpose/target/support routing, Existing v0.2 coverage
- Request: checkbox, request type, field label–value, program hierarchy, completeness

표의 물리 구조만 공유하고 업무 의미는 각 Profile adapter가 해석한다. Existing 100건
통과를 Request PDF 개방 근거로 사용하지 않는다. 실제 Request 문서를 구할 수 없는 현재는
합성·adversarial fixture로 fail-closed 동작만 검증하며 Request PDF production은 닫아 둔다.

## 10. RunPod Serverless 비동기 계약

RunPod queue endpoint의 비동기 `/run`으로 제출하고 `/status/{job_id}`를 polling한다.
동기 `/runsync`와 Pod HTTP proxy는 긴 OCR 작업에 사용하지 않는다. RunPod 공식 계약상
async result 보존 시간이 유한하므로 완료 결과를 즉시 우리 Storage로 회수·검증한다.

### 10.1 원격 계산 identity와 fence

```text
logical_compute_key = SHA-256(
  source_sha256 || capability || page_image_sha256[] || page_range || mode ||
  pipeline_revision || model_weights_sha256 || config_digest ||
  coordinate_manifest_sha256
)
```

- `logical_compute_key`: attempt가 바뀌어도 같은 계산을 식별한다.
- `processing_run_pk`: 현재 DB lease/fenced commit 권한이다.
- 같은 logical key와 다른 request digest 조합은 충돌로 거절한다.
- submit 전에 현재 `ops.processing_run.run_metadata`에 dispatch intent를 fence 하에 기록하고,
  응답 직후 external job ID·시간·digest를 추가한다.
- 새 attempt는 이전 attempt metadata에서 같은 logical key의 external job을 찾아 먼저
  재부착·polling하고, terminal/cancelled/retention-expired일 때만 정책에 따라 재제출한다.

RunPod가 client idempotency를 보장한다고 가정하지 않는다. 외부 POST가 성공하고 job ID를
저장하기 전에 EC2가 죽는 좁은 구간에는 중복 GPU 계산이 가능하다. 정확히 한 번 실행을
거짓으로 약속하지 않고 다음으로 피해를 제한한다.

- deterministic result object key와 immutable/no-upsert 저장
- 같은 key 결과가 이미 유효하면 handler가 재사용할 수 있는 preflight
- DB 최종 commit은 live fence로 exactly-once 효과 보장
- 문서별 동시 external job 상한과 비용 budget
- uncertain-submit/duplicate-compute telemetry 및 운영 경보

### 10.2 요청

- logical compute key와 request digest
- 허용된 Storage host·bucket·정확한 object에만 유효한 짧은 수명의 page-image signed GET
- page image SHA-256와 coordinate manifest
- 허용 capability와 page 범위
- input/output schema, pipeline, model, config, worker image revision
- 정확한 결과 object에만 유효한 짧은 수명의 immutable signed upload token
- `executionTimeout`과 `ttl`; ttl은 queue 시간과 실행 시간을 모두 포함

RunPod handler는 임의 URL을 받지 않는다. HTTPS, host/bucket/prefix allow-list, redirect
금지, method/path 고정, expiry, 입력 size/hash/MIME를 검증한다. URL과 문서 본문은 로그에
남기지 않는다.

### 10.3 응답과 수용

RunPod job 응답은 다음 artifact manifest만 반환한다.

- external job ID와 terminal status
- logical compute key와 request digest
- artifact key, SHA-256, size, MIME, schema version
- 입력 page image/coordinate manifest SHA-256
- engine/model/config/worker image revision
- timing과 공개 가능한 reason code

EC2 worker가 service-role로 artifact를 가져오되 streaming size cap을 적용하고 SHA-256,
MIME/magic, schema, source/page/좌표 결속을 다시 검증한다. callback body나 RunPod가 주장한
hash만 신뢰하지 않는다. fence loss 시 best-effort `/cancel/{job_id}`를 호출하고, 취소 실패
결과와 stale fence 결과는 DB에 commit하지 않는다.

### 10.4 자원·재시도

GPU 제출 전 source bytes, page 수, page별/전체 rendered pixels, render 시간, 입력/출력
artifact bytes와 예상 비용에 hard cap을 둔다. 초기 숫자는 Existing PDF 47건의 p99 측정
후 문서화하며, cap이 정해지기 전 production traffic은 NO-GO다.

실패는 구분한다.

- `CONTENT_FAILED`: malformed/encrypted/PDF bomb/hash·schema·geometry mismatch. 자동 GPU 재시도
  없음.
- `INFRA_RETRYABLE`: capacity/일시 네트워크/provider 5xx. 같은 logical key/job 재부착 우선.
- `FENCE_LOST`: 계산 취소 시도 후 결과 폐기. 새 DB attempt의 콘텐츠 재시도 예산과 분리.

120초 lease와 30초 heartbeat를 GPU 기본값으로 간주하지 않는다. cold/warm queue·load·실행
p50/p95/p99를 측정해 lease, polling, `executionTimeout`, `ttl`을 함께 정한다.

## 11. 단계별 구현과 stop/go gate

| 단계 | 구현 범위 | GO 조건 |
|---|---|---|
| 0 | 기준 corpus manifest 동결 | 100건 전체와 PDF 47건 subset의 source/artifact SHA 목록 확정 |
| 1 | native capture vendoring·pdf-inspector pin·baseline 재생 | 두 번 실행 byte hash 100% 동일; HWP/HWPX 변화 0 |
| 2 | coordinate/render/extraction manifest와 순수 validator | rot 0/90/180/270, crop≠media 왕복 ≤0.5pt; tamper 100% 거절 |
| 3 | default-off feature flag + ODL table-only cached shadow + reject ledger | Common IR/Profile/공개 API byte 변화 0; ambiguous/normalized-only 승격 0 |
| 4 | Surya artifact contract와 local fake RunPod adapter | DB credential 부재; size/hash/schema/geometry/stale fence negative test 통과 |
| 5 | 실제 RunPod async shadow | crash/reclaim 재부착, duplicate telemetry, cap 강제, cold/warm 분포 확보 |
| 6 | 세 구조 결과 table agreement shadow | duplicate ownership/orphan 0; hard negative 전부 partial |
| 7 | 승인된 Existing PDF allow-list에만 explicit table | Profile exact-span 100%; 비대상 Fact/evidence 변화 0 |
| 8 | context group sidecar A/B | held-out coverage 개선과 token/latency budget 동시 충족 시만 유지 |
| 9 | Request PDF | 별도 실제 corpus와 frontend/OpenAPI/MIME 계약 승인 전 NO-GO |
| 10 | OCR semantic Common IR v2 | 별도 RFC/schema/migration/privacy/UI 승인 전 NO-GO |

현재 코드는 stage 0과 stage-2 contract slice만 닫는다. Stage 1/2 전체 완료를
주장하지 않는다: pinned `pdf-inspector`, native replay, 실제 renderer는 이후
gate로 남아 있다. 첫 구현 PR은 다음 작은 수직 단위로 제한한다.

1. corpus baseline manifest schema와 고정 도구
2. `pdf_coordinate_manifest/v1` schema/validator
3. `user_to_pixel`, `pixel_to_user`, sidecar binding 순수 함수
4. source hash를 포함한 deterministic render manifest
5. 회전·crop·비정상 좌표·tamper fixture

이 PR은 DB schema, production queue, Common IR/Profile 바이트와 공개 API를 바꾸지 않는다.

기존 `common-ir-pdf-ocr-layout` CLI는 삭제하지 않되 EC2/local renderer 진단용으로만
허용한다. RunPod fusion worker로 사용하는 것은 금지한다.

## 12. 검증 매트릭스

| 층 | 검증 |
|---|---|
| 단위 | 좌표 왕복, hash/page/transform mismatch, out-of-page bbox, parser version 미확인 실패 |
| 단위 | duplicate string, wrong geometry, normalized-only, separator/offset 변조, 다중 cell 소유 거절 |
| 단위 | merged/blank/multiline/nested table, orphan native, ambiguous table-to-block matching |
| 회귀 | Existing 100건 전체 및 PDF 47건 subset; feature-off artifact/Profile/evidence parity |
| 회귀 | 모든 Fact의 `value_raw == CandidatePack canonical exact span` 왕복 100% |
| 통합 | submit 전/후 EC2 crash, ack 유실, lease loss, 재claim, result retention 만료 |
| 보안 | expired/wrong host/path/method URL, redirect, oversized/spoofed artifact, hidden native injection |
| E2E | Existing PDF upload/import→shadow→profile→DB 결과 조회; mixed/image-only fail-closed |
| E2E | Request adversarial checkbox/field/table fixture는 production 개방이 아니라 거절 동작 확인 |
| 운영 | cold/warm p50/p95/p99, queue delay, GPU wall time/cost, reject reason, duplicate compute, stale discard |

일부 제한된 샌드박스는 Python `socketpair`를 차단해 FastAPI `TestClient` 구간이 정지한
것처럼 보일 수 있다. 전체 pytest는 로컬 socket을 허용하는 실제 실행 환경에서 수행하고,
검증 결과에는 실행한 suite와 timeout을 정확히 기록한다. 일부 targeted test 통과를 전체
suite 통과라고 표현하지 않는다.

## 13. 공개 API와 프론트엔드 영향

0~8단계 shadow는 공개 FastAPI DTO/OpenAPI snapshot을 바꾸지 않는다. 내부
`partial_source_coverage`, fusion reason, external job status를 기존 public status enum에
임의로 노출하지 않는다.

Request PDF 개방은 단순 문서 수정이 아니다. 최소 다음을 같은 변경 단위에서 맞춘다.

- 업로드 API 확장자·MIME/magic과 용량 정책
- worker suffix/format gate
- OpenAPI와 단일 프론트 API 명세
- 처리 지연·실패 reason의 public mapping
- 프론트 upload validation과 상태 표시

## 14. 참고 자료

- OpenDataLoader repository/schema: <https://github.com/opendataloader-project/opendataloader-pdf>
- RunPod Serverless 요청·상태·timeout: <https://docs.runpod.io/serverless/endpoints/send-requests>
- RunPod endpoint TTL/result retention: <https://docs.runpod.io/serverless/endpoints/endpoint-configurations>
- RunPod Pod HTTP proxy 100초 제한: <https://docs.runpod.io/pods/configuration/expose-ports>
