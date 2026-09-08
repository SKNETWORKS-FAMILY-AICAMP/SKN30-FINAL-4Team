import { useState, useEffect } from 'react'
import AiChat from './AiChat'

// 1. CPL 필드 코드 영문을 한글로 변환해 주는 맵핑 객체
const FIELD_LABEL_MAP: Record<string, string> = {
    REQUEST_TYPE: '요청유형 체크값',
    PURPOSE_GOAL: '사업 목적·목표',
    IMPLEMENTATION_PLAN: '연차별·내역사업별 추진계획',
    BUSINESS_PERIOD: '사업기간',
    NEW_OR_CHANGED_CONTENT: '신설·변경 주요내용',
    BUSINESS_NEED: '사업필요성 최소 논리구조',
    LEGAL_BASIS: '지원근거',
    LINKED_POLICY: '연계정책',
    BUDGET: '사업예산',
    TARGET_AND_CONDITIONS: '지원대상·지원조건',
    SUPPORT_CONTENT_AND_SCALE: '지원내용·지원규모',
    DELIVERY_SYSTEM: '수행기관·수행방식·수행체계',
    EXPECTED_EFFECTS_AND_PERFORMANCE: '기대효과·성과 관련 정보',
}

// 2. FIT 관계 코드 영문을 한글로 변환해 주는 맵핑 객체
const FIT_LABEL_MAP: Record<string, string> = {
    'FIT-1': '목적 ↔︎ 지원대상',
    'FIT-2': '목적 ↔︎ 지원내용',
    'FIT-3': '목적 ↔︎ 기대효과·성과지표',
    'FIT-4': '세부사업 ↔︎ 내역사업/내내역사업',
    'FIT-5': '지원대상 ↔︎ 지원조건',
    'FIT-6': '수행기관 ↔︎ 역할·수행절차',
    'FIT-7': '지원내용 ↔︎ 지원규모의 수치·조건',
}

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

    // result_2.json 구조에 맞춰 데이터 추출
    const caseInfo = report?.case || {}
    const reportInfo = report?.report || {}
    const cplData = reportInfo?.cpl || {}
    const fitData = reportInfo?.fit || {}
    const similarCandidates = reportInfo?.similar_candidates || []

    // CPL 상태에 따른 배지 및 스타일 반환 헬퍼
    const getCplBadge = (status: string) => {
        switch (status) {
            case 'PRESENT':
                return (
                    <div className="w-1/3 h-full flex justify-center items-center bg-[#E6F4EA] font-medium text-[13px] gap-xs" style={{ color: 'rgb(22, 101, 52)' }}>
                        <span className="material-symbols-outlined text-[16px]">task_alt</span>
                        확인됨
                    </div>
                )
            case 'NEEDS_CONFIRMATION':
                return (
                    <div className="w-1/3 h-full flex justify-center items-center bg-[#FEF7E0] font-medium text-[13px] gap-xs" style={{ color: 'rgb(133, 77, 14)' }}>
                        <span className="material-symbols-outlined text-[16px]">pending</span>
                        확인필요
                    </div>
                )
            case 'MISSING':
                return (
                    <div className="w-1/3 h-full flex justify-center items-center bg-error-container font-medium text-[13px] gap-xs" style={{ color: 'rgb(153, 27, 27)' }}>
                        <span className="material-symbols-outlined text-[16px]">error</span>
                        내용없음
                    </div>
                )
            case 'N/A':
            default:
                return (
                    <div className="w-1/3 h-full flex justify-center items-center bg-[#F1F3F4] font-medium text-[13px] gap-xs" style={{ color: 'rgb(55, 65, 81)' }}>
                        <span className="material-symbols-outlined text-[16px]">do_not_disturb_on</span>
                        해당없음
                    </div>
                )
        }
    }

    // FIT 상태에 따른 배지 및 텍스트 스타일 반환 헬퍼
    const getFitBadge = (status: string) => {
        switch (status) {
            case 'FIT':
            case 'CONFIRMED':
                return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-[#E6F4EA] text-[#166534]">연결 확인</span>
            case 'INSUFFICIENT':
                return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-[#F1F3F4] text-[#5F6368]">비교 정보 부족</span>
            case 'CONFLICT':
            case 'NEEDS_REVIEW':
                return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-error-container text-error">충돌 확인</span>
            case 'PARTIAL':
                return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-[#FEF7E0] text-[#B06000]">추가 검토 필요</span>
            default:
                return <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full text-center bg-[#F1F3F4] text-[#5F6368]">기타</span>
        }
    }

    return (
        <>
            <div className="flex-1 flex flex-col min-w-0 overflow-y-auto h-screen pb-xl px-md md:px-lg xl:px-xl w-full relative">
                
                {/* 상단 헤더 영역 */}
                <div className="mb-lg flex flex-col sm:flex-row sm:items-end justify-between gap-md pt-lg">
                    <div className="flex-1">
                        <h2 className="font-headline-md text-headline-md text-on-surface">분석 결과</h2>
                        <div className="mt-xs">
                            <p className="font-semibold text-body-md text-on-surface" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                {caseInfo?.title || '분석 리포트'}
                            </p>
                            <p className="font-body-sm text-body-sm text-on-surface-variant mt-xs" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                분석완료일시: {caseInfo?.completed_at ? new Date(caseInfo.completed_at).toLocaleString() : '-'}
                            </p>
                        </div>
                    </div>
                    <div className="flex items-end gap-sm">
                        <button 
                            onClick={onExportPDF}
                            className="px-md py-sm border border-outline-variant rounded bg-surface hover:bg-surface-container-low font-label-caps text-label-caps text-on-surface flex items-center gap-xs transition-colors cursor-pointer"
                        >
                            <span className="material-symbols-outlined text-[16px]">download</span>
                            보고서 내보내기
                        </button>
                        {onClose && (
                            <button 
                                type="button"
                                onClick={onClose}
                                className="px-md py-sm border border-outline-variant rounded bg-primary-container text-on-primary font-label-caps text-label-caps flex items-center gap-xs transition-colors cursor-pointer"
                            >
                                닫기
                            </button>
                        )}
                    </div>
                </div>

                {/* 분석 본문 */}
                <div className="flex flex-col gap-lg pb-xl">
                    
                    {/* Category 1: 요청자료 완전성·기초구조 점검 */}
                    <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                    1. 요청자료 완전성·기초구조 점검
                                </h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                    현재 요청서에서 주요 항목들을 확인합니다 (확인된 항목: {cplData?.confirmed_count ?? 0}개)
                                </p>
                            </div>
                        </div>

                        <ul className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3">
                            {cplData?.items?.map((item: any, index: number) => (
                                <li key={item.field_code || index} className="flex items-center bg-surface border border-outline-variant">
                                    <div className="w-2/3 p-md font-medium text-[16px] text-on-surface" style={{ whiteSpace: 'normal', overflow: 'visible', wordBreak: 'keep-all' }}>
                                        {getFieldLabel(item.field_code)}
                                    </div>
                                    {getCplBadge(item.status)}
                                </li>
                            ))}
                        </ul>
                    </div>

                    {/* Category 2: 내부 정합성 점검 */}
                    <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                    2. 내부 정합성 점검
                                </h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                    현재 요청서에서 확인되는 항목 간 연결성·범위 차이·충돌 여부를 점검합니다
                                </p>
                            </div>
                        </div>

                        <div className="grid grid-cols-1 xl:grid-cols-2 gap-sm">
                            {fitData?.relations?.map((relation: any, index: number) => (
                                <div key={relation.relation_id || index} className="flex flex-col justify-between border border-outline-variant p-md rounded-lg bg-surface hover:bg-surface-container-low text-left">
                                    <div className="flex justify-between items-center w-full mb-xs">
                                        <span className="font-label-caps text-on-surface-variant font-semibold">
                                            {getFitLabel(relation.relation_id)}
                                        </span>
                                        {getFitBadge(relation.status)}
                                    </div>
                                    <div className="bg-surface-container-lowest p-2 rounded mt-2 text-xs text-on-surface-variant w-full">
                                        {relation.summary}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* Category 3: 기존 사업과의 유사·중복성 검토 */}
                    <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                    3. 기존 사업과의 유사·중복성 검토
                                </h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant" style={{ whiteSpace: 'normal', overflow: 'visible' }}>
                                    기존 지원사업 데이터에서 비교가 필요한 후보를 검색합니다
                                </p>
                            </div>
                        </div>

                        <table className="w-full border-collapse border-t-2 border-b-2 text-left zebra-table" style={{ tableLayout: 'fixed' }}>
                            <thead>
                                <tr className="border-b border-outline-variant bg-surface-container-low">
                                    <th className="p-md font-semibold text-body-sm text-on-surface w-1/3">사업명</th>
                                    <th className="p-md font-semibold text-body-sm text-on-surface">요약</th>
                                    <th className="w-[84px] xl:w-[108px]"></th>
                                </tr>
                            </thead>
                            <tbody className="text-body-sm text-on-surface-variant">
                                {similarCandidates?.map((candidate: any, index: number) => (
                                    <tr key={index} className="border-b border-outline-variant hover:bg-surface-container-low transition-colors">
                                        <td className="p-md font-medium text-on-surface">
                                            <a href={candidate.source_url || '#'} target="_blank" rel="noopener noreferrer" className="hover:underline hover:text-primary transition-colors">
                                                {candidate.title}
                                            </a>
                                        </td>
                                        <td className="p-md">
                                            {candidate.comparison_summary}
                                        </td>
                                        <td className="p-md text-center">
                                            <a 
                                                href={candidate.source_url || '#'} 
                                                target="_blank" 
                                                rel="noopener noreferrer"
                                                className="inline-block w-[52px] xl:w-[74px] px-3 py-1.5 border border-outline-variant rounded bg-surface hover:bg-white text-[12px] font-medium transition-colors text-center text-on-surface"
                                            >
                                                상세보기
                                            </a>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>

                </div>
            </div>

            {/* AI 질의응답 오버레이 */}
            <AiChat readOnly={readOnlyChat} />
        </>
    )
}