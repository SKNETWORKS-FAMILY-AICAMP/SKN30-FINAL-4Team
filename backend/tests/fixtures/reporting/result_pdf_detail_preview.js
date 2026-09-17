(() => {
    const report = window.REPORT_PREVIEW_DATA;
    if (!report) return;

    const cplLabels = {
        'CPL-01': '요청유형 체크값', 'CPL-02': '사업 목적 · 목표', 'CPL-03': '연차별 · 내역사업별 추진계획',
        'CPL-04': '사업기간', 'CPL-05': '신설 · 변경 주요내용', 'CPL-06': '사업필요성 최소 논리구조',
        'CPL-07': '지원근거', 'CPL-08': '연계정책', 'CPL-09': '사업예산', 'CPL-10': '지원대상 · 지원조건',
        'CPL-11': '지원내용 · 지원규모', 'CPL-12': '수행기관 · 수행방식 · 수행체계', 'CPL-13': '기대효과 · 성과 관련 정보',
    };
    const fitLabels = {
        'FIT-1': '목적 ↔︎ 지원대상', 'FIT-2': '목적 ↔︎ 지원내용', 'FIT-3': '목적 ↔︎ 기대효과·성과지표',
        'FIT-4': '세부사업 ↔︎ 내역사업/내내역사업', 'FIT-5': '지원대상 ↔︎ 지원조건',
        'FIT-6': '수행기관 ↔︎ 역할·수행절차', 'FIT-7': '지원내용 ↔︎ 지원규모의 수치·조건',
    };
    const fieldLabels = {
        request_type: '요청 유형', purpose_goal: '사업 목적 · 목표', nodes: '사업 구조',
        implementation_plan: '연차별 · 내역사업별 추진계획', support_components: '세부 지원', program_period: '사업기간',
        legal_basis: '지원 근거', linked_policy: '연계 정책', total_budget: '사업예산',
        business_need: '사업 필요성', support_target: '지원 대상', support_targe: '지원 대상', eligibility_conditions: '지원 자격 · 조건',
        support_activities: '지원 활동', support_methods: '지원 방식', support_items: '지원 항목',
        support_content: '지원 내용', support_scale: '지원 규모', cost_sharing: '자부담 · 비용 분담',
        beneficiary: '실제 수혜자', participation_requirements: '참여 요건',
        delivery_relations: '수행기관 · 역할', delivery_methods: '수행 방식',
        expected_effect: '기대효과', performance_indicator: '성과지표',
        applicant_eligibility: '신청 자격', exclusions: '지원 제외 대상',
    };
    const getFieldLabel = label => {
        const raw = String(label || '').trim();
        const key = raw.split('.').pop();
        if (fieldLabels[key]) return fieldLabels[key];
        return /^[a-z][a-z0-9_]*$/i.test(key) ? '확인 항목' : raw || '항목';
    };
    const cplStatus = {
        confirmed: ['확인됨', 'good'], needs_confirmation: ['확인 필요', 'cpl-warning'],
        no_content: ['내용 없음', 'danger'], not_applicable: ['해당 없음', 'muted'],
    };
    const fitStatus = {
        FIT: ['적합', 'good'], NEEDS_REVIEW: ['검토 필요', 'warning'],
        CONFLICT: ['충돌', 'danger'], INSUFFICIENT: ['근거 부족', 'warning'], NOT_APPLICABLE: ['해당 없음', 'muted'],
    };
    const simStatus = {
        similar: ['유사', 'good'], partial: ['일부 유사', 'warning'],
        different: ['상이', 'danger'], insufficient: ['근거 부족', 'muted'],
    };
    const axisLabels = { purpose: '사업 목적', target: '지원 대상', support: '지원 내용 · 수단', delivery: '수행 · 전달체계' };
    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    };
    const safeHttpUrl = value => {
        try {
            const url = new URL(value);
            return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
        } catch {
            return null;
        }
    };
    const statusBadge = (definition, value) => {
        const [label, tone] = definition[value] || ['알 수 없음', 'muted'];
        return el('span', `report-status ${tone}`, label);
    };
    const sectionHeading = (kicker, title, description) => {
        const fragment = document.createDocumentFragment();
        fragment.append(el('p', 'report-section-kicker', kicker), el('h2', 'report-section-title', title), el('p', 'report-section-description', description));
        return fragment;
    };

    const hasReportPayload = report.schema_version === 'report_payload_v1' && Array.isArray(report.sim_details);
    if (!hasReportPayload) throw new Error("Invalid report payload");
    const evidenceById = new Map((report.evidences || []).map(item => [item.evidence_id, item]));

    const evidenceText = evidence => evidence?.excerpt?.trim() || evidence?.raw_value || '연결된 근거 내용이 없습니다.';
    const evidenceBlock = (ids, pool, note) => {
        const wrapper = document.createDocumentFragment();
        const items = ids.map(id => pool.get(id)).filter(Boolean);
        if (!items.length) return wrapper;
        wrapper.append(el('p', 'report-detail-label', note));
        items.forEach(item => wrapper.append(el('blockquote', 'report-evidence', evidenceText(item))));
        return wrapper;
    };

    const container = document.querySelector('.pdf-container');
    const footer = container?.querySelector('footer');
    if (!container || !footer) return;

    const cplSection = el('section', 'report-detail-section');
    cplSection.append(sectionHeading('DETAIL 01', '요청자료 완전성 상세', '각 점검 항목에서 확인된 값, 판단 사유 및 요청서 원문 근거를 제공합니다.'));
    report.cpl.items.forEach(item => {
        const cardTone = item.status === 'no_content' ? ' is-danger' : item.status === 'needs_confirmation' ? ' is-warning' : '';
        const card = el('article', `report-detail-card${cardTone}`);
        const heading = el('div', 'report-card-heading');
        const titleBox = el('div');
        titleBox.append(el('span', 'report-card-code', item.code), el('h3', 'report-card-title', cplLabels[item.code] || item.code));
        heading.append(titleBox, statusBadge(cplStatus, item.status));
        card.append(heading, el('p', 'report-detail-label', '판단 요약'), el('p', 'report-detail-text', item.summary || '표시할 요약이 없습니다.'));
        if (item.detail?.values?.length) {
            card.append(el('p', 'report-detail-label', '확인된 값'));
            const table = el('table', 'report-values');
            const body = el('tbody');
            item.detail.values.forEach(value => {
                const row = el('tr');
                row.append(el('th', '', getFieldLabel(value.label)), el('td', '', value.value));
                body.append(row);
            });
            table.append(body);
            card.append(table);
        }
        if (item.detail?.reason && item.detail.reason !== item.summary) card.append(el('p', 'report-detail-label', '판단 사유'), el('p', 'report-detail-text', item.detail.reason));
        card.append(evidenceBlock(item.detail?.evidence_ids || [], evidenceById, '대표 원문 근거'));
        cplSection.append(card);
    });

    const fitSection = el('section', 'report-detail-section');
    fitSection.append(sectionHeading('DETAIL 02', '내부 정합성 상세', '비교 관계의 양쪽 값과 판단 사유, 각각의 원문 근거를 함께 제공합니다.'));
    report.fit.items.forEach(item => {
        const cardTone = item.status === 'CONFLICT' ? ' is-danger' : item.status === 'NEEDS_REVIEW' ? ' is-warning' : '';
        const card = el('article', `report-detail-card${cardTone}`);
        const heading = el('div', 'report-card-heading');
        const titleBox = el('div');
        titleBox.append(el('span', 'report-card-code', item.code), el('h3', 'report-card-title', fitLabels[item.code] || item.code));
        heading.append(titleBox, statusBadge(fitStatus, item.status));
        const compare = el('div', 'report-compare-grid');
        const left = el('div', 'report-compare-side');
        left.append(el('strong', '', '비교 기준'), el('p', '', item.detail?.left?.value_summary || '비교 값 없음'));
        const right = el('div', 'report-compare-side');
        right.append(el('strong', '', '비교 대상'), el('p', '', item.detail?.right?.value_summary || '비교 값 없음'));
        compare.append(left, el('div', 'report-compare-arrow', '↔'), right);
        card.append(heading, compare, el('p', 'report-detail-label', '판단'), el('p', 'report-detail-text', item.detail?.reason || item.summary || '표시할 판단이 없습니다.'));
        card.append(evidenceBlock(item.detail?.left?.evidence_ids || [], evidenceById, '비교 기준 근거'));
        card.append(evidenceBlock(item.detail?.right?.evidence_ids || [], evidenceById, '비교 대상 근거'));
        fitSection.append(card);
    });

    const simSection = el('section', 'report-detail-section');
    simSection.append(sectionHeading('DETAIL 03', '유사 사업 후보 상세', '후보별 공고 정보와 목적·대상·지원 내용·수행 체계의 비교 결과를 제공합니다.'));
    report.sim_details.forEach((candidate, candidateIndex) => {
        const article = el('article', `report-detail-card report-candidate${candidateIndex === 0 ? ' report-candidate-first' : ''}`);
        const heading = el('div', 'report-card-heading');
        const titleBox = el('div');
        titleBox.append(el('span', 'report-card-code', `유사 후보 ${candidate.rank}`), el('h3', 'report-card-title', candidate.metadata.title || '사업명 미확인'));
        heading.append(titleBox, statusBadge(simStatus, candidate.comparison.status));
        article.append(heading, el('p', 'report-detail-text', candidate.comparison.summary || '표시할 비교 요약이 없습니다.'));
        const meta = el('div', 'report-candidate-meta');
        [['지원 분야', candidate.metadata.support_field], ['소관 부처', candidate.metadata.ministry], ['수행 기관', candidate.metadata.executing_agency], ['신청 기간', candidate.metadata.apply_period], ['등록일', candidate.metadata.registered_at], ['원문 링크', candidate.metadata.source_url]].forEach(([label, value]) => {
            const item = el('div', 'report-meta-item');
            const sourceUrl = label === '원문 링크' ? safeHttpUrl(value) : null;
            const valueNode = sourceUrl ? el('a', 'report-source-link', '바로가기') : el('strong', '', value || '-');
            if (sourceUrl) valueNode.href = sourceUrl;
            item.append(el('span', '', label), valueNode);
            meta.append(item);
        });
        article.append(meta);
        const candidateEvidence = new Map(candidate.evidences.map(item => [item.evidence_id, item]));
        Object.entries(candidate.axes).forEach(([axisName, axis]) => {
            const axisCard = el('div', 'report-axis');
            const axisHead = el('div', 'report-axis-head');
            axisHead.append(el('h4', '', `${axis.code} ${axisLabels[axisName]}`), statusBadge(simStatus, axis.status));
            axisCard.append(axisHead, el('p', 'report-detail-text', axis.reason || axis.summary));
            const points = el('div', 'report-point-grid');
            [['공통점', axis.common_points], ['차이점', axis.differences]].forEach(([label, values]) => {
                const box = el('div', 'report-point-box');
                box.append(el('strong', '', label));
                const list = el('ul');
                (values.length ? values : ['확인된 내용이 없습니다.']).forEach(value => list.append(el('li', '', value)));
                box.append(list);
                points.append(box);
            });
            axisCard.append(points);
            axisCard.append(evidenceBlock(axis.request_evidence_ids, candidateEvidence, '요청서 근거'));
            axisCard.append(evidenceBlock(axis.existing_evidence_ids, candidateEvidence, '기존 공고 근거'));
            article.append(axisCard);
        });
        simSection.append(article);
    });


    container.insertBefore(cplSection, footer);
    container.insertBefore(fitSection, footer);
    container.insertBefore(simSection, footer);
})();
