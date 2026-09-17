import { useState, useRef, useCallback, useEffect } from 'react'

export interface UseFileDropOptions {
    onFileSelected: (file: File) => void
    disabled?: boolean
}

export interface UseFileDropReturn {
    isDragging: boolean
    fileInputRef: React.RefObject<HTMLInputElement | null>
    handleDragOver: (e: React.DragEvent<HTMLDivElement>) => void
    handleDragLeave: (e: React.DragEvent<HTMLDivElement>) => void
    handleFileDrop: (e: React.DragEvent<HTMLDivElement>) => void
    handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void
    handleDropZoneClick: () => void
    resetFileInput: () => void
}

export function useFileDrop({
    onFileSelected,
    disabled = false,
}: UseFileDropOptions): UseFileDropReturn {
    const [isDragging, setIsDragging] = useState(false)
    const fileInputRef = useRef<HTMLInputElement | null>(null)
    const onFileSelectedRef = useRef(onFileSelected)

    useEffect(() => {
        onFileSelectedRef.current = onFileSelected
    }, [onFileSelected])

    const handleDragOver = useCallback(
        (e: React.DragEvent<HTMLDivElement>) => {
            e.preventDefault()
            e.stopPropagation()
            if (disabled) return
            setIsDragging(true)
        },
        [disabled]
    )

    const handleDragLeave = useCallback(
        (e: React.DragEvent<HTMLDivElement>) => {
            e.preventDefault()
            e.stopPropagation()
            setIsDragging(false)
        },
        []
    )

    const handleFileDrop = useCallback(
        (e: React.DragEvent<HTMLDivElement>) => {
            e.preventDefault()
            e.stopPropagation()
            setIsDragging(false)
            if (disabled) return

            if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                const droppedFile = e.dataTransfer.files[0]
                onFileSelectedRef.current(droppedFile)
                e.dataTransfer.clearData()
            }
        },
        [disabled]
    )

    const handleFileSelect = useCallback(
        (e: React.ChangeEvent<HTMLInputElement>) => {
            if (disabled) return
            if (e.target.files && e.target.files[0]) {
                onFileSelectedRef.current(e.target.files[0])
            }
        },
        [disabled]
    )

    const handleDropZoneClick = useCallback(() => {
        if (disabled) return
        fileInputRef.current?.click()
    }, [disabled])

    const resetFileInput = useCallback(() => {
        if (fileInputRef.current) {
            fileInputRef.current.value = ''
        }
    }, [])

    return {
        isDragging,
        fileInputRef,
        handleDragOver,
        handleDragLeave,
        handleFileDrop,
        handleFileSelect,
        handleDropZoneClick,
        resetFileInput,
    }
}

export default useFileDrop
