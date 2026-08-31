import { useState, useRef } from 'react'
import UploadView from './UploadView'
import { caseService } from '../../services/caseService'

interface UploadProps {
    onAnalysisComplete: () => void
}

export default function Upload({ onAnalysisComplete }: UploadProps) {
    const [isUploading, setIsUploading] = useState(false)
    const [isDragging, setIsDragging] = useState(false)
    const fileInputRef = useRef<HTMLInputElement | null>(null)

    const handleProcessFile = async (file: File) => {
        setIsUploading(true)

        try {
            const formData = new FormData()
            formData.append('file', file)

            // 1. 파일 업로드 API 호출 (/api/v1/cases)[cite: 3]
            const uploadData = await caseService.uploadCase(formData)
            const caseId = uploadData?.case_id || uploadData?.data?.case_id || uploadData?.id

            if (!caseId) {
                throw new Error('케이스 ID를 받아오지 못했습니다.')
            }

            // 2. 분석 시작 API 호출 (/api/v1/cases/{case_id}/analyze)[cite: 3]
            await caseService.analyzeCase(caseId)

            // 3. 🔄 폴링 루프 추가 (상태가 COMPLETED가 될 때까지 주기적으로 체크)
            const maxAttempts = 1000;    // 최대 30번 시도 (약 1분 30초)
            const intervalTime = 3000; // 3초 간격
            let attempts = 0;
            let isCompleted = false;

            while (attempts < maxAttempts) {
                attempts++;
                // 대기 시간 주기 적용
                await new Promise(resolve => setTimeout(resolve, intervalTime));

                // 상태 조회 API 호출 (예: caseService에 getStatus가 있다고 가정)
                const statusData = await caseService.getStatus(caseId);
                const currentStatus = statusData?.status || statusData?.data?.status;

                console.log(`[폴링 ${attempts}차] 현재 분석 상태:`, currentStatus);

                if (currentStatus === 'COMPLETED') {
                    isCompleted = true;
                    break;
                } else if (currentStatus === 'FAILED') {
                    throw new Error('서버에서 분석 작업이 실패했습니다.');
                }
            }

            if (!isCompleted) {
                throw new Error('분석 작업 시간이 초과되었습니다.');
            }

            // 4. 완료 떨어지면 리포트 조회 API 호출 (/api/v1/cases/{case_id}/report)[cite: 3]
            const reportData = await caseService.getReport(caseId)
            console.log('최종 리포트 데이터:', reportData)

            onAnalysisComplete()
        } catch (error: any) {
            console.error('파일 업로드 및 분석 프로세스 실패:', error)
            const errorMsg = error.response?.data?.message || error.message || '처리 중 오류가 발생했습니다.'
            alert(errorMsg)
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