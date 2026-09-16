import HistoryDetailView from './HistoryDetailView'

interface HistoryDetailProps {
    historyId: string | null
    onClose: () => void
}

export default function HistoryDetail({ historyId, onClose }: HistoryDetailProps) {
    if (!historyId) return null

    return (
        <HistoryDetailView 
            key={historyId}
            historyId={historyId} 
            onClose={onClose} 
        />
    )
}