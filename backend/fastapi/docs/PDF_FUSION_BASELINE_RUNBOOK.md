# PDF Fusion 기준 Corpus Baseline 실행 안내

`backend/scripts/freeze_pdf_fusion_baseline.py`는 PDF 융합 0단계의 기준
corpus를 **읽기 전용**으로 스캔해 재현 가능한 JSON manifest를 만든다. 원본 파일,
Common IR, Profile, CandidatePack을 수정·업로드·DB 적재하지 않는다.

생성한 manifest는 다음 단계에서 “같은 100건과 PDF subset에 대해 실행했는가”를 확인하는
입력이며, 원문 텍스트를 보관하거나 표준 출력으로 내보내지 않는다.

현재 검토 기준은 `backend/baselines/pdf_fusion/bizinfo_existing_100.v1.json`에 동결되어
있다. 전체 100건, PDF 47건, HWP 48건, HWPX 5건과 Common IR/Profile/CandidatePack
각 100건을 기록하며 manifest SHA-256은
`78c828fc0de4e32ccfe681cece53dfbf1a3b106c48e762484be5e8a1cd46358f`이다.
교환 형식은 같은 디렉터리의 `pdf_fusion_corpus_baseline_v1.schema.json`으로 검증한다.

## 입력

가장 안전한 방식은 corpus tree와 명시 inventory를 함께 사용하는 것이다. inventory는
corpus 밖에 둘 수 있지만, `source_path`와 artifact 경로는 반드시 corpus root 기준의
상대 POSIX 경로여야 한다. 절대경로, `..`, 백슬래시, NUL, symlink는 거절된다.

```json
{
  "schema_version": "pdf_fusion_corpus_inventory/v1",
  "corpus_id": "bizinfo-existing-100-20260914",
  "expected": { "all_runs": 100, "pdf_runs": 47 },
  "runs": [
    {
      "logical_id": "PBLN_123456789012345",
      "source_path": "PBLN_123456789012345/attachments/original.pdf",
      "artifacts": {
        "common_ir": "PBLN_123456789012345/pipeline/common_ir_v1/PBLN_123456789012345.pdf.json",
        "structured_profile": "PBLN_123456789012345/pipeline/structured_profile.v0.2.json",
        "candidate_pack": "PBLN_123456789012345/pipeline/source_selection.json"
      }
    }
  ]
}
```

`expected`는 선택 사항이지만 100건 baseline에서는 위처럼 `100`과 `47`을 고정한다.
Common IR, Profile, CandidatePack 세 artifact는 모든 run에 필수이며 존재와 JSON shape
검증을 모두 통과해야 한다. inventory에서 하나라도 생략하거나 실제 파일이 없으면 최초
baseline 생성부터 실패한다.

inventory 없이도 표준 Existing pack tree를 발견 모드로 읽을 수 있다. 이때 root 바로 아래
각 run 디렉터리에 `attachments/`가 있어야 하고, 그 하위에는 HWP/HWPX/PDF 원본이 정확히
하나여야 한다. 표준 위치의 Profile, CandidatePack, Common IR은 자동 기록되며 셋 중
하나라도 없으면 실패한다.
100건/47건을 정확히 고정해야 하므로 실제 baseline 생성에는 inventory 사용을 권장한다.

## 생성

출력 경로의 부모 디렉터리는 미리 존재해야 한다. 이미 존재하는 manifest는 절대 덮어쓰지
않으며, 새 파일은 `O_EXCL`로 원자 생성한다.

```bash
cd /path/to/SKN30-FINAL-4Team

backend/.venv/bin/python backend/scripts/freeze_pdf_fusion_baseline.py \
  --corpus-root /srv/pre-review/imports/bizinfo-existing/extracted-100 \
  --corpus-id bizinfo-existing-100-20260914 \
  --output /srv/pre-review/baselines/pdf-fusion-existing-100.v1.json
```

성공 표준 출력은 다음처럼 상태와 manifest의 SHA-256만 출력한다. 파일 원문, API key,
절대 source path는 출력하지 않는다.

```json
{"manifest_sha256":"<64 hex>","status":"created"}
```

생성 manifest는 `all_runs`(전체 HWP/HWPX/PDF)와 `pdf_subset.runs`(PDF만)를 별도 배열로
보관한다. 각 run에는 logical ID, 안전한 상대 원본 경로, 확장자, 원본 SHA-256 및 존재하는
artifact의 경로/SHA-256/구조적 count가 기록된다.

## 재검증

동일 corpus와 inventory를 다시 스캔해서 기존 manifest와 byte 단위로 비교한다. `--check`는
파일을 쓰지 않는다. source bytes, artifact bytes, 대상 목록, 경로, count 중 하나라도 달라지면
실패한다.

```bash
backend/.venv/bin/python backend/scripts/freeze_pdf_fusion_baseline.py \
  --corpus-root /srv/pre-review/imports/bizinfo-existing/extracted-100 \
  --corpus-id bizinfo-existing-100-20260914 \
  --output backend/baselines/pdf_fusion/bizinfo_existing_100.v1.json \
  --check
```

실패 시 source/artifact의 원문은 출력하지 않고 안전성 사유만 stderr에 JSON으로 출력한다.
symlink, traversal, 중복 logical ID, 같은 logical ID의 상이한 source hash, 선언된 파일 누락은
모두 fail-closed다. baseline이 생성된 뒤 corpus를 수정하지 말고, 변경이 필요하면 새
`corpus_id`와 새 output 파일로 기준선을 다시 만든다.
