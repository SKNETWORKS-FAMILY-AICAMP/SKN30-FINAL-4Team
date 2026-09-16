# 프론트엔드 배포 가이드

이 문서는 React 정적 빌드를 Caddy로 배포하는 절차다. Caddy는 Docker가 아닌 호스트
`systemd` 서비스로 실행한다. 아래 주소와 경로는 모두 예시이므로 실제 환경에 맞게 바꾼다.

## 운영 구성

```text
인터넷
  -> Caddy :80/:443
     |- /                  -> 정적 파일 디렉터리
     `- /api/*             -> Tailnet의 백엔드 API
```

| 항목 | 문서 예시 | 설명 |
|---|---|---|
| 공개 도메인 | `app.example.com` | 실제 DNS 이름으로 교체 |
| 정적 파일 경로 | `/srv/app` | Caddy가 읽을 빌드 배포 경로 |
| 백엔드 | `100.64.0.10:8001` | 실제 Tailnet IP와 API 포트로 교체 |
| 저장소 Caddy 예제 | `frontend/deploy/Caddyfile.example` | 실제 주소를 포함하지 않는 커밋 대상 |
| 로컬 Caddy 설정 | `frontend/deploy/Caddyfile.local` | 실제 주소를 넣는 Git 제외 파일 |
| 운영 Caddy 설정 | `/etc/caddy/Caddyfile` | Caddy 서비스가 읽는 파일 |

프론트 코드는 `/api/v1` 상대 경로를 사용한다. 브라우저에 백엔드 Tailnet 주소를 노출하거나
별도 `VITE_API_URL`을 설정하지 않는다.

## 최초 Caddy 설정

저장소 루트에서 예제 파일을 복사한다. `*.local`은 저장소의 `.gitignore`에 포함되어 있다.

```bash
cp frontend/deploy/Caddyfile.example frontend/deploy/Caddyfile.local
```

`frontend/deploy/Caddyfile.local`의 다음 세 값을 실제 환경에 맞게 바꾼다.

```text
app.example.com      -> 실제 공개 도메인
100.64.0.10:8001     -> 실제 Tailnet 백엔드 주소
/srv/app             -> 실제 정적 파일 경로
```

설정을 검증하고 운영 경로에 설치한다.

```bash
caddy validate --config frontend/deploy/Caddyfile.local --adapter caddyfile
sudo install -o root -g root -m 644 \
  frontend/deploy/Caddyfile.local \
  /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
```

## 평상시 프론트 재배포

모든 저장소 명령은 저장소 루트에서 시작한다.

### 1. 원격 `main` 반영

```bash
git -c core.fileMode=false status --short
git fetch origin main
git -c core.fileMode=false merge --ff-only FETCH_HEAD
git log -1 --oneline
```

`core.fileMode=false`는 일부 서버에서 발생하는 `644 -> 755` 파일 모드 차이를 무시하기 위한
명령이다. 내용이 수정된 파일이나 알 수 없는 untracked 파일이 있으면 임의로 reset하지 말고
먼저 변경 담당자에게 확인한다.

### 2. 프로덕션 빌드

```bash
cd frontend
npm ci
npm run build
cd ..
```

성공하면 `frontend/dist/`에 `index.html`과 해시가 붙은 JS/CSS 번들이 생성된다.
Vite의 `configLoader: native` 관련 경고는 현재 빌드를 실패시키지 않는 경고다.

### 3. 기존 배포 백업 및 새 빌드 반영

아래 `STATIC_ROOT`를 Caddyfile에 설정한 실제 정적 파일 경로로 바꾼다.

```bash
STATIC_ROOT=/srv/app
sudo mkdir -p "$STATIC_ROOT" "${STATIC_ROOT}.previous"
sudo rsync -a --delete "$STATIC_ROOT/" "${STATIC_ROOT}.previous/"
sudo rsync -a --delete frontend/dist/ "$STATIC_ROOT/"
sudo chmod -R a+rX "$STATIC_ROOT"
```

정적 파일만 교체할 때는 Caddy를 reload하거나 restart할 필요가 없다.

### 4. 배포 확인

`APP_DOMAIN`을 실제 공개 도메인으로 바꾼다.

```bash
APP_DOMAIN=app.example.com
sudo systemctl is-active caddy
curl -I "http://${APP_DOMAIN}"
curl -I "https://${APP_DOMAIN}"
curl -i "https://${APP_DOMAIN}/api/v1/auth/me"
```

정상 기준은 다음과 같다.

- HTTP는 HTTPS로 `301` 또는 `308` redirect된다.
- HTTPS 메인 페이지는 `200`을 반환한다.
- 로그인 전 `/api/v1/auth/me`의 `401`은 정상이다. `502`는 정상이 아니다.

브라우저에 이전 화면이 남으면 `Ctrl+Shift+R` 또는 `Cmd+Shift+R`로 강력 새로고침한다.

## Caddy 설정 변경

`/etc/caddy/Caddyfile`을 직접 수정하지 말고 Git에서 제외된
`frontend/deploy/Caddyfile.local`을 수정한다. 문법 검증 후 운영 파일로 설치한다.

```bash
caddy validate --config frontend/deploy/Caddyfile.local --adapter caddyfile
sudo install -o root -g root -m 644 \
  frontend/deploy/Caddyfile.local \
  /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
```

운영 설정과 로컬 설정이 같은지 확인한다.

```bash
cmp frontend/deploy/Caddyfile.local /etc/caddy/Caddyfile
```

출력이 없고 종료 코드가 `0`이면 두 파일이 같다.

## 직전 배포로 롤백

`STATIC_ROOT`를 실제 정적 파일 경로로 설정한 뒤 한 세대 백업으로 되돌린다.

```bash
STATIC_ROOT=/srv/app
sudo rsync -a --delete "${STATIC_ROOT}.previous/" "$STATIC_ROOT/"
```

정적 파일 롤백에도 Caddy reload는 필요하지 않다. 롤백 후 브라우저에서 강력 새로고침하고
메인 페이지와 로그인 화면을 확인한다.

## 장애 확인

### Caddy 상태와 로그

```bash
sudo systemctl status caddy --no-pager
sudo journalctl -u caddy -n 100 --no-pager
```

### API가 `502 Bad Gateway`인 경우

`BACKEND_TAILNET_IP`를 실제 백엔드 Tailnet IP로 바꾼 뒤 프론트 서버에서 확인한다.

```bash
BACKEND_TAILNET_IP=100.64.0.10
tailscale status
tailscale ping "$BACKEND_TAILNET_IP"
curl -fsS "http://${BACKEND_TAILNET_IP}:8001/health/live"
curl -fsS "http://${BACKEND_TAILNET_IP}:8001/health/ready"
```

백엔드 health가 실패하면 프론트 빌드나 Caddy 정적 파일 문제가 아니다. 백엔드 API bind,
Tailscale ACL, 백엔드 컨테이너 상태를 확인한다.

### HTTPS 인증서 문제가 있는 경우

- 공개 DNS 레코드가 프론트 서버의 공개 IP를 가리키는지 확인한다.
- 서버 방화벽과 보안 그룹에서 TCP 80/443을 허용한다.
- 인증서와 Caddy 상태 데이터가 있는 `/var/lib/caddy`를 삭제하지 않는다.

## 주의사항

- 프론트 서버에는 백엔드 `.env`, 외부 API 키, 데이터베이스 관리자 키를 복사하지 않는다.
- 외부에는 80/443만 공개하고 백엔드, DB, Caddy 관리 포트를 공개하지 않는다.
- `frontend/dist`는 Git에 커밋하지 않는다. 배포할 때마다 `npm run build`로 생성한다.
- `frontend/public/html`의 디자인 참고 파일은 빌드에 포함되지만 Caddy가 `/html/*` 요청을
  `404`로 차단한다.
- 운영 Caddy는 `caddy` 전용 계정으로 실행한다. 일반적인 설정 변경은 `reload`를 사용하고
  불필요하게 서비스를 중지하지 않는다.
