import { useState, useRef } from 'react'
import UploadView from './UploadView'
import { analysisService } from '../../services/analysisService'

interface UploadProps {
    onAnalysisComplete: (caseId: string) => void
}

export default function Upload({ onAnalysisComplete }: UploadProps) {
    const [isUploading, setIsUploading] = useState(false)
    const [isDragging, setIsDragging] = useState(false)
    const fileInputRef = useRef<HTMLInputElement | null>(null)

    const handleProcessFile = async (file: File) => {
        setIsUploading(true)

        try {
            // 1. 작업 생성, Storage 업로드, 큐 등록 통합 실행 (RUN-01 ~ 03)[cite: 18, 19]
            const run = await analysisService.uploadAndAnalyze(file)
            const runId = run?.analysis_run_pk || run?.analysis_run_id

            // 2. Realtime을 통한 상태 수신 구독 (RUN-04)[cite: 18, 19]
            await new Promise<any>((resolve, reject) => {
                const channel = analysisService.subscribeRun(runId, (row) => {
                    const currentStatus = String(row?.status || '').trim()
                    console.log(`[Realtime 상태 수신]`, currentStatus)

                    if (currentStatus === 'succeeded') {
                        channel.unsubscribe()
                        resolve(row)
                    } else if (currentStatus === 'failed' || currentStatus === 'cancelled') {
                        channel.unsubscribe()
                        reject(new Error(row?.error_message || '서버에서 분석 작업이 실패했습니다.'))
                    }
                })
            })

            // 3. 분석 성공 시 caseId만 추출하여 상위 컴포넌트로 전달
            const caseId = run?.analysis_case_pk || run?.analysis_case_id
            if (!caseId) {
                throw new Error('분석 케이스 ID를 찾을 수 없습니다.')
            }

            onAnalysisComplete(caseId)

        } catch (error: any) {
            console.error('파일 업로드 및 분석 프로세스 실패:', error)
            const errorMsg = error.message || '처리 중 오류가 발생했습니다'
            alert(errorMsg)
            if (fileInputRef.current) {
                fileInputRef.current.value = ''
            }
        } finally {
            setIsUploading(false)
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
        <UploadView 
            isUploading={isUploading}
            isDragging={isDragging}
            onFileDrop={handleFileDrop}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onFileSelect={handleFileSelect}
            onDropZoneClick={handleDropZoneClick}
            fileInputRef={fileInputRef}
        />
    )
}