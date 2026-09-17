import { getCplLabel, getCplBadge } from '../../../utils/resultData'

interface CplSectionProps {
    items: any[]
}

export default function CplSection({ items }: CplSectionProps) {
    return (
        <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
            <div className="flex justify-between items-start mb-md">
                <div>
                    <h3 className="font-title-sm text-title-sm text-on-surface">1. 요청자료 완전성 · 기초구조 점검</h3>
                    <p className="font-body-sm text-body-sm text-on-surface-variant">현재 요청서에서 13개 주요 항목들을 확인합니다</p>
                </div>
            </div>

            <ul className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3 gap-sm">
                {items.map((item: any, index: number) => (
                    <li key={item.code || index} className="flex justify-between items-center bg-surface border border-outline-variant rounded-xs">
                        <div className="p-md font-medium text-[14px] text-on-surface">
                            {getCplLabel(item.code)}
                        </div>
                        {getCplBadge(item.status)}
                    </li>
                ))}
            </ul>
        </div>
    )
}
