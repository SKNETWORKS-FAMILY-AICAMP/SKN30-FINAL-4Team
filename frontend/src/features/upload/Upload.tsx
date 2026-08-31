import { useState, useRef } from 'react'
import UploadView from './UploadView'
import { caseService } from '../../services/caseService'

interface UploadProps {
    onAnalysisComplete: (caseId: string | number, reportData: any) => void
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

            // 3. 🔄 폴링 루프 (한글 상태값 대응 버전)
            const maxAttempts = 30;    
            const intervalTime = 3000; 
            let attempts = 0;
            let isCompleted = false;

            while (attempts < maxAttempts) {
                attempts++;

                const statusData = await caseService.getStatus(caseId);

                // data 객체 안의 status 추출 (한글이므로 toUpperCase는 빼고 공백만 trim 처리)
                const rawStatus = statusData?.status || statusData?.data?.status || '';
                const currentStatus = String(rawStatus).trim();

                console.log(`[폴링 ${attempts}차] 서버 응답 원본:`, currentStatus);

                // 백엔드가 내려주는 한글 상태값에 대응 ("분석 완료", "COMPLETED", "SUCCESS" 등 모두 방어)
                if (currentStatus === '분석 완료' || currentStatus === 'COMPLETED' || currentStatus === 'SUCCESS' || currentStatus === 'DONE') {
                    isCompleted = true;
                    break;
                } else if (currentStatus === '분석 실패' || currentStatus === 'FAILED' || currentStatus === 'ERROR') {
                    throw new Error('서버에서 분석 작업이 실패했습니다.');
                }

                if (attempts < maxAttempts) {
                    await new Promise(resolve => setTimeout(resolve, intervalTime));
                }
            }

            if (!isCompleted) {
                throw new Error('분석 작업 시간이 초과되었습니다.');
            }

            // 4. 완료 떨어지면 리포트 조회 API 호출
            // Upload.tsx 내부의 완료 지점
            const reportData = await caseService.getReport(caseId)

            // 부모의 handleAnalysisComplete 함수에 데이터를 실어 보냄
            onAnalysisComplete(caseId, reportData)
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