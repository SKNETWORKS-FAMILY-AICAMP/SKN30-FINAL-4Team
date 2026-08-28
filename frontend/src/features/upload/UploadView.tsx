interface UploadViewProps {
    isUploading: boolean
    isDragging: boolean
    onFileDrop: (e: React.DragEvent<HTMLDivElement>) => void
    onDragOver: (e: React.DragEvent<HTMLDivElement>) => void
    onDragLeave: (e: React.DragEvent<HTMLDivElement>) => void
    onFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void
    onDropZoneClick: () => void
    fileInputRef: React.RefObject<HTMLInputElement | null>
}

export default function UploadView({
    isUploading,
    isDragging,
    onFileDrop,
    onDragOver,
    onDragLeave,
    onFileSelect,
    onDropZoneClick,
    fileInputRef,
}: UploadViewProps) {
    return (
        <div className="p-lg md:p-xl flex-1 flex flex-col items-center max-w-container-max mx-auto w-full pb-xl">
            <div className="w-full max-w-4xl flex flex-col gap-lg mt-md">
                <div className="text-center mb-xl pt-lg">
                    <h1 className="font-display-lg text-display-lg text-on-surface mb-sm">사전협의 요청서 업로드</h1>
                    <p className="font-body-md text-body-md text-on-surface-variant">AI 사전검토를 위한 새 사업계획안을 제출하거나 최근 활동을 모니터링하세요.</p>
                </div>

                <div className="bg-surface-container-lowest rounded-2xl border border-outline-variant p-xl flex flex-col shadow-sm relative overflow-hidden">
                    
                    {/* 업로드/분석 진행 중 Dim 오버레이 */}
                    {isUploading && (
                        <div className="absolute inset-0 bg-surface/65 backdrop-blur-sm z-10 flex flex-col items-center justify-center animate-fadeIn">
                            <div className="relative w-24 h-24 flex items-center justify-center mb-lg">
                                <div className="absolute inset-0 border-4 border-primary/20 border-t-primary rounded-full animate-spin"></div>
                            </div>
                            <div className="text-center p-lg rounded-2xl border-outline-variant/30">
                                <p className="font-display-lg text-[24px] font-bold text-on-surface">파일을 업로드하고 분석을 진행 중입니다</p>
                                <p className="font-body-md text-on-surface-variant mt-sm">업로드가 완료되는 대로 분석 결과를 확인하실 수 있습니다.</p>
                            </div>
                        </div>
                    )}

                    {/* ① 파일 업로드 영역 (드래그 호버 스타일 및 아이콘 인라인 스타일 적용) */}
                    <div 
                        onClick={onDropZoneClick}
                        onDragOver={onDragOver}
                        onDragLeave={onDragLeave}
                        onDrop={onFileDrop}
                        className={`border-2 border-dashed rounded-xl p-xl flex flex-col items-center justify-center text-center transition-all cursor-pointer mb-xl h-80 shrink-0 ${
                            isDragging 
                                ? 'border-primary bg-primary-container/10 scale-[1.01] shadow-md' 
                                : 'border-outline-variant hover:bg-surface-container-low hover:border-primary bg-surface'
                        }`}
                    >
                        <input 
                            type="file" 
                            ref={fileInputRef} 
                            onChange={onFileSelect} 
                            accept=".hwp,.hwpx,.pdf,.docx" 
                            className="hidden" 
                        />
                        <span 
                            className={`material-symbols-outlined mb-md transition-transform ${isDragging ? 'text-primary scale-110 animate-bounce' : 'text-outline'}`}
                            style={{ fontSize: '48px' }}
                        >
                            cloud_upload
                        </span>
                        <p className={`font-title-sm text-title-sm font-semibold mb-sm ${isDragging ? 'text-primary' : 'text-on-surface'}`}>
                            {isDragging ? '여기에 파일을 놓아주세요!' : '문서를 여기에 드래그 앤 드롭하세요'}
                        </p>
                        <p className="font-body-md text-body-md text-on-surface-variant mb-md">지원 형식: HWP, HWPX</p>
                        <button type="button" className="text-primary font-title-sm text-[15px] font-semibold hover:underline">
                            또는 클릭하여 파일 찾아보기
                        </button>
                    </div>

                    {/* ② 파일 업로드 가이드 영역 (아이콘 인라인 스타일 적용) */}
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-md">
                        <div className="p-md bg-surface-container-low rounded-lg border border-outline-variant">
                            <div className="flex items-center gap-sm mb-xs text-primary">
                                <span className="material-symbols-outlined" style={{ fontSize: '20px' }}>description</span>
                                <span className="font-bold text-body-md">지원 포맷 상세</span>
                            </div>
                            <p className="text-body-sm text-on-surface-variant">PDF, HWP, DOCX 파일을 지원하며, 표와 이미지가 포함된 문서도 분석 가능합니다.</p>
                        </div>
                        <div className="p-md bg-surface-container-low rounded-lg border border-outline-variant">
                            <div className="flex items-center gap-sm mb-xs text-primary">
                                <span className="material-symbols-outlined" style={{ fontSize: '20px' }}>security</span>
                                <span className="font-bold text-body-md">보안 정책 안내</span>
                            </div>
                            <p className="text-body-sm text-on-surface-variant">업로드된 문서는 암호화되어 처리되며, 분석 완료 후 즉시 파기하거나 안전하게 보관됩니다.</p>
                        </div>
                        <div className="p-md bg-surface-container-low rounded-lg border border-outline-variant">
                            <div className="flex items-center gap-sm mb-xs text-primary">
                                <span className="material-symbols-outlined" style={{ fontSize: '20px' }}>info</span>
                                <span className="font-bold text-body-md">분석 가이드</span>
                            </div>
                            <p className="text-body-sm text-on-surface-variant">최대 50MB 용량까지 지원하며, 텍스트가 선명한 문서를 권장합니다.</p>
                        </div>
                    </div>

                </div>
            </div>
        </div>
    )
}