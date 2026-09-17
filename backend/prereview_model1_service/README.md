# 상주 Model 1 서비스

이 디렉터리는 KLUE-BERT Model 1을 프로세스 시작 시 한 번만 적재하고 이후 요청에서
재사용하는 내부 HTTP 서비스다. 같은 API를 RunPod GPU와 백엔드 CPU에서 사용할 수 있다.
Gemma·Surya와 프로세스, venv, 포트, bearer를 공유하지 않는다. Model 2·3과 DB
queue·최종 적재는 백엔드 CPU worker의 책임으로 남는다.

- 바인딩: 직접 실행은 `127.0.0.1:8791`; Compose는 격리된
  `model1-internal` network 안에서만 `0.0.0.0:8791`을 사용하고 host port를
  publish하지 않는다. 이 패키지는 Tailscale Serve를 수정하지 않는다.
- API: 인증된 `GET /v1/model1/ready`, `POST /v1/model1/predict`
- 요청: 네 문자열 `title`, `purpose`, `content`, `target_text`만 허용. 네 필드의 canonical SHA-256을 `Idempotency-Key`로 요구한다.
- 응답: 분류 결과와 weight SHA-256, service runtime manifest SHA-256을 반환한다.
- 시작: 가중치 검증·모델 load·warmup을 완료하기 전에는 요청을 받지 않는다. 실제 DB
  공고를 사용한 현재 WSL 노트북 측정에서는 CPU 최초 load+warmup이 약 19.8초, 상주
  HTTP 분류가 약 0.71초였다. 짧은 합성 입력으로 얻었던 약 0.14초는 실제 공고 지연을
  대표하지 않는다. 머신·디스크·입력 길이에 따라 달라지며 초기 비용은 프로세스를
  재시작할 때만 다시 든다. 전체 조건과 5회 원자료는
  [ML CPU 상주 벤치마크](../fastapi/docs/ML_CPU_RESIDENCY_BENCHMARK.md)에 기록한다.
- device: `runpod` profile은 기본 `cuda`이고 CUDA 없이 시작을 거부한다. 명시적으로
  `PREREVIEW_MODEL1_DEVICE=cpu`를 지정한 측정도 허용한다. `backend-cpu` profile은
  `cpu`만 허용하며 GPU가 보여도 CPU를 강제한다.

## 배포 profile과 고정 경로

임의 경로를 환경 변수로 받지 않는다. 다음 두 profile 중 하나가 코드가 정한 경로를
선택하며, 경로가 다르면 startup이 실패한다.

| profile | Model 1 runtime | 전처리 코드 | 기본 device |
|---|---|---|---|
| `runpod` (기본값) | `/workspace/project/prereview-model1/model1` | `/workspace/project/prereview-model1/worker/ml/pipelines/model1/dl07_m1_apply.py` | `cuda` |
| `backend-cpu` | `/opt/prereview/model1` | `/app/ml/pipelines/model1/dl07_m1_apply.py` | `cpu` |

bearer는 기존 `PREREVIEW_MODEL1_API_BEARER_TOKEN` 직접값 또는
`PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE` 중 정확히 하나만 지정한다. 파일 방식은 절대
경로의 소유자 전용 `0600` 일반 파일이어야 하며 symlink와 hardlink를 거부한다.

## 백엔드 CPU 상주 실행

이미지에 CPU용 PyTorch와 이 디렉터리의 Python 의존성을 설치하고, 검증된 Model 1
runtime과 전처리 코드를 위 고정 경로에 둔다. 다음은 컨테이너 내부 실행 예시다.

```bash
install -d -m 700 /run/prereview-model1
umask 077
openssl rand -base64 48 | tr '+/' '-_' | tr -d '=' > /run/prereview-model1/api-bearer-token
chmod 0600 /run/prereview-model1/api-bearer-token

PREREVIEW_MODEL1_DEPLOYMENT_PROFILE=backend-cpu \
PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE=/run/prereview-model1/api-bearer-token \
PYTHONPATH=/app \
python -m uvicorn prereview_model1_service.entrypoint:app \
  --host 127.0.0.1 --port 8791 --workers 1 --no-access-log
```

CPU profile은 `PREREVIEW_MODEL1_DEVICE=cuda`와 임의 runtime/preprocessor 경로를
fail-closed로 거부한다. 프로세스를 하나만 띄워야 모델 사본과 메모리 사용도 하나로
유지된다.

Compose를 시작하기 전에는 `.env`의 실제 mount 경로와 같은 runtime으로 identity를
준비한다. 기본 경로가 아닌 runtime을 mount하면서 `--runtime`을 생략하면 서로 다른
artifact의 manifest가 만들어져 healthcheck가 실패한다.

```bash
backend/.venv/bin/python backend/scripts/prepare_model1_resident_config.py \
  --runtime "$PREREVIEW_MODEL1_SERVING_HOST_DIR"

# 승인된 runtime 또는 service 코드가 변경된 경우. 기존 bearer는 회전하지 않는다.
backend/.venv/bin/python backend/scripts/prepare_model1_resident_config.py \
  --runtime "$PREREVIEW_MODEL1_SERVING_HOST_DIR" --refresh-identity
```

## RunPod GPU 실행

RunPod에서 source bundle을 `/workspace/project/prereview-model1/worker`에 풀고, 검증된 `serving.zip`을 별도로 전달한 뒤 순서대로 실행한다.

```bash
worker=/workspace/project/prereview-model1/worker
PYTHONPATH="$worker/backend" python3 "$worker/backend/prereview_model1_service/scripts/prepare_model1_runtime.py" --archive /workspace/serving.zip
bash "$worker/backend/prereview_model1_service/scripts/setup_runpod.sh"
install -d -m 700 /run/prereview-model1
umask 077; openssl rand -base64 48 | tr '+/' '-_' | tr -d '=' > /run/prereview-model1/api-bearer-token
bash "$worker/backend/prereview_model1_service/scripts/start_runpod.sh"
PYTHONPATH="$worker/backend" "$worker/.venv-model1/bin/python" "$worker/backend/prereview_model1_service/scripts/print_runtime_identity.py"
```

마지막 출력의 두 SHA를 백엔드 Model 1 remote adapter 설정에 pin한다. bearer 값은 RunPod와 backend의 root 전용 파일에만 둔다. 이 서비스에는 DB URL, Supabase 키, OpenAI 키를 전달하지 않는다.
