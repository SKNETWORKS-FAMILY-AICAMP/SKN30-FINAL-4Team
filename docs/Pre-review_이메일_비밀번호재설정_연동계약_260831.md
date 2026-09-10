# 이메일 비밀번호 재설정 — 프론트 연동 계약

> **[2026-09-03] 이 문서는 대체됐다. 아래 본문은 2026-08-31 시점의 기록이다.**
>
> 두 가지가 바뀌었다.
>
> 1. **재설정 방식.** 링크와 확인 API 를 없애고 **임시 비밀번호를 메일로 보낸다.**
>    사용자는 그 값으로 그냥 로그인한다. 프론트가 만들 재설정 화면은 없다.
>    `POST /auth/password-reset/confirm` 과 `PASSWORD_RESET_URL` 설정은 삭제했다.
> 2. **응답 형식.** 아래에 나오는 `status_code`·`code`·`data`·`errors` 공통 래퍼는
>    없어졌다. 지금은 `{"message": "..."}` 하나이며 상태 코드가 상황을 구분한다.
>
> 현행 계약은 `SIMS_API_프론트정합화_초안.md` v0.9 의 AUTH-04 와 2.7 절이다.
> 아래 본문에서 지금도 유효한 것은 **비밀번호 규칙**(8~128자, ASCII 영문자·숫자·
> 특수문자 각 1개 이상)과 **요청 제한**(IP·이메일당 15분에 5회, `Retry-After: 900`)
> 뿐이다.

2026-08-31 사용자 승인: `이동욱` 브랜치에서 이메일 재설정을 추가하며, 새 비밀번호 규칙을 화면설계서와 맞춘다. 기존 알파의 이메일 재설정 제외 결정을 이 기능에 한해 대체한다. 기존 공통응답 전달안 전체를 서버에 적용하는 작업은 아니다.

## 화면 흐름

1. 로그인 화면의 비밀번호 찾기 → 이메일 입력 → 발송 요청 API.
2. 등록된 이메일에 도착한 링크 → 프론트 새 비밀번호 화면.
3. 새 비밀번호·확인 입력. 프론트에서 일치 여부 확인 후 재설정 API.
4. 성공하면 보관한 로그인 토큰을 삭제하고 로그인 화면으로 이동. 자동 로그인하지 않는다.

프론트의 새 비밀번호 규칙: **8~128자, ASCII 영문자·숫자·특수문자 각각 한 개 이상**. 특수문자는 ASCII punctuation (`!`부터 `~` 사이의 기호)이며 공백이나 한글 자체를 특수문자로 세지 않는다. 비밀번호를 trim하거나 대소문자를 변경하지 않는다. 서버도 같은 검증을 수행한다. 기존 비밀번호 로그인에는 새 규칙을 적용하지 않는다.

## 1. 재설정 링크 발송 요청

`POST /api/v1/auth/password-reset/request` — 로그인 불필요

```json
{"email":"tester@example.com"}
```

성공 HTTP 200. 공통 필드는 `status_code`, `code`, `message`, `data`, `errors`다. `code`는 `SUCCESS`, `data`는 `null`, `errors`는 빈 배열이다.

```json
{
  "status_code": 200,
  "code": "SUCCESS",
  "message": "등록된 이메일이라면 비밀번호 재설정 안내가 발송됩니다.",
  "data": null,
  "errors": []
}
```

등록된 계정과 미등록 계정의 응답은 동일하다. 200은 요청 수락이며 이메일 도착 보장이 아니다. 실제 등록된 활성 계정에만 발송한다. 화면설계서의 "등록되지 않은 이메일" 안내는 계정 존재 여부 노출을 막기 위해 공통 안내로 대체한다.

메일 발송은 응답 뒤 백그라운드에서 처리한다. 메일 서비스의 지연·거부·스팸 분류로 도착하지 않을 수 있다. 반복 클릭을 막고 제한 시간이 지난 뒤 다시 요청할 수 있게 한다.

## 2. 새 비밀번호 확정

`POST /api/v1/auth/password-reset/confirm` — 로그인 불필요

```json
{"token":"메일 링크에서 읽은 토큰","new_password":"New-password1!"}
```

성공 HTTP 200, `code: SUCCESS`, `data: null`, `errors: []`. 확인 비밀번호는 화면 입력이며 API에 보내지 않는다.

링크 유효기간은 **10분**이다. 성공한 링크는 재사용할 수 없다. 같은 계정의 다른 재설정 링크와 이전 로그인 토큰도 비밀번호 변경 후 무효화된다. 현재 비밀번호와 같은 값으로는 재설정할 수 없다. 새 링크를 발급받았다는 이유만으로 이전 링크가 즉시 취소되는 것은 아니며, 비밀번호 변경 또는 만료 시 취소된다.

이메일 변경·계정 비활성화 등으로 링크가 유효하지 않으면 새 발송을 요청한다. GET으로 링크를 여는 것만으로 비밀번호가 변경되거나 링크가 소모되지는 않는다.

## 오류 처리

| HTTP | code | 프론트 행동 |
|---|---|---|
| 400 | BAD_REQUEST | 링크 만료·변조·사용 완료 또는 현재 비밀번호 재사용. message 표시 |
| 422 | VALIDATION_ERROR | 이메일·필수 입력·비밀번호 규칙 확인. errors의 loc/msg 표시 |
| 429 | TOO_MANY_REQUESTS | 반복 요청 중단. Retry-After(초) 이후 다시 요청 |
| 503 | SERVICE_UNAVAILABLE | 메일 설정 등 서비스 준비 상태 확인 |

입력 오류에도 공통 5필드를 제공한다. 비밀번호·토큰 원문을 오류 응답에 포함하지 않는다. 프론트는 message 문자열로 로직을 분기하지 않는다. 네트워크 오류는 별도 처리한다. 기존 API 전체의 미처리 500 응답까지 공통 JSON으로 바꾸지는 않는다.

브라우저에서 제한 시간을 읽을 수 있도록 CORS 노출 헤더에 기존 `Content-Disposition`과 함께 `Retry-After`를 추가한다.

발송 요청은 **IP당·이메일당 각각 15분에 5회**, 확정 요청은 **IP당 15분에 10회**까지다. 제한 시 `Retry-After: 900`을 반환한다. 같은 공유 IP를 사용하는 팀원들은 IP 제한을 함께 사용하므로 테스트 시 연속 클릭을 피한다.

## 프론트 URL·토큰 취급

`PASSWORD_RESET_URL`은 프론트의 실제 재설정 화면 URL이다. 프론트 라우트는 이번 백엔드 작업에서 생성하지 않는다. 메일 링크는 `https://프론트주소/reset-password#token=...` 형태다.

```javascript
const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
history.replaceState(null, "", window.location.pathname + window.location.search);
// token은 화면 메모리에만 보관하고 confirm 요청 JSON에 넣는다.
```

fragment는 웹 서버 요청 URL에 전달되지 않는다. 토큰을 localStorage, 분석 로그, 오류 수집 도구에 남기지 않는다. 화면에서 토큰을 메모리로 옮긴 뒤 주소에서 제거한다. 이 상태에서 새로고침하면 메일 링크를 다시 열어야 한다. 재설정 화면은 외부 스크립트를 최소화하고 Referrer-Policy: no-referrer를 적용한다. hash 기반 라우터라면 이 URL 형식과 충돌하므로 프론트 라우팅 합의가 필요하다.

## Gmail 테스트 설정

실제 로컬 `.env`에만 입력한다. 비밀번호나 앱 비밀번호를 Git 또는 채팅에 올리지 않는다.

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USERNAME=팀테스트용구글계정@gmail.com
SMTP_PASSWORD=구글에서발급한앱비밀번호
SMTP_FROM_EMAIL=팀테스트용구글계정@gmail.com
PASSWORD_RESET_URL=http://localhost:3000/reset-password
```

Gmail 2단계 인증을 설정한 뒤 앱 비밀번호를 사용한다. 일반 계정 로그인 비밀번호를 넣지 않는다. 계정 종류·보안 정책에 따라 앱 비밀번호를 사용할 수 없을 수 있다. TLS를 검증하는 SMTP SSL 연결을 사용하며, 포트 587 STARTTLS는 이번 구현 범위가 아니다. [Google 앱 비밀번호 안내](https://support.google.com/accounts/answer/185833?hl=ko).

팀원에게 보낼 때 `localhost`는 **메일을 여는 사람의 PC**를 뜻한다. 공유 테스트에서는 실제 접근 가능한 HTTPS 프론트 주소로 바꾸고 `CORS_ALLOWED_ORIGINS`에도 해당 origin을 추가한다. 설정 변경 후 백엔드를 재시작한다. 등록된 사용자 행의 `email`이 실제 수신 주소인지 확인한다. 계정 생성·이메일 수정 API는 이번에 추가하지 않는다.

## 기존 기능·운영 경계

- 신규 2개 추가로 API는 헬스체크 포함 기존 14개에서 16개가 된다.
- 기존 `/change-password`는 새 비밀번호 검증만 변경하며 현재 204 빈 응답을 유지한다. 현재 비밀번호 입력과 성공 후 재로그인이 필요하다. 기존 로그인·로그아웃·분석 응답 형식은 유지한다.
- DB 스키마·migration 추가 없음. 기존 비밀번호 변경 트리거와 이력 테이블을 그대로 사용한다. 재설정 토큰은 서명·만료 및 현재 사용자 상태를 검증한다.
- 발송·확정 남용 제한은 프로세스 메모리에서 동작한다. 팀 내부 단일 worker 테스트 범위이며, 여러 worker/서버 또는 재시작을 넘는 제한은 공유 저장소나 게이트웨이 제한이 필요하다. 프록시 배포 시 클라이언트 IP 전달 설정도 점검한다.
- 백그라운드 이메일 작업은 영속 큐가 아니다. 서버 재시작으로 발송이 유실되면 다시 요청한다. SMTP 실패는 서버 로그에서 비밀정보 없이 확인한다.
- 실제 Gmail 발송과 실제 프론트 화면 E2E는 메일 계정 설정·프론트 화면 연결 후 별도 확인이 필요하다.

보안 흐름 참고: [OWASP Forgot Password Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html).

## 구현·검증 결과

- 구현 파일: `api/v1/password_reset.py`, `services/password_reset.py`, `core/password_policy.py`, `ports/mail_sender.py`, `infrastructure/smtp_mail_sender.py`. 기존 `api/v1/auth.py`, `services/auth.py`에 공통 비밀번호 검증을 연결했다.
- 통합 파일: `core/config.py`, `main.py`, `api/router.py`, `.env.example`, `README.md`. CORS 테스트는 추가 노출 헤더에 맞췄다. 기존 CPL·분석 작업을 수정하지 않았다.
- 실제 PostgreSQL 15.19 / pgvector 0.8.6의 별도 DB `sims_password_reset_260831`에 현재 스키마를 적용해 검증한 뒤 해당 테스트 DB만 삭제했다. 기존 서비스 DB와 외부 기준 문서는 변경하지 않았다.
- 최종 전체 회귀: **294 passed**, 26.49초. 기존 Starlette/httpx deprecation 경고 1건. `git diff --check` 통과.
- 검증 내용: 이메일 발송→재설정→새 비밀번호 로그인, 이전 로그인 토큰 거부, 링크 만료·변조·재사용·동시 사용, 비활성화·이메일 변경, 동일 비밀번호 거부와 이력 원자성, IP·이메일 요청 제한과 저장 한도, SSL 검증, 민감 입력 비노출, OpenAPI 오류 형식과 전체 16개 API.
- 실제 SMTP 통신은 수행하지 않았다. SMTP 어댑터는 가짜 연결로 인증·인증서 검증 설정과 메시지를 검사했다. LLM·Rule·분석 판정 변경 없음. DB migration·신규 패키지 없음. 커밋·푸시·실행 서버 재시작 없음.

재현 명령(별도 테스트 DB 생성 및 `backend/app/db/schema.sql` 적용 후, 작업 디렉터리 `backend`):

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://postgres:simstest@127.0.0.1:55533/sims_password_reset_260831'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
$env:JWT_SECRET='test-secret-that-is-at-least-32-bytes'
$env:OPENAI_API_KEY=''
$env:BIZINFO_API_KEY=''
$env:SMTP_HOST=''
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider --tb=short
```

다음 단계는 이 계약으로 프론트 재설정 화면 두 곳을 연결하고, 로컬 환경에 Gmail 앱 비밀번호·프론트 URL을 설정한 뒤 실제 수신부터 재로그인까지 확인하는 것이다.
