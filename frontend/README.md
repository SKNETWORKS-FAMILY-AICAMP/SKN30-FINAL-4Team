<img src="./public/images/pre-review.png" alt="Pre-review Logo" width="200" />

<br><br>

## 🚀 시작하기

프로젝트를 로컬 환경에서 실행하는 방법입니다. `Node.js` 설치 시 `npm`이 기본 포함되므로 별도의 추가 패키지 매니저는 필요하지 않습니다.

<br>

### 1. 사전 요구 사항

![Node.js](https://shields.io/badge/node.js%2024.18.0-5FA04E?style=for-the-badge&logo=nodedotjs&logoColor=white)

<br>

### 2. 패키지 설치

frontend 디렉토리(`/frontend`)에서 아래 명령어를 실행하여 의존성을 설치합니다.

```bash
npm install
```

<br>

### 3. 개발 서버 실행

설치가 완료되면 아래 명령어로 로컬 개발 서버를 실행합니다.

```bash
npm run dev
```

> 💡 API 통신 및 프록시 안내
별도의 VITE_API_BASE_URL 환경 변수 설정 없이, Vite 개발 서버의 프록시 설정(vite.config.ts)을 통해 /api/v1 요청이 백엔드 서버(http://localhost:8001)로 자동 라우팅됩니다.

<br>

### 4. 프로덕션 빌드

배포를 위한 빌드를 생성하려면 아래 명령어를 입력합니다.

```bash
npm run build
```

운영 프론트 서버의 Caddy 설정, 정적 파일 반영, 검증 및 롤백 절차는
[배포 가이드](deploy/README.md)를 참고합니다.

<br><br>

## 🛠️ 기술 스택

![React](https://shields.io/badge/react%2019.2-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![React Router](https://shields.io/badge/react%20router%207.18-CA4245?style=for-the-badge&logo=reactrouter&logoColor=white)
![Tailwind CSS](https://shields.io/badge/tailwind%20css%204.3-06B6D4?style=for-the-badge&logo=tailwindcss&logoColor=white)
![Vite](https://shields.io/badge/vite%208.2-9135FF?style=for-the-badge&logo=vite&logoColor=white)
![TypeScript](https://shields.io/badge/typescript%206.0-3178C6?style=for-the-badge&logo=typescript&logoColor=white)

<br><br>

## 📁 폴더 구조

```text
frontend/
├── public/                # 정적 에셋 (이미지, html 등)
├── src/
│   ├── components/        # 공통 UI 컴포넌트
│   │   ├── common/
│   │   └── layout/
│   ├── features/          # 기능별 모듈
│   │   ├── auth/          # 인증
│   │   ├── chat/          # AI 질의응답
│   │   ├── history/       # 과거 분석 - 화면 표시는 최근 분석 이력
│   │   ├── landing/       # 랜딩 페이지
│   │   ├── my/            # 마이페이지
│   │   ├── result/        # 분석 결과
│   │   └── upload/        # 업로드 및 분석 대기
│   ├── hooks/             # 커스텀 훅
│   ├── pages/             # 라우터 매핑용 페이지 단위 컴포넌트
│   ├── providers/         # 전역 상태 관리
│   ├── routes/            # 라우팅 설정
│   ├── services/          # API 통신 및 에러 핸들링 모듈
│   ├── utils/             # 유틸리티 함수
│   ├── App.tsx            # 루트 컴포넌트
│   ├── index.css          # 글로벌 스타일 및 테마 정의
│   └── main.tsx           # 애플리케이션 진입점
├── eslint.config.js       # ESLint 설정
├── index.html             # HTML 템플릿
├── package.json           # 패키지 및 의존성 관리
├── tsconfig.json          # TypeScript 설정
└── vite.config.ts         # Vite 빌드 및 프록시 설정
```
