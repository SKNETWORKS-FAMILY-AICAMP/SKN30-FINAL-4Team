# Original User Request

## 2026-09-09T10:00:28Z

Full team (System Architect, Pipeline Engineer, Diagram/Visual Specialist)

EC2 단일 인스턴스 환경에서 구동되는 Self-hosted Supabase(PostgreSQL 16, pgvector, Auth, Storage, Realtime)와 FastAPI 백엔드(CPL·FIT·ML·SIM 파이프라인, 비동기 워커, PDF 렌더러) 간의 정확한 데이터 흐름과 역할을, 화살표 꼬임 없이 한눈에 들어오는 극도로 단순하고 집약적인 다이어그램(Mermaid 및 시각화 명세)으로 정립합니다.

Working directory: c:\Users\playdata2\OneDrive\Desktop\프로젝트\Final Project\SKN30-FINAL-4Team
Integrity mode: development

## Requirements

### R1. 데이터 흐름의 기술적 정합성 100% 일치
- 클라이언트(React) ➔ Supabase Auth(GoTrue 로그인/JWT) ➔ Storage(HWP 업로드) ➔ DB 큐(analysis_run) ➔ AnalysisWorker(선점) ➔ AI 파이프라인(CPL·FIT·ML·SIM-R) ➔ pgvector 검색 ➔ PDF 렌더 ➔ DB 영구 적재(result.*) ➔ Realtime 웹소켓 알림 ➔ FastAPI 챗봇으로 이어지는 실제 코드 라이프사이클과 화살표 방향 및 주체를 100% 일치시킵니다.
- 허구의 직접 연결(예: Storage가 DB 큐를 직접 등록하거나, PDF 엔진이 DB에 직접 저장하는 등)을 일체 배제합니다.

### R2. 극단적 단순화 및 집약적 시각화 (Compact & Cute Aesthetic)
- 거대하고 복잡하게 흩어진 노드들을 하나의 컴팩트한 EC2 박스 안에 직관적으로 응축합니다.
- 네모난 컨테이너([ ]), 알약형 프로세스(([ ])), 원형 에이전트((( ))), 원통형 DB([( )])의 시각적 위계를 명확히 분리하여 난잡함을 없앱니다.
- 화살표 교차(Cross-over)를 0건으로 최적화하여 3초 만에 시스템 전체 구조를 이해할 수 있도록 설계합니다.

### R3. 아키텍처 문서 및 다이어그램 산출물 반영
- 확정된 최종 다이어그램과 흐름 설명을 docs/03_architecture/EC2_ALL_IN_ONE_ARCHITECTURE.md 및 프로젝트 루트의 관련 문서에 최신화하여 팀 전체가 공유할 수 있도록 반영합니다.

## Acceptance Criteria

### 구조적 정합성 및 시각화 검증
- [ ] 다이어그램의 모든 화살표가 실제 코드베이스(rontend/src/services/, ackend/app/, ackend/supabase/)의 호출 주체 및 방향과 완전히 일치해야 함.
- [ ] 화살표 선 교차 없이 위에서 아래 또는 좌에서 우로 한눈에 읽히는 집약적 레이아웃이어야 함.
- [ ] 노드별 형태(네모/알약/원형/원통) 구분이 일관되고 가독성이 극대화되어야 함.
- [ ] 프론트엔드/백엔드/인프라 담당자 누구나 보고 자신의 역할을 즉시 파악할 수 있는 설명 표가 포함되어야 함.
