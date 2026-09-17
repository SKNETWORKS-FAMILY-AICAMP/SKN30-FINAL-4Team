import { getFitLabel, getFitBadge } from '../../../utils/resultData'

interface FitSectionProps {
    items: any[]
}

export default function FitSection({ items }: FitSectionProps) {
    return (
        <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
            <div className="flex justify-between items-start mb-md">
                <div>
                    <h3 className="font-title-sm text-title-sm text-on-surface">2. 내부 정합성 점검</h3>
                    <p className="font-body-sm text-body-sm text-on-surface-variant">현재 요청서에서 확인되는 항목 간 연결성·범위 차이·충돌 여부를 점검합니다</p>
                </div>
            </div>

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-sm">
                {items.map((item: any, index: number) => (
                    <div key={item.code || index} className="flex flex-col justify-between border border-outline-variant p-md rounded-lg bg-surface hover:bg-surface-container-low text-left">
                        <div className="flex justify-between items-center w-full mb-xs">
                            <span className="font-label-caps text-on-surface-variant font-semibold">{getFitLabel(item.code)}</span>
                            {getFitBadge(item.status)}
                        </div>
                        <div className="bg-surface-container-lowest p-2 rounded mt-2 text-xs text-on-surface-variant w-full">{item.summary}</div>
                    </div>
                ))}
            </div>
        </div>
    )
}
