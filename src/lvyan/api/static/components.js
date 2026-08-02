// 法律分析结构化渲染组件（语义类名版）
// 所有颜色由 legal-answer.css 的语义类控制，不写内联 style
// 由 app.js 在 final_output 事件携带 legal_answer_v1 时调用

const FACT_STATUS_META = {
  confirmed: { label: '已确认', cls: 'fact-confirmed', icon: '✓' },
  claimed:   { label: '待核实', cls: 'fact-claimed', icon: '○' },
  inferred:  { label: '系统推断', cls: 'fact-inferred', icon: '△' },
  missing:   { label: '需补充', cls: 'fact-missing', icon: '!' },
};

const RISK_RATING_META = {
  high:   { label: '高风险', cls: 'risk-high' },
  medium: { label: '中风险', cls: 'risk-medium' },
  low:    { label: '低风险', cls: 'risk-low' },
};

// P2：枚举中文化映射
const EVIDENCE_STATUS_CN = { provided: '已提供', missing: '未提供', partial: '部分提供' };
const PROBATIVE_FORCE_CN = { key: '关键', strong: '较强', medium: '一般', weak: '较弱', unevaluated: '待评估' };
const MATERIAL_COMPLETENESS_CN = { complete: '材料较完整', partial: '材料部分完整', insufficient: '材料不足' };
const CITATION_STATUS_CN = { effective: '现行有效', repealed: '已废止', not_yet_effective: '尚未生效', unknown: '状态未知' };
const CITATION_LEVEL_CN = { law: '法律', regulation: '行政法规', judicial_interpretation: '司法解释', guiding_case: '指导案例', reference_case: '参考案例', normative: '规范性文件' };

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
    renderReportHeader(answer.meta),
    renderSummaryGrid(answer.executive_summary, answer.risks || [], answer.meta),
    renderImmediateActions(answer.action_plan || []),
    renderFacts(answer.facts || []),
    renderIssues(answer.issues || []),
    renderEvidence(answer.evidence || []),
    renderRisks(answer.risks || []),
    renderFullActionPlan(answer.action_plan || []),
    renderCitations(answer.citations || []),
    renderUncertainties(answer.uncertainties || []),
    renderDisclaimer(answer.disclaimer),
  ];
  return '<div class="legal-answer">' + parts.join('') + '</div>';
}

function renderReportHeader(meta) {
  if (!meta) return '';
  const risk = RISK_RATING_META[meta.risk_level] || RISK_RATING_META.medium;
  const completeness = MATERIAL_COMPLETENESS_CN[meta.material_completeness] || meta.material_completeness || '';
  return `
    <div class="la-report-header">
      <div class="la-report-eyebrow">法律分析报告 <span class="risk-badge ${risk.cls}">${risk.label}</span></div>
      <h2 class="la-report-title">${esc(meta.title || '法律分析意见')}</h2>
      <div class="la-meta-grid">
        <span>${esc(meta.case_type || '')} · ${esc(meta.jurisdiction || '')}</span>
        <span>法律适用截至 ${esc(meta.law_as_of_date || '')}</span>
        <span>${esc(completeness)}</span>
      </div>
    </div>`;
}

function renderSummaryGrid(summary, risks, meta) {
  if (!summary) return '';
  const reasons = (summary.key_reasons || []).map(r => `<li>${esc(r)}</li>`).join('');
  const riskItems = (risks || []).slice(0, 4).map(r => {
    const m = RISK_RATING_META[r.rating] || RISK_RATING_META.medium;
    return `<div class="la-risk-panel-item">
      <div class="la-risk-panel-label">${esc(r.dimension)}</div>
      <div class="la-risk-panel-value"><span class="risk-badge ${m.cls}">${m.label}</span></div>
    </div>`;
  }).join('');
  return `
    <section class="la-section">
      <div class="la-summary-grid">
        <div>
          <h3>核心结论</h3>
          <p class="la-conclusion">${esc(summary.conclusion)}</p>
          ${reasons ? `<ul class="la-reasons">${reasons}</ul>` : ''}
        </div>
        <div class="la-risk-panel">
          ${riskItems}
          <div class="la-risk-panel-item">
            <div class="la-risk-panel-label">主要不确定点</div>
            <div class="la-risk-panel-value" style="font-size:13px;font-weight:400">${esc(summary.main_uncertainty)}</div>
          </div>
        </div>
      </div>
    </section>`;
}

function renderImmediateActions(plan) {
  const immediate = (plan || []).filter(a => a.phase === 'immediate').slice(0, 3);
  if (!immediate.length) return '';
  const items = immediate.map(a => `<li><strong>${esc(a.description)}</strong></li>`).join('');
  return `
    <section class="la-section">
      <h3>立即行动</h3>
      <ol class="la-reasons">${items}</ol>
    </section>`;
}

function renderFacts(facts) {
  if (!facts || !facts.length) return '';
  const items = facts.map(f => {
    const m = FACT_STATUS_META[f.status] || FACT_STATUS_META.claimed;
    return `<li class="la-fact ${m.cls}">
      <span class="la-fact-tag">${m.label}</span>
      <span class="la-fact-content">${esc(f.content)}</span>
    </li>`;
  }).join('');
  return `<section class="la-section"><h3>事实基础</h3><ul class="la-facts">${items}</ul></section>`;
}

function renderIssues(issues) {
  if (!issues || !issues.length) return '';
  const blocks = issues.map((issue, idx) => {
    const counter = (issue.counterarguments || []).map(c => `<li>${esc(c)}</li>`).join('');
    const rules = (issue.rules || []).map(rid => `<span class="la-rule-chip">${esc(rid)}</span>`).join('');
    const facts = (issue.supporting_facts || []).map(fid => `<span class="la-rule-chip">${esc(fid)}</span>`).join('');
    const openAttr = idx === 0 ? ' open' : '';
    return `<div class="la-issue${openAttr}">
      <button type="button" class="la-issue-toggle" onclick="this.parentElement.classList.toggle('open')">
        <span>${esc(issue.question)}</span>
        <span class="la-issue-arrow">▼</span>
      </button>
      <div class="la-issue-body">
        <p><strong>初步结论：</strong>${esc(issue.conclusion)}</p>
        ${rules ? `<div class="la-issue-rules">${rules}</div>` : ''}
        ${facts ? `<div class="la-issue-rules">${facts}</div>` : ''}
        ${issue.analysis ? `<p><strong>适用分析：</strong>${esc(issue.analysis)}</p>` : ''}
        ${counter ? `<div class="la-counter"><strong>不利因素：</strong><ul>${counter}</ul></div>` : ''}
      </div>
    </div>`;
  }).join('');
  return `<section class="la-section"><h3>争议焦点</h3>${blocks}</section>`;
}

function renderEvidence(evidence) {
  if (!evidence || !evidence.length) return '';
  const rows = evidence.map(e => `
    <tr>
      <td>${esc(e.name)}</td>
      <td>${esc(e.purpose)}</td>
      <td>${esc(EVIDENCE_STATUS_CN[e.status] || e.status)}</td>
      <td>${esc(PROBATIVE_FORCE_CN[e.probative_force] || e.probative_force)}</td>
      <td>${esc(e.next_step || '')}</td>
    </tr>`).join('');
  return `<section class="la-section"><h3>证据分析</h3>
    <div class="la-table-wrapper"><table class="la-evidence-table">
      <thead><tr><th>证据</th><th>证明目的</th><th>状态</th><th>证明力</th><th>下一步</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div></section>`;
}

function renderRisks(risks) {
  if (!risks || !risks.length) return '';
  const rows = risks.map(r => {
    const m = RISK_RATING_META[r.rating] || RISK_RATING_META.medium;
    return `<tr><td>${esc(r.dimension)}</td><td><span class="risk-badge ${m.cls}">${m.label}</span></td><td>${esc(r.detail)}</td></tr>`;
  }).join('');
  return `<section class="la-section"><h3>完整风险评估</h3>
    <div class="la-table-wrapper"><table class="la-risk-table">
      <thead><tr><th>维度</th><th>等级</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div></section>`;
}

function renderFullActionPlan(plan) {
  const nonImmediate = (plan || []).filter(a => a.phase !== 'immediate');
  if (!nonImmediate.length) return '';
  const phaseLabel = { short_term: '近期（3日内）', contingency: '协商失败后' };
  const grouped = {};
  nonImmediate.forEach(a => { const p = a.phase || 'short_term'; if (!grouped[p]) grouped[p] = []; grouped[p].push(a); });
  const blocks = Object.keys(grouped).map(p => {
    const items = grouped[p].map(a => `<li><strong>${esc(a.description)}</strong>${a.required_materials && a.required_materials.length ? `<br><small>所需材料：${a.required_materials.map(esc).join('、')}</small>` : ''}${a.deadline ? `<br><small>截止：${esc(a.deadline)}</small>` : ''}</li>`).join('');
    return `<div class="la-action-phase"><h4>${phaseLabel[p] || p}</h4><ol>${items}</ol></div>`;
  }).join('');
  return `<section class="la-section"><h3>完整行动时间线</h3>${blocks}</section>`;
}

function renderCitations(citations) {
  if (!citations || !citations.length) return '';
  const items = citations.map(c => {
    const levelLabel = CITATION_LEVEL_CN[c.level] || c.level || '';
    const statusLabel = CITATION_STATUS_CN[c.status] || c.status || '';
    const role = c.role_in_analysis ? `<p><small>本案作用：${esc(c.role_in_analysis)}</small></p>` : '';
    return `<details class="la-citation">
      <summary><span class="la-citation-level level-${esc(c.level)}">${esc(levelLabel)}</span>《${esc(c.full_name)}》${esc(c.article_number)}</summary>
      <div class="la-citation-detail">
        <p>${esc(c.article_text)}</p>
        ${role}
        <p><small>效力状态：${esc(statusLabel)}　来源：${esc(c.official_source || '官方数据库')}</small></p>
      </div>
    </details>`;
  }).join('');
  return `<section class="la-section"><h3>法律依据</h3>${items}</section>`;
}

function renderUncertainties(uncertainties) {
  if (!uncertainties || !uncertainties.length) return '';
  const items = uncertainties.map(u => `<li><strong>${esc(u.description)}</strong><br><small>影响：${esc(u.impact)}${u.resolution ? '　建议：' + esc(u.resolution) : ''}</small></li>`).join('');
  return `<section class="la-section la-uncertainties"><h3>不确定性说明</h3><ul>${items}</ul></section>`;
}

function renderDisclaimer(disclaimer) {
  return `<div class="la-disclaimer"><p>${esc(disclaimer || '')}</p></div>`;
}

window.renderLegalAnswer = renderLegalAnswer;
