// CPL 항목 코드 맵핑 (HTML에 명시된 13개 항목 기준)
export const CPL_LABELS: Record<string, string> = {
    REQUEST_TYPE: '요청유형 체크값',
    PURPOSE_GOAL: '사업 목적 · 목표',
    IMPLEMENTATION_PLAN: '연차별 · 내역사업별 추진계획',
    BUSINESS_PERIOD: '사업기간',
    NEW_OR_CHANGED_CONTENT: '신설 · 변경 주요내용',
    BUSINESS_NEED: '사업필요성 최소 논리구조',
    LEGAL_BASIS: '지원근거',
    LINKED_POLICY: '연계정책',
    BUDGET: '사업예산',
    TARGET_AND_CONDITIONS: '지원대상 · 지원조건',
    SUPPORT_CONTENT_AND_SCALE: '지원내용 · 지원규모',
    DELIVERY_SYSTEM: '수행기관 · 수행방식 · 수행체계',
    EXPECTED_EFFECTS_AND_PERFORMANCE: '기대효과 · 성과 관련 정보',
}

// FIT 관계 코드 맵핑
export const FIT_LABEL: Record<string, string> = {
    'FIT-1': '목적 ↔︎ 지원대상',
    'FIT-2': '목적 ↔︎ 지원내용',
    'FIT-3': '목적 ↔︎ 기대효과·성과지표',
    'FIT-4': '세부사업 ↔︎ 내역사업/내내역사업',
    'FIT-5': '지원대상 ↔︎ 지원조건',
    'FIT-6': '수행기관 ↔︎ 역할·수행절차',
    'FIT-7': '지원내용 ↔︎ 지원규모의 수치·조건',
}

export const getCplLabel = (code: string): string => {
    return CPL_LABELS[code] || code
}

export const getFitLabel = (code: string): string => {
    return FIT_LABEL[code] || code
}

// CPL 상태 배지 헬퍼
export const getCplBadge = (status: string) => {
    switch (status) {
        case 'PRESENT':
        case 'confirmed':
            return (
                <div className="flex-none w-1/3 h-full flex justify-center items-center gap-xs bg-[#E6F4EA] rounded-r-xs font-medium text-[13px] text-[#166534]">
                    <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>task_alt</span>
                    확인됨
                </div>
            )
        case 'NEEDS_CONFIRMATION':
            return (
                <div className="flex-none w-1/3 h-full flex justify-center items-center gap-xs bg-[#FEF7E0] rounded-r-xs font-medium text-[13px] text-[#854D0E]">
                    <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>pending</span>
                    확인필요
                </div>
            )
        case 'MISSING':
            return (
                <div className="flex-none w-1/3 h-full flex justify-center items-center gap-xs bg-error-container rounded-r-xs font-medium text-[13px] text-error">
                    <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>error</span>
                    내용없음
                </div>
            )
        default:
            return (
                <div className="flex-none w-1/3 h-full flex justify-center items-center gap-xs bg-[#F1F3F4] rounded-r-xs font-medium text-[13px] text-gray-700">
                    <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>do_not_disturb_on</span>
                    해당없음
                </div>
            )
    }
}

// FIT 상태 배지 헬퍼
export const getFitBadge = (status: string) => {
    switch (status) {
        case 'FIT':
        case 'CONFIRMED':
            return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-[#E6F4EA] text-[#166534]">연결 확인</span>
        case 'INSUFFICIENT':
            return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-[#F1F3F4] text-[#5F6368]">비교 정보 부족</span>
        case 'CONFLICT':
        case 'NEEDS_REVIEW':
            return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-error-container text-error">충돌 확인</span>
        default:
            return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-[#FEF7E0] text-[#B06000]">추가 검토 필요</span>
    }
}