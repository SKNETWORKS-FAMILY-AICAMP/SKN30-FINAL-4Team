import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'

import PublicLayout from '../components/layout/PublicLayout'

import LandingPage from '../pages/LandingPage'

export default function App() {
    return (
        <BrowserRouter>
            <Routes>
                {/* --- 퍼블릭 / 인증 그룹 (PublicLayout 공유) --- */}
                <Route element={<PublicLayout />}>
                    <Route path="/" element={<LandingPage />} />
                </Route>

                {/* --- 메인 앱 그룹 (추후 연동) --- */}
                
                {/* 잘못된 경로는 랜딩으로 리다이렉트 */}
                <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
        </BrowserRouter>
    )
}