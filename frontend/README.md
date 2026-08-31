# 🏛️ Pre-review

## 🚀시작하기

프로젝트를 로컬 환경에서 실행하는 방법입니다. Node.js 설치 시 `npm`이 기본 포함되므로 별도의 추가 패키지 매니저는 필요하지 않습니다.

### 1. 사전 요구 사항

![Node.js](https://shields.io/badge/node.js%2024.18.0-5FA04E?style=for-the-badge&logo=nodedotjs&logoColor=white)

### 2. 패키지 설치

frontend 디렉토리(`frontend/`)에서 아래 명령어를 실행하여 의존성을 설치합니다.

```bash
npm install
```

### 3. 개발 서버 실행

설치가 완료되면 아래 명령어로 로컬 개발 서버를 실행합니다.

```bash
npm run dev
```

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
│   ├── hooks/             # 커스텀 훅
│   ├── pages/             # 페이지 단위 컴포넌트
│   ├── routes/            # 라우팅 설정 (AppRouter.tsx)
│   ├── services/          # API 통신 및 비즈니스 로직
│   ├── index.css          # 글로벌 스타일 및 테마 정의
│   └── main.tsx           # 애플리케이션 진입점
├── eslint.config.js       # ESLint 설정
├── index.html             # HTML 템플릿
├── package.json           # 패키지 및 의존성 관리
├── tsconfig.json          # TypeScript 설정
└── vite.config.ts         # Vite 빌드 설정

```