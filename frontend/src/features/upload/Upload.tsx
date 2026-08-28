import { useState, useRef } from 'react'
import UploadView from './UploadView'

interface UploadProps {
    onAnalysisComplete: () => void
}

export default function Upload({ onAnalysisComplete }: UploadProps) {
    const [isUploading, setIsUploading] = useState(false)
    const [isDragging, setIsDragging] = useState(false)
    const fileInputRef = useRef<HTMLInputElement | null>(null)

    const handleProcessFile = (file: File) => {
        console.log('Processed file:', file.name)
        setIsUploading(true)

        setTimeout(() => {
            setIsUploading(false)
            onAnalysisComplete()
        }, 2000)
    }

    const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files[0]) {
            handleProcessFile(e.target.files[0])
        }
    }

    // 파일이 드롭존 위에 올라와 있을 때
    const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
        e.preventDefault()
        e.stopPropagation()
        setIsDragging(true)
    }

    // 파일이 드롭존 영역을 벗어났을 때
    const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
        e.preventDefault()
        e.stopPropagation()
        setIsDragging(false)
    }

    // 드롭 시 파일 처리 및 호버 상태 해제
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