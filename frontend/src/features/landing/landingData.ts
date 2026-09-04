export const HIGHLIGHTS_DATA = [
    {
        title: '신속한 검토',
        subtitle: '사전검토 소요 시간 경감',
        description: '표준 알고리즘 기반 서류 스크리닝으로 검토 소요 시간 경감 지원',
    },
    {
        title: '완전성 검증',
        subtitle: '필수 요건 전수 점검',
        description: '13대 필수 제출 항목 및 법정 규정 점검 자동화',
    },
    {
        title: '선제적 탐지',
        subtitle: '정합성 불일치 분석',
        description: '목적·예산·대상 간 상충 및 기존 지원사업과의 중복 배제',
    },
]

export const WORKFLOW_DATA = [
    {
        step: '01',
        number: 1,
        title: '요청서 접수',
        description: 'HWP, HWPX 형식의 사업계획안 및 사전협의 요청서를 시스템에 드래그&드롭으로 손쉽게 업로드합니다.',
        footerText: '공문서 규격 HWP, HWPX 호환',
        footerIcon: 'upload_file',
    },
    {
        step: '02',
        number: 2,
        title: '5대 점검체계 자동 교차 검증',
        description: 'AI 분석 엔진이 13대 필수항목 누락 여부, 사업 내부 논리 정합성, 기존 사업 DB 유사·중복성을 정밀 스크리닝합니다.',
        footerText: 'AI 자동 다차원 교차 검증',
        footerIcon: 'psychology',
    },
    {
        step: '03',
        number: 3,
        title: '사전검토 요약서 도출',
        description: '검토 의견, 보완 권고사항, 유사사업 비교표가 포함된 공공 표준 양식의 결과 리포트(PDF)를 즉시 발급합니다.',
        footerText: '표준 사전검토 요약 리포트 자동 생성',
        footerIcon: 'description',
    },
]

export const CAPABILITIES_DATA = [
    {
        index: '01',
        title: '요청자료 완전성·기초구조 점검',
        description: '사업 개요, 지원 규모, 신청 자격 등 13개 주요 필수 항목의 누락 여부를 신속 스캔하여 검토 반려 요인을 조기 식별합니다.',
        footerText: '13대 법정 필수항목 체크리스트 자동 대조',
        footerIcon: 'check_circle',
        icon: 'checklist',
    },
    {
        index: '02',
        title: '내부 정합성 점검',
        description: '사업 목적과 지원 대상, 세부 추진일정 및 산출 예산 간의 수치 불일치나 논리적 충돌을 AI 교차 분석으로 판별합니다.',
        footerText: '목적-대상-예산-일정 간 인과 논리 분석',
        footerIcon: 'rule',
        icon: 'sync_problem',
    },
    {
        index: '03',
        title: '기존 사업 유사·중복성 검토',
        description: '축적된 중앙부처 및 지자체 유사 지원사업 DB와 정밀 텍스트 매칭을 실시하여 중복 수혜 및 유사 지원 리스크를 사전 검증합니다.',
        footerText: '과거 정부 지원사업 통합 DB 비교 검증',
        footerIcon: 'library_books',
        icon: 'difference',
    },
    {
        index: '04',
        title: 'AI 질의응답 및 결과 리포트',
        description: '검토 담당자가 자연어로 질의하면 사업계획안 근거 조항을 즉각 추출하고, 검토 심의용 최종 요약 리포트를 원클릭 출력합니다.',
        footerText: '자연어 쟁점 소명 및 표준 보고서 출력',
        footerIcon: 'contact_support',
        icon: 'chat',
    },
]