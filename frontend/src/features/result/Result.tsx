import { useState, useEffect } from 'react'
import ResultView from './ResultView'

interface ResultProps {
    reportData: any
    onBackToUpload?: () => void
    readOnlyChat?: boolean
}
export default function Result({ reportData, onBackToUpload, readOnlyChat = false }: ResultProps) {
    const handleExportPDF = () => {
        window.print()
    }

    return (
        <ResultView 
            reportData={reportData} // 💡 뷰 컴포넌트로 전달
            onExportPDF={handleExportPDF}
            onBackToUpload={onBackToUpload}
            readOnlyChat={readOnlyChat}
        />
    )
}