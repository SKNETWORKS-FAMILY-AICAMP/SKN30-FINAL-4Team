import { useState, useRef, useEffect } from 'react'
import UploadView from './UploadView'
import { analysisService } from '../../services/analysisService'
import AlertModal from '../../components/common/AlertModal'
import { usePolling } from '../../hooks/usePolling'

interface UploadProps {
    initialViewState?: 'upload' | 'analyzing'
    runId?: string | null
    onAnalysisComplete: (caseId: string) => void
}

export default function Upload({ initialViewState = 'upload', runId, onAnalysisComplete }: UploadProps) {
    const [isUploading, setIsUploading] = useState(initialViewState === 'analyzing')
    const [isDragging, setIsDragging] = useState(false)
    const [analysisStatus, setAnalysisStatus] = useState<string>(initialViewState === 'analyzing' ? 'running' : 'uploading')
    const [alertMessage, setAlertMessage] = useState<string | null>(null)
    const [targetRunId, setTargetRunId] = useState<string | null>(initialViewState === 'analyzing' && runId ? runId : null)
    const fileInputRef = useRef<HTMLInputElement | null>(null)

    // 💡 분석 중(analyzing) 상태로 진입하고 runId가 있으면 폴링 대상 설정
    useEffect(() => {
        if (initialViewState === 'analyzing' && runId) {
            setIsUploading(true)
            setTargetRunId(runId)
        }
    }, [initialViewState, runId])

    usePolling(
        async () => {
            if (!targetRunId) return false

            try {
                const statusRes = await analysisService.getRunStatus(targetRunId)
                const currentStatus = String(statusRes?.status || statusRes?.state || '').trim()
                console.log(`[분석 폴링 상태 수신]`, currentStatus)

                if (currentStatus) {
                    setAnalysisStatus(currentStatus)
                }

                if (
                    currentStatus === 'uploading' ||
                    currentStatus === 'queued' ||
                    currentStatus === 'running' ||
                    currentStatus === 'processing' ||
                    currentStatus === 'processing-run'
                ) {
                    return // 계속 폴링 진행
                }

                // 완료 또는 중단 상태 도달 시 폴링 종료
                setTargetRunId(null)

                if (currentStatus === 'succeeded' || currentStatus === 'ready') {
                    const resolvedCaseId = statusRes?.analysis_case_pk || statusRes?.analysis_case_id || statusRes?.case_id || statusRes?.id
                    if (resolvedCaseId) {
                        onAnalysisComplete(resolvedCaseId)
                    } else {
                        throw new Error('분석 케이스 ID를 찾을 수 없습니다')
                    }
                } else if (currentStatus === 'failed' || currentStatus === 'cancelled') {
                    throw new Error(statusRes?.error_message || '서버에서 분석 작업이 실패했습니다')
                } else {
                    const fallbackId = statusRes?.analysis_case_pk || statusRes?.analysis_case_id || statusRes?.case_id || statusRes?.id
                    if (fallbackId) {
                        onAnalysisComplete(fallbackId)
                    }
                }
                return false
            } catch (pollError: any) {
                setTargetRunId(null)
                setIsUploading(false)
                setAnalysisStatus('uploading')
                setAlertMessage(pollError.message || '처리 중 오류가 발생했습니다')
                return false
            }
        },
        {
            enabled: isUploading && !!targetRunId,
            interval: 3000
        }
    )

    const handleProcessFile = async (file: File) => {
        setIsUploading(true)
        setAnalysisStatus('uploading')

        try {
            const run = await analysisService.uploadAndAnalyze(file)
            const createdRunId = run?.analysis_run_pk || run?.analysis_run_id || run?.id

            if (!createdRunId) {
                throw new Error('분석 작업 ID를 받지 못했습니다')
            }

            setTargetRunId(createdRunId)
        } catch (error: any) {
            console.error('파일 업로드 및 분석 프로세스 실패:', error)
            const errorMsg = error.message || '처리 중 오류가 발생했습니다'
            
            setAlertMessage(errorMsg)
            setIsUploading(false)
            setAnalysisStatus('uploading')

            if (fileInputRef.current) {
                fileInputRef.current.value = ''
            }
        }
    }

    const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files[0]) {
            handleProcessFile(e.target.files[0])
        }
    }

    const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
        e.preventDefault()
        e.stopPropagation()
        setIsDragging(true)
    }

    const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
        e.preventDefault()
        e.stopPropagation()
        setIsDragging(false)
    }

    const handleFileDrop = (e: React.DragEvent<HTMLDivElement>) => {
        e.preventDefault()
        e.stopPropagation()
        setIsDragging(false)
        
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            const droppedFile = e.dataTransfer.files[0]
            handleProcessFile(droppedFile)
            e.dataTransfer.clearData()
        }
    }

    const handleDropZoneClick = () => {
        fileInputRef.current?.click()
    }

    return (
        <>
            <UploadView 
                isUploading={isUploading}
                isDragging={isDragging}
                analysisStatus={analysisStatus}
                onFileDrop={handleFileDrop}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onFileSelect={handleFileSelect}
                onDropZoneClick={handleDropZoneClick}
                fileInputRef={fileInputRef}
            />

            {alertMessage && (
                <AlertModal
                    title="알림"
                    description={alertMessage}
                    type="alert"
                    onConfirm={() => setAlertMessage(null)}
                />
            )}
        </>
    )
}