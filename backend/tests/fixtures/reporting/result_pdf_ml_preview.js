(() => {
    const report = window.REPORT_PREVIEW_DATA;
    if (!report) return;

    const definitions = [
        {
            key: 'model_1',
            model: 'MODEL 1',
            title: '지원유형 참고 분류',
            scope: '요청 사업의 내용이 과거 지원사업의 어느 지원유형과 가까운지 참고할 때 사용합니다.',
        },
        {
            key: 'model_2',
            model: 'MODEL 2',
            title: '지원금액 참고 예측',
            scope: '과거 비교군을 기준으로 지원금액 수준을 검토할 때 참고합니다.',
        },
        {
            key: 'model_3',
            model: 'MODEL 3',
            title: '비교군 이례성 분석',
            scope: '과거 유사 사업의 일반적인 패턴과 차이가 있는 항목을 확인할 때 참고합니다.',
        },
    ];
    const fallback = '현재 제공할 수 있는 참고 결과가 없습니다.';
    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    };

    // 기존 mockup에서는 ML 문구가 SIM 카드 안에 있었다. 해당 상자만 제거하고
    // 세 핵심 점검과 동등한 번호를 갖지 않는 독립 참고 블록으로 다시 만든다.
    const mainSections = document.querySelectorAll('main > section');
    const simSummary = mainSections[2];
    const oldMlList = simSummary?.querySelector('.mt-4 ul');
    const oldMlBox = oldMlList?.closest('.mt-4');
    oldMlBox?.remove();

    if (simSummary) {
        const summary = el('section', 'report-ml-summary');
        const head = el('div', 'report-ml-summary-head');
        const headingText = el('div');
        headingText.append(
            el('h2', '', '머신러닝 참고 결과'),
            el('p', '', '과거 사업 데이터를 기반으로 산출한 분류·예측·이례성 결과입니다.'),
        );
        head.append(headingText, el('span', 'report-reference-chip', '참고 정보'));
        summary.append(head);
        const grid = el('div', 'report-ml-summary-grid');
        definitions.forEach(definition => {
            const card = el('article', 'report-ml-summary-card');
            card.append(
                el('span', 'report-ml-model', definition.model),
                el('strong', '', definition.title),
                el('p', '', report.ml?.[definition.key]?.message || fallback),
            );
            grid.append(card);
        });
        summary.append(
            grid,
            el('p', 'report-reference-notice', '※ 과거 사업 데이터를 기반으로 한 참고 결과입니다.'),
        );
        simSummary.insertAdjacentElement('afterend', summary);
    }

    // 상세부에서는 번호가 붙은 DETAIL과 구분해 REFERENCE로 표시한다.
    const simDetail = [...document.querySelectorAll('.report-detail-section')]
        .find(section => section.querySelector('.report-section-kicker')?.textContent === 'DETAIL 03');
    if (!simDetail) return;
    const reference = el('section', 'report-detail-section');
    reference.append(
        el('p', 'report-section-kicker', 'REFERENCE'),
        el('h2', 'report-section-title', '머신러닝 참고 결과'),
        el('p', 'report-section-description', '머신러닝 모델이 과거 사업 데이터를 바탕으로 산출한 보조 정보입니다. 공식 점검 결과와 분리하여 해석해야 합니다.'),
    );
    const detailGrid = el('div', 'report-ml-detail-grid');
    definitions.forEach(definition => {
        const card = el('article', 'report-ml-detail-card');
        card.append(
            el('span', 'report-ml-model', definition.model),
            el('h3', '', definition.title),
            el('p', 'report-ml-detail-message', report.ml?.[definition.key]?.message || fallback),
            el('p', 'report-ml-scope', `활용 범위 · ${definition.scope}`),
        );
        detailGrid.append(card);
    });
    reference.append(
        detailGrid,
        el('p', 'report-reference-notice', '※ 모델 결과는 입력 자료와 학습 비교군의 범위에 영향을 받으며, 적합·부적합 또는 승인 여부를 의미하지 않습니다.'),
    );
    simDetail.insertAdjacentElement('afterend', reference);
})();
