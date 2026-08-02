// 法律分析结构化渲染组件
// 设计：低饱和专业配色，单栏正文，渐进展开
// 由 app.js 在 final_output 事件携带 legal_answer_v1 时调用

const LEGAL_COLORS = {
  primary: '#1F4B7A',
  success: '#287A5B',
  warning: '#B7791F',
  danger: '#B42318',
  inferred: '#6B5CA5',
  neutral: '#667085',
};

const FACT_STATUS_META = {
  confirmed: { label: '已确认', color: LEGAL_COLORS.success, icon: '✓' },
  claimed:   { label: '待核实', color: LEGAL_COLORS.neutral, icon: '○' },
  inferred:  { label: '系统推断', color: LEGAL_COLORS.inferred, icon: '△' },
  missing:   { label: '需补充', color: LEGAL_COLORS.warning, icon: '!' },
};

const RISK_RATING_META = {
  high:   { label: '高风险', color: LEGAL_COLORS.danger },
  medium: { label: '中风险', color: LEGAL_COLORS.warning },
  low:    { label: '低风险', color: LEGAL_COLORS.success },
};

function esc(text) {
  if (text == null) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderLegalAnswer(answer) {
  if (!answer || answer.schema_version !== 'legal_answer_v1') return '';
  const parts = [
    renderMeta(answer.meta),
    renderExecutiveSummary(answer.executive_summary),
    renderFacts(answer.facts || []),
    renderIssues(answer.issues || []),
    renderEvidence(answer.evidence || []),
    renderRisks(answer.risks || []),
    renderActionPlan(answer.action_plan || []),
    renderCitations(answer.citations || []),
    renderUncertainties(answer.uncertainties || []),
    renderDisclaimer(answer.disclaimer),
  ];
  return '<div class="legal-answer">' + parts.join('') + '</div>';
}

function renderMeta(meta) {
  if (!meta) return '';
  const risk = RISK_RATING_META[meta.risk_level] || RISK_RATING_META.medium;
  return `
    <div class="la-meta">
      <h1>法律分析意见</h1>
      <div class="la-meta-grid">
        <span>案件类型：<strong>${esc(meta.case_type)}</strong></span>
        <span>适用法域：<strong>${esc(meta.jurisdiction)}</strong></span>
        <span>法律适用时间：<strong>${esc(meta.law_as_of_date)}</strong></span>
        <span>风险等级：<strong style="color:${risk.color}">${risk.label}</strong></span>
        <span>材料完整度：<strong>${esc(meta.material_completeness)}</strong></span>
      </div>
    </div>`;
}

function renderExecutiveSummary(summary) {
  if (!summary) return '';
  const reasons = (summary.key_reasons || []).map(r => `<li>${esc(r)}</li>`).join('');
  return `
    <section class="la-section">
      <h2>核心结论</h2>
      <p class="la-conclusion">${esc(summary.conclusion)}</p>
      ${reasons ? `<ul class="la-reasons">${reasons}</ul>` : ''}
      <p class="la-uncertainty-main">主要不确定点：${esc(summary.main_uncertainty)}</p>
    </section>`;
}

function renderFacts(facts) {
  if (!facts || !facts.length) return '';
  const items = facts.map(f => {
    const meta = FACT_STATUS_META[f.status] || FACT_STATUS_META.claimed;
    return `
      <li class="la-fact" style="border-left:3px solid ${meta.color}">
        <span class="la-fact-icon" style="color:${meta.color}">${meta.icon}</span>
        <span class="la-fact-tag" style="color:${meta.color}">${meta.label}</span>
        <span class="la-fact-content">${esc(f.content)}</span>
      </li>`;
  }).join('');
  return `
    <section class="la-section">
      <h2>事实基础</h2>
      <ul class="la-facts">${items}</ul>
    </section>`;
}

function renderIssues(issues) {
  if (!issues || !issues.length) return '';
  const blocks = issues.map(issue => {
    const counter = (issue.counterarguments || []).map(c => `<li>${esc(c)}</li>`).join('');
    return `
      <div class="la-issue">
        <h3>${esc(issue.question)}</h3>
        <p><strong>初步结论：</strong>${esc(issue.conclusion)}</p>
        ${issue.analysis ? `<p><strong>适用分析：</strong>${esc(issue.analysis)}</p>` : ''}
        ${counter ? `<div class="la-counter"><strong>不利因素：</strong><ul>${counter}</ul></div>` : ''}
      </div>`;
  }).join('');
  return `
    <section class="la-section">
      <h2>争议焦点</h2>
      ${blocks}
    </section>`;
}

function renderEvidence(evidence) {
  if (!evidence || !evidence.length) return '';
  const rows = evidence.map(e => `
    <tr>
      <td>${esc(e.name)}</td>
      <td>${esc(e.purpose)}</td>
      <td>${esc(e.status)}</td>
      <td>${esc(e.probative_force)}</td>
      <td>${esc(e.next_step || '')}</td>
    </tr>`).join('');
  return `
    <section class="la-section">
      <h2>证据分析</h2>
      <table class="la-evidence-table">
        <thead><tr><th>证据</th><th>证明目的</th><th>状态</th><th>证明力</th><th>下一步</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function renderRisks(risks) {
  if (!risks || !risks.length) return '';
  const rows = risks.map(r => {
    const meta = RISK_RATING_META[r.rating] || RISK_RATING_META.medium;
    return `
      <tr>
        <td>${esc(r.dimension)}</td>
        <td style="color:${meta.color}">${meta.label}</td>
        <td>${esc(r.detail)}</td>
      </tr>`;
  }).join('');
  return `
    <section class="la-section">
      <h2>风险评估</h2>
      <table class="la-risk-table">
        <thead><tr><th>维度</th><th>等级</th><th>说明</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function renderActionPlan(plan) {
  if (!plan || !plan.length) return '';
  const phaseLabel = { immediate: '24小时内', short_term: '近期（3日内）', contingency: '协商失败后' };
  const grouped = {};
  plan.forEach(a => {
    const p = a.phase || 'short_term';
    if (!grouped[p]) grouped[p] = [];
    grouped[p].push(a);
  });
  const blocks = ['immediate', 'short_term', 'contingency']
    .filter(p => grouped[p])
    .map(p => {
      const items = grouped[p].map(a => `
        <li>
          <strong>${esc(a.description)}</strong>
          ${a.required_materials && a.required_materials.length ? `<br><small>所需材料：${a.required_materials.map(esc).join('、')}</small>` : ''}
          ${a.deadline ? `<br><small>截止：${esc(a.deadline)}</small>` : ''}
        </li>`).join('');
      return `<div class="la-action-phase"><h4>${phaseLabel[p] || p}</h4><ol>${items}</ol></div>`;
    }).join('');
  return `
    <section class="la-section">
      <h2>下一步行动</h2>
      ${blocks}
    </section>`;
}

function renderCitations(citations) {
  if (!citations || !citations.length) return '';
  const items = citations.map(c => `
    <details class="la-citation">
      <summary><strong>《${esc(c.full_name)}》${esc(c.article_number)}</strong></summary>
      <div class="la-citation-detail">
        <p>${esc(c.article_text)}</p>
        <p><small>效力状态：${esc(c.status)}　来源：${esc(c.official_source || '官方数据库')}</small></p>
      </div>
    </details>`).join('');
  return `
    <section class="la-section">
      <h2>法律依据</h2>
      ${items}
    </section>`;
}

function renderUncertainties(uncertainties) {
  if (!uncertainties || !uncertainties.length) return '';
  const items = uncertainties.map(u => `
    <li>
      <strong>${esc(u.description)}</strong>
      <br><small>影响：${esc(u.impact)}</small>
      ${u.resolution ? `<br><small>建议：${esc(u.resolution)}</small>` : ''}
    </li>`).join('');
  return `
    <section class="la-section la-uncertainties">
      <h2>不确定性说明</h2>
      <ul>${items}</ul>
    </section>`;
}

function renderDisclaimer(disclaimer) {
  return `
    <section class="la-section la-disclaimer">
      <p>${esc(disclaimer || '')}</p>
    </section>`;
}

window.renderLegalAnswer = renderLegalAnswer;
