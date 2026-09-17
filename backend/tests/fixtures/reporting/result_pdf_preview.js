(() => {
    const report = window.REPORT_PREVIEW_DATA;
    if (!report) return;

    const cplLabels = {
        'CPL-01': '요청유형 체크값', 'CPL-02': '사업 목적 · 목표',
        'CPL-03': '연차별 · 내역사업별 추진계획', 'CPL-04': '사업기간',
        'CPL-05': '신설 · 변경 주요내용', 'CPL-06': '사업필요성 최소 논리구조',
        'CPL-07': '지원근거', 'CPL-08': '연계정책', 'CPL-09': '사업예산',
        'CPL-10': '지원대상 · 지원조건', 'CPL-11': '지원내용 · 지원규모',
        'CPL-12': '수행기관 · 수행방식 · 수행체계', 'CPL-13': '기대효과 · 성과 관련 정보',
    };
    const fitLabels = {
        'FIT-1': '목적 ↔︎ 지원대상', 'FIT-2': '목적 ↔︎ 지원내용',
        'FIT-3': '목적 ↔︎ 기대효과·성과지표', 'FIT-4': '세부사업 ↔︎ 내역사업/내내역사업',
        'FIT-5': '지원대상 ↔︎ 지원조건', 'FIT-6': '수행기관 ↔︎ 역할·수행절차',
        'FIT-7': '지원내용 ↔︎ 지원규모의 수치·조건',
    };
    const cplStatuses = {
        confirmed: ['확인됨', 'check_circle', 'status-good'],
        needs_confirmation: ['확인 필요', 'more_horiz', 'status-cpl-warning'],
        no_content: ['내용 없음', 'error_outline', 'status-danger'],
        not_applicable: ['해당 없음', 'remove_circle_outline', 'status-muted'],
    };
    const fitStatuses = {
        FIT: ['적합', 'border-emerald-200 bg-emerald-50/30', 'status-good'],
        NEEDS_REVIEW: ['검토 필요', 'border-amber-200 bg-amber-50/30', 'status-warning'],
        CONFLICT: ['충돌', 'border-red-200 bg-red-50/30', 'status-danger'],
        INSUFFICIENT: ['근거 부족', 'border-slate-200 bg-slate-50/50', 'status-warning'],
        NOT_APPLICABLE: ['해당 없음', 'border-slate-200 bg-slate-50/50', 'status-muted'],
    };
    const simStatuses = {
        similar: ['유사', 'status-good'],
        partial: ['일부 유사', 'status-warning'],
        different: ['상이', 'status-danger'],
        insufficient: ['근거 부족', 'status-muted'],
    };
    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    };

    const sections = document.querySelectorAll('main > section');
    const title = document.querySelector('header h1 + p');
    if (title) title.textContent = `대상 과제 : ${report.case.program_name || report.case.original_filename || '분석 리포트'}`;
    const metaValues = document.querySelectorAll('header .mt-5 .font-bold');
    if (metaValues[0]) metaValues[0].textContent = new Intl.DateTimeFormat('ko-KR', { dateStyle: 'long', timeStyle: 'short' }).format(new Date(report.case.completed_at));

    const cplGrid = sections[0]?.querySelector('.grid');
    if (cplGrid) {
        cplGrid.replaceChildren(...report.cpl.items.map((item, index) => {
            const row = el('div', `flex items-center justify-between p-2.5 bg-slate-50 rounded-lg border border-slate-200${index === report.cpl.items.length - 1 && index % 2 === 0 ? ' md:col-span-2' : ''}`);
            row.append(el('span', 'font-medium text-slate-700', cplLabels[item.code] || item.code));
            const [label, icon, tone] = cplStatuses[item.status] || ['알 수 없음', 'help_outline', 'status-muted'];
            const badge = el('span', `inline-flex items-center gap-1 font-bold px-2 py-0.5 rounded border ${tone}`);
            badge.title = item.summary || '';
            badge.append(el('span', 'material-symbols-outlined text-sm', icon), document.createTextNode(` ${label}`));
            row.append(badge);
            return row;
        }));
        const badge = sections[0].querySelector('.border-b + .grid')?.previousElementSibling;
        const confirmed = report.cpl.items.filter(item => item.status === 'confirmed').length;
        const headerBadge = sections[0].querySelector('.flex.items-center.justify-between .bg-slate-100');
        if (headerBadge) headerBadge.textContent = `${confirmed}/${report.cpl.items.length}개 확인`;
    }

    const fitGrid = sections[1]?.querySelector('.grid');
    if (fitGrid) fitGrid.replaceChildren(...report.fit.items.map(item => {
        const [label, cardTone, badgeTone] = fitStatuses[item.status] || ['알 수 없음', 'border-slate-200 bg-slate-50/50', 'status-muted'];
        const card = el('div', `border rounded-lg p-3 ${cardTone}`);
        const head = el('div', 'flex items-center justify-between mb-1.5');
        head.append(el('span', 'font-bold text-slate-800', fitLabels[item.code] || item.code));
        head.append(el('span', `text-[11px] font-bold px-2 py-0.5 rounded border ${badgeTone}`, label));
        card.append(head, el('p', 'text-slate-600 leading-relaxed text-[11px]', item.summary || item.detail?.reason || '표시할 비교 설명이 없습니다.'));
        return card;
    }));

    const simSection = sections[2];
    const simDescription = simSection?.querySelector('h2 + p');
    if (simDescription) simDescription.textContent = report.sim.summary;
    const simCount = simSection?.querySelector('.flex.items-center.justify-between .bg-slate-100');
    if (simCount) simCount.textContent = `유사 항목 ${report.sim.candidates.length}건 식별`;
    const simBody = simSection?.querySelector('tbody');
    if (simBody) simBody.replaceChildren(...report.sim.candidates.map(candidate => {
        const row = el('tr', 'break-inside-avoid');
        row.append(el('td', 'py-3 px-4 font-bold text-slate-800', candidate.title || '사업명 미확인'));
        row.append(el('td', 'py-3 px-4 text-slate-600', candidate.comparison_summary || '비교 요약이 없습니다.'));
        const statusCell = el('td', 'py-3 px-3 text-center');
        const [label, tone] = simStatuses[candidate.comparison_status] || ['알 수 없음', 'status-muted'];
        statusCell.append(el('span', `inline-block px-2 py-0.5 rounded border text-[11px] font-medium ${tone}`, label));
        row.append(statusCell);
        return row;
    }));

    const mlList = simSection?.querySelector('.mt-4 ul');
    if (mlList) {
        const messages = Object.values(report.ml || {}).map(model => model?.message).filter(Boolean);
        mlList.replaceChildren(...messages.map(message => el('li', '', message)));
    }
})();
