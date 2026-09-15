# ExistingProfile v0.2 + RequestProfile v0.1.2 이식 패키지

Common IR v1을 이미 생성한 환경에서 Existing 지원공고와 사전협의 요청서를 구조화하기 위한 코드·계약·예시 묶음이다.

## 범위

```text
Common IR v1
  ├─ ExistingProfile v0.2  → existing_program_profile/v0.2
  └─ RequestProfile v0.1.2 → pre_review_request_profile/v0.1
```

이 패키지는 HWP/HWPX/PDF/Markdown을 Common IR로 만드는 파서 자체는 포함하지 않는다. 입력 Common IR은 `common_ir_v1` 계약과 schema validation을 통과해야 한다. PDF는 native-only semantic text 정책을 전제로 한다.

## 포함물

| 경로 | 내용 |
| --- | --- |
| `semantic_structuring/` | CandidatePack, exact-span materializer, Existing v0.2 assembler/validator, Request v0.1.2 runner/validator와 테스트 |
| `common_ir_schema/` | Common IR v1 schema validator |
| `docs/common_ir/` | Common IR을 구조화 입력으로 쓰는 계약 |
| `docs/existing/` | Existing Profile v0.2 계약 |
| `docs/request/` | Request Profile v0.1.2 계약·JSON 설명·파이프라인 정책 |
| `examples/existing/` | 실제 HWP Common IR, v0.2 source selection, 최종 프로필 예시 |
| `examples/request/` | clean Markdown fixture 기반 Common IR, Luna selection, 최종 요청 프로필 예시 |
| `scripts/` | Existing example 검증 및 Request dry-run 명령 |
| `exploratory_study/results/` | Request unit-test가 참조하는 최소 Common IR/HWP 구조 fixture |

`.env`, API key, 원본 HWP/HWPX/PDF, 대량 실행 산출물은 의도적으로 제외했다. 계약 회귀에 필요한 소형 Common IR fixture와 Existing v0.2 Gold fixture는 코드 테스트와 함께 포함했다.

## 설치

Python 3.11 이상과 `uv`가 필요하다.

```bash
cd portable_existing_request_profiles_20260831
uv sync
```

원격 Request 실행 전에는 로컬 `.env`에만 아래 값을 넣는다. 이 파일은 커밋·전달하지 않는다.

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-luna
```

## 먼저 실행할 검증

```bash
uv run python -m semantic_structuring.test_contracts
uv run python -m semantic_structuring.test_request_profile_v012
```

`test_request_profile_v012`은 pytest 모듈로도 실행할 수 있다. 이식 환경에 pytest가 없다면 `uv run --with pytest python -m pytest semantic_structuring/test_request_profile_v012.py -q`를 사용한다.

## 예시 실행

Existing Profile은 Common IR과 profile의 exact-span/provenance를 오프라인 검증하는 예시다.

```bash
./scripts/validate_existing_example.sh
```

Request Profile은 모델 호출 없이 CandidatePack과 실제 원격 호출 payload를 출력하는 dry-run 예시다.

```bash
./scripts/dry_run_request_example.sh
```

Request의 실제 구조화는 `--remote` opt-in으로만 실행된다. `docs/request/Request_Profile_v0.1.2_pipeline_scaffold_20260831.md`의 원격 실행·실패 artifact·최대 1회 repair 정책을 따른다.

## 핵심 안전 계약

- 모델은 field별 exact-span locator만 선택하며, `value_raw`, char offset, evidence, normalized 값은 서버가 생성한다.
- 최종 Raw Fact는 하나의 연속 `value_source`만 가지며 `value_raw`는 그 span에서 복원한다.
- Common IR block ID와 CandidatePack block ID는 별도 namespace다. Common IR provenance 조인은 `evidence.common_ir_document_id + evidence.common_ir_block_id`를 사용한다.
- HWPX → HWP → eligible native PDF 우선순위는 profile 내부가 아니라 ingest resolver의 정책이다. 서로 다른 포맷 profile을 한 JSON으로 병합하지 않는다.
- Request의 `request_type`은 체크박스를 서버가 판정하는 read-only 정보다. LLM이 추론·수정하지 않는다.

## 이식 시 확인할 항목

1. Common IR producer가 `common_ir_v1` schema 및 source hash/span/dangling validation을 통과하는지 확인한다.
2. Common IR의 `document_id`, `schema_version`, `source_kind`, SHA-256, source location을 profile lineage로 보존한다.
3. Existing과 Request의 shared comparison field vocabulary는 맞추되, Request 전용 field를 Existing에 강제하지 않는다.
4. RDB 적재는 전역 ID로 오해하지 않고 `(profile_id, fact_id)` 및 provenance 복합키를 사용한다. 상세는 Existing 계약 2·4절을 따른다.

## 예시의 성격

예시 JSON은 데이터 형태·실행 경로를 보여주기 위한 샘플이다. Existing의 `source_selection_v02.json`은 과거 selection-contract artifact라 현 모델의 재조립 입력으로 사용하지 않는다. 이식 환경에서는 새 Common IR에서 현재 `SourceSelectionExtractionV02` 계약에 맞는 selection을 만든 뒤 `final_profile_assembler`로 재조립한다. 새 적재는 현재 계약과 새 Common IR lineage로 독립 재생성해야 하며, 예시 JSON을 수동 보정해서 production 산출물로 사용하면 안 된다.
