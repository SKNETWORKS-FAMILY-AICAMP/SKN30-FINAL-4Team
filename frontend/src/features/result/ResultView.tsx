import { useState, useEffect } from 'react'
import AiChat from './AiChat'

// 1. CPL 필드 코드 영문을 한글로 변환해 주는 맵핑 객체
const FIELD_LABEL_MAP: Record<string, string> = {
    REQUEST_TYPE: '사전협의 요청 유형',
    PURPOSE_GOAL: '사업 목적·목표',
    IMPLEMENTATION_PLAN: '추진계획',
    BUSINESS_PERIOD: '사업 기간',
    NEW_OR_CHANGED_CONTENT: '신설·변경 내용',
    BUSINESS_NEED: '사업 필요성',
    LEGAL_BASIS: '법적 근거',
    LINKED_POLICY: '연계 정책·계획',
    BUDGET: '예산',
    TARGET_AND_CONDITIONS: '지원 대상·조건',
    SUPPORT_CONTENT_AND_SCALE: '지원 내용·규모',
    DELIVERY_SYSTEM: '수행 체계',
    EXPECTED_EFFECTS_AND_PERFORMANCE: '기대효과·성과지표',
}

// 2. FIT 관계 코드 영문을 한글로 변환해 주는 맵핑 객체
const FIT_LABEL_MAP: Record<string, string> = {
    'FIT-1': '목적의 대상 조건 ↔ 지원 대상',
    'FIT-2': '목적 방향 ↔ 지원 활동·수단',
    'FIT-3': '목적 방향 ↔ 기대효과·성과지표',
    'FIT-4': '사업 계층 간 비교',
    'FIT-5': '대상군 ↔ 지원 조건',
    'FIT-6': '수행기관 ↔ 절차·역할',
    'FIT-7': '지원 내용 ↔ 지원 규모 정량값',
}

// 3. 변환 헬퍼 함수들
const getFieldLabel = (fieldCode: string): string => {
    return FIELD_LABEL_MAP[fieldCode] || fieldCode
}

const getFitLabel = (relationId: string): string => {
    return FIT_LABEL_MAP[relationId] || relationId
}

interface ResultViewProps {
    reportData: any               
    onExportPDF: () => void
    onBackToUpload?: () => void
    onClose?: () => void          
    readOnlyChat?: boolean       
}

export default function ResultView({ 
    reportData,
    onExportPDF, 
    onClose, 
    readOnlyChat = false 
}: ResultViewProps) {
    const [report, setReport] = useState<any>(reportData || {})

    useEffect(() => {
        if (reportData) {
            setReport(reportData)
        }
    }, [reportData])

    const responseData = report?.data || report || {}

    const {
        case: caseInfo = {},          
        ui_status = '',               
        self_check = {},              
        structural_consistency = {},  
        similar_candidates = [],      
        report_download_url = ''
    } = responseData

    const getStatusBadge = (status: string, type: 'cpl' | 'fit') => {
        if (type === 'cpl') {
            switch (status) {
                case 'PRESENT':
                    return <span className="px-2 py-0.5 rounded text-[12px] font-medium bg-[#E6F4EA] text-[#137333]">확인됨</span>
                case 'NEEDS_CONFIRMATION':
                    return <span className="px-2 py-0.5 rounded text-[12px] font-medium bg-[#FEF7E0] text-[#B06000]">미확인</span>
                case 'N/A':
                default:
                    return <span className="px-2 py-0.5 rounded text-[12px] font-medium bg-[#F1F3F4] text-[#5F6368]">해당 없음</span>
            }
        } else {
            switch (status) {
                case 'FIT':
                    return <span className="px-2 py-0.5 rounded text-[12px] font-medium bg-[#E6F4EA] text-[#137333]">정합</span>
                case 'NEEDS_REVIEW':
                    return <span className="px-2 py-0.5 rounded text-[12px] font-medium bg-[#FEF7E0] text-[#B06000]">검토 필요</span>
                case 'INSUFFICIENT':
                    return <span className="px-2 py-0.5 rounded text-[12px] font-medium bg-[#FEF7E0] text-[#B06000]">비교정보 부족</span>
                default:
                    return <span className="px-2 py-0.5 rounded text-[12px] font-medium bg-[#F1F3F4] text-[#5F6368]">해당 없음</span>
            }
        }
    }

    return (
        <>
            <div className="flex-1 flex flex-col min-w-0 overflow-y-auto h-screen pb-xl px-md md:px-lg xl:px-xl w-full relative">
                
                {/* 상단 헤더 영역 */}
                <div className="mb-lg flex flex-col sm:flex-row sm:items-end justify-between gap-md pt-lg relative border-b border-outline-variant pb-md">
                    <div>
                        <h2 className="font-headline-md text-headline-md text-on-surface">분석 결과</h2>
                        <p className="font-body-md text-body-md text-on-surface-variant mt-xs">
                            문서명: {caseInfo?.title} {caseInfo?.caseId ? `(Case ID: ${caseInfo.caseId})` : ''} • 처리일시: {caseInfo?.created_at || caseInfo?.completed_at}
                        </p>
                    </div>
                    <div className="flex items-center gap-sm">
                        <button
                            type="button"
                            onClick={() => {
                                {/% TODO %/}
                            }}
                            className="px-md py-sm border border-outline-variant rounded bg-surface hover:bg-surface-container-low font-label-caps text-label-caps text-on-surface flex items-center gap-xs transition-colors cursor-pointer"
                        >
                            <span className="material-symbols-outlined text-[16px]">download</span> 
                            보고서 내보내기
                        </button>

                        {onClose && (
                            <button 
                                type="button"
                                onClick={onClose}
                                className="w-12 h-12 flex items-center justify-center bg-primary-container text-on-tertiary hover:bg-primary transition-all rounded-none cursor-pointer"
                                title="닫기"
                            >
                                <span className="material-symbols-outlined text-[24px]">close</span>
                            </button>
                        )}
                    </div>
                </div>

                {/* 본문 콘텐츠 영역 */}
                <div className="flex flex-col gap-lg pb-xl">
                    
                    {/* 1. 요청자료 완전성·기초구조 점검 */}
                    <div className="bg-surface border border-outline-variant rounded p-lg flex flex-col shadow-sm">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface">1. 요청자료 완전성·기초구조 점검</h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant">
                                    핵심 필드 확인 상태 (확인된 항목: {self_check?.confirmed_count}개 / 전체: {self_check?.total_count}개)
                                </p>
                            </div>
                            <span className="material-symbols-outlined text-primary-container p-sm bg-primary-fixed rounded">checklist</span>
                        </div>
                        
                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-md">
                            {self_check?.items?.map((item: any, index: number) => (
                                <div
                                    key={item.field_code || index}
                                    className="group border border-outline-variant p-md rounded bg-surface-container-lowest hover:bg-surface-container-low cursor-pointer transition-all relative"
                                >
                                    <span className="font-label-caps text-on-surface-variant block mb-xs">
                                        {getFieldLabel(item.field_code)}
                                    </span>
                                    <div className="flex items-center justify-between">
                                        {getStatusBadge(item.status, 'cpl')}
                                        <span className="material-symbols-outlined text-on-surface-variant/50 text-[18px] group-hover:text-primary transition-colors">find_in_page</span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* 2. 내부 정합성 점검 */}
                    <div className="bg-surface border border-outline-variant rounded p-lg flex flex-col shadow-sm">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface">2. 내부 정합성 점검</h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant">
                                    {structural_consistency?.description || '요청서 내 항목 간 최소 연결성·범위 차이·충돌 여부를 점검합니다.'}
                                </p>
                            </div>
                            <span className="material-symbols-outlined text-primary-container p-sm bg-primary-fixed rounded">rule</span>
                        </div>
                        
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-sm mb-md">
                            {structural_consistency?.relations?.map((relation: any, index: number) => (
                                <div
                                    key={relation.relation_id || index}
                                    className="group border border-outline-variant p-md rounded-lg bg-surface hover:bg-surface-container-low cursor-pointer transition-all relative"
                                >
                                    <span className="font-label-caps text-on-surface-variant block mb-xs">
                                        {getFitLabel(relation.relation_id) !== relation.relation_id 
                                            ? getFitLabel(relation.relation_id) 
                                            : (relation.title || relation.relation_label)}
                                    </span>
                                    <div className="flex items-center justify-between mb-2">
                                        {getStatusBadge(relation.status, 'fit')}
                                        <span className="material-symbols-outlined text-on-surface-variant/50 text-[18px] group-hover:text-primary transition-colors">link</span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                </div>
            </div>

            {/* AI 질의응답 */}
            <AiChat readOnly={readOnlyChat} />
        </>
    )
}