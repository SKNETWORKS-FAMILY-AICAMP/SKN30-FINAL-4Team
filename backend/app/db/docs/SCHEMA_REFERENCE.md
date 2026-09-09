# PoC Supabase 스키마 요약

내부 PK는 모두 UUID다. 별도 분석번호나 합성 PK는 두지 않으며, 업무상 유일성은 복합 `UNIQUE` 제약으로 보장한다.

| 영역 | 주요 엔티티 | 역할 |
| --- | --- | --- |
| `workspace` | `analysis_run` | 파일 업로드부터 분석 완료까지의 임시 작업 |
| `result` | `analysis_case` | 90일 보관하는 최종 분석 건 |
| `result` | `axis_result` | CPL 13개·FIT 7개 결과. 상세는 `result_data` JSONB |
| `result` | `sim_candidate` | 상위 5개 유사 공고와 네 축 비교 스냅샷 |
| `result` | `evidence_snapshot` | 결과가 참조하는 원문 근거 스냅샷 |
| `result` | `analysis_session` | 분석 건당 대화 세션. 사용자당 활성 세션 하나 |
| `result` | `conversation_message` | 사용자 질문·AI 답변·생성 실패 상태 |
| `result` | `report_generation` | PDF 생성·재생성 상태 |
| `ops` | `processing_run` | 분석·채팅·PDF·정리 작업의 Postgres 큐 |

## 생명주기

`analysis_run`은 `uploading → queued → running → succeeded | failed`로 전이한다. 성공 후 원본과 `workspace.*`는 정리하고, `analysis_case`·축 결과·근거·대화·PDF만 90일 보관한다.

`analysis_session`은 `active | closed`다. 활동 시 만료 시각을 현재부터 30분으로 재설정하고, 새 분석 또는 무활동으로 닫힌 뒤에도 `analysis_case`는 `ready` 상태로 보관한다.

## 결과 저장 원칙

- `analysis_case.input_profile_snapshot`: 최종 분석에 실제 사용한 정제 Request Profile
- `axis_result`: CPL/FIT의 목록 상태·요약은 열, 팝업 상세는 JSONB
- `evidence_snapshot`: 원문은 한 번만 저장하고 결과 JSON은 UUID로 참조
- `sim_candidate`: 제목·기관·URL·요약도 분석 시점 값으로 스냅샷 저장
- `total_budget`은 FIT-7에서 직접 지원액으로 계산하지 않는다.
