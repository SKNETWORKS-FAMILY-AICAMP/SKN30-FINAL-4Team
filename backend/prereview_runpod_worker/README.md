# RunPod Surya 레이아웃 워커

이 디렉터리는 Existing PDF의 **이미 렌더링된 PNG 페이지**에 Surya 0.22.1 레이아웃 추론을 수행하는 RunPod 배포물이다. 일반 API·PostgreSQL worker와 별도 프로세스이며, RunPod에는 DB 연결 정보나 Supabase service-role/JWT를 전달하지 않는다. 같은 추론 core를 RunPod Serverless handler와 Tailscale 전용 지속형 Pod API에서 재사용한다.

## 역할과 신뢰 경계

```text
신뢰된 EC2 worker
  PDF -> canonical PNG + coordinate/render manifest
  -> 각 PNG/manifest에 한정된 HTTPS signed capability 발급
  -> RunPod Serverless

RunPod A100
  signed GET으로 manifest/PNG만 제한적으로 읽음
  Surya layout (텍스트·OCR HTML을 산출물에 보관하지 않음)
  signed create-only PUT으로 geometry artifact만 저장
  -> EC2가 hash/lineage/fence를 재검증한 뒤 DB 반영
```

RunPod은 다음을 **보유하거나 호출하지 않는다**.

- PostgreSQL `DATABASE_URL`, Supabase service-role key, anon key, 사용자 JWT, API 세션 쿠키
- 원본 PDF/HWP/HWPX, DB RPC, Storage의 bucket-wide credential
- OCR 텍스트, HTML, Surya 원시 응답/진단 로그

허용되는 권한은 요청마다 발급되는 읽기 전용 PNG/manifest capability 및 create-only 결과 capability뿐이다. capability URL은 로그·응답·DB에 보관하지 않는다.

## 고정 런타임

배포 기준 경로는 다음 하나다.

```text
/workspace/project/prereview-surya/worker/
├── backend/                         # 이 저장소의 backend/ 사본 또는 checkout
├── .venv-surya-client/              # RunPod handler + Surya client
├── .venv-surya-vllm/                # 로컬 vLLM server 전용
└── logs/
```

보안 관련 runtime 파일은 mode bit를 보존하지 않는 RunPod Network Volume에 두지 않는다.

```text
/run/prereview-surya/                 # pod/container 로컬 tmpfs/overlay, mode 700
├── config.json                       # mode 600, secret은 넣지 않음
├── attestations/surya-vllm.json      # mode 600
└── pids/                             # PID + process start-time binding
```

공유 캐시는 pod 재시작에도 남는 다음 경로를 사용한다.

```text
/workspace/persistent/prereview/cache/uv
/workspace/persistent/prereview/cache/huggingface
/workspace/persistent/prereview/cache/datalab
/workspace/persistent/prereview/cache/vllm
```

두 Python 환경을 섞지 않는다.

| 환경 | 용도 | Torch 계열 |
| --- | --- | --- |
| `.venv-surya-client` | RunPod SDK, handler, Surya client | `--system-site-packages`로 pod의 검증된 A100 Torch `2.8.0+cu128` / torchvision `0.23.0+cu128` 재사용 |
| `.venv-surya-vllm` | 로컬 모델 서버만 | vLLM `0.20.1`이 소유하는 Torch `2.11` / CUDA `13.0` |

client 환경은 `surya-ocr==0.22.1`, `runpod==1.12.0`, Pillow `10.4.0`, OpenCV `4.11.0.86`을 고정한다. RunPod SDK의 공식 dependency graph는 정상 해석해 설치하고 `pip check`를 통과해야 한다. Surya 쪽은 pod의 CUDA Torch가 resolver에 의해 교체되지 않도록 명시적 closure를 `--no-deps`로 설치한다. vLLM 환경은 `vllm==0.20.1` 및 `transformers==5.16.1`, `huggingface-hub==1.31.0`, `tokenizers==0.23.1`을 함께 고정한다.

Surya tokenizer의 `TokenizersBackend`를 기본 Transformers `4.57.6`이 읽지 못하므로 후자의 세 버전은 의도적인 호환성 override다. 이 조합에서 `pip check`가 xgrammar 메타데이터 충돌을 보고할 수 있다. 이를 무시하거나 숨기지 않는다. 경고가 있으면 기록하고, 아래의 **실제 adapter smoke가 통과하기 전에는 배포 성공으로 판단하지 않는다.**

## 1. 코드 배치

RunPod에 로그인한 뒤 `/workspace` 아래에 검토한 worker bundle을 배치한다. `.env`, 로컬 venv, 다른 handover 자료가 함께 전송되지 않도록 `backend/` 전체를 그대로 rsync하지 않는다. 다음처럼 명시적 allowlist bundle을 만든다.

```bash
backend/prereview_runpod_worker/scripts/build_deploy_bundle.sh \
  /tmp/prereview-surya-worker.tar.gz
sha256sum /tmp/prereview-surya-worker.tar.gz

scp -P <RUNPOD_SSH_PORT> -i ~/.ssh/id_ed25519 \
  /tmp/prereview-surya-worker.tar.gz root@<RUNPOD_IP>:/workspace/
```

RunPod에서는 새 staging 디렉터리에만 풀고 해시를 대조한 뒤 고정 경로로 배치한다. 기존 디렉터리를 무조건 삭제하거나 덮어쓰지 않는다.

```bash
sha256sum /workspace/prereview-surya-worker.tar.gz
mkdir -p /workspace/project/prereview-surya/worker
tar -xzf /workspace/prereview-surya-worker.tar.gz \
  -C /workspace/project/prereview-surya/worker
test -f /workspace/project/prereview-surya/worker/backend/pyproject.toml
test -f /workspace/project/prereview-surya/worker/backend/vendor/common_ir_pipeline/pyproject.toml
```

스크립트는 `PREREVIEW_RUNPOD_WORKER_HOME`을 다른 위치로 바꾸는 것을 거부한다. 또한 `PYTHONHOME`, `PYTHONPATH`, `UV_PROJECT_ENVIRONMENT`, `LD_PRELOAD`가 설정된 상태도 거부한다. global Python/CUDA driver를 바꾸는 설치는 하지 않는다.

## 2. 안전한 설정 문서

설정은 `PREREVIEW_SURYA_WORKER_CONFIG_JSON` 하나로만 전달되며, launcher가 Pod 로컬 권한
`0600`의 `/run/prereview-surya/config.json`을 읽어 이 환경 변수에 넣는다. `/workspace`
Network Volume은 이 Pod에서 `chmod 600` 후에도 mode 666을 반환하고 `allow_other`로
mount될 수 있으므로 **실행 중인 config·PID·attestation·secret의 보안 경계로 사용하지
않는다.** 다만 credential이 없는 검토 완료 config 원본은 Pod 재생성 후 복구를 위해
`/workspace`에 둘 수 있다. lifecycle이 그 원본의 SHA-256을 확인하고 `/run`에 안전한
사본과 digest marker를 만든다.

```bash
cd /workspace/project/prereview-surya/worker
umask 077
mkdir -p /workspace/persistent/prereview/config
test -e /workspace/persistent/prereview/config/surya-worker.json || \
  cp backend/prereview_runpod_worker/config.example.json \
    /workspace/persistent/prereview/config/surya-worker.json

# placeholder를 아래 설명에 맞는 실제 배포 identity로 수정한 뒤 기록한다.
sha256sum /workspace/persistent/prereview/config/surya-worker.json
```

`config.example.json`은 키와 값의 모양을 보여 주는 **비밀 없는 placeholder**다. all-zero identity를 그대로 둔 설정은 worker가 의도적으로 거절한다. `schema_version`은 유지하고 다음 값을 실제 배포 identity로 바꿔야 한다.

- `storage_scopes`: EC2가 mint하는 signed capability와 정확히 같은 host, path template, bucket, prefix. GET input과 PUT result를 분리한다.
- `producer.pipeline_revision`: 검토한 pipeline Git SHA.
- `producer.config_sha256`: EC2가 계산·기록하는 검토된 worker-config identity SHA-256. 이 파일을 자기 자신으로 해시하지 않는다.
- `producer.worker_image_digest`: 태그가 아닌 immutable RunPod image digest.

아래 모델 identity는 변경하지 않는다.

```text
model_id:             datalab-to/surya-ocr-2
model_revision:       3b3d4cdf88d6928b0acdc75181b13206ea67c4a3
model.safetensors:    5755f82a997dd0b111964fa8b31cc2daef7aeb7a706bbd17d73d6a93ef3f723e
```

`surya_endpoint.url`은 반드시 `http://127.0.0.1:8000/v1`이다. `allowed_origins`는 빈 배열로 둔다. 외부 endpoint를 쓰는 변경은 별도 보안 검토가 필요하다.

설정에는 signed URL, token, key, password, DB URL을 추가하지 않는다. runtime은 그런 credential-shaped field를 요청 계약에서 거부한다.

## 3. 설치

RunPod A100 pod 내부에서 실행한다.

```bash
cd /workspace/project/prereview-surya/worker
bash backend/prereview_runpod_worker/scripts/setup_runpod.sh
```

이 스크립트는 재실행 가능하다. A100/CUDA 접근을 확인하고, persistent cache를 만들며, 두 venv만 생성·갱신한다. global Python에 `pip install`하지 않는다.

설치가 끝나면 출력에서 다음을 확인한다.

- client: CUDA 가능, A100, `surya-ocr==0.22.1`, `runpod==1.12.0`, Pillow 10, OpenCV 4.11
- vLLM: CUDA 가능, A100, vLLM 0.20.1, Transformers 5.16.1, Hub 1.31.0, Tokenizers 0.23.1

## 4. vLLM 시작·상태·중지

vLLM은 인터넷에 열지 않는다. 항상 `127.0.0.1:8000`으로만 시작한다.

```bash
# 시작: pinned revision, BF16, max model len 18000, GPU util 0.85
bash backend/prereview_runpod_worker/scripts/start_surya_vllm.sh

# 상태: PID와 실제 cmdline, /v1/models를 함께 확인
bash backend/prereview_runpod_worker/scripts/status_surya_vllm.sh

# 중지: PID·process start time·정확한 argv가 모두 일치할 때만 TERM
bash backend/prereview_runpod_worker/scripts/stop_surya_vllm.sh
```

시작 스크립트는 Hugging Face의 pinned snapshot에서 `model.safetensors`의 SHA-256과 `/v1/models`의 served model id까지 검증한다. 성공한 뒤에만 `/run/prereview-surya/attestations/surya-vllm.json`을 권한 600으로 원자 기록한다. worker는 이 파일의 PID와 revision뿐 아니라 실제 `vllm serve`의 positional model, served alias, process start time을 대조하고 가중치 SHA-256도 다시 계산한다. 추론 직전에도 같은 process identity와 `/v1/models`를 재확인하므로 설정 문자열만으로 model identity를 주장할 수 없다. PID·start time은 `/run/prereview-surya/pids`, 로그는 `logs/surya-vllm.log`에 남는다. stale PID 기록은 자동 삭제하지 않으며, 명령을 확인한 뒤 운영자가 해당 파일만 정리한다.

최초 실행은 모델 다운로드와 vLLM compile/CUDA graph 준비 때문에 오래 걸릴 수 있다. A100 직접 측정에서는 약 9분이었으며 시작 스크립트는 최대 20분 동안 readiness를 기다린다. persistent cache가 유지된 이후의 재시작은 더 짧아질 수 있지만 이를 SLA로 간주하지 않는다.

## 5. SSH Pod에서 GPU adapter smoke

이 검증의 범위는 local GPU adapter까지다.

```bash
# 직접 GPU adapter smoke: local vLLM + Surya client만 검증
# signed Storage, RunPod queue, EC2 artifact acceptance는 포함하지 않음.
bash backend/prereview_runpod_worker/scripts/smoke_surya_adapter.sh

```

adapter smoke는 작은 synthetic PNG를 실제 Surya layout adapter에 넣는다. 따라서 model revision, tokenizer override, local vLLM, CUDA, 결과 canvas를 함께 확인한다. 문제가 생기면 `logs/surya-vllm.log`를 먼저 확인한다. raw provider response나 config 값은 issue에 붙이지 않는다.

SSH Pod에서 `python -m ...surya_layout_worker.entrypoint`를 직접 실행한다고 RunPod Serverless endpoint에 등록되지는 않는다. 이 저장소의 `start_runpod_worker.sh`는 Serverless가 관리하는 image 내부 foreground entrypoint이며 `PREREVIEW_RUNPOD_SERVERLESS_MANAGED=true`가 없으면 fail-closed한다. 지속형 Pod에서는 아래의 별도 HTTP API launcher를 사용한다.

## 6. 지속형 Pod API와 Tailscale

현재 시연 경로는 API를 `127.0.0.1:8787`에만 bind하고 Tailscale Serve를 통해 Backend EC2에 노출한다. Uvicorn worker는 반드시 1개다. API 내부에도 GPU consumer가 정확히 하나뿐이므로 동시에 두 추론이 GPU로 들어가지 않는다.

필수 환경 변수와 기본값은 다음과 같다.

| 변수 | 의미 | 기본값 |
| --- | --- | --- |
| `/run/prereview-surya/api-bearer-token` | EC2와 RunPod 사이의 별도 API bearer. 단일 32~512자 URL-safe 줄, 현재 사용자 소유 regular file, mode `0600`, hard link 없음 | 필수 |
| `PREREVIEW_SURYA_API_STATE_DIRECTORY` | credential 없는 작업 상태 journal. 운영 loader는 Pod 교체 후 복구를 위해 아래 Network Volume 경로만 허용하며 `/run`·`/tmp` 등의 override를 거부한다. | `/workspace/persistent/prereview/surya-jobs` (고정) |
| `PREREVIEW_SURYA_API_QUEUE_CAPACITY` | 실행 중 1건 외 대기 가능한 작업 수 | `4` |
| `PREREVIEW_SURYA_API_MAX_REQUEST_BYTES` | signed-capability 요청 body 상한 | `2097152` |
| `PREREVIEW_SURYA_API_MAX_JOB_RECORDS` | journal에 둘 수 있는 레코드 상한 | `10000` |
| `PREREVIEW_SURYA_API_SHUTDOWN_GRACE_SECONDS` | 실행 중 작업의 종료 대기 시간 | `210` |
| `PREREVIEW_SURYA_API_PORT` | loopback API port | `8787` |

토큰은 EC2와 RunPod에 별도 secret으로 주입한다. RunPod launcher는 `PREREVIEW_SURYA_API_BEARER_TOKEN` 또는 임의 `*_FILE` 환경 변수를 입력으로 받지 않는다. 아래 고정 runtime 파일만 열어 child process에 한 번 전달하며, shell history·명령행·로그에는 토큰을 남기지 않는다. launcher는 inherited `bash -x`/xtrace를 secret 파일 접근 전에 끄며 secret reader 진입 시에도 다시 끈다. `/workspace`에는 토큰, Tailscale auth key, signed URL을 파일로 두지 않는다. 작업 journal만 Pod 교체 뒤에도 reconciliation할 수 있도록 Network Volume의 `/workspace/persistent/prereview/surya-jobs`에 둔다. 시작 시 `/workspace` 자체가 non-symlink 별도 mountpoint인지 확인하고, `persistent`, `prereview`, `surya-jobs`를 한 단계씩 생성 또는 검사하면서 각 구성요소가 directory·non-symlink이고 `realpath`가 정확한 고정 경로인지 확인한다. 하나라도 다르면 API를 시작하지 않으며 journal을 `/run`으로 대체하지 않는다. 이 volume의 `chmod`/소유자는 보안 경계가 아니므로, 각 파일은 bearer에서 프로세스 메모리 안에서 domain-separated HMAC-SHA-256 키를 유도해 서명한 엄격한 envelope로 기록한다. journal에는 키·bearer·capability가 절대 들어가지 않는다. unsigned legacy, 손상, 형식 불명, MAC 불일치 파일은 `.quarantine`으로 best-effort 이동하고 작업이 존재하지 않는 것으로 처리한다. API는 MAC/key 관련 진단을 반환하지 않으며 EC2는 해당 id를 reconciliation 또는 새 capability로 재제출한다.

bearer를 회전하면 이전 bearer로 서명된 모든 journal record는 새 키로 검증되지 않는다. 따라서 회전은 의도적으로 기존 journal을 quarantine/not-found로 만들며, EC2가 진행·종료 상태를 reconciliation하고 필요하면 새 capability로 재제출한 뒤에 수행한다. 이전 기록을 유지해야 하면 회전 전 reconciliation을 끝내고 journal을 운영 절차에 따라 비운다. 이전 bearer를 fallback key로 보관하거나 volume에 MAC key를 저장하지 않는다.

```bash
install -d -m 0700 /run/prereview-surya
( umask 077; openssl rand -hex 32 > /run/prereview-surya/api-bearer-token )
chmod 0600 /run/prereview-surya/api-bearer-token
```

RunPod가 `/dev/net/tun` 없는 Tailscale userspace mode라면 **수신 연결뿐 아니라 RunPod에서 tailnet Storage로 나가는 signed GET/PUT도 proxy가 필요하다.** `tailscaled`의 loopback HTTP 또는 SOCKS listener를 켜고 `/run/prereview-surya/config.json`의 `http.proxy_url`에 명시적으로 넣는다.

```json
"http": {
  "proxy_url": "http://127.0.0.1:1056"
}
```

허용되는 scheme은 `http`, `socks5`, `socks5h`이고 host는 숫자 loopback `127.0.0.1` 또는 `::1`만 가능하다. 사용자 정보, query, fragment, 임의 path, `localhost`와 tailnet/public IP는 거절한다. Storage client는 계속 `trust_env=False`이므로 `HTTP_PROXY`, `ALL_PROXY` 같은 ambient 환경 변수는 사용하지 않는다. SOCKS를 선택하면 고정된 `socksio` dependency를 사용한다.

Supabase Storage의 signed GET path는 `/storage/v1/object/sign/{bucket}/{object_key}`, signed upload PUT path는 `/storage/v1/object/upload/sign/{bucket}/{object_key}`로 서로 다르다. scope를 같은 template으로 합치지 않는다. input prefix는 `accelerator/input/`, 결과 key는 `accelerator/surya-layout/{logical_compute_key}/result.json`이므로 PUT prefix는 `accelerator/surya-layout/`이다. self-hosted Storage의 signed upload 기본 TTL은 60초이며 서버 환경 변수 `UPLOAD_SIGNED_URL_EXPIRATION_TIME`(fallback `SIGNED_UPLOAD_URL_EXPIRATION_TIME`)으로 조정한다. 현재 배포는 1800초로 맞추므로 example의 `max_ttl_seconds`도 1800이다. 운영자 E2E 스크립트는 request TTL을 1200초, read-capability ceiling을 1800초로 고정해 분리하며, `dispatch_policy.max_ttl_seconds < 1800`이거나 execution timeout이 request TTL을 넘으면 config 오류로 중단한다. 따라서 허용된 모든 policy에서 발급·전송을 위한 600초 dispatch slack이 유지된다. EC2 issuer는 실제 JWT `exp`를 읽어 capability `expires_at`을 기록하며, worker는 임의로 expiry를 늘리거나 URL을 재발급하지 않는다.

위 파일의 값은 EC2가 보유한 API bearer와 같은 값이어야 한다. secret manager/안전한 운영 절차로 파일에 기록하되, `cat`, `echo`, `export`로 값을 화면·shell history에 노출하지 않는다. launcher는 symlink, non-regular file, 다른 사용자 소유, mode가 `0600`이 아닌 파일, hard link, 여러 줄·범위 밖 문자를 fail-closed한다. 디버깅 시에도 `bash -x` stderr에 bearer 값이 나오지 않도록 launcher의 xtrace 차단을 제거하거나 secret read 뒤에 다시 켜지 않는다.

### 6.0 Persistent Pod lifecycle supervisor

지속형 Pod의 표준 진입점은 `persistent_lifecycle.sh`다. backend 서비스 디렉터리에
스크립트를 넣지 않고 이 worker bundle 안에 둔 이유는, GPU Pod의 vLLM·userspace
Tailscale·고정 `/run` credential의 수명이 EC2 backend process와 다르기 때문이다.
`run`만 foreground supervisor이며 `start`/`stop`/`restart`는 그것을 관리한다. systemd나
Docker-in-Docker는 사용하지 않는다.

첫 부팅에서는 비밀을 repo, `/workspace`, 명령행에 두지 않는다. 배포 환경에서는 RunPod
Secret 두 개를 만들고 다음처럼 각각의 bootstrap 환경 변수에 참조한다. 실제 값을 일반
환경 변수 칸에 직접 붙여 넣지 않는다.

```text
PREREVIEW_RUNPOD_BOOTSTRAP_API_BEARER_TOKEN={{ RUNPOD_SECRET_prereview_surya_api_bearer }}
PREREVIEW_RUNPOD_BOOTSTRAP_TAILSCALE_AUTH_KEY={{ RUNPOD_SECRET_prereview_surya_tailscale_auth_key }}
```

두 secret은 서로 독립적이다. API bearer는 작업 journal의 HMAC 연속성을 위해 Pod를
교체해도 같은 값을 유지한다. Tailscale auth key는 fresh/`NeedsLogin` 상태에서만 쓰며,
RunPod Secret에는 만료·재사용 정책을 의도적으로 정한 key를 넣는다. 기존 인증 state를
승계한 동일 Pod 재시작에서는 Tailscale key를 생략할 수 있다. 완전히 새 `/run`으로
시작하는 Pod에는 보통 두 값이 모두 필요하다.

SSH에서 수동으로 검증할 때만 `lifecycle.env.example`을 repo 밖 mode `0600` 파일로
복사해 source할 수 있다.

```bash
cd /workspace/project/prereview-surya/worker
umask 077
# /secure/operator/prereview-surya-lifecycle.env 는 repo 밖의 0600 파일
set -a
. /secure/operator/prereview-surya-lifecycle.env
set +a
bash backend/prereview_runpod_worker/scripts/persistent_lifecycle.sh start
unset PREREVIEW_RUNPOD_BOOTSTRAP_API_BEARER_TOKEN
unset PREREVIEW_RUNPOD_BOOTSTRAP_TAILSCALE_AUTH_KEY
```

supervisor는 제공된 값을 `/run/prereview-surya/api-bearer-token` 및 선택적
`/run/prereview-surya/tailscale-auth-key`(`0600`)에 원자 기록하고, 자식 process를
시작하기 전에 bootstrap 환경 변수를 unset한다. lifecycle lock wrapper와 서비스 자식에도
bootstrap 변수를 상속하지 않는다. API launcher와 Tailscale CLI는 이 고정 파일만 읽는다.
`tailscale up`에는 `file:/run/...` 경로만 전달하므로 auth key 값 자체는 argv에 들어가지
않는다. API bearer는 항상 fixed file 또는 bootstrap 값이 필요하다.
Tailscale auth key는 fresh/NeedsLogin state에는 필요하지만, 보호된 authenticated state를
재사용하는 restart에서는 없어도 된다. Pod 교체 후 `/run`은 사라지므로 fresh Pod에는 두 값을
함께 주입한다.

기존 API bearer가 있으면 새 bootstrap 값은 반드시 완전히 같아야 한다. 다른 값으로
덮어써 journal HMAC continuity를 끊는 동작은 거절한다. Tailscale auth key도 같은
protected-existing-file 정책을 따른다. `lifecycle.env.example`의 non-secret
`PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME`·`PREREVIEW_RUNPOD_TAILSCALE_DNS_NAME`은 필수다.
supervisor는 `tailscale up --reset --hostname=...` 뒤 JSON의 Running/Online 및 HostName/DNSName
exact match를 확인한다. Serve JSON도 HTTPS 443의 단 하나 `/ → http://127.0.0.1:8787`만
허용하고 Funnel 또는 추가 handler를 거절한다. cold boot config source와 SHA-256도 example에
명시하며, placeholder example 또는 동일 hash는 `/run/config.json` source로 사용할 수 없다.
첫 bootstrap에서 `/run/prereview-surya/config.sha256`을 `0600`으로 만들고 이후 재기동마다
runtime config와 exact match를 확인한다. marker가 없으면 config source와 SHA-256을 다시
제공해야 하며, 같은 값을 반복 제공하는 것은 허용한다.

RunPod Template의 non-secret 환경 변수는 다음처럼 둔다.

```text
PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME=<tailscale status의 exact HostName>
PREREVIEW_RUNPOD_TAILSCALE_DNS_NAME=<tailscale status의 exact DNSName>
PREREVIEW_RUNPOD_CONFIG_SOURCE_FILE=/workspace/persistent/prereview/config/surya-worker.json
PREREVIEW_RUNPOD_CONFIG_SHA256=<위 파일의 검토된 sha256>
```

Template의 Startup Command는 foreground supervisor를 Pod의 주 process로 실행한다.

```bash
exec env -u PYTHONHOME -u PYTHONPATH -u UV_PROJECT_ENVIRONMENT -u LD_PRELOAD \
  bash /workspace/project/prereview-surya/worker/backend/prereview_runpod_worker/scripts/persistent_lifecycle.sh run
```

```bash
# foreground: SSH session을 붙여 두고 supervisor 로그를 직접 볼 때
bash backend/prereview_runpod_worker/scripts/persistent_lifecycle.sh run

# background lifecycle control
bash backend/prereview_runpod_worker/scripts/persistent_lifecycle.sh start
bash backend/prereview_runpod_worker/scripts/persistent_lifecycle.sh status
bash backend/prereview_runpod_worker/scripts/persistent_lifecycle.sh restart
bash backend/prereview_runpod_worker/scripts/persistent_lifecycle.sh stop
```

여기서 `restart`는 `/run`이 유지되는 같은 Pod/container 안의 서비스 재기동이다. Pod/container
재생성으로 `/run`이 사라지면 Secret 두 개와 config source/digest를 다시 bootstrap한다.
외부 distributed lease는 아직 없으므로 같은
`/workspace/persistent/prereview/surya-jobs`를 사용하는 활성 Pod는 반드시 한 대뿐이어야
한다. 교체 전 기존 Pod의 Serve와 API가 종료됐는지 확인한다.

`start`/`stop`/`restart`는 5초 bounded `flock`으로 PID identity 확인과 signal delivery만
직렬화한다. API의 최대 300초 graceful shutdown 동안 lock을 잡지 않는다. status는 PID,
start time, exact argv, loopback readiness를 읽기만 하며 directory·PID·Serve 설정을 만들거나
고치지 않는다. stale PID 파일은 자동 삭제하지 않는다.

기동 순서는 userspace `tailscaled`(TUN 없음, SOCKS `127.0.0.1:1055`, HTTP proxy
`127.0.0.1:1056`) → pinned vLLM readiness → persistent API readiness → Tailscale Serve다.
tailscaled의 tailnet HTTPS certificate/private material은 `/workspace`가 아닌 Pod-local
`/run/prereview-surya/tailscale-var`(current owner, mode `0700`, non-symlink)에만 둔다.
종료는 Serve off → API grace → vLLM → tailscaled의 역순으로 완료한다. Serve target은
오직 `127.0.0.1:8787`의 HTTPS 443이며,
script는 `tailscale funnel`을 호출하지 않는다.

직접 API foreground launcher는 supervisor 진단 목적에만 사용한다.

```bash
bash backend/prereview_runpod_worker/scripts/start_persistent_api.sh
```

Tailscale Serve는 loopback `127.0.0.1:8787`을 tailnet HTTPS 기본 포트 `443`으로만 proxy한다. Storage capability issuer도 현재 HTTPS 443 origin을 요구하므로 별도 공개 port를 붙이지 않는다. 설치된 Tailscale 버전의 `tailscale serve --help`로 문법을 확인하고, public Funnel은 사용하지 않는다. Backend EC2에서는 tailnet HTTPS 주소와 bearer를 사용한다. 표준 종료는 supervisor가 Serve를 먼저 off하고 API grace를 기다리는 역순이다. API를 직접 진단 실행한 경우에도 새 요청을 막기 위해 Serve를 먼저 내린 뒤 API를 종료하고 `tailscale serve status`로 빈 구성을 확인한다.

API 계약은 다음과 같다.

- `GET /health`: 인증 정보나 model 상세 없이 readiness, queue 깊이, `gpu_concurrency: 1`만 반환
- `POST /jobs`: bearer와 단 하나의 `Idempotency-Key`가 필수다. key는 parsed `SuryaLayoutRequest.logical_compute_key`와 정확히 같은 64자 소문자 SHA-256이어야 한다. body는 `{"input": <SuryaLayoutRequest.to_wire_payload()>}`이며 성공 시 `202`와 `Location` 반환
- `GET /jobs/{id}`: bearer 필수. `queued|running|succeeded|content_failed|infra_retryable|cancelled` 및 검증된 terminal output 반환
- `POST /jobs/{id}/cancel`: bearer 필수. `AcceleratorPort`가 아직 `queued`인 작업을 취소하고 `200` 상태 JSON을 받음
- `DELETE /jobs/{id}`: 같은 queued-only 취소의 운영 편의 alias. 성공은 빈 `204`; 실행 중·종료 작업은 두 경로 모두 `409`

signed URL을 포함한 원래 요청 body는 bounded memory queue에만 존재한다. 디스크 journal에는 job id, 상태, request/logical digest, 시각, 고정 reason, 검증된 terminal artifact metadata만 원자 기록된다. 재시작으로 memory queue가 사라지면 이전 `queued`·`running`은 `infra_retryable/worker_restarted`로 fence한다. EC2는 먼저 durable reconciliation handle과 external job ID/fence로 deterministic result를 재확인하고, 결과가 없을 때만 새 capability를 발급해 같은 logical key로 재제출한다. RunPod는 DB 상태를 임의 복원하지 않는다. 정상 종료 시 대기 작업은 `infra_retryable/worker_shutdown`이 된다. 실행 중 동기 추론이 grace 안에 끝나지 않으면 레코드를 `running`으로 남겨 다음 시작에서 같은 restart fence를 적용한다.

같은 logical compute key(`Idempotency-Key`)와 request digest를 다시 보내면 진행 중·성공·content failure·cancelled 상태는 기존 레코드를 그대로 반환한다. `infra_retryable`은 EC2가 durable handle로 result를 reconcile한 뒤, 결과가 없을 때 새 signed capability를 발급한 명시적 재시도로 간주하여 같은 remote job id를 `queued`로 다시 전환한다. RunPod journal은 그 remote id의 최신 시도만 가지며, 전체 시도 이력과 retry budget은 EC2의 `ops.processing_run`이 소유한다.

queue가 차면 `429 Retry-After: 5`, journal 상한이나 worker 장애 시 admission을 닫는다. 상한에 도달하면 Pod를 중지한 상태에서 retention 정책에 따라 종료 레코드를 외부 감사 저장소로 옮긴 뒤 정리해야 한다. journal에는 capability나 credential이 없지만 임의 삭제로 작업 이력을 잃지 않도록 한다.

## 6.1. 운영자용 persistent signed-Storage E2E

`backend/scripts/run_persistent_surya_storage_e2e.py`는 한 번의 accelerator-only 왕복을 확인하는 운영자 도구다. 실행 전에 다음을 확인한다.

- laptop의 Supabase gateway가 `127.0.0.1:8000`에서 실행 중이고, RunPod이 접근할 Storage origin은 laptop의 Tailscale Serve tailnet 주소로 설정되어 있어야 한다. RunPod persistent API는 `127.0.0.1:8787`에만 bind한다.
- service-role key를 사용하는 host-side Supabase URL은 canonical literal loopback `http://127.0.0.1:<PORT>`만 허용한다(현재 local gateway는 `http://127.0.0.1:8000`). `localhost`, DNS/private 주소, HTTPS 또는 경로가 있는 URL은 거절된다.
- laptop 8000과 RunPod 8787 각각에 Tailscale Serve만 설정한다(두 target 모두 loopback). `tailscale serve status`로 tailnet 전용 HTTPS proxy를 확인하고, `tailscale funnel` 또는 public Funnel은 절대 사용하지 않는다. 설치된 Tailscale 버전의 `tailscale serve --help`에 맞춰 다음 target을 설정한다.

```text
laptop  : http://127.0.0.1:8000
RunPod  : http://127.0.0.1:8787
```

설치된 CLI가 이 형식을 지원하면 각 host에서 다음처럼 Serve를 background로 설정한다. 두 명령은 서로 다른 host에서 실행한다.

```bash
# laptop
tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status

# RunPod
tailscale serve --bg --https=443 http://127.0.0.1:8787
tailscale serve status
```

중지할 때는 각 host에서 supervisor가 먼저 Serve를 내리고 foreground API grace를 기다린다.
단일 Serve 구성을 내릴 때는 `off`를 사용하고, host의 Serve 구성을
전부 초기화해야 하는 승인된 운영 절차에서만 `reset`을 사용한다. 어느 경우든 마지막에
`status`가 빈 구성을 가리키는지 확인한다.

```bash
tailscale serve --https=443 off
tailscale serve status

# 여러 Serve 구성을 모두 지워야 하는 별도 승인 절차에서만
tailscale serve reset
tailscale serve status
```

- RunPod의 `/run/prereview-surya/config.json`·`/run/prereview-surya/api-bearer-token`과 명령을 실행하는 host의 `.runtime/runpod-e2e/config.json`·`.runtime/runpod-e2e/api-client.json`·`.runtime/prereview-surya-api-token`·`.runtime/supabase-dev/.env`는 각각 현재 사용자 소유 regular file, mode `0600`, hard link 없음이어야 한다. config에는 secret을 넣지 않으며, bearer·service-role key는 명령행이나 환경변수 inline으로 넣지 않는다. RunPod userspace Tailscale이면 config의 `http.proxy_url`도 loopback listener로 설정한다.
- API bearer의 목적지는 명령행 URL이 아니라 보호된 `api-client.json`이 고정한다. 파일 형식은 `{"schema_version":"prereview.persistent-api-client/v1","origin":"https://<RUNPOD_TAILNET_DNS_NAME>"}`이며 origin은 path·query·userinfo·명시 port가 없는 소문자 `https://*.ts.net` exact origin이어야 한다. `--runpod-api-base-url`은 선택적 호환 assertion일 뿐이고 설정값과 다르면 bearer를 읽기 전에 실패한다.
- `--artifact-root`에는 `.runtime/evaluations/persistent-surya/<RENDER_ARTIFACT_ROOT>/render_manifest.json`과 PNG가 있고, `--runpod-config-json`은 `.runtime/prereview-surya/config.json`처럼 배포와 동일한 실제 설정(예제의 all-zero identity가 아님)이어야 한다. Storage scope의 origin은 `https://<LAPTOP_TAILNET_DNS_NAME>`, API 주소는 `https://<RUNPOD_TAILNET_DNS_NAME>`처럼 tailnet DNS placeholder를 사용한다.

host의 backend checkout에서 아래처럼 실행한다. 경로·bucket·tailnet DNS는 실제 값으로 바꾸되 secret 값 자체는 바꾸어 넣지 않는다.

```bash
cd /home/paim/Project/SKN30-FINAL-4Team
umask 077
test "$(stat -c '%a' .runtime/prereview-surya-api-token)" = 600
test "$(stat -c '%a' .runtime/supabase-dev/.env)" = 600
test "$(stat -c '%a' .runtime/runpod-e2e/config.json)" = 600
test "$(stat -c '%a' .runtime/runpod-e2e/api-client.json)" = 600
backend/.venv/bin/python backend/scripts/run_persistent_surya_storage_e2e.py \
  --artifact-root .runtime/evaluations/persistent-surya/<RENDER_ARTIFACT_ROOT> \
  --runpod-config-json .runtime/runpod-e2e/config.json \
  --runpod-api-client-config .runtime/runpod-e2e/api-client.json \
  --runpod-bearer-token-file .runtime/prereview-surya-api-token \
  --storage-bucket <STORAGE_BUCKET> \
  --supabase-runtime-env .runtime/supabase-dev/.env \
  --poll-timeout-seconds 180
```

stdout는 `mode`, `operator`, `disposition`, `reason_code`, 선택적 `provider_state`만 담은 한 줄의 safe redacted JSON이다. exit code가 성공을 나타내더라도 URL·request body·token·service-role key·raw provider 진단을 출력하거나 이슈에 첨부하지 않는다. 이 검사는 input과 deterministic result Storage object를 의도적으로 보존하며, DB lease/commit을 수행하지 않고 Storage object를 삭제·overwrite하거나 자동 정리하지 않는다. 보존 기간이 끝난 뒤의 GC/삭제는 별도 승인된 절차로 수행한다.

작업 journal은 `/workspace/persistent/prereview/surya-jobs` Network Volume에 계속 둔다. launcher는 `/workspace`가 별도 mountpoint가 아니거나 고정 경로의 어느 구성요소라도 symlink/realpath mismatch이면 시작을 거부한다. mode bit가 보안 경계가 아닌 volume이므로 bearer에서 process memory 안에서만 domain-separated HMAC-SHA-256 key를 유도해 envelope를 보호하고, journal에는 key·bearer·capability를 쓰지 않는다. `/run/prereview-surya`에는 secret/config/PID 같은 ephemeral runtime만 두며 journal을 옮기지 않는다.

계약의 `SuryaLayoutReconciliationHandle`은 trusted render lineage, pinned producer, logical key와 deterministic result binding만 담는 credential-free 영속화 단위다. signed URL, provider status, request digest와 capability는 담지 않는다. 이 스크립트는 동일한 프로세스 안에서는 initial submit의 불확실한 `infra_retryable`(job ID 없음 포함) 또는 deadline 직전에 이 handle로 deterministic result를 reconcile하고, logical key를 persistent API job ID로 사용해 deadline까지 poll한다. 다만 handle·external job ID·lease를 파일/DB에 저장하지 않으므로 operator 프로세스나 Pod가 재시작되면 이 실행을 이어서 reconcile하지 않는다. Production worker는 dispatch 전에 handle과 external job ID 및 DB lease/fence를 durable state에 함께 저장하고 restart 후 이를 사용해 result를 reconcile해야 한다.

### 6.1.1. A4.5 공개 평가 코퍼스 결과 저장

`backend/scripts/export_pdf_primary_corpus_surya_artifact.py`는 A4.5 split에 공개된 한 case의
native capture와 canonical render를 다시 검증하고, 위 persistent signed-Storage E2E가 반환한
strict Surya 결과를 다음 고정 경로에 한 번만 생성한다.

```text
<CASE_ROOT>/surya/surya_layout_artifact.json
```

blind reveal은 입력으로 받지 않는다. `case_id`는 tracked split의 공개 case여야 하고,
source PDF·전체 render page·native capture·Common IR과 split/source baseline의 결속이 모두
맞아야 네트워크 단계로 넘어간다. 새 결과 디렉터리는 mode `0700`으로 만들고, 기존
디렉터리는 현재 사용자 소유이면서 group/world-write가 없어야 한다. 결과 파일은 `0600`으로
만들며, 이미 결과가 있으면 덮어쓰지 않고 실패한다.

```bash
cd /home/paim/Project/SKN30-FINAL-4Team
backend/.venv/bin/python \
  backend/scripts/export_pdf_primary_corpus_surya_artifact.py \
  --split backend/baselines/pdf_reconstruction/primary_corpus_split_a45.v1.json \
  --expected-split-sha256 d6820fbe176df095cbd09e3faf56568167243b9db990982f295243d50f49fd51 \
  --source-baseline backend/baselines/pdf_fusion/bizinfo_existing_100.v1.json \
  --case-id held-out-104102 \
  --case-root .runtime/evaluations/pdf-primary-corpus-a45/cases/PBLN_000000000104102 \
  --runpod-config-json .runtime/runpod-e2e/config.json \
  --runpod-api-client-config .runtime/runpod-e2e/api-client.json \
  --runpod-bearer-token-file .runtime/prereview-surya-api-token \
  --storage-bucket request-temp \
  --supabase-runtime-env .runtime/supabase-dev/.env \
  --poll-timeout-seconds 180
```

공개 case ID는 `tuning-114788`, `known-regression-121019`, `held-out-104102`,
`held-out-124791`, `negative-control-115310`이다. 각 `case_root`는 해당 notice의 실제
source/native/render lineage에 결속된 evidence root여야 한다. stdout의
`{"status":"exported"}`는 검증된 artifact를
로컬에 저장했다는 뜻이다. 같은 logical compute key에 대응하는 유효한 deterministic
Storage 결과가 이미 있으면 이를 재검증해 사용할 수 있으므로, 이 문구가 새 RunPod 추론을
증명하지는 않는다. RunPod은 cache miss일 때만 켜져 있으면 된다.

persistent journal에 terminal record가 남아 있는데 대응하는 Storage result가 사라진 경우,
같은 logical key의 재요청은 기존 terminal job에 고정되어 결과를 다시 만들지 못하고 timeout이
날 수 있다. `max_records` 도달 문제와 함께 운영 retention 대상으로 관리해야 한다. 임의로
journal record를 삭제하지 말고, deterministic result와 EC2의 durable 실행 이력을 대조해
감사 저장소로 옮긴 뒤 승인된 정리 절차를 수행한다.

## 6.2. Existing PDF one-shot 운영자 경로

`backend/scripts/run_existing_pdf_one_shot.py`는 공개 Request 업로드 API가 아니라, 외부에서
수집한 Existing 공고 PDF 한 건을 검증·구조화·적재하는 trusted operator 명령이다. 순서는
다음과 같다.

1. PDF를 200 DPI canonical PNG와 render manifest로 만든다.
2. native-only replay를 먼저 실행하고, 모든 페이지에 substantive native text가 있는지
   확인한다. image-only 또는 일부 페이지만 native text인 PDF는 GPU 호출 전에 거절한다.
3. 같은 render·producer의 deterministic Surya 결과가 Storage에 있으면 전체 계약을 다시
   검증해 재사용하고, 정확한 404일 때만 signed capability를 발급해 persistent Pod에 보낸다.
4. native capture와 textless Surya geometry를 Common IR `1.1.0`에 결속한다.
5. OpenAI `gpt-5.6-terra`와 검토된 Existing 후보 정책으로 Profile·source-selection을 만든다.
6. PDF fusion 산출물을 포함한 표준 Existing pack을 검증하고, 선택 시 기존 trusted
   importer로 `kb.*`와 private `existing-kb` Storage에 적재한다.

입력 PDF·metadata와 파생 Common IR 본문은 각각 RunPod/Supabase Storage 및 OpenAI로
전송될 수 있으므로 승인된 공개·합성·비식별 자료만 사용한다. `--no-ingest`는 KB DB commit만
생략한다. render, Storage 결과 조회/dispatch, RunPod 추론, OpenAI 구조화는 그대로 실행한다.
`OPENAI_LOG=debug`는 원문 노출 위험 때문에 파일·ambient 환경 모두 거절한다.

먼저 새 private output에서 DB commit 없이 패키지를 만든다.

```bash
cd /path/to/repository
umask 077
backend/.venv/bin/python backend/scripts/run_existing_pdf_one_shot.py \
  --pdf /private/input/notice.pdf \
  --metadata /private/input/metadata.json \
  --output-dir /private/output/PBLN_<SYNTHETIC_OR_NEW_ID> \
  --runpod-config-json .runtime/runpod-e2e/config.json \
  --runpod-api-client-config .runtime/runpod-e2e/api-client.json \
  --runpod-bearer-token-file .runtime/prereview-surya-api-token \
  --supabase-runtime-env .runtime/supabase-dev/.env \
  --backend-env backend/.env \
  --storage-bucket request-temp \
  --poll-timeout-seconds 180 \
  --no-ingest
```

패키지 검토 뒤 같은 인자와 `--resume`을 사용하고 `--no-ingest`만 제거하면 적재한다.
실제 공고 ID를 갱신하려는 작업이 아니라 E2E라면, 실행 전에 해당 synthetic ID가
`kb.notice`에 없음을 읽기 전용으로 확인한다.

```bash
backend/.venv/bin/python backend/scripts/run_existing_pdf_one_shot.py \
  --pdf /private/input/notice.pdf \
  --metadata /private/input/metadata.json \
  --output-dir /private/output/PBLN_<SYNTHETIC_OR_NEW_ID> \
  --runpod-config-json .runtime/runpod-e2e/config.json \
  --runpod-api-client-config .runtime/runpod-e2e/api-client.json \
  --runpod-bearer-token-file .runtime/prereview-surya-api-token \
  --supabase-runtime-env .runtime/supabase-dev/.env \
  --backend-env backend/.env \
  --storage-bucket request-temp \
  --poll-timeout-seconds 180 \
  --resume
```

pack에는 authoritative attachment/Common IR/Profile 외에
`pipeline/pdf_fusion/{native_capture,render_manifest,surya_layout_artifact,replay_manifest}.json`,
`source.pdf`, `rendered/*.png`가 들어간다. fusion의 `source.pdf`는 authoritative attachment와
동일한 바이트임을 증명하는 pack 복사본이며, importer는 이를 중복 artifact로 올리지 않고
기존 `source` artifact 하나로 표현한다. 나머지 fusion 산출물은 `kb.artifact`와
`kb.artifact_lineage`에 등록한다. Profile의 canonical `notice_id`는 `bizinfo:PBLN_*`이므로
과거 bare `PBLN_*` notice를 보존하는 DB는 명시적 identity migration이 필요하고, 개발 DB는
clean bootstrap을 사용한다. DB transaction 전에 content-addressed Storage upload가
일어나므로 DB rollback 뒤 orphan object가 남을 수 있으며, 이를 전체 Storage+DB 원자
commit이라고 부르지 않는다. 보존·GC는 별도 승인된 retention 절차가 소유한다.

이 명령의 fence는 `AlwaysCurrentFence`이고 durable queue lease를 소유하지 않는다. 따라서
Existing offline operator 검증 경로이지 production Request queue E2E가 아니다. Surya 결과도
현재는 geometry-only shadow라 Profile 의미 근거는 native text만 사용한다. 표·다이어그램의
텍스트·관계를 의미 근거로 승격하는 작업은 agreement/promotion 회귀 gate 이후 별도 단계다.

## 7. RunPod Serverless 배포 경계

운영 handler는 검토된 bundle로 별도 container image를 만들고, RunPod Serverless template과 endpoint에 그 immutable image digest를 등록해야 한다. image는 CI나 별도 build host에서 만들며 SSH Pod 내부의 Docker-in-Docker를 전제로 하지 않는다. endpoint의 `executionTimeout`과 `ttl`은 다음 조건을 모두 만족하도록 RunPod control plane에서 설정하고 배포 기록으로 검증한다.

- `executionTimeout <= dispatch_policy.max_execution_timeout_seconds`
- `executionTimeout < signed capability 만료까지 남은 시간`
- `ttl >= queue 지연 예산 + executionTimeout`, 단 capability TTL을 넘지 않음

동기 Surya client가 내부에서 멈추는 경우 Python thread가 이를 강제로 끊지는 못한다. 최종 hard wall-clock boundary는 RunPod의 실제 `executionTimeout`이 worker process를 종료하는 동작이다. 따라서 endpoint 설정을 확인하지 않은 SSH smoke만으로 Stage 5 운영 준비 완료를 선언하지 않는다. 배포 image의 entrypoint에서만 다음 opt-in을 지정한다.

```text
PREREVIEW_RUNPOD_SERVERLESS_MANAGED=true
```

지속형 Pod API는 이제 이 bundle의 지원 경로다. `start_persistent_api.sh`가 loopback
`127.0.0.1:8787`의 인증 HTTP API를 foreground로 실행하고, supervisor와 Tailscale Serve가
수명·tailnet ingress를 관리한다. `/workspace/persistent/prereview/surya-jobs`의 HMAC
journal 재조회와 operator process 안의 credential-free handle을 이용한 deterministic
result reconciliation을 signed-Storage E2E로 검증했다. EC2 DB에 handle·external job·lease를
영속화하는 production restart reconciliation은 아직 구현·검증하지 않았다. Serverless SDK
handler와 immutable image 배포도 별도의 Serverless 경계로 운영한다.

## 검증 기록

2026-09-16 A100-SXM4-80GB direct Pod에서 allowlist source bundle을 검증했다.
정확한 검증 bundle SHA-256은 bundle 밖의
`backend/fastapi/docs/PDF_DOCUMENT_FUSION_DESIGN.md`에 기록한다. README는 bundle
입력이라 자신의 SHA-256을 직접 포함하지 않는다.

- fresh setup 및 재실행: 통과
- client: Torch `2.8.0+cu128`, torchvision `0.23.0+cu128`
- vLLM: `0.20.1`, Torch `2.11.0+cu130`, loopback only
- model revision·weights SHA-256·PID start-time·actual/served model 검증: 통과
- synthetic PNG 실제 Surya adapter smoke: 통과, textless region 2개
- 새 SSH session의 status 및 handler cold-start composition: 통과
- runtime mode: root 700, config/attestation 600

이 기록의 direct GPU adapter smoke와 별도로 다음 persistent 경로를 검증했다.

- persistent Pod API + Tailscale Serve tailnet-only + signed Storage GET/PUT E2E: 통과
- create-only 결과의 hash/size/canonical JSON/coordinate lineage/fence acceptance: 통과
- Pod/API restart 후 HMAC journal 재조회와 deterministic result 재검증: 통과

운영자 one-shot 스크립트는 queue lease/fence를 소유하지 않는다. `--no-ingest`를 제거하면
trusted importer의 공고 단위 DB transaction은 수행하지만, 그 스크립트 프로세스가 재시작되면
RunPod 실행의 in-memory handle을 이어받지 않는다. Production worker는 dispatch 전에 handle과
external job ID 및 DB lease/fence를 durable state에 함께 저장하고 restart 후 이를 사용해
result를 reconcile해야 한다.

2026-09-17에는 8쪽 synthetic PDF로 다음 one-shot 전체 경로를 추가 검증했다.

- CPU render/native preflight → tailnet-only persistent RunPod Surya → fused Common IR: 통과
- OpenAI `gpt-5.6-terra` Existing Profile/source-selection 생성: 통과
- `--no-ingest` pack 검증 후 같은 output의 `--resume` trusted import: 통과
- pack/DB 관계 데이터 일치: Profile 1건, Fact 34건, support component 7건
- PDF fusion 포함 artifact 17개와 lineage 24개: 일치
- private `existing-kb` Storage 17개 객체의 SHA-256/크기 재다운로드 검증: 통과
- embedding은 이 one-shot 범위에서 생성하지 않았으며 별도 bootstrap 단계로 남는다.

로컬 self-hosted Supabase Envoy가 Storage create/sign 요청에 간헐적으로 `502/503/504`를
반환할 수 있어, 읽기 및 capability 발급에는 제한된 3회 재시도를 적용한다. create-only
upload의 gateway 응답이 모호하면 mutation을 재전송하지 않고 exact GET의 hash/size로만
성공 여부를 reconcile한다.

**목표 production E2E**는 신뢰된 EC2 worker가 실제 Existing PDF를 canonical PNG/manifest로 렌더하고,
scope에 맞는 short-lived signed GET/PUT capability를 발급해 persistent Pod job을 보낸 뒤,
EC2가 create-only artifact를 SHA/size/canonical JSON/coordinate lineage/fence까지 다시
검증하는 흐름이다. restart 중 capability-bearing request가 사라져도 durable handle과
external job/fence로 deterministic result를 재확인하고, 없을 때만 같은 logical key로
안전하게 재제출해야 한다. 현재 one-shot은 Existing trusted importer transaction까지
연결하지만 durable queue lease/fence 경로는 아니며, production worker commit은 후속이다.

## 운영 제한과 rollback

- 현재 범위는 `existing_pdf_shadow`의 geometry-only Surya layout이다. Request HWP/HWPX 구조화나 native PDF Common IR을 RunPod으로 이전하지 않는다.
- worker는 PDF를 받거나 재렌더링하지 않는다. 페이지 PNG 수·bytes·pixels·TTL·output·regions 상한은 config 및 요청 정책 양쪽에서 제한된다.
- 결과 PUT은 create-only다. 409/412는 overwrite하지 않고 EC2 쪽 reconciler가 hash로 재확인한다.
- RunPod endpoint의 queue/timeout은 capability TTL보다 짧게 운영한다. TTL 만료나 GPU/Storage 장애는 infra retryable이며 EC2가 재시도 정책을 소유한다.

rollback은 다음 순서다.

1. EC2의 accelerator dispatch feature flag를 끄고 native Existing PDF 경로만 사용한다.
2. 실행 중인 handler와 vLLM을 위 stop script로 중지한다.
3. immutable result object와 persistent cache는 즉시 삭제하지 않는다. 사고 조사·hash 재검증 또는 정해진 retention GC에 맡긴다.
4. revision/weights/config을 바꿀 때는 새 worker image digest 및 새 producer identity로 별도 배포하고, adapter smoke와 signed-Storage E2E를 다시 통과시킨다.

이 문서는 Docker-in-Docker를 전제로 하지 않는다. RunPod pod의 global Python 및 다른 `/workspace` 프로젝트는 변경하거나 삭제하지 않는다.
