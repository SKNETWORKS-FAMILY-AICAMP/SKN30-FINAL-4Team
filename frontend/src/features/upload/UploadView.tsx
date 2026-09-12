import PageTemplate from "../../components/common/MainPageTemplate";
import { getUploadStatusMessage, UPLOAD_GUIDES } from "../../utils/uploadData";

interface UploadViewProps {
    isUploading: boolean;
    isDragging: boolean;
    analysisStatus: string;
    onFileDrop: (e: React.DragEvent<HTMLDivElement>) => void;
    onDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
    onDragLeave: (e: React.DragEvent<HTMLDivElement>) => void;
    onFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
    onDropZoneClick: () => void;
    fileInputRef: React.RefObject<HTMLInputElement | null>;
}

export default function UploadView({ isUploading, isDragging, analysisStatus, onFileDrop, onDragOver, onDragLeave, onFileSelect, onDropZoneClick, fileInputRef }: UploadViewProps) {
    const statusInfo = getUploadStatusMessage(analysisStatus);

    return (
        <PageTemplate title="사전협의 요청서 업로드" subtitle="AI 사전검토를 위한 새 사업계획안을 제출하거나 최근 활동을 모니터링하세요">
            {/* 업로드/분석 진행 중 Dim 오버레이 */}
            {isUploading && (
                <div className="absolute inset-0 bg-surface/65 backdrop-blur-sm z-10 flex flex-col items-center justify-center animate-fadeIn">
                    <div className="relative w-24 h-24 flex items-center justify-center mb-lg">
                        <div className="absolute inset-0 border-4 border-primary/20 border-t-primary rounded-full animate-spin"></div>
                    </div>
                    <div className="text-center p-lg rounded-2xl border-outline-variant/30">
                        <p className="font-display-lg text-[24px] font-bold text-on-surface">{statusInfo.title}</p>
                        <p className="font-body-md text-on-surface-variant mt-sm" dangerouslySetInnerHTML={{ __html: statusInfo.description }} />
                    </div>
                </div>
            )}

            {/* ① 파일 업로드 영역 */}
            <div
                onClick={onDropZoneClick}
                onDragOver={onDragOver}
                onDragLeave={onDragLeave}
                onDrop={onFileDrop}
                className={`border-2 border-dashed rounded-xl p-xl flex flex-col items-center justify-center text-center transition-all cursor-pointer mb-xl h-80 shrink-0 ${isDragging ? "border-primary bg-primary-container/10 scale-[1.01] shadow-md" : "border-outline-variant hover:bg-surface-container-low hover:border-primary bg-surface"
                    }`}
            >
                <input type="file" ref={fileInputRef} onChange={onFileSelect} accept=".hwp,.hwpx,.pdf,.docx" className="hidden" />
                
                {/* 💡 pointer-events-none 추가하여 내부 요소가 마우스 이벤트를 방해하지 않도록 처리 */}
                <div className="pointer-events-none flex flex-col items-center justify-center w-full">
                    <span className={`material-symbols-outlined mb-md transition-transform ${isDragging ? "text-primary scale-110 animate-bounce" : "text-outline"}`} style={{ fontSize: "48px" }}>
                        cloud_upload
                    </span>
                    <p className={`font-title-sm text-title-sm font-semibold mb-sm ${isDragging ? "text-primary" : "text-on-surface"}`}>{isDragging ? "여기에 파일을 놓아주세요!" : "문서를 여기에 드래그 앤 드롭하세요"}</p>
                    <p className="font-body-md text-body-md text-on-surface-variant mb-md">지원 형식: HWP, HWPX</p>
                    <button type="button" className="text-primary font-title-sm text-[15px] font-semibold hover:underline pointer-events-auto">
                        또는 클릭하여 파일 찾아보기
                    </button>
                </div>
            </div>

            {/* ② 파일 업로드 가이드 영역 */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-md">
                {UPLOAD_GUIDES.map((guide) => (
                    <div key={guide.title} className="p-md bg-surface-container-low rounded-lg border border-outline-variant">
                        <div className="flex items-center gap-sm mb-xs text-primary">
                            <span className="material-symbols-outlined" style={{ fontSize: "20px" }}>
                                {guide.icon}
                            </span>
                            <span className="font-bold text-body-md">{guide.title}</span>
                        </div>
                        <p className="text-body-sm text-on-surface-variant">{guide.description}</p>
                    </div>
                ))}
            </div>
        </PageTemplate>
    );
}
