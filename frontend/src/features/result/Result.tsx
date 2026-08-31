import ResultView from './ResultView'

interface ResultProps {
    onBackToUpload?: () => void
    readOnlyChat?: boolean
}

export default function Result({ onBackToUpload, readOnlyChat = false }: ResultProps) {
    // [보고서 내보내기] PDF 다운로드 핸들러
    const handleExportPDF = () => {
        window.print() // 브라우저 인쇄 유도
    }

    return (
        <ResultView 
            onExportPDF={handleExportPDF}
            onBackToUpload={onBackToUpload}
            readOnlyChat={readOnlyChat}
        />
    )
}