# 🏛️ Pre-review

## 🚀시작하기

프로젝트를 로컬 환경에서 실행하는 방법입니다. Node.js 설치 시 `npm`이 기본 포함되므로 별도의 추가 패키지 매니저는 필요하지 않습니다.

### 1. 사전 요구 사항

![Node.js](https://shields.io/badge/node.js%2024.18.0-5FA04E?style=for-the-badge&logo=nodedotjs&logoColor=white)

### 2. 패키지 설치

frontend 디렉토리(`frontend/`)에서 아래 명령어를 실행하여 의존성을 설치합니다.

```bash
npm ci
```

`package-lock.json`을 갱신하려는 경우에만 `npm install`을 사용합니다.

### 3. 개발 서버 실행

설치가 완료되면 아래 명령어로 로컬 개발 서버를 실행합니다.

```bash
npm run dev
```

개발 서버 주소는 `vite.config.ts`에 고정된 `http://localhost:3000`입니다.

## 백엔드 API 연동 기준

현재 화면에는 로그인 상태 고정, 업로드 타이머 등 mock 동작이 남아 있습니다. 실제 API
service 연결은 아래 계약을 기준으로 진행합니다.

- FastAPI base URL: `http://localhost:8001/api/v1`
- Swagger UI: `http://localhost:8001/docs`
- OpenAPI JSON: `http://localhost:8001/openapi.json`
- 인증: Bearer token이 아니라 FastAPI가 발급하는 HttpOnly Cookie
- 모든 브라우저 요청: Fetch의 `credentials: "include"` 또는 Axios의
  `withCredentials: true`
- 업로드: HWP/HWPX만 허용, 최대 50 MiB

같은 개발 PC에서 `localhost:3000`과 `localhost:8001`을 쓰는 방식이 기본이다. 다른
PC의 LAN IP에 있는 shared backend를 평문 HTTP로 직접 호출하면 CORS가 허용되어도
SameSite Cookie가 전송되지 않을 수 있다. 이 경우 Vite의 `/api` 개발 proxy를 사용하거나
HTTPS 환경으로 연결한다. 현재 `vite.config.ts`에는 proxy가 없으므로 shared-backend 방식이
필요하면 프론트 연동 작업에서 추가한다.

로그인 → 업로드 → 상태 polling → 결과 조회 순서와 오류 계약은
[프론트엔드 FastAPI API 명세](../backend/fastapi/docs/0.FASTAPI_FRONTEND_API_SPEC.md)를
따릅니다. DB·Supabase·backend까지 함께 실행하는 방법과 별도 데이터 준비 조건은
[저장소 실행 안내](../README.md)를 확인합니다.

프론트 API base 환경변수와 실제 Axios/fetch service는 아직 구현되지 않았습니다. 이를
추가할 때 `VITE_API_BASE_URL` 같은 한 가지 이름으로 통일하고, Supabase URL/key나
PostgreSQL/OpenAI 자격증명을 프론트 환경 파일에 넣지 않습니다.

### 4. 프로덕션 빌드

배포를 위한 빌드를 생성하려면 아래 명령어를 입력합니다.

```bash
npm run build
```

## 📦 기술 스택

- Core:
<img src="https://shields.io/badge/react%2019.2-61DAFB?style=for-the-badge&logo=react&logoColor=black" alt="React" style="vertical-align: middle; display: inline-block;" />

- Routing: 
<img src="https://shields.io/badge/react%20router%207.18-CA4245?style=for-the-badge&logo=reactrouter&logoColor=white" alt="React Router" style="vertical-align: middle; display: inline-block;" />

- Styling:
<img src="https://shields.io/badge/css-663399?style=for-the-badge&logo=css&logoColor=white" alt="CSS" style="vertical-align: middle; display: inline-block;" />
<img src="https://shields.io/badge/tailwind%20css%204.3-06B6D4?style=for-the-badge&logo=tailwindcss&logoColor=white" alt="Tailwind CSS" style="vertical-align: middle; display: inline-block;" />

- Network:
<img src="https://shields.io/badge/Axios%201.19-5A29E4?style=for-the-badge&logo=axios&logoColor=white" alt="Axios" style="vertical-align: middle; display: inline-block;" />

- Build Tool:
<img src="https://shields.io/badge/vite%208.2-9135FF?style=for-the-badge&logo=vite&logoColor=white" alt="Vite" style="vertical-align: middle; display: inline-block;" />

- Language:
<img src="https://shields.io/badge/typescript%206.0-3178C6?style=for-the-badge&logo=typescript&logoColor=white" alt="TypeScript" style="vertical-align: middle; display: inline-block;" />

## 📁 폴더 구조

```text
frontend/
├── public/                # 정적 에셋 (이미지 등)
├── src/
│   ├── assets/            # 스타일 및 이미지 에셋
│   ├── components/        # 공통 UI 컴포넌트
│   │   ├── common/        # 재사용 공통 컴포넌트
│   │   └── layout/        # 레이아웃 관련 컴포넌트
│   │       └── shared/    # 공유 레이아웃 서브 컴포넌트
│   ├── features/          # 기능별 모듈 (랜딩, 인증, 분석 등)
│   ├── pages/             # 페이지 단위 컴포넌트
│   ├── App.tsx            # 현재 라우팅과 mock 인증 상태
│   ├── index.css          # 글로벌 스타일 및 테마 정의
│   └── main.tsx           # 애플리케이션 진입점
├── eslint.config.js       # ESLint 설정
├── index.html             # HTML 템플릿
├── package.json           # 패키지 및 의존성 관리
├── tsconfig.json          # TypeScript 설정
└── vite.config.ts         # Vite 빌드 설정

```

`hooks/`, `routes/`, `services/`는 아직 생성되지 않았다. 실제 API 연동 시 기능별
service와 공통 Cookie 포함 HTTP client를 추가한다.
