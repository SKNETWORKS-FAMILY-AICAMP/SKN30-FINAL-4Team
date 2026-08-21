import { Outlet } from 'react-router-dom'
import Header from './shared/Header'
import Footer from './shared/Footer'

export default function PublicLayout() {
    return (
        <div className="pt-16 bg-background text-on-background min-h-screen flex flex-col w-full">
            <Header />
            
            <main className="flex-1 flex flex-col w-full">
                <Outlet />
            </main>

            <Footer />
        </div>
    )
}