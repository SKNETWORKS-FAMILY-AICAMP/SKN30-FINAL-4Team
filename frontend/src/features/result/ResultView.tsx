import { useState, useEffect } from 'react'
import AiChat from './AiChat'

interface ResultViewProps {
    reportData: any               // 컨트롤러에서 전달받은 API 응답 객체
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
    // API 데이터를 컴포넌트 내부 상태로 등록
    const [report, setReport] = useState<any>(reportData || {})

    useEffect(() => {
        if (reportData) {
            setReport(reportData)
        }
    }, [reportData])

    // API 응답 데이터 구조 분해 할당 (데이터가 없을 경우를 대비해 빈 배열/객체 기본값 설정)
    const {
        fileName = '',
        caseId = '',
        timestamp = '',
        status = '',
        summary = {},
        selfChecks = [],     // 2번 자기진단 항목 데이터 배열
        fitAnalysis = [],    // 3번 구조적 정합성 검증 결과 테이블 데이터 배열
        similarCandidates = [] // 4번 유사 사업 공고 추천 데이터 배열
    } = report

    return (
        <>
            <div className="flex-1 flex flex-col min-w-0 overflow-y-auto h-screen pb-xl px-md md:px-lg xl:px-xl w-full relative">
                
                {/* 상단 헤더 영역 */}
                <div className="mb-lg flex flex-col sm:flex-row sm:items-end justify-between gap-md pt-lg relative">
                    <div>
                        <h2 className="font-headline-md text-headline-md text-on-surface">분석 결과</h2>
                        <p className="font-body-md text-body-md text-on-surface-variant mt-xs">
                            문서명: {fileName} (Case ID: {caseId}) • 처리일시: {timestamp}
                        </p>
                    </div>
                    <div className="flex items-center gap-sm">
                        <button 
                            type="button"
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
                                className="w-12 h-12 flex items-center justify-center bg-primary-container text-on-tertiary hover:bg-primary transition-all rounded-none cursor-pointer"
                                title="닫기"
                            >
                                <span className="material-symbols-outlined text-[24px]">close</span>
                            </button>
                        )}
                    </div>
                </div>

                {/* 대시보드 그리드 본문 */}
                <div className="grid grid-cols-12 gap-lg">
                    
                    {/* 1. 종합 분석 요약 */}
                    <div className="col-span-12 bg-surface-container-low p-md rounded border border-outline-variant">
                        <h3 className="font-title-md text-on-surface mb-sm">1. 📋 종합 분석 요약</h3>
                        <ul className="list-disc pl-md space-y-xs font-body-sm text-on-surface-variant">
                            <li>자기진단 확인율: {summary.selfCheckRate} ({summary.selfCheckDetail})</li>
                            <li>구조적 정합성 점수: {summary.fitScore} ({summary.fitScoreDetail})</li>
                            <li>UI 처리 상태: {status}</li>
                        </ul>
                    </div>

                    {/* 2. 주요 항목별 자기진단 (Self-Check) 현황 (동적 렌더링) */}
                    <div className="col-span-12 lg:col-span-6 bg-surface-container-low p-md rounded border border-outline-variant">
                        <h3 className="font-title-md text-on-surface mb-sm">2. 🔍 주요 항목별 자기진단 (Self-Check) 현황</h3>
                        <ul className="list-disc pl-md space-y-xs font-body-sm text-on-surface-variant">
                            {selfChecks.map((item: any, index: number) => (
                                <li key={index}>
                                    {item.label}: {item.value} {item.status ? `(${item.status})` : ''}
                                </li>
                            ))}
                        </ul>
                    </div>

                    {/* 3. 유사 사업 공고 추천 (동적 렌더링) */}
                    <div className="col-span-12 lg:col-span-6 bg-surface-container-low p-md rounded border border-outline-variant">
                        <h3 className="font-title-md text-on-surface mb-sm">4. 🔗 유사 사업 공고 추천 (Similar Candidates)</h3>
                        <ul className="list-disc pl-md space-y-xs font-body-sm text-on-surface-variant">
                            {similarCandidates.map((candidate: any, index: number) => (
                                <li key={index}>
                                    Rank {candidate.rank || index + 1}: {candidate.title} (유사도: {candidate.similarity}%, 상태: {candidate.status})
                                </li>
                            ))}
                        </ul>
                    </div>

                    {/* 4. 구조적 정합성 검증 결과 테이블 (동적 매핑 렌더링) */}
                    <div className="col-span-12 bg-surface-container-low p-md rounded border border-outline-variant">
                        <h3 className="font-title-md text-on-surface mb-sm">3. ⚠️ 구조적 정합성 검증 (FIT Analysis) 결과</h3>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left border-collapse font-body-sm text-on-surface-variant">
                                <thead>
                                    <tr className="border-b border-outline-variant">
                                        <th className="p-xs">항목 ID</th>
                                        <th className="p-xs">상태 (Status)</th>
                                        <th className="p-xs">점수</th>
                                        <th className="p-xs">주요 내용 및 요약</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {fitAnalysis.map((fit: any, index: number) => (
                                        <tr key={index} className="border-b border-outline-variant/50">
                                            <td className="p-xs font-bold">{fit.id}</td>
                                            <td className={`p-xs ${fit.status === 'CONFLICT' ? 'text-error font-bold' : ''}`}>
                                                {fit.status}
                                            </td>
                                            <td className="p-xs">{fit.score !== null && fit.score !== undefined ? `${fit.score}점` : '-'}</td>
                                            <td className="p-xs">{fit.description}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>

                </div>
            </div>

            {/* AI 질의응답 */}
            <AiChat readOnly={readOnlyChat} />
        </>
    )
}