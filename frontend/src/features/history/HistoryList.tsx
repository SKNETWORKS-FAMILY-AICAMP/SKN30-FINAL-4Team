import { useState, useEffect } from 'react'
import HistoryListView from './HistoryListView'
import { historyService, type HistoryItemModel } from '../../services/historyService'

interface HistoryListProps {
    onHistoryClick: (id: string) => void
}

const formatDate = (isoString?: string | null) => {
    if (!isoString) return ''
    const date = new Date(isoString)
    if (isNaN(date.getTime())) return isoString

    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')

    return `${year}.${month}.${day} ${hours}:${minutes}`
}

export default function HistoryList({ onHistoryClick }: HistoryListProps) {
    const [histories, setHistories] = useState<any[]>([])
    const [cursor, setCursor] = useState<string | null>(null)
    const [hasMore, setHasMore] = useState<boolean>(false)
    const [isLoading, setIsLoading] = useState(true)

    const fetchHistories = async (targetCursor?: string, isAppend = false) => {
        try {
            setIsLoading(true)
            const response = await historyService.listHistory(targetCursor)
            const rawItems = response.items || []

            const mappedList = rawItems.map((item: HistoryItemModel) => ({
                id: String(item.analysis_case_id),
                title: item.program_name || item.original_filename || '제목 없음',
                date: formatDate(item.completed_at),
            }))

            if (isAppend) {
                setHistories((prev) => [...prev, ...mappedList])
            } else {
                setHistories(mappedList)
            }

            setCursor(response.next_cursor)
            setHasMore(!!response.next_cursor)
        } catch (error) {
            if (!isAppend) {
                setHistories([])
            }
            setHasMore(false)
        } finally {
            setIsLoading(false)
        }
    }

    useEffect(() => {
        fetchHistories()
    }, [])

    const handleLoadMore = () => {
        if (cursor && !isLoading) {
            fetchHistories(cursor, true)
        }
    }

    if (isLoading && histories.length === 0) {
        return null
    }

    if (!isLoading && histories.length === 0) {
        return null
    }

    return (
        <HistoryListView 
            histories={histories} 
            hasMore={hasMore}
            totalCount={histories.length}
            onHistoryClick={onHistoryClick} 
            onLoadMore={handleLoadMore}
        />
    )
}