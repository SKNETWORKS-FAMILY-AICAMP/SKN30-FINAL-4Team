# PoC Supabase API 요약

브라우저는 `workspace`·`result`·`kb`의 base table을 직접 조회하지 않는다. `workspace.analysis_run`의 본인 행만 Realtime 진행 표시를 위해 읽는다.

| 논리 API | 실제 Supabase 객체 | 용도 |
| --- | --- | --- |
| `GET /analysis-sessions/active` | `api.v_active_analysis_session` | 로그인 후 활성 분석 복원 |
| `GET /analysis-history` | `api.v_my_analysis_history` | 과거 분석 이력 |
| `GET /analysis-cases/{id}` | `api.rpc_get_analysis_result` | CPL·FIT·SIM·PDF·세션 화면 데이터 |
| `GET /sim-candidates/{id}` | `api.rpc_get_sim_candidate_detail` | 선택 SIM 후보 팝업 |
| `POST /analysis-sessions/{id}/touch` | `api.rpc_touch_active_analysis_session` | 30분 분석 세션 갱신 |
| `POST /analysis-sessions/{id}/close` | `api.rpc_close_active_analysis_session` | 새 분석 전 활성 세션 닫기 |
| `GET /conversation-messages` | `api.v_conversation_messages` | 대화 이력 읽기 |

## Edge Function

Edge Function은 JWT·소유권을 확인한 뒤 service role로 다단계 쓰기를 수행한다.

- `edge-analysis-run-create`: 업로드 경로 예약 및 `uploading` 작업 생성
- `edge-analysis-run-complete-upload`: 업로드 검증 후 `queued` 전환
- `edge-conversation-create-message`, `edge-conversation-retry-message`: 질문·assistant placeholder·큐 작업 생성
- `edge-report-regenerate`: 결과 JSON 기반 PDF 재생성 작업 등록
- `edge-report-create-download-url`: 권한 확인 후 60초 signed URL 발급

## 권한

RLS와 `api.*`의 소유권 확인으로 사용자는 자기 분석 데이터만 읽는다. CPU 워커와 Edge Function만 service role을 사용한다. Runpod GPU에는 Supabase 자격 증명을 주지 않는다.
