# 인증 API 명세 위치 안내

이 문서는 더 이상 독립 계약이 아니다. 프론트엔드가 참조할 유일한 사람용 API 명세는
[프론트엔드 FastAPI API 명세서](fastapi/docs/0.FASTAPI_FRONTEND_API_SPEC.md)다.

인증 endpoint의 exact request/response 구조, Cookie security scheme과 상태 코드는 실행 중인
FastAPI의 `/openapi.json` 또는 `/docs`에서 확인한다. 특히 `POST /api/v1/auth/sign-in`과
`GET /api/v1/auth/me`의 `user`에는 non-null `display_name`이 포함된다.

이 파일의 과거 상세 내용은 중복·불일치 방지를 위해 폐기했으며 프론트 구현 근거로 사용하지
않는다.
