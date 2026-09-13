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
    const [allHistories, setAllHistories] = useState<any[]>([]) // 전체 데이터
    const [displayedHistories, setDisplayedHistories] = useState<any[]>([]) // 화면에 보여줄 데이터 (5개씩)
    const [page, setPage] = useState<number>(1)
    const [hasMore, setHasMore] = useState<boolean>(false)
    const [isLoading, setIsLoading] = useState(true)

    const PAGE_LIMIT = 5

    useEffect(() => {
        const fetchHistories = async () => {
            try {
                setIsLoading(true)
                const { data } = await historyService.listHistory()
                const rawList = data || []

                const mappedList = rawList.map((item: any) => ({
                    id: String(item.analysis_case_id || item.id),
                    title: item.title || item.program_name || item.original_filename || '제목 없음',
                    date: formatDate(item.completed_at || item.created_at),
                }))

                setAllHistories(mappedList)
                setDisplayedHistories(mappedList.slice(0, PAGE_LIMIT))
                setHasMore(mappedList.length > PAGE_LIMIT)
            } catch (error) {
                setAllHistories([])
                setDisplayedHistories([])
            } finally {
                setIsLoading(false)
            }
        }

        fetchHistories()
    }, [])

    const handleLoadMore = () => {
        const nextPage = page + 1
        const endIndex = nextPage * PAGE_LIMIT
        const nextSlice = allHistories.slice(0, endIndex)

        setDisplayedHistories(nextSlice)
        setPage(nextPage)
        setHasMore(endIndex < allHistories.length)
    }

    if (isLoading || displayedHistories.length === 0) {
        return null
    }

    return (
        <HistoryListView 
            histories={displayedHistories} 
            hasMore={hasMore}
            totalCount={allHistories.length}
            onHistoryClick={onHistoryClick} 
            onLoadMore={handleLoadMore}
        />
    )
}