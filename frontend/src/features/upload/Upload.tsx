import { useState, useRef, useEffect } from 'react'
import UploadView from './UploadView'
import { analysisService } from '../../services/analysisService'
import AlertModal from '../../components/common/AlertModal'

interface UploadProps {
    onAnalysisComplete: (caseId: string) => void
}

export default function Upload({ onAnalysisComplete }: UploadProps) {
    const [isUploading, setIsUploading] = useState(false)
    const [isDragging, setIsDragging] = useState(false)
    const [analysisStatus, setAnalysisStatus] = useState<string>('uploading')
    const [alertMessage, setAlertMessage] = useState<string | null>(null)
    const fileInputRef = useRef<HTMLInputElement | null>(null)

    // 💡 컴포넌트 마운트 시 활성 세션을 조회하여 진행 중인 분석이 있으면 상태 복구
    useEffect(() => {
        const checkActiveSession = async () => {
            try {
                const activeRes = await analysisService.getActiveSession?.() // 서비스 구현에 맞춰 호출
                // 만약 active 세션 응답에 진행 중인 run 정보나 status가 있다면
                if (activeRes && (activeRes.status === 'queued' || activeRes.status === 'running' || activeRes.status === 'uploading')) {
                    setIsUploading(true)
                    setAnalysisStatus(activeRes.status)
                    
                    // 폴링 재개 로직 연결
                    const runId = activeRes.analysis_run_pk || activeRes.analysis_run_id
                    if (runId) {
                        startPolling(runId)
                    }
                }
            } catch (e) {
                // 활성 세션이 없거나 조회 실패 시 무시
            }
        }

        checkActiveSession()
    }, [])

    // 폴링 로직 분리 (새로고침 복구와 파일 업로드 공용)
    const startPolling = (runId: string) => {
        setIsUploading(true)

        const pollInterval = setInterval(async () => {
            try {
                const statusRes = await analysisService.getRunStatus(runId)
                const currentStatus = String(statusRes?.status || '').trim()
                console.log(`[분석 폴링 상태 수신]`, currentStatus)

                if (currentStatus) {
                    setAnalysisStatus(currentStatus)
                }

                if (currentStatus === 'queued' || currentStatus === 'running') {
                    return
                }

                clearInterval(pollInterval)

                if (currentStatus === 'succeeded') {
                    const caseId = statusRes?.analysis_case_pk || statusRes?.analysis_case_id
                    if (caseId) {
                        onAnalysisComplete(caseId)
                    } else {
                        throw new Error('분석 케이스 ID를 찾을 수 없습니다')
                    }
                } else if (currentStatus === 'failed' || currentStatus === 'cancelled') {
                    throw new Error(statusRes?.error_message || '서버에서 분석 작업이 실패했습니다')
                } else {
                    throw new Error(`알 수 없는 분석 상태입니다: ${currentStatus}`)
                }
            } catch (pollError: any) {
                clearInterval(pollInterval)
                setIsUploading(false)
                setAnalysisStatus('uploading')
                setAlertMessage(pollError.message || '처리 중 오류가 발생했습니다')
            }
        }, 3000)
    }

    const handleProcessFile = async (file: File) => {
        setIsUploading(true)
        setAnalysisStatus('uploading')

        try {
            const run = await analysisService.uploadAndAnalyze(file)
            const runId = run?.analysis_run_pk || run?.analysis_run_id

            if (!runId) {
                throw new Error('분석 작업 ID를 받지 못했습니다')
            }

            startPolling(runId)
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