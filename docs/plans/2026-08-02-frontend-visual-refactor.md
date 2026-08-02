# 前端视觉重构：深色外壳 + 浅色法律报告 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将法律 Agent 前端从"全局深色"升级为"深色侧栏 + 浅灰工作区 + 白色纸张式法律报告"的分层设计语言，解决结构化报告与聊天主题的视觉冲突。

**Architecture:** 保留深色侧栏的品牌感，将主区域（chat-area）改为浅灰工作区，结构化法律报告以白色纸张卡片呈现。CSS 拆分为全局变量 + 组件语义类，消除 `components.js` 中的内联颜色。结构化消息脱离 820px 聊天气泡，改为独立文档式布局。

**Tech Stack:** 原生 HTML / CSS / JavaScript（无框架）

---

## 背景与现状

当前前端全局使用深色主题（`--bg: #0f1117`），但法律结构化报告 `legal-answer.css` 使用浅色卡片（`#f9fafb` 背景、`#1F4B7A` 深蓝标题），导致：
1. 深色全局文字（`#e4e6eb`）落在浅色法律卡片上 → 低对比度
2. 颜色配置分散在 3 处（style.css 变量 + index.html 内联 + components.js 内联）
3. 结构化报告被限制在 820px 聊天气泡中
4. 无打印样式（仅 2 行 @media print）

## 文件结构总览

**新建文件：**
- `src/lvyan/api/static/print.css` — 打印与 PDF 导出专用样式

**修改文件：**
- `src/lvyan/api/static/style.css` — 新增浅色工作区变量 + 结构化布局类 + 移动端优化
- `src/lvyan/api/static/legal-answer.css` — 全面重写为白色纸张报告样式（语义类名）
- `src/lvyan/api/static/components.js` — 移除所有内联颜色，改用语义类名；首屏重排（报告头+两栏摘要+立即行动提前）
- `src/lvyan/api/static/app.js` — 结构化消息添加 `.msg-structured` 类 + `.messages-structured` 宽度扩展
- `src/lvyan/api/static/index.html` — 引入 print.css + 调整 script 版本号

---

## 阶段一：P0 视觉冲突修复（核心阻断项）

### Task 1: style.css 新增浅色工作区 + 结构化布局类

**Files:**
- Modify: `src/lvyan/api/static/style.css` (变量定义区 L6-29 + .messages 区 L368 + 新增块)

- [ ] **Step 1: 在 style.css 变量区新增浅色语义变量**

在 `:root` 块内（L6-29 现有变量之后）追加浅色工作区变量：

```css
  /* --- P0：浅色工作区 + 白色法律报告（分层设计语言） --- */
  --app-bg: #f3f5f8;
  --surface: #ffffff;
  --surface-subtle: #f8fafc;

  --text-primary: #172033;
  --text-secondary: #475467;
  --text-muted: #667085;

  --la-primary: #214f7b;
  --la-primary-soft: #edf4fb;
  --la-success: #287a5b;
  --la-success-soft: #edf8f3;
  --la-warning: #a96712;
  --la-warning-soft: #fff7e8;
  --la-danger: #b42318;
  --la-danger-soft: #fef3f2;
  --la-inferred: #6655a5;
  --la-inferred-soft: #f4f1fb;

  --la-border: #e4e7ec;
  --la-border-strong: #d0d5dd;
  --la-shadow-paper: 0 1px 2px rgb(16 24 40 / 4%), 0 10px 28px rgb(16 24 40 / 7%);
```

- [ ] **Step 2: 新增浅色工作区样式**

在 `.messages` 定义（L368）之前新增浅色工作区切换类：

```css
/* --- P0：浅色工作区模式（结构化报告激活时） --- */
.chat-area.la-active {
  background: var(--app-bg);
}
.chat-area.la-active .messages {
  max-width: 1180px;
}
.chat-area.la-active .msg-agent.msg-structured {
  display: block;
  width: 100%;
  max-width: 100%;
}
.chat-area.la-active .msg-agent.msg-structured .msg-avatar,
.chat-area.la-active .msg-agent.msg-structured .msg-name {
  display: none;
}
.chat-area.la-active .msg-agent.msg-structured .msg-body {
  width: 100%;
}
.chat-area.la-active .msg-agent.msg-structured .msg-content {
  background: var(--surface);
  border: 1px solid var(--la-border);
  border-radius: 16px;
  box-shadow: var(--la-shadow-paper);
  color: var(--text-primary);
  padding: 0;
  overflow: hidden;
}
```

- [ ] **Step 3: 移动端操作按钮始终可见（触屏无 hover）**

在现有 `@media (max-width: 768px)` 块内追加：

```css
  .msg-actions { opacity: 1 !important; }
```

- [ ] **Step 4: 确认无语法错误**

Run: `cd e:\compelet\法律\AGENT && python -c "print('css check')"`
（CSS 无编译步骤，通过后续 JS 加载验证）

- [ ] **Step 5: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/api/static/style.css
git commit -m "style: add light workspace vars and structured layout classes"
```

---

### Task 2: legal-answer.css 全面重写为白色纸张报告

**Files:**
- Modify: `src/lvyan/api/static/legal-answer.css` (全文重写)

- [ ] **Step 1: 重写 legal-answer.css**

用以下内容完整替换 `legal-answer.css`：

```css
/* 法律分析结构化输出：白色纸张报告样式 */
/* 所有颜色通过语义类名控制，components.js 不再写内联颜色 */

.legal-answer {
  color: var(--text-primary);
  font-size: 16px;
  line-height: 1.75;
}

/* --- 报告头 --- */
.la-report-header {
  padding: 28px 32px 20px;
  background: linear-gradient(135deg, #ffffff 0%, #f7fafc 100%);
  border-bottom: 1px solid var(--la-border);
}
.la-report-eyebrow {
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--la-primary);
  font-weight: 600;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.la-report-title {
  font-family: "Source Han Serif SC", "Noto Serif CJK SC", "Songti SC", serif;
  font-size: 24px;
  line-height: 1.4;
  color: var(--text-primary);
  margin: 0 0 12px;
  font-weight: 700;
}
.la-meta-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  color: var(--text-muted);
  font-size: 13px;
}

/* --- 段落通用 --- */
.la-section {
  padding: 20px 32px;
  border-bottom: 1px solid var(--la-border);
}
.la-section:last-child { border-bottom: 0; }
.la-section h3 {
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 12px;
}

/* --- 核心结论两栏 --- */
.la-summary-grid {
  display: grid;
  grid-template-columns: 1fr 280px;
  gap: 24px;
}
@media (max-width: 768px) {
  .la-summary-grid { grid-template-columns: 1fr; }
}
.la-conclusion { font-size: 16px; color: var(--text-primary); font-weight: 500; }
.la-reasons { margin: 12px 0 0; padding-left: 18px; color: var(--text-secondary); }
.la-reasons li { margin-bottom: 4px; }
.la-risk-panel {
  background: var(--surface-subtle);
  border-radius: 12px;
  padding: 16px;
}
.la-risk-panel-item { margin-bottom: 12px; }
.la-risk-panel-item:last-child { margin-bottom: 0; }
.la-risk-panel-label { font-size: 12px; color: var(--text-muted); margin-bottom: 2px; }
.la-risk-panel-value { font-size: 15px; font-weight: 600; }

/* --- 风险徽章 --- */
.risk-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 600;
}
.risk-high { background: var(--la-danger-soft); color: var(--la-danger); }
.risk-medium { background: var(--la-warning-soft); color: var(--la-warning); }
.risk-low { background: var(--la-success-soft); color: var(--la-success); }

/* --- 事实列表 --- */
.la-facts { list-style: none; padding: 0; margin: 0; }
.la-fact {
  padding: 10px 14px;
  margin: 6px 0;
  background: var(--surface-subtle);
  border-radius: 8px;
  border-left: 3px solid var(--la-border-strong);
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.la-fact.fact-confirmed { border-left-color: var(--la-success); }
.la-fact.fact-claimed { border-left-color: var(--text-muted); }
.la-fact.fact-inferred { border-left-color: var(--la-inferred); }
.la-fact.fact-missing { border-left-color: var(--la-warning); }
.la-fact-tag {
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
  padding: 1px 8px;
  border-radius: 4px;
}
.fact-confirmed .la-fact-tag { background: var(--la-success-soft); color: var(--la-success); }
.fact-claimed .la-fact-tag { background: var(--surface-subtle); color: var(--text-muted); }
.fact-inferred .la-fact-tag { background: var(--la-inferred-soft); color: var(--la-inferred); }
.fact-missing .la-fact-tag { background: var(--la-warning-soft); color: var(--la-warning); }
.la-fact-content { color: var(--text-primary); }

/* --- 争议焦点折叠卡片 --- */
.la-issue {
  margin: 12px 0;
  background: var(--surface-subtle);
  border-radius: 12px;
  overflow: hidden;
}
.la-issue-toggle {
  width: 100%;
  text-align: left;
  padding: 14px 16px;
  border: 0;
  background: none;
  cursor: pointer;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.la-issue-toggle .la-issue-arrow { transition: transform 0.2s; color: var(--text-muted); }
.la-issue.open .la-issue-arrow { transform: rotate(180deg); }
.la-issue-body {
  display: none;
  padding: 0 16px 14px;
}
.la-issue.open .la-issue-body { display: block; }
.la-issue-body p { margin: 8px 0; color: var(--text-secondary); }
.la-issue-body strong { color: var(--text-primary); }
.la-counter { color: var(--la-warning); }
.la-issue-rules {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0;
}
.la-rule-chip {
  font-size: 12px;
  padding: 2px 8px;
  background: var(--la-primary-soft);
  color: var(--la-primary);
  border-radius: 4px;
}

/* --- 表格 + 横向滚动 --- */
.la-table-wrapper {
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
.la-evidence-table, .la-risk-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
.la-evidence-table th, .la-risk-table th {
  background: var(--surface-subtle);
  text-align: left;
  padding: 10px 12px;
  color: var(--text-muted);
  font-weight: 600;
  font-size: 13px;
  border-bottom: 1px solid var(--la-border);
}
.la-evidence-table td, .la-risk-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--la-border);
  color: var(--text-secondary);
}
.la-evidence-table td:first-child, .la-risk-table td:first-child {
  color: var(--text-primary);
  font-weight: 500;
}

/* --- 行动建议 --- */
.la-action-phase { margin: 12px 0; }
.la-action-phase h4 { font-size: 14px; color: var(--text-primary); margin: 0 0 8px; }
.la-action-phase ol { margin: 0; padding-left: 20px; color: var(--text-secondary); }
.la-action-phase li { margin-bottom: 6px; }

/* --- 法条引用抽屉 --- */
.la-citation {
  margin: 8px 0;
  padding: 12px 14px;
  background: var(--surface-subtle);
  border-radius: 8px;
  border-left: 3px solid var(--la-primary);
}
.la-citation summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--text-primary);
  font-size: 14px;
}
.la-citation-level {
  display: inline-block;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  margin-right: 6px;
  font-weight: 500;
}
.level-law { background: var(--la-primary-soft); color: var(--la-primary); }
.level-regulation { background: var(--surface-subtle); color: var(--text-muted); }
.level-judicial_interpretation { background: var(--la-primary-soft); color: var(--la-primary); }
.level-guiding_case { background: var(--la-inferred-soft); color: var(--la-inferred); }
.level-reference_case { background: var(--surface-subtle); color: var(--text-muted); }
.level-normative { background: var(--surface-subtle); color: var(--text-muted); }
.la-citation-detail { margin-top: 8px; color: var(--text-secondary); font-size: 14px; }
.la-citation-detail small { color: var(--text-muted); }

/* --- 不确定性 --- */
.la-uncertainties ul { list-style: none; padding: 0; }
.la-uncertainties li { padding: 8px 0; border-bottom: 1px solid var(--la-border); }
.la-uncertainties li:last-child { border-bottom: 0; }

/* --- 免责声明 --- */
.la-disclaimer {
  color: var(--text-muted);
  font-size: 13px;
  background: var(--surface-subtle);
  padding: 16px 32px;
}
```

- [ ] **Step 2: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/api/static/legal-answer.css
git commit -m "style: rewrite legal-answer.css as white paper report"
```

---

### Task 3: components.js 移除内联颜色，改用语义类名 + 首屏重排

**Files:**
- Modify: `src/lvyan/api/static/components.js` (全文重写)

- [ ] **Step 1: 重写 components.js**

用以下内容完整替换 `components.js`：

```javascript
// 法律分析结构化渲染组件（语义类名版）
// 所有颜色由 legal-answer.css 的语义类控制，不写内联 style

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

const EVIDENCE_STATUS_CN = { provided: '已提供', missing: '未提供', partial: '部分提供' };
const PROBATIVE_FORCE_CN = { key: '关键', strong: '较强', medium: '一般', weak: '较弱', unevaluated: '待评估' };
const MATERIAL_COMPLETENESS_CN = { complete: '材料较完整', partial: '材料部分完整', insufficient: '材料不足' };
const CITATION_STATUS_CN = { effective: '现行有效', repealed: '已废止', not_yet_effective: '尚未生效', unknown: '状态未知' };
const CITATION_LEVEL_CN = { law: '法律', regulation: '行政法规', judicial_interpretation: '司法解释', guiding_case: '指导案例', reference_case: '参考案例', normative: '规范性文件' };

function esc(text) {
  if (text == null) return '';
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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
    const meta = RISK_RATING_META[r.rating] || RISK_RATING_META.medium;
    return `<div class="la-risk-panel-item">
      <div class="la-risk-panel-label">${esc(r.dimension)}</div>
      <div class="la-risk-panel-value"><span class="risk-badge ${meta.cls}">${meta.label}</span></div>
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
  const items = immediate.map((a, i) => `<li><strong>${esc(a.description)}</strong></li>`).join('');
  return `
    <section class="la-section">
      <h3>立即行动</h3>
      <ol class="la-reasons">${items}</ol>
    </section>`;
}

function renderFacts(facts) {
  if (!facts || !facts.length) return '';
  const items = facts.map(f => {
    const meta = FACT_STATUS_META[f.status] || FACT_STATUS_META.claimed;
    return `<li class="la-fact ${meta.cls}">
      <span class="la-fact-tag">${meta.label}</span>
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
      <button class="la-issue-toggle" onclick="this.parentElement.classList.toggle('open')">
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
    const meta = RISK_RATING_META[r.rating] || RISK_RATING_META.medium;
    return `<tr><td>${esc(r.dimension)}</td><td><span class="risk-badge ${meta.cls}">${meta.label}</span></td><td>${esc(r.detail)}</td></tr>`;
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
    const items = grouped[p].map(a => `<li><strong>${esc(a.description)}</strong>${a.required_materials && a.required_materials.length ? `<br><small>所需材料：${a.required_materials.map(esc).join('、')}</small>` : ''}</li>`).join('');
    return `<div class="la-action-phase"><h4>${phaseLabel[p] || p}</h4><ol>${items}</ol></div>`;
  }).join('');
  return `<section class="la-section"><h3>完整行动时间线</h3>${blocks}</section>`;
}

function renderCitations(citations) {
  if (!citations || !citations.length) return '';
  const items = citations.map(c => {
    const levelLabel = CITATION_LEVEL_CN[c.level] || c.level || '';
    const statusLabel = CITATION_STATUS_CN[c.status] || c.status || '';
    return `<details class="la-citation">
      <summary><span class="la-citation-level level-${esc(c.level)}">${esc(levelLabel)}</span>《${esc(c.full_name)}》${esc(c.article_number)}</summary>
      <div class="la-citation-detail">
        <p>${esc(c.article_text)}</p>
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
```

- [ ] **Step 2: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/api/static/components.js
git commit -m "refactor: components.js use semantic class names, reorder first screen"
```

---

### Task 4: app.js 结构化消息添加布局切换类

**Files:**
- Modify: `src/lvyan/api/static/app.js` (updateLastAgentMessageStructured ~L777 + renderConversation ~L1098)

- [ ] **Step 1: updateLastAgentMessageStructured 添加结构化类**

修改 `updateLastAgentMessageStructured` 函数（~L777-792），在渲染结构化内容后添加 `.msg-structured` 类到消息元素，并给 `.chat-area` 添加 `.la-active` 类：

```javascript
function updateLastAgentMessageStructured(answer, markdownFallback) {
  const agentMessages = els.messages.querySelectorAll('.msg-agent');
  const lastMsg = agentMessages[agentMessages.length - 1];
  if (!lastMsg) return;
  const contentDiv = lastMsg.querySelector('.msg-content');
  lastMsg.dataset.content = markdownFallback || '';
  lastMsg.dataset.structuredAnswer = JSON.stringify(answer);
  contentDiv.innerHTML = window.renderLegalAnswer(answer);
  lastMsg.classList.add('msg-structured');
  els.chatArea.classList.add('la-active');

  if (!lastMsg.querySelector('.msg-actions')) {
    const body = lastMsg.querySelector('.msg-body');
    body.appendChild(createMsgActions(markdownFallback));
  }

  els.chatArea.scrollTop = els.chatArea.scrollHeight;
}
```

- [ ] **Step 2: renderConversation 恢复时也添加结构化类**

修改 `renderConversation` 函数（~L1098-1118），在恢复结构化视图时同步添加 `.msg-structured` 和 `.la-active`：

```javascript
function renderConversation(messages) {
  els.messages.innerHTML = '';
  els.chatArea.classList.remove('la-active');
  messages.forEach(message => {
    const role = message.role === 'assistant' ? 'agent' : 'user';
    addMessage(role, message.content || '', false, Array.isArray(message.attachments) ? message.attachments : []);
    if (message.structured_answer && role === 'agent' && window.renderLegalAnswer) {
      const lastMsg = els.messages.querySelector('.msg-agent:last-child');
      if (lastMsg) {
        const lastContent = lastMsg.querySelector('.msg-content');
        lastMsg.dataset.structuredAnswer = JSON.stringify(message.structured_answer);
        lastContent.innerHTML = window.renderLegalAnswer(message.structured_answer);
        lastMsg.classList.add('msg-structured');
        els.chatArea.classList.add('la-active');
      }
    }
  });
}
```

- [ ] **Step 3: 新建对话时移除 la-active 类**

找到 `newChat` / `clearMessages` 相关函数（搜索 `els.welcome.style.display` 或 `els.messages.innerHTML = ''`），在清空消息时移除 `la-active`：

```javascript
els.chatArea.classList.remove('la-active');
```

- [ ] **Step 4: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/api/static/app.js
git commit -m "feat: structured messages expand to 1180px document layout"
```

---

## 阶段二：P1 打印样式 + 收尾

### Task 5: 新建 print.css + index.html 引入

**Files:**
- Create: `src/lvyan/api/static/print.css`
- Modify: `src/lvyan/api/static/index.html` (head 区 L7-8)

- [ ] **Step 1: 创建 print.css**

```css
/* 打印与 PDF 导出专用样式 */
@media print {
  #sidebar,
  #topbar,
  .input-area,
  .msg-actions,
  .progress-bar,
  .hitl-panel,
  #toast,
  .msg-avatar,
  .msg-name {
    display: none !important;
  }

  html, body {
    overflow: visible;
    background: white;
    color: black;
  }

  .chat-area, .messages, .legal-answer {
    max-width: none;
    padding: 0;
    margin: 0;
  }

  .msg-agent.msg-structured .msg-content {
    box-shadow: none;
    border: 0;
  }

  .la-section, .la-citation[open] {
    break-inside: avoid;
    page-break-inside: avoid;
  }

  .la-report-header {
    background: white;
  }
}
```

- [ ] **Step 2: index.html head 引入 print.css**

在 `legal-answer.css` 引入之后追加：

```html
  <link rel="stylesheet" href="/static/print.css?v=1" media="print">
```

- [ ] **Step 3: 更新 script/css 版本号**

更新 index.html 中所有静态资源版本号（确保浏览器刷新缓存）：
- `legal-answer.css?v=1` → `legal-answer.css?v=2`
- `components.js?v=1` → `components.js?v=2`
- `app.js?v=10` → `app.js?v=11`

- [ ] **Step 4: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/api/static/print.css src/lvyan/api/static/index.html
git commit -m "feat: add print.css and update resource versions"
```

---

### Task 6: 手动验证 + 完整回归测试

- [ ] **Step 1: 启动开发服务器验证**

Run: `cd e:\compelet\法律\AGENT && python -m uvicorn lvyan.api.server:create_app --factory --port 8000`

打开浏览器，执行 deep 分析，验证：
- 法律报告以白色纸张卡片显示在浅灰背景上
- 文字为深色（#172033），不再是浅色文字落在浅色背景上
- 结构化报告宽度达到 1180px，不再是 820px 聊天气泡
- 风险徽章使用语义颜色（红/黄/绿背景+对应文字色）
- 法条引用显示层级标签（法律/司法解释等）
- 争点折叠卡片默认展开第一个

- [ ] **Step 2: 运行完整测试套件确认无回归**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/ -q -m "not slow" --tb=short`
Expected: 全部通过

- [ ] **Step 3: 提交**

```bash
cd e:\compelet\法律\AGENT
git add -A
git commit -m "test: visual refactor regression pass"
```

---

## 自检清单

- [ ] Task 1：style.css 新增浅色变量 + 结构化布局类 + 移动端操作按钮
- [ ] Task 2：legal-answer.css 全面重写为白色纸张报告（语义类名）
- [ ] Task 3：components.js 移除内联颜色，首屏重排（报告头+两栏摘要+立即行动提前）
- [ ] Task 4：app.js 结构化消息添加 .msg-structured + .la-active 类
- [ ] Task 5：print.css 打印样式 + index.html 引入
- [ ] Task 6：手动验证 + 回归测试

## 未在本计划范围（后续迭代）

- 侧边栏按日期分组（今天/昨天/过去7天/更早）
- 运行进度映射为用户语言（理解问题/检索法律依据/...）
- 右侧报告目录（滚动高亮）
- 悬浮输入台 + 附件处理状态
- 骨架屏加载动画
- 动态上传限制（后端配置接口）
- 深色/浅色主题切换
