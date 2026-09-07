# 벤더링한 팀원 패키지

초안 v0.2 §4 "팀원 패키지는 전달 버전·해시·출처를 기록해 편입한다"에 따른 기록이다.
편입 시점의 원본을 그대로 복사했고, 아래 해시가 달라지면 로컬 수정이 있었다는 뜻이다.

| 항목 | 값 |
|---|---|
| 편입일 | 2026-09-07 |
| 편입 방식 | 원본 디렉터리 전체 복사 (`__pycache__`, `.venv`, `*.pyc`, `.git` 제외) |

## common_ir_pipeline

| | |
|---|---|
| 출처 | `C:/Users/playdata2/Downloads/common_ir_pipeline` |
| 계약 문서 | `HANDOVER.md`, `docs/COMMON_IR_V1_CONTRACT.md` |
| 파일 수 | 56 |
| tree sha256 | `2c9b06aaf7dde39d017eaef76d1e2343d4f6793bc5841734dbe9c10ade2a311e` |
| 런타임 의존성 | Python 3.11+, `jsonschema`, HWP/HWPX 경로는 `rhwp` |

## profile_structuring

| | |
|---|---|
| 출처 | `C:/Users/playdata2/Downloads/portable_existing_request_profiles_20260831` |
| 원본 폴더명 | `portable_existing_request_profiles_20260831` |
| 계약 문서 | `README.md`, `docs/request/`, `docs/existing/`, `docs/common_ir/` |
| 파일 수 | 92 |
| tree sha256 | `37ac8d0e7b548ac2d5ac68f55bc781020a6ca6e252de2e5de27e7b31cec8f2fc` |
| 런타임 의존성 | `jsonschema`, `openai>=2.26,<3`, `pydantic`, `python-dotenv` |

## 로컬 수정 이력

없음. 패키지 자체를 수정하게 되면 변경 이유·입력 사례·검증을 여기에 남기고 해시를 갱신한다.

`semantic_structuring`의 원격 경로(`run_request_profile_v012._remote_selection`)는 OpenAI
Responses API(`client.responses.create` + `reasoning.effort`)를 사용한다. vLLM은 이 API를
제공하지 않으므로 그 경로는 사용하지 않고, 같은 파일의
`select_and_materialize_with_repairs(selector=...)` 주입 지점에 자체 vLLM selector를 넘긴다.
따라서 패키지 수정 없이 자체 운영 모델로 연결한다.

## 해시 재계산

```bash
python packages/verify_vendor_hashes.py
```
