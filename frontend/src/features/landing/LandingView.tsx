import Button from '../../components/common/Button'
import Card from '../../components/common/Card'

interface LandingViewProps {
    onLoginClick: () => void
}

export default function LandingView({ onLoginClick }: LandingViewProps) {
    return (
        <main className="flex-grow flex flex-col">
            {/* Hero Section */}
            <section
                className="flex-grow flex flex-col justify-center items-center px-gutter py-[80px] bg-surface-container-low text-center bg-cover bg-center"
                style={{
                    backgroundImage: `linear-gradient(rgba(0, 32, 69, 0.7), rgba(0, 32, 69, 0.7)), url('/images/bg_landing.png')`
                }}
            >
                <div className="max-w-3xl space-y-lg">
                    <h1 className="font-display-lg text-display-lg text-primary-container">
                        <span className="text-on-primary">
                            중소기업 지원사업 사전검토 서비스, Pre-review
                        </span>
                    </h1>
                    <p className="font-title-sm text-title-sm text-secondary max-w-2xl mx-auto">
                        <span className="text-primary-fixed">
                            복잡한 지원사업 신청, AI 기술을 통해 정확하고 신속하게 사전 검토를 받아보세요.<br />
                            성공적인 사업 참여를 위한 첫 걸음입니다.
                        </span>
                    </p>
                    <div className="pt-xl flex gap-md justify-center">
                        <Button 
                            onClick={onLoginClick} 
                            variant="secondary" 
                            className="px-[32px] py-[12px] bg-surface text-primary hover:bg-surface-container-high shadow-md"
                        >
                            Login
                        </Button>
                    </div>
                </div>
            </section>

            {/* Features Section */}
            <section className="py-[80px] px-gutter max-w-container-max mx-auto w-full">
                <div className="text-center mb-[60px]">
                    <h2 className="font-headline-md text-headline-md text-primary-container">주요 기능</h2>
                    <p className="text-secondary mt-sm font-body-md">신뢰성 높은 데이터 기반의 분석 시스템을 제공합니다.</p>
                </div>
                <div className="flex flex-col md:flex-row justify-center gap-lg md:items-stretch">
                    {/* Feature 1 - 아토믹 Card 컴포넌트 적용 */}
                    <Card className="hover:shadow-lg transition-shadow w-full md:max-w-[400px] flex flex-col">
                        <div className="w-12 h-12 rounded-full bg-primary-container text-on-primary flex items-center justify-center mb-lg">
                            <span className="material-symbols-outlined">analytics</span>
                        </div>
                        <h3 className="font-title-sm text-title-sm text-primary mb-sm">AI 자동 분석</h3>
                        <p className="text-secondary font-body-md text-body-md">
                            제출된 서류와 데이터를 기반으로 인공지능 알고리즘이 지원사업 요건 부합 여부를 신속하게 분석합니다. 주관적인 판단을 배제하고 객관적인 지표를 산출합니다.
                        </p>
                    </Card>

                    {/* Feature 2 - 아토믹 Card 컴포넌트 적용 */}
                    <Card className="hover:shadow-lg transition-shadow w-full md:max-w-[400px] flex flex-col">
                        <div className="w-12 h-12 rounded-full bg-primary-container text-on-primary flex items-center justify-center mb-lg">
                            <span className="material-symbols-outlined">compare_arrows</span>
                        </div>
                        <h3 className="font-title-sm text-title-sm text-primary mb-sm">상세 비교 리포트</h3>
                        <p className="text-secondary font-body-md text-body-md">
                            유사 기업군 및 성공 사례와의 상세 비교 리포트를 제공하여, 신청 기업의 상대적인 강점과 약점을 파악하고 경쟁력을 높일 수 있도록 지원합니다.
                        </p>
                    </Card>
                </div>
            </section>
        </main>
    )
}