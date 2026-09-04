import { Link } from 'react-router-dom'
import { HIGHLIGHTS_DATA, WORKFLOW_DATA, CAPABILITIES_DATA } from './landingData'

export default function LandingView() {
    return (
        <>
            {/* Hero Section */}
            <section
                className="flex-grow flex flex-col justify-center items-center px-6 py-20 md:py-28 text-center text-white"
                style={{
                    background: 'linear-gradient(rgba(0, 32, 69, 0.85), rgba(0, 32, 69, 0.85)), url(/images/bg_landing.avif) center/cover no-repeat'
                }}
            >
                <div className="relative max-w-5xl mx-auto text-center flex flex-col items-center">
                    <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-white/10 backdrop-blur-sm border border-white/20 text-xs md:text-sm font-semibold text-blue-200 mb-6 shadow-inner">
                        <span className="material-symbols-outlined text-base text-blue-300">verified_user</span>
                        <span>중소기업 지원사업 사전협의 지원 시스템 | Pre-review</span>
                    </div>
                    <h1 className="text-3xl md:text-[42px] leading-tight md:leading-[1.3] font-extrabold tracking-tight text-white mb-6">
                        정부 지원사업 사전협의와 기획안 검토,
                        <br className="hidden sm:inline" />
                        <span className="text-blue-300">AI 기반 5대 점검 체계</span>
                        로 더 신속하고 정확하게
                    </h1>
                    <p className="text-base md:text-lg text-slate-300 font-normal max-w-3xl leading-relaxed mb-10">
                        요청서 완전성부터 내부 논리 정합성, 기존 사업 유사·중복성까지 사전 스크리닝을 자동화하여
                        <br className="hidden sm:inline" />
                        중앙부처 및 지자체 담당자의 지원사업 기획·사전검토 업무 신뢰도를 극대화합니다.
                    </p>
                    <div className="flex flex-col sm:flex-row items-center gap-4 w-full sm:w-auto">
                        <Link to="/login" className="w-full sm:w-auto px-8 py-3.5 bg-white text-[#002045] hover:bg-slate-100 font-bold rounded text-base shadow-lg transition-all inline-flex items-center justify-center gap-2 no-underline">
                            <span>사전검토 시작하기</span>
                            <span className="material-symbols-outlined text-lg">arrow_forward</span>
                        </Link>
                    </div>
                </div>
            </section>

            {/* Highlights Section */}
            <section className="bg-white border-b border-slate-200">
                <div className="max-w-7xl mx-auto px-6 py-10">
                    <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-slate-200">
                        {HIGHLIGHTS_DATA.map((item, index) => (
                            <div key={index} className="py-4 md:py-2 md:px-8 text-center">
                                <div className="text-3xl md:text-4xl font-extrabold text-[#002045] tracking-tight">{item.title}</div>
                                <div className="text-sm md:text-base font-bold text-slate-800 mt-1">{item.subtitle}</div>
                                <p className="text-xs text-slate-500 mt-1">{item.description}</p>
                            </div>
                        ))}
                    </div>
                </div>
            </section>

            {/* Workflow Section */}
            <section className="py-20 px-6 max-w-7xl mx-auto w-full" id="process">
                <div className="text-center max-w-2xl mx-auto mb-16">
                    <div className="inline-block text-xs font-extrabold text-[#002045] bg-blue-50 border border-blue-200 px-3 py-1 rounded mb-3">WORKFLOW</div>
                    <h2 className="text-2xl md:text-3xl font-extrabold text-slate-900">핵심 사전검토 프로세스 3단계</h2>
                    <p className="text-slate-600 mt-2 text-sm md:text-base">제출 서류 업로드부터 공문용 표준 사전검토서 산출까지 원스톱으로 지원합니다.</p>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-8 relative">
                    {WORKFLOW_DATA.map((workflow, index) => (
                        <div key={index} className="bg-white p-8 rounded-lg border border-slate-200 shadow-sm relative flex flex-col items-start hover:border-slate-400 transition-colors">
                            <div className="w-12 h-12 rounded-lg bg-[#002045] text-white flex items-center justify-center font-bold text-xl mb-6 shadow-sm">{workflow.number}</div>
                            <div className="text-xs font-bold text-blue-700 uppercase tracking-wider mb-1">STEP {workflow.step}</div>
                            <h3 className="text-xl font-bold text-slate-900 mb-3">{workflow.title}</h3>
                            <p className="text-slate-600 text-sm leading-relaxed mb-4">{workflow.description}</p>
                            <div className="mt-auto pt-4 border-t border-slate-100 w-full flex items-center text-xs text-slate-500 gap-1">
                                <span className="material-symbols-outlined text-[16px] text-slate-400">{workflow.footerIcon}</span>
                                <span>{workflow.footerText}</span>
                            </div>
                        </div>
                    ))}
                </div>
            </section>

            {/* Core Capabilities / Framework Section */}
            <section className="bg-[#f1f5f9] py-20 px-6 border-y border-slate-200" id="framework">
                <div className="max-w-7xl mx-auto w-full">
                    <div className="text-center max-w-3xl mx-auto mb-16">
                        <div className="inline-block text-xs font-extrabold text-[#002045] bg-white border border-slate-300 px-3 py-1 rounded mb-3">CORE CAPABILITIES</div>
                        <h2 className="text-2xl md:text-3xl font-extrabold text-slate-900">신뢰형 사전검토를 위한 핵심 점검 체계</h2>
                        <p className="text-slate-600 mt-2 text-sm md:text-base">사전협의 검토 과정에서 발생할 수 있는 오류와 행정 비효율을 사전에 원천 차단합니다.</p>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                        {CAPABILITIES_DATA.map((capability, index) => (
                            <div key={index} className="bg-white p-7 rounded-lg border border-slate-200 shadow-sm flex gap-5 hover:border-slate-400 transition-all">
                                <div className="flex-shrink-0 w-12 h-12 rounded-lg bg-[#002045]/5 text-[#002045] border border-[#002045]/15 flex items-center justify-center">
                                    <span className="material-symbols-outlined text-2xl">{capability.icon}</span>
                                </div>
                                <div>
                                    <div className="text-xs font-bold text-slate-400 mb-1">검증 체계 {capability.index}</div>
                                    <h3 className="text-lg font-bold text-slate-900 mb-2">{capability.title}</h3>
                                    <p className="text-slate-600 text-sm leading-relaxed mb-3">{capability.description}</p>
                                    <div className="inline-flex items-center text-xs font-semibold text-[#002045] gap-1">
                                        <span className="material-symbols-outlined text-[16px]">{capability.footerIcon}</span>
                                        <span>{capability.footerText}</span>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            </section>

            {/* CTA Banner */}
            <section className="bg-[#002045] text-white py-14 px-6">
                <div className="max-w-5xl mx-auto flex flex-col md:flex-row items-center justify-between gap-6 text-center md:text-left">
                    <div>
                        <h3 className="text-xl md:text-2xl font-bold tracking-tight mb-2">중소기업 지원사업 사전협의를 지금 시작하십시오</h3>
                        <p className="text-slate-300 text-sm">기관 인증 후 별도의 소프트웨어 설치 없이 웹 환경에서 즉시 사용 가능합니다.</p>
                    </div>
                    <div className="flex items-center gap-3">
                        <Link to="/login" className="px-6 py-3 bg-white text-[#002045] hover:bg-slate-100 font-bold rounded text-sm transition-all shadow no-underline">
                            사전검토 시스템 로그인
                        </Link>
                    </div>
                </div>
            </section>
        </>
    )
}