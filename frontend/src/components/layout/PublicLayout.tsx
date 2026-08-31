import { Outlet } from 'react-router-dom'
import Header from './shared/Header'
import Footer from './shared/Footer'

export default function PublicLayout() {
    return (
        <div className="w-full min-h-screen pt-18 flex flex-col bg-background text-on-background">
            <Header />
            
            <main className="flex-1 flex flex-col w-full">
                <Outlet />
            </main>

            <Footer />
        </div>
    )
}