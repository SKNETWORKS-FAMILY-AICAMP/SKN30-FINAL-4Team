import { useState, useEffect } from 'react'
import HistoryListView from './HistoryListView'
import { historyService } from '../../services/historyService'

interface HistoryListProps {
    onHistoryClick: (id: string) => void
}

const formatDate = (isoString?: string) => {
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
    const [page, setPage] = useState<number>(0)
    const [hasMore, setHasMore] = useState<boolean>(false)
    const [totalCount, setTotalCount] = useState<number>(0)
    const [isLoading, setIsLoading] = useState(true)
    const [isFetchingMore, setIsFetchingMore] = useState(false)

    const fetchHistories = async (targetPage: number, isAppend = false) => {
        try {
            const limit = 5
            const { data, count } = await historyService.listHistory(targetPage, limit)
            const rawList = data || []
            const total = count || 0

            const mappedList = rawList.map((item: any) => ({
                id: String(item.analysis_case_id || item.id),
                title: item.title || item.case_name || '제목 없음',
                date: formatDate(item.completed_at || item.created_at),
            }))

            if (isAppend) {
                setHistories((prev) => [...prev, ...mappedList])
            } else {
                setHistories(mappedList)
            }

            setTotalCount(total)
            // 현재까지 불러온 개수가 전체 개수보다 적으면 더보기 가능
            setHasMore((isAppend ? histories.length + mappedList.length : mappedList.length) < total)
        } catch (error) {
            if (!isAppend) setHistories([])
        } finally {
            setIsLoading(false)
            setIsFetchingMore(false)
        }
    }

    useEffect(() => {
        fetchHistories(0, false)
    }, [])

    const handleLoadMore = () => {
        if (!hasMore || isFetchingMore) return
        setIsFetchingMore(true)
        const nextPage = page + 1
        setPage(nextPage)
        fetchHistories(nextPage, true)
    }

    // 이력이 없거나 로딩 중이면 타이틀을 포함해 통째로 숨김
    if (isLoading || histories.length === 0) {
        return null
    }

    return (
        <HistoryListView 
            histories={histories} 
            hasMore={hasMore}
            totalCount={totalCount}
            onHistoryClick={onHistoryClick} 
            onLoadMore={handleLoadMore}
        />
    )
}