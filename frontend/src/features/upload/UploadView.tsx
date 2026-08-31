import PageTemplate from '../../components/common/MainPageTemplate'

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

// 가이드 데이터 상수 분리
const UPLOAD_GUIDES = [
    {
        icon: 'description',
        title: '지원 포맷 상세',
        description: 'PDF, HWP, DOCX 파일을 지원하며, 표와 이미지가 포함된 문서도 분석 가능합니다.',
    },
    {
        icon: 'security',
        title: '보안 정책 안내',
        description: '업로드된 문서는 암호화되어 처리되며, 분석 완료 후 즉시 파기하거나 안전하게 보관됩니다.',
    },
    {
        icon: 'info',
        title: '분석 가이드',
        description: '최대 50MB 용량까지 지원하며, 텍스트가 선명한 문서를 권장합니다.',
    },
]

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
        <PageTemplate 
            title="사전협의 요청서 업로드" 
            subtitle="AI 사전검토를 위한 새 사업계획안을 제출하거나 최근 활동을 모니터링하세요"
        >
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

            {/* ① 파일 업로드 영역 */}
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

            {/* ② 파일 업로드 가이드 영역 (div + map 조합) */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-md">
                {UPLOAD_GUIDES.map((guide) => (
                    <div 
                        key={guide.title}
                        className="p-md bg-surface-container-low rounded-lg border border-outline-variant"
                    >
                        <div className="flex items-center gap-sm mb-xs text-primary">
                            <span className="material-symbols-outlined" style={{ fontSize: '20px' }}>
                                {guide.icon}
                            </span>
                            <span className="font-bold text-body-md">{guide.title}</span>
                        </div>
                        <p className="text-body-sm text-on-surface-variant">{guide.description}</p>
                    </div>
                ))}
            </div>
        </PageTemplate>
    )
}