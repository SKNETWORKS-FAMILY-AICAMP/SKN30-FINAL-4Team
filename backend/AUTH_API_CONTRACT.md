# FastAPI 인증 API 명세

## 1. 구조와 책임

프론트엔드는 Supabase Auth를 직접 호출하지 않는다.

```text
Frontend → FastAPI /api/v1/auth/* → Supabase Auth
                                     ↓
                              access / refresh token
                                     ↓
                         FastAPI가 HttpOnly Cookie 설정
```

- Supabase Auth: 사용자, 비밀번호, 세션, access token, refresh token 관리
- FastAPI: Supabase Auth 호출을 프론트엔드용 API로 래핑하고, 업무 API 요청을 인증
- Frontend: FastAPI 인증 API만 호출하며 토큰을 직접 저장하거나 읽지 않음

FastAPI는 자체 JWT를 발급하거나 자체 비밀번호를 저장·검증하지 않는다.

## 2. API 목록

```text
POST /api/v1/auth/sign-up
POST /api/v1/auth/sign-in
POST /api/v1/auth/sign-out
POST /api/v1/auth/refresh
POST /api/v1/auth/password-reset
POST /api/v1/auth/update-password
GET  /api/v1/auth/me
```

## 3. API 상세

### `POST /api/v1/auth/sign-in`

요청:

```json
{
  "email": "user@example.com",
  "password": "password"
}
```

FastAPI는 Supabase Auth 비밀번호 로그인 API를 호출한다.

```text
POST {SUPABASE_URL}/auth/v1/token?grant_type=password
```

성공하면 Supabase가 발급한 access token과 refresh token을 HttpOnly Cookie로 설정한다.
응답 본문에는 토큰을 포함하지 않는다.

```json
{
  "user": {
    "id": "Supabase user UUID",
    "email": "user@example.com"
  }
}
```

### `GET /api/v1/auth/me`

FastAPI는 `pre_review_access` Cookie의 access token을 검증한다.

```text
GET {SUPABASE_URL}/auth/v1/user
apikey: {SUPABASE_ANON_KEY}
Authorization: Bearer <access token>
```

성공 응답:

```json
{
  "user": {
    "id": "Supabase user UUID",
    "email": "user@example.com"
  }
}
```

### `POST /api/v1/auth/refresh`

`pre_review_refresh` Cookie를 사용해 Supabase Auth에 session refresh를 요청한다.
성공하면 새 access token과 refresh token Cookie를 다시 설정한다.

### `POST /api/v1/auth/sign-out`

Supabase Auth 세션을 종료하고 `pre_review_access`, `pre_review_refresh` Cookie를 삭제한다.

### `POST /api/v1/auth/sign-up`

Supabase Auth 가입 API를 호출한다. 이메일 확인이 활성화된 경우에는 이메일 확인 안내를 반환한다.

### `POST /api/v1/auth/password-reset`

Supabase Auth 비밀번호 재설정 메일 API를 호출한다. 계정 존재 여부를 드러내지 않기 위해
항상 같은 성공 메시지를 반환한다.

현재 FastAPI 공개 API에는 재설정 메일의 token/PKCE를 검증하여 Cookie 세션으로 교환하는
callback endpoint가 **없다**. 따라서 이 endpoint만으로 비로그인 사용자의 비밀번호 재설정
완료 화면을 구현할 수는 없다. `PREREVIEW_AUTH_PASSWORD_RESET_REDIRECT_TO`는 Supabase에
전달할 redirect URL 설정일 뿐이며, 그 URL을 처리할 frontend/backend callback 구현을 뜻하지
않는다.

### `POST /api/v1/auth/update-password`

로그인된 사용자의 새 비밀번호를 Supabase Auth에 반영한다. 반드시 유효한
`pre_review_access` HttpOnly Cookie가 있어야 하며, 없거나 만료되면 `401`이다. 위의
password-reset 메일로 시작한 비로그인 재설정 완료에는 현재 사용할 수 없다.

## 4. Cookie 규칙

FastAPI가 설정하는 Cookie 이름은 다음과 같다.

```text
pre_review_access
pre_review_refresh
```

속성:

```text
HttpOnly=true
SameSite=Lax
Path=/
Secure=true   # 운영 HTTPS 환경
```

개발 환경이 HTTP라면 `Secure=false`를 허용할 수 있다. 운영 환경에서는 HTTPS와 `Secure=true`가 필수다.

### 배포 환경 설정

```text
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_ANON_KEY=<publishable-or-anon-key>  # SUPABASE_KEY도 호환용으로 읽음
PREREVIEW_AUTH_ALLOWED_ORIGINS=https://app.example.com
PREREVIEW_AUTH_COOKIE_SECURE=true
PREREVIEW_AUTH_COOKIE_SAMESITE=lax           # lax | strict | none
PREREVIEW_AUTH_COOKIE_DOMAIN=                # host-only cookie 권장
PREREVIEW_AUTH_REFRESH_COOKIE_MAX_AGE=2592000
PREREVIEW_AUTH_PASSWORD_RESET_REDIRECT_TO=https://app.example.com/reset-password
```

`PREREVIEW_AUTH_ALLOWED_ORIGINS`는 쉼표로 구분한 정확한 origin 목록이다. 모든
`POST /auth/*` 요청은 `Origin`이 이 목록에 있어야 하며, 미설정·누락 origin은
403으로 거부된다. `SameSite=none`은 `Secure=true`일 때만 허용된다.

## 5. 업무 API 인증

업무 API는 `Authorization: Bearer ...` 헤더를 요구하지 않는다. 브라우저가 Cookie를 자동 전송하고, FastAPI가 `pre_review_access`를 검증한다.

OpenAPI에는 `PreReviewAccessCookie`, `PreReviewRefreshCookie` API-key cookie security scheme이
표시된다. 이는 Swagger/client generator를 위한 Cookie 전달 계약이며, frontend는 토큰 값을
읽거나 `Authorize` 창에 복사하지 않는다. `fetch`/axios 요청에는 반드시
`credentials: "include"`를 사용한다. offline 개발 모드의 `X-PreReview-Dev-User` header는
테스트 전용 호환 경로이고 운영 브라우저 계약이 아니므로 Swagger에도 표시하지 않는다.
이 헤더는 offline 모드에서 임의 사용자·역할을 가장할 수 있으므로 LAN이나 공유 서버에서는
반드시 `PREREVIEW_OFFLINE_MODE=false`로 실행한다. Swagger에서 숨기는 것 자체는 보안
경계가 아니다.

검증된 Supabase Auth의 `user.id`를 현재 사용자 UUID로 사용한다.

```text
current_user_id = Supabase user.id = JWT sub
```

클라이언트가 `user_id`, 권한, 역할을 요청 본문이나 query parameter로 보내도 신뢰하지 않는다.

## 6. 금지 사항

- FastAPI 자체 JWT 발급 금지
- FastAPI 자체 비밀번호 저장·검증 금지
- 프론트엔드에 service-role key 또는 DB 비밀번호 전달 금지
- worker에 사용자 Cookie 또는 사용자 access token 전달 금지
