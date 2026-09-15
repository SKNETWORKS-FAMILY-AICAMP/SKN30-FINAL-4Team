import Logo from '../../common/Logo'

export default function Header() {
    return (
        <header className="fixed top-0 w-full py-md z-50 bg-surface border-b border-outline-variant shadow-sm font-body-md text-body-md">
            <div className="px-gutter max-w-container-max mx-auto">
                <Logo size="md" />
            </div>
        </header>
    )
}