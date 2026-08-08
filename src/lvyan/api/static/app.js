/* =========================================================================
   律言法律智能体 · 前端应用逻辑 v8
   功能：对话 / 文件上传 / 历史管理 / 消息操作 / 导出 / 快捷键
   ========================================================================= */

// --- 全局状态 ---
const state = {
  threadId: null,
  runId: null,
  mode: 'auto',
  isRunning: false,
  history: [],        // [{threadId, query, mode, ts, messages}]
  runStartTime: 0,
  attachments: [],    // [{file_id, filename, size, content_type, text_preview}]
  lastQuery: '',      // 用于重新生成
  sidebarCollapsed: false,
  settingsOpen: false,
  workspaceOpen: false,
  selectedCaseId: null,
  cases: [],
};

// P1-2：全局运行超时定时器。只在终态（final_output/error/cancelled）或
// stopGeneration 中清理，不能在 POST /run 的 finally 里清理——否则 POST 一返回
// 定时器就被取消，「5 分钟超时自动停止」实际从未生效。
let runTimeoutId = null;

// 全局 SSE AbortController 引用
let currentSSEController = null;

const UI_PREFERENCES_KEY = 'lvyan_ui_preferences';
const DEFAULT_UI_PREFERENCES = {
  density: 'comfortable',
  reduceMotion: false,
};
let uiPreferences = { ...DEFAULT_UI_PREFERENCES };

// --- 节点中文名映射（仅用于 node_error toast 展示，不参与进度统计） ---
const NODE_LABELS = {
  preflight: '预检',
  jurisdiction_triage: '管辖分流',
  fact_extractor: '事实抽取',
  missing_fact_assessor: '缺失评估',
  planner: '规划',
  parallel_retrieval: '法条检索',
  authority_resolver: '权威解析',
  legal_reasoner: '法律推理',
  critic: '评审',
  citation_verifier: '引用校验',
  composer: '生成',
  output_guardrail: '安全护栏',
};

// =========================================================================
// 统一认证层（P0：内置前端完整支持 AUTH_MODE=jwt / trusted_proxy）
// =========================================================================
// 所有 REST / SSE 请求统一走 apiFetch / apiFetchStream，自动携带
// Authorization: Bearer <token>，避免逐接口零散补 Header 遗漏。
function getAuthToken() {
  if (window.__authToken) return window.__authToken;
  // 部署方可在 index.html 注入 <meta name="auth-token" content="...">
  const meta = document.querySelector('meta[name="auth-token"]');
  if (meta && meta.content) return meta.content;
  try {
    return localStorage.getItem('lvyan_auth_token') || '';
  } catch { return ''; }
}

function authHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getAuthToken();
  if (token) headers.Authorization = 'Bearer ' + token;
  return headers;
}

async function apiFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    credentials: 'same-origin',
    headers: authHeaders(options.headers || {}),
  });
}

// P2：SSE 流式请求（EventSource 无法携带 Authorization，改用 fetch stream）
function closeSSE() {
  if (currentSSEController) {
    currentSSEController.abort();
    currentSSEController = null;
  }
}

// --- DOM 引用 ---
const $ = (id) => document.getElementById(id);
const els = {
  welcome: $('welcome'),
  messages: $('messages'),
  chatArea: $('chat-area'),
  input: $('query-input'),
  sendBtn: $('send-btn'),
  stopBtn: $('stop-btn'),
  uploadBtn: $('upload-btn'),
  fileInput: $('file-input'),
  attachmentPreview: $('attachment-preview'),
  progressBar: $('progress-bar'),
  progressFill: $('progress-fill'),
  progressNodes: $('progress-nodes'),
  hitlPanel: $('hitl-panel'),
  hitlMessage: $('hitl-message'),
  hitlApprove: $('hitl-approve'),
  hitlReject: $('hitl-reject'),
  hitlEditToggle: $('hitl-edit-toggle'),
  hitlEditArea: $('hitl-edit-area'),
  hitlEditText: $('hitl-edit-text'),
  hitlEditSubmit: $('hitl-edit-submit'),
  historyList: $('history-list'),
  historySearch: $('history-search'),
  threadLabel: $('thread-label'),
  healthStatus: $('health-status'),
  newChat: $('new-chat'),
  clearAllBtn: $('clear-all-history'),
  exportBtn: $('export-btn'),
  sidebarToggle: $('sidebar-toggle'),
  inputArea: $('input-area'),
  settingsBtn: $('settings-btn'),
  settingsView: $('settings-view'),
  settingsClose: $('settings-close'),
  settingsDensity: $('settings-density'),
  settingsReduceMotion: $('settings-reduce-motion'),
  settingsHealthDot: $('settings-health-dot'),
  settingsHealthTitle: $('settings-health-title'),
  settingsHealthDetail: $('settings-health-detail'),
  settingsAuthStatus: $('settings-auth-status'),
  settingsRefreshHealth: $('settings-refresh-health'),
  settingsHistoryCount: $('settings-history-count'),
  settingsClearLocal: $('settings-clear-local'),
  workspaceBtn: $('workspace-btn'),
  workspaceView: $('workspace-view'),
  workspaceClose: $('workspace-close'),
  caseCreateForm: $('case-create-form'),
  caseTitleInput: $('case-title-input'),
  caseDescriptionInput: $('case-description-input'),
  caseList: $('case-list'),
  workspaceEmpty: $('workspace-empty'),
  workspaceContent: $('workspace-content'),
  workspaceCaseTitle: $('workspace-case-title'),
  workspaceCaseDescription: $('workspace-case-description'),
  workspaceCaseStatus: $('workspace-case-status'),
  workspaceDocumentList: $('workspace-document-list'),
  workspaceAuditList: $('workspace-audit-list'),
  documentCreateForm: $('document-create-form'),
  documentTitleInput: $('document-title-input'),
  documentTypeInput: $('document-type-input'),
  documentContentInput: $('document-content-input'),
  charCount: $('char-count'),
  msgMenu: $('msg-menu'),
  toast: $('toast'),
};

// =========================================================================
// 初始化
// =========================================================================
function init() {
  // 重置可能残留的运行状态
  state.isRunning = false;
  closeSSE();
  if (window.matchMedia('(max-width: 768px)').matches) {
    state.sidebarCollapsed = true;
    document.body.classList.add('sidebar-collapsed');
    els.sidebarToggle.setAttribute('aria-expanded', 'false');
  }
  loadUiPreferences();

  // 快捷问题
  document.querySelectorAll('.quick-q').forEach(btn => {
    btn.addEventListener('click', () => {
      els.input.value = btn.dataset.q;
      autoResize();
      updateCharCount();
      sendQuery();
    });
  });

  // 发送
  els.sendBtn.addEventListener('click', sendQuery);
  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendQuery();
    }
  });
  els.input.addEventListener('input', () => {
    autoResize();
    updateCharCount();
  });

  // 停止生成
  els.stopBtn.addEventListener('click', stopGeneration);

  // 新对话
  els.newChat.addEventListener('click', startNewChat);

  // 清空所有历史
  els.clearAllBtn.addEventListener('click', clearAllHistory);

  // 导出对话
  els.exportBtn.addEventListener('click', exportConversation);

  // 侧栏折叠
  els.sidebarToggle.addEventListener('click', toggleSidebar);

  // 设置页
  els.settingsBtn.addEventListener('click', openSettings);
  els.settingsClose.addEventListener('click', closeSettings);
  document.querySelectorAll('.settings-nav-item').forEach(btn => {
    btn.addEventListener('click', () => showSettingsPanel(btn.dataset.settingsPanel));
  });
  els.settingsDensity.addEventListener('change', () => {
    uiPreferences.density = els.settingsDensity.value;
    persistUiPreferences();
    applyUiPreferences();
  });
  els.settingsReduceMotion.addEventListener('change', () => {
    uiPreferences.reduceMotion = els.settingsReduceMotion.checked;
    persistUiPreferences();
    applyUiPreferences();
  });
  els.settingsRefreshHealth.addEventListener('click', refreshSettingsHealth);
  els.settingsClearLocal.addEventListener('click', clearLocalHistoryCache);
  els.workspaceBtn.addEventListener('click', openWorkspace);
  els.workspaceClose.addEventListener('click', closeWorkspace);
  els.caseCreateForm.addEventListener('submit', createWorkspaceCase);
  els.documentCreateForm.addEventListener('submit', createWorkspaceDocument);

  // 文件上传
  els.uploadBtn.addEventListener('click', () => els.fileInput.click());
  els.fileInput.addEventListener('change', handleFileUpload);

  // 拖拽上传
  els.input.addEventListener('dragover', (e) => {
    e.preventDefault();
    els.input.parentElement.classList.add('drag-over');
  });
  els.input.addEventListener('dragleave', () => {
    els.input.parentElement.classList.remove('drag-over');
  });
  els.input.addEventListener('drop', (e) => {
    e.preventDefault();
    els.input.parentElement.classList.remove('drag-over');
    if (e.dataTransfer.files.length > 0) {
      handleFiles(e.dataTransfer.files);
    }
  });

  // 历史搜索
  els.historySearch.addEventListener('input', renderHistory);

  // HITL 按钮
  els.hitlApprove.addEventListener('click', () => resolveHitl('approve'));
  els.hitlReject.addEventListener('click', () => resolveHitl('reject'));
  els.hitlEditToggle.addEventListener('click', () => {
    els.hitlEditArea.style.display = 'block';
    els.hitlEditText.focus();
  });
  els.hitlEditSubmit.addEventListener('click', () => {
    const text = els.hitlEditText.value.trim();
    if (text) resolveHitl('edit', text);
  });

  // 消息菜单外部点击关闭
  document.addEventListener('click', () => {
    els.msgMenu.style.display = 'none';
  });

  // 全局键盘快捷键
  document.addEventListener('keydown', handleGlobalShortcut);

  // 健康检查
  checkHealth();
  setInterval(checkHealth, 30000);

  // 加载历史
  loadHistory();
  updateCharCount();
}

// =========================================================================
// 全局快捷键
// =========================================================================
function handleGlobalShortcut(e) {
  if (e.key === 'Escape' && state.workspaceOpen) {
    closeWorkspace();
    return;
  }
  if (e.key === 'Escape' && state.settingsOpen) {
    closeSettings();
    return;
  }
  // Ctrl+N: 新对话
  if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
    e.preventDefault();
    startNewChat();
    return;
  }
  // Ctrl+/ : 聚焦输入框
  if ((e.ctrlKey || e.metaKey) && e.key === '/') {
    e.preventDefault();
    els.input.focus();
    return;
  }
  // Esc: 关闭非阻断菜单。HITL 必须显式批准或拒绝，不能静默隐藏。
  if (e.key === 'Escape') {
    els.msgMenu.style.display = 'none';
  }
}

// =========================================================================
// 设置页（浏览器本地偏好，不传递或展示服务端敏感配置）
// =========================================================================
function setActiveMode(mode) {
  state.mode = 'auto';
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.classList.add('active');
  });
}

function loadUiPreferences() {
  try {
    const raw = JSON.parse(localStorage.getItem(UI_PREFERENCES_KEY) || '{}');
    if (raw && typeof raw === 'object') {
      if (['comfortable', 'compact'].includes(raw.density)) uiPreferences.density = raw.density;
      if (typeof raw.reduceMotion === 'boolean') uiPreferences.reduceMotion = raw.reduceMotion;
    }
  } catch { /* 浏览器隐私模式或损坏缓存时使用默认值 */ }
  // 设置页字段可能在滚动发布或浏览器旧缓存中暂时不一致；可选控件缺失
  // 不应阻断整个应用初始化和事件绑定。
  if (els.settingsDensity) els.settingsDensity.value = uiPreferences.density;
  if (els.settingsReduceMotion) els.settingsReduceMotion.checked = uiPreferences.reduceMotion;
  setActiveMode();
  applyUiPreferences();
}

function persistUiPreferences() {
  try {
    localStorage.setItem(UI_PREFERENCES_KEY, JSON.stringify(uiPreferences));
  } catch {
    showToast('无法保存浏览器偏好设置', 'warning');
  }
}

function applyUiPreferences() {
  document.body.classList.toggle('compact-ui', uiPreferences.density === 'compact');
  document.body.classList.toggle('reduce-motion', uiPreferences.reduceMotion);
}

function showSettingsPanel(panel) {
  document.querySelectorAll('.settings-nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.settingsPanel === panel);
  });
  document.querySelectorAll('.settings-panel').forEach(section => {
    const active = section.dataset.settingsContent === panel;
    section.classList.toggle('active', active);
    section.hidden = !active;
  });
}

function openSettings() {
  if (state.isRunning) {
    showToast('请先结束当前运行，再打开设置', 'warning');
    return;
  }
  if (state.workspaceOpen) closeWorkspace();
  state.settingsOpen = true;
  els.settingsBtn.setAttribute('aria-expanded', 'true');
  els.settingsView.style.display = 'flex';
  els.chatArea.style.display = 'none';
  els.inputArea.style.display = 'none';
  els.progressBar.style.display = 'none';
  els.exportBtn.style.display = 'none';
  els.threadLabel.textContent = '设置';
  showSettingsPanel('general');
  updateSettingsHistoryCount();
  refreshSettingsHealth();
  els.settingsClose.focus();
}

function closeSettings() {
  if (!state.settingsOpen) return;
  state.settingsOpen = false;
  els.settingsBtn.setAttribute('aria-expanded', 'false');
  els.settingsView.style.display = 'none';
  els.chatArea.style.display = 'flex';
  els.inputArea.style.display = '';
  els.threadLabel.textContent = state.threadId ? (state.lastQuery || '当前会话').slice(0, 20) : '新对话';
  els.exportBtn.style.display = state.threadId ? 'block' : 'none';
  els.settingsBtn.focus();
}

function updateSettingsHistoryCount() {
  const count = state.history.length;
  els.settingsHistoryCount.textContent = count
    ? `此浏览器保存了 ${count} 条会话副本。`
    : '此浏览器尚未保存会话副本。';
}

async function refreshSettingsHealth() {
  els.settingsAuthStatus.textContent = getAuthToken() ? '已检测到浏览器令牌' : '未检测到浏览器令牌';
  els.settingsHealthDot.className = 'dot dot-pending';
  els.settingsHealthTitle.textContent = '正在检查服务';
  els.settingsHealthDetail.textContent = '连接到健康检查接口…';
  try {
    const resp = await apiFetch('/api/health');
    if (!resp.ok) throw new Error('health request failed');
    const data = await resp.json();
    const failed = ['database', 'retrieval', 'model_gateway'].filter(key => data[key] !== 'ok');
    const labels = { database: '数据库', retrieval: '检索服务', model_gateway: '模型网关' };
    const healthy = failed.length === 0;
    els.settingsHealthDot.className = `dot ${healthy ? 'dot-ok' : 'dot-pending'}`;
    els.settingsHealthTitle.textContent = healthy ? '服务运行正常' : '服务降级运行';
    els.settingsHealthDetail.textContent = healthy
      ? '数据库、检索服务和模型网关均可用。'
      : `待恢复：${failed.map(key => labels[key]).join('、')}。`;
  } catch {
    els.settingsHealthDot.className = 'dot dot-error';
    els.settingsHealthTitle.textContent = '无法连接服务';
    els.settingsHealthDetail.textContent = '请检查网络连接或稍后重试。';
  }
}

// =========================================================================
// 案件工作台：前端只展示当前身份能够访问的案件，详情始终从受控 API 读取。
// =========================================================================
function setWorkspaceVisible(visible) {
  state.workspaceOpen = visible;
  els.workspaceBtn.setAttribute('aria-expanded', String(visible));
  els.workspaceView.style.display = visible ? 'flex' : 'none';
  els.chatArea.style.display = visible ? 'none' : 'flex';
  els.inputArea.style.display = visible ? 'none' : '';
  els.progressBar.style.display = 'none';
  els.exportBtn.style.display = visible ? 'none' : (state.threadId ? 'block' : 'none');
  els.threadLabel.textContent = visible
    ? '案件工作台'
    : (state.threadId ? (state.lastQuery || '当前会话').slice(0, 20) : '新对话');
}

async function openWorkspace() {
  if (state.isRunning) {
    showToast('请先结束当前运行，再打开案件工作台', 'warning');
    return;
  }
  if (state.settingsOpen) closeSettings();
  setWorkspaceVisible(true);
  await loadWorkspaceCases();
  els.workspaceClose.focus();
}

function closeWorkspace() {
  if (!state.workspaceOpen) return;
  setWorkspaceVisible(false);
  els.workspaceBtn.focus();
}

async function workspaceJson(url, options = {}) {
  const response = await apiFetch(url, options);
  if (!response.ok) {
    let detail = '请求失败';
    try { detail = (await response.json()).detail || detail; } catch { /* use fallback */ }
    throw new Error(detail);
  }
  return response.json();
}

function renderCaseList() {
  els.caseList.replaceChildren();
  if (!state.cases.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '还没有案件。';
    els.caseList.append(empty);
    return;
  }
  state.cases.forEach(item => {
    const button = document.createElement('button');
    button.type = 'button';
    button.classList.toggle('active', item.case_id === state.selectedCaseId);
    const title = document.createElement('strong');
    title.textContent = item.title;
    const meta = document.createElement('small');
    meta.textContent = item.description || '未填写案件摘要';
    button.append(title, meta);
    button.addEventListener('click', () => selectWorkspaceCase(item.case_id));
    els.caseList.append(button);
  });
}

async function loadWorkspaceCases() {
  try {
    state.cases = await workspaceJson('/api/cases');
    if (state.selectedCaseId && !state.cases.some(item => item.case_id === state.selectedCaseId)) {
      state.selectedCaseId = null;
    }
    renderCaseList();
    if (state.selectedCaseId) await selectWorkspaceCase(state.selectedCaseId);
  } catch (error) {
    showToast(`无法加载案件：${error.message}`, 'error');
  }
}

function addWorkspaceRow(container, primary, secondary = '') {
  const row = document.createElement('div');
  const title = document.createElement('div');
  title.textContent = primary;
  row.append(title);
  if (secondary) {
    const meta = document.createElement('small');
    meta.textContent = secondary;
    row.append(meta);
  }
  container.append(row);
}

async function selectWorkspaceCase(caseId) {
  state.selectedCaseId = caseId;
  renderCaseList();
  try {
    const [caseItem, documents, auditEvents] = await Promise.all([
      workspaceJson(`/api/cases/${encodeURIComponent(caseId)}`),
      workspaceJson(`/api/cases/${encodeURIComponent(caseId)}/documents`),
      workspaceJson(`/api/cases/${encodeURIComponent(caseId)}/audit-events`),
    ]);
    els.workspaceEmpty.style.display = 'none';
    els.workspaceContent.style.display = 'block';
    els.workspaceCaseTitle.textContent = caseItem.title;
    els.workspaceCaseDescription.textContent = caseItem.description || '未填写案件摘要';
    els.workspaceCaseStatus.textContent = caseItem.status === 'active' ? '进行中' : caseItem.status;
    els.workspaceDocumentList.replaceChildren();
    els.workspaceAuditList.replaceChildren();
    if (documents.length) {
      documents.forEach(item => addWorkspaceRow(
        els.workspaceDocumentList,
        `${item.title} · ${item.status}`,
        item.document_type,
      ));
    } else {
      addWorkspaceRow(els.workspaceDocumentList, '尚无文书草稿');
    }
    if (auditEvents.length) {
      auditEvents.slice(0, 5).forEach(item => addWorkspaceRow(
        els.workspaceAuditList,
        item.action,
        new Date(item.created_at).toLocaleString(),
      ));
    } else {
      addWorkspaceRow(els.workspaceAuditList, '尚无审计记录');
    }
  } catch (error) {
    state.selectedCaseId = null;
    renderCaseList();
    els.workspaceContent.style.display = 'none';
    els.workspaceEmpty.style.display = 'grid';
    showToast(`无法打开案件：${error.message}`, 'error');
  }
}

async function createWorkspaceCase(event) {
  event.preventDefault();
  const title = els.caseTitleInput.value.trim();
  if (!title) return;
  try {
    const created = await workspaceJson('/api/cases', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, description: els.caseDescriptionInput.value.trim() }),
    });
    els.caseCreateForm.reset();
    await loadWorkspaceCases();
    await selectWorkspaceCase(created.case_id);
    showToast('案件已创建');
  } catch (error) {
    showToast(`创建案件失败：${error.message}`, 'error');
  }
}

async function createWorkspaceDocument(event) {
  event.preventDefault();
  if (!state.selectedCaseId) {
    showToast('请先选择案件', 'warning');
    return;
  }
  const title = els.documentTitleInput.value.trim();
  const documentType = els.documentTypeInput.value.trim();
  if (!title || !documentType) return;
  try {
    await workspaceJson(`/api/cases/${encodeURIComponent(state.selectedCaseId)}/documents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        document_type: documentType,
        content: els.documentContentInput.value,
      }),
    });
    els.documentCreateForm.reset();
    els.documentTypeInput.value = 'legal_memo';
    await selectWorkspaceCase(state.selectedCaseId);
    showToast('文书草稿已保存');
  } catch (error) {
    showToast(`保存文书失败：${error.message}`, 'error');
  }
}

function clearLocalHistoryCache() {
  if (!state.history.length) {
    showToast('本地会话缓存已经为空');
    return;
  }
  if (!confirm('仅清除本浏览器保存的会话副本？服务端会话不会被删除。')) return;
  state.history = [];
  try { localStorage.removeItem('lvyan_history'); } catch { /* 忽略私有模式限制 */ }
  renderHistory();
  updateSettingsHistoryCount();
  showToast('已清除本地会话缓存');
}

async function responseError(resp, fallback) {
  const payload = await resp.json().catch(() => ({}));
  return payload.detail || payload.message || `${fallback}（HTTP ${resp.status}）`;
}

// =========================================================================
// 健康检查
// =========================================================================
async function checkHealth() {
  try {
    const resp = await apiFetch('/api/health');
    const data = await resp.json();
    const dot = els.healthStatus.querySelector('.dot');
    const text = els.healthStatus.querySelector('.health-text');

    if (data.status === 'ok') {
      dot.className = 'dot dot-ok';
      text.textContent = '服务正常';
    } else {
      dot.className = 'dot dot-pending';
      const parts = [];
      if (data.database !== 'ok') parts.push('数据库');
      if (data.retrieval !== 'ok') parts.push('检索');
      if (data.model_gateway !== 'ok') parts.push('模型网关');
      text.textContent = parts.length ? `降级: ${parts.join(' · ')}` : '服务降级';
    }
  } catch {
    const dot = els.healthStatus.querySelector('.dot');
    const text = els.healthStatus.querySelector('.health-text');
    dot.className = 'dot dot-error';
    text.textContent = '服务离线';
  }
}

// =========================================================================
// 发送查询
// =========================================================================
async function sendQuery() {
  const query = els.input.value.trim();
  if (!query || state.isRunning) return;

  // 隐藏欢迎屏，显示消息区
  els.welcome.style.display = 'none';
  els.messages.style.display = 'flex';
  els.exportBtn.style.display = 'block';

  // 添加用户消息（含附件标记）
  const attachmentNames = state.attachments.map(a => a.filename);
  addMessage('user', query, false, attachmentNames);

  // 保存 lastQuery 用于重新生成
  state.lastQuery = query;

  // 清空输入
  els.input.value = '';
  autoResize();
  updateCharCount();

  // 禁用发送，显示停止
  state.isRunning = true;
  state.runStartTime = Date.now();
  els.messages.setAttribute('aria-busy', 'true');
  els.sendBtn.style.display = 'none';
  els.stopBtn.style.display = 'flex';
  els.uploadBtn.disabled = true;

  // 重置进度（语义阶段由 phase_start / phase_progress 事件驱动）
  showProgress(true);

  // 深度法律分析可能耗时较长，5 分钟后才主动请求服务端停止。
  // P1-2：定时器存到全局 runTimeoutId，由 finalizeRun/resetRunState 清理，
  // 不在 POST /run 的 finally 中清理（否则 POST 返回即被取消，超时形同虚设）。
  if (runTimeoutId) clearTimeout(runTimeoutId);
  runTimeoutId = setTimeout(async () => {
    if (state.isRunning && state.runStartTime && (Date.now() - state.runStartTime > 300000)) {
      console.warn('Agent 运行超时（5 分钟），请求服务端停止');
      await stopGeneration('运行超过 5 分钟，已停止。你可以缩短问题后重试。');
    }
  }, 301000);

  try {
    // 启动 Agent
    const body = {
      query,
      thread_id: state.threadId,
    };
    // 附加 file_id 列表
    if (state.attachments.length > 0) {
      body.attachments = state.attachments.map(a => a.file_id);
    }

    const resp = await apiFetch('/api/agent/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!resp.ok) throw new Error(await responseError(resp, '请求失败'));
    const data = await resp.json();

    state.runId = data.run_id;
    state.threadId = data.thread_id;

    // 更新顶栏
    els.threadLabel.textContent = query.length > 20
      ? query.slice(0, 20) + '…'
      : query;

    // 添加 Agent 占位消息
    addMessage('agent', '', true);
    saveHistory(state.threadId);

    // 清空已上传附件（已提交给 Agent）
    clearAttachments();

    // 启动 SSE 监听
    listenSSE(data.run_id);
  } catch (err) {
    addMessage('agent', `请求失败: ${err.message}`);
    resetRunState();
  }
  // 注意：此处不再 finally clearTimeout —— 见上方 runTimeoutId 说明。
}

// 停止生成：同时通知服务端取消后台任务。
async function stopGeneration(message = '已停止生成。') {
  if (!state.isRunning || !state.runId) return;
  els.stopBtn.disabled = true;
  try {
    const resp = await apiFetch(`/api/agent/cancel/${state.runId}`, { method: 'POST' });
    if (resp.status === 409) {
      showToast('任务已结束，正在同步最终结果', 'info');
      return;
    }
    // P1-2：503 = 持久化失败（任务可能仍在运行）；404 = run 不存在（可能跨实例）。
    // 这两种情况都不应直接当作「已成功停止」并关闭 SSE。
    if (resp.status === 503) {
      throw new Error('服务端持久化失败，任务可能仍在后台运行');
    }
    if (resp.status === 404) {
      // run 不在本实例、已结束或已过期：不假定成功，提示用户刷新查看实际状态。
      showToast('未找到运行任务，可能已在其他实例结束', 'warning');
      finalizeRun();
      return;
    }
    if (!resp.ok) {
      throw new Error(await responseError(resp, '停止失败'));
    }
    // 202 cancel_requested：远端实例将停止，等待 SSE cancelled 事件到达。
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 202 || data.status === 'cancel_requested') {
      showToast('已请求远端实例停止，请稍候', 'info');
      // 不关闭 SSE，等待 cancelled 事件
      return;
    }
    closeSSE();
    updateLastAgentMessage(message);
    finalizeRun();
  } catch (err) {
    showToast(`${err.message}，任务可能仍在后台运行`, 'error');
  } finally {
    els.stopBtn.disabled = false;
  }
}

// =========================================================================
// SSE 监听（P2：改用 fetch + ReadableStream，可携带 Authorization；EventSource 不能设 Header）
// =========================================================================
async function listenSSE(runId) {
  closeSSE();
  const controller = new AbortController();
  currentSSEController = controller;

  let resp;
  try {
    resp = await apiFetch(`/api/agent/stream/${runId}`, {
      signal: controller.signal,
      headers: { Accept: 'text/event-stream' },
    });
  } catch (err) {
    if (err.name === 'AbortError') return;
    console.error('SSE connect error:', err);
    if (state.isRunning) {
      updateLastAgentMessage('连接中断，请重试。');
      resetRunState();
    }
    return;
  }

  if (!resp.ok || !resp.body) {
    let reason = '无法连接流';
    try {
      const body = await resp.json();
      reason = body.detail || reason;
    } catch { /* ignore */ }
    if (state.isRunning) {
      updateLastAgentMessage(`连接中断（${reason}），请重试。`);
      resetRunState();
    }
    currentSSEController = null;
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let terminalReceived = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // 逐帧解析 SSE：帧之间以空行（\n\n）分隔
      let sep;
      while ((sep = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const dataLine = frame.split('\n').find(line => line.startsWith('data:'));
        if (dataLine) {
          try {
            const event = JSON.parse(dataLine.slice(5).trim());
            if (['final_output', 'error', 'cancelled'].includes(event.event)) {
              terminalReceived = true;
            }
            handleSSEEvent(event);
          } catch (err) {
            console.error('SSE parse error:', err);
          }
        }
      }
    }
    if (!terminalReceived && state.isRunning && !controller.signal.aborted) {
      updateLastAgentMessage('连接意外结束，请重新连接或刷新查看运行状态。');
      resetRunState();
    }
  } catch (err) {
    if (err.name === 'AbortError') return; // 主动停止，静默退出
    console.error('SSE stream error:', err);
    if (state.isRunning) {
      updateLastAgentMessage('连接中断，请重试。');
      resetRunState();
    }
  } finally {
    if (currentSSEController === controller) currentSSEController = null;
  }
}

function handleSSEEvent(event) {
  switch (event.event) {
    case 'phase_start':
      handlePhaseStart(event);
      break;

    case 'phase_progress':
      handlePhaseProgress(event);
      break;

    case 'node_error':
      updateNodeChip(event.node, 'error');
      showToast(`${NODE_LABELS[event.node] || event.node}执行失败`, 'error');
      break;

    case 'hitl_required':
      showHitlPanel(event.message || 'Agent 尝试执行不可逆操作，请确认是否批准。');
      break;

    case 'final_output':
      closeSSE();
      if (event.answer && event.schema_version === 'legal_answer_v1'
          && event.answer.meta && event.answer.meta.analysis_mode !== 'light'
          && window.renderLegalAnswer) {
        updateLastAgentMessageStructured(event.answer, event.markdown_fallback || event.output || '');
      } else {
        updateLastAgentMessage(event.output || '(无输出)');
      }
      // P1-3：文书已生成 → 在最后一条 Agent 消息中追加 DOCX 下载按钮
      if (event.document_file && event.document_file.download_url) {
        appendDocumentDownload(event.document_file);
      }
      finalizeRun();
      break;

    case 'warning':
      showToast(event.message || '部分状态未能持久化，请保存当前内容', 'warning');
      addConversationNotice(
        event.message || '部分状态未能持久化，请保存当前内容',
        'warning',
      );
      break;

    case 'cancelled':
      closeSSE();
      updateLastAgentMessage(event.message || '已停止生成。');
      finalizeRun();
      break;

    case 'error':
      closeSSE();
      updateLastAgentMessage(`运行错误: ${event.message || '未知错误'}`);
      finalizeRun();
      break;
  }
}

function resetRunState() {
  state.isRunning = false;
  state.runStartTime = 0;
  els.sendBtn.style.display = 'flex';
  els.stopBtn.style.display = 'none';
  els.uploadBtn.disabled = false;
  els.hitlPanel.style.display = 'none';
  els.messages.setAttribute('aria-busy', 'false');
  updateCharCount();
  // P1-2：清理全局运行超时定时器（终态 / 重置时）
  if (runTimeoutId) {
    clearTimeout(runTimeoutId);
    runTimeoutId = null;
  }
}

function finalizeRun() {
  resetRunState();
  // 保存到历史
  saveHistory(state.threadId);
  checkHealth();
}

// =========================================================================
// 进度条（P2：语义阶段驱动，不再依赖 LangGraph 节点数）
// 后端通过 phase_start / phase_progress 事件推送阶段，前端只消费事件。
// =========================================================================
function showProgress(show) {
  els.progressBar.style.display = show ? 'block' : 'none';
  if (show) {
    els.progressFill.style.width = '0%';
    els.progressNodes.innerHTML = '';
    els.progressBar.setAttribute('aria-valuenow', '0');
  }
}

// phase_start：新阶段开始 → 创建/点亮对应 chip（label 由后端下发，前端无映射表）
function handlePhaseStart(event) {
  const chipId = `phase-${event.phase}`;
  let chip = $(chipId);
  if (!chip) {
    chip = document.createElement('span');
    chip.className = 'node-chip running';
    chip.id = chipId;
    chip.textContent = event.label || event.phase;
    els.progressNodes.appendChild(chip);
  } else {
    chip.className = 'node-chip running';
  }
}

// phase_progress：某阶段完成 → 标记 chip done + 更新进度填充（completed/total）
function handlePhaseProgress(event) {
  const chip = $(`phase-${event.phase}`);
  if (chip) chip.className = 'node-chip done';
  const total = event.total > 0 ? event.total : 1;
  const pct = Math.min((event.completed / total) * 100, 100);
  els.progressFill.style.width = `${pct}%`;
  els.progressBar.setAttribute('aria-valuenow', String(Math.round(pct)));
}

function updateNodeChip(node, status) {
  // node_error 时点亮对应语义阶段 chip（尽力而为；未知节点忽略）
  const phase = nodeToPhaseKey(node);
  if (!phase) return;
  const chip = $(`phase-${phase}`);
  if (chip) chip.className = 'node-chip error';
}

function nodeToPhaseKey(node) {
  const map = {
    preflight: 'comprehension',
    jurisdiction_triage: 'comprehension',
    fact_extractor: 'comprehension',
    missing_fact_assessor: 'comprehension',
    attachment_retriever: 'preparation',
    planner: 'preparation',
    parallel_retrieval: 'retrieval',
    authority_resolver: 'retrieval',
    legal_reasoner: 'analysis',
    critic: 'verification',
    citation_verifier: 'verification',
    output_guardrail: 'verification',
    composer: 'generation',
    legal_answer_finalizer: 'generation',
  };
  return map[node] || null;
}

// =========================================================================
// HITL
// =========================================================================
function showHitlPanel(message) {
  els.hitlMessage.textContent = message;
  els.hitlPanel.style.display = 'block';
  els.hitlEditArea.style.display = 'none';
  els.hitlEditText.value = '';
  els.hitlPanel.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

async function resolveHitl(action, editedOutput = null) {
  if (!state.runId) return;
  els.hitlPanel.style.display = 'none';
  try {
    const resp = await apiFetch(`/api/agent/hitl/${state.runId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, edited_output: editedOutput }),
    });
    if (!resp.ok) throw new Error(await responseError(resp, '审批提交失败'));
  } catch (err) {
    console.error('HITL resolve failed:', err);
    els.hitlPanel.style.display = 'block';
    showToast(err.message, 'error');
  }
}

// =========================================================================
// 文件上传
// =========================================================================
async function handleFileUpload(e) {
  const files = e.target.files;
  if (files && files.length > 0) {
    await handleFiles(files);
  }
  e.target.value = '';
}

async function handleFiles(files) {
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) {
      showToast(`文件 ${file.name} 超过 10 MB 限制`);
      continue;
    }

    showToast(`正在上传 ${file.name}…`);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const resp = await apiFetch('/api/upload', {
        method: 'POST',
        body: formData,
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      state.attachments.push(data);
      renderAttachments();
      // 根据转换器类型显示不同提示
      const converterMsg = data.converter && data.converter !== 'none'
        ? ` · ${data.converter} · ${data.char_count} 字`
        : '';
      showToast(`已上传 ${file.name}${converterMsg}`);
    } catch (err) {
      showToast(`上传失败: ${err.message}`);
    }
  }
}

function renderAttachments() {
  if (state.attachments.length === 0) {
    els.attachmentPreview.style.display = 'none';
    els.attachmentPreview.innerHTML = '';
    return;
  }

  els.attachmentPreview.style.display = 'flex';
  els.attachmentPreview.innerHTML = '';

  state.attachments.forEach((att, idx) => {
    const chip = document.createElement('div');
    chip.className = 'att-chip';

    // 根据文件类别显示不同图标
    const icon = document.createElement('span');
    icon.className = 'att-icon';
    const cat = att.category || 'unknown';
    if (cat === 'image') {
      icon.textContent = '🖼️';
    } else if (cat === 'doc') {
      icon.textContent = '📄';
    } else if (cat === 'text') {
      icon.textContent = '📝';
    } else {
      icon.textContent = '📎';
    }

    const name = document.createElement('span');
    name.className = 'att-name';
    name.textContent = att.filename;
    const converterLabel = att.converter ? ` · ${att.converter}` : '';
    const charLabel = att.char_count ? ` · ${att.char_count} 字` : '';
    name.title = `${att.filename} (${formatSize(att.size)})${converterLabel}${charLabel}`;

    const remove = document.createElement('button');
    remove.className = 'att-remove';
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = '移除';
    remove.setAttribute('aria-label', `移除附件：${att.filename}`);
    remove.addEventListener('click', () => {
      state.attachments.splice(idx, 1);
      renderAttachments();
    });

    chip.appendChild(icon);
    chip.appendChild(name);
    chip.appendChild(remove);
    els.attachmentPreview.appendChild(chip);
  });
}

function clearAttachments() {
  state.attachments = [];
  renderAttachments();
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// =========================================================================
// 消息渲染
// =========================================================================
function addMessage(role, content, isTyping = false, attachmentNames = []) {
  const div = document.createElement('div');
  div.className = `msg msg-${role}`;
  div.dataset.role = role === 'agent' ? 'assistant' : role;
  div.dataset.content = content || '';
  div.dataset.attachments = JSON.stringify(attachmentNames || []);

  const avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  avatar.textContent = role === 'user' ? '你' : '律';

  const body = document.createElement('div');
  body.className = 'msg-body';

  const name = document.createElement('div');
  name.className = 'msg-name';
  name.textContent = role === 'user' ? '用户' : '律言 Agent';

  const contentDiv = document.createElement('div');
  contentDiv.className = 'msg-content';

  if (isTyping) {
    contentDiv.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  } else {
    contentDiv.innerHTML = renderMarkdown(content);
  }

  body.appendChild(name);
  body.appendChild(contentDiv);

  // 附件标记（用户消息）
  if (role === 'user' && attachmentNames.length > 0) {
    const attDiv = document.createElement('div');
    attDiv.className = 'msg-attachments';
    attachmentNames.forEach(fn => {
      const tag = document.createElement('span');
      tag.className = 'att-tag';
      tag.textContent = `📎 ${fn}`;
      attDiv.appendChild(tag);
    });
    body.appendChild(attDiv);
  }

  // Agent 消息：添加操作按钮
  if (role === 'agent' && !isTyping) {
    const actions = createMsgActions(content);
    body.appendChild(actions);
  }

  div.appendChild(avatar);
  div.appendChild(body);
  els.messages.appendChild(div);

  els.chatArea.scrollTop = els.chatArea.scrollHeight;
}

function createMsgActions(content) {
  const actions = document.createElement('div');
  actions.className = 'msg-actions';

  const copyBtn = document.createElement('button');
  copyBtn.className = 'msg-action-btn';
  copyBtn.textContent = '复制';
  copyBtn.title = '复制回答';
  copyBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(content).then(() => {
      showToast('已复制到剪贴板');
    }).catch(() => showToast('复制失败'));
  });

  const regenBtn = document.createElement('button');
  regenBtn.className = 'msg-action-btn';
  regenBtn.textContent = '重新生成';
  regenBtn.title = '基于原问题重新生成回答';
  regenBtn.addEventListener('click', regenerateLast);

  actions.appendChild(copyBtn);
  actions.appendChild(regenBtn);
  return actions;
}

function updateLastAgentMessage(content) {
  const agentMessages = els.messages.querySelectorAll('.msg-agent');
  const lastMsg = agentMessages[agentMessages.length - 1];
  if (!lastMsg) return;
  const contentDiv = lastMsg.querySelector('.msg-content');
  lastMsg.dataset.content = content || '';
  contentDiv.innerHTML = renderMarkdown(content);

  // 如果还没有操作按钮，添加
  if (!lastMsg.querySelector('.msg-actions')) {
    const body = lastMsg.querySelector('.msg-body');
    body.appendChild(createMsgActions(content));
  }

  els.chatArea.scrollTop = els.chatArea.scrollHeight;
}

// 结构化法律分析渲染：使用 components.js 的 renderLegalAnswer 渲染 LegalAnswerV1
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

// P1-3：文书下载按钮
function appendDocumentDownload(docFile) {
  const agentMessages = els.messages.querySelectorAll('.msg-agent');
  const lastMsg = agentMessages[agentMessages.length - 1];
  if (!lastMsg) return;
  // 避免重复追加
  if (lastMsg.querySelector('.doc-download-btn')) return;
  const body = lastMsg.querySelector('.msg-body');
  if (!body) return;
  const wrapper = document.createElement('div');
  wrapper.className = 'doc-download-wrapper';
  const sizeKb = docFile.file_size ? (docFile.file_size / 1024).toFixed(1) + ' KB' : '';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'doc-download-btn';
  btn.innerHTML = '⬇ 下载文书 ' + (docFile.filename || '') + (sizeKb ? ' (' + sizeKb + ')' : '');
  // P1-5：改用 fetch 下载，可携带认证头（JWT），而非裸 <a href>（后者无法附加 Authorization）
  btn.addEventListener('click', async function () {
    btn.disabled = true;
    btn.textContent = '下载中…';
    try {
      await downloadDocumentFile(docFile);
    } catch (err) {
      showToast('下载失败：' + (err && err.message ? err.message : String(err)), 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '⬇ 下载文书 ' + (docFile.filename || '') + (sizeKb ? ' (' + sizeKb + ')' : '');
    }
  });
  wrapper.appendChild(btn);
  body.appendChild(wrapper);
  // P1-6：把下载产物记录到消息 dataset，供 captureConversation 持久化，
  // 历史会话重新打开时可恢复下载按钮。
  const runId = runIdFromDownloadUrl(docFile.download_url);
  if (runId) {
    const artifacts = JSON.parse(lastMsg.dataset.artifacts || '[]');
    if (!artifacts.some(a => a.type === 'document' && a.run_id === runId)) {
      artifacts.push({
        type: 'document',
        run_id: runId,
        filename: docFile.filename || '法律文书.docx',
        format: docFile.format || 'docx',
        file_size: docFile.file_size || 0,
      });
      lastMsg.dataset.artifacts = JSON.stringify(artifacts);
    }
  }
  els.chatArea.scrollTop = els.chatArea.scrollHeight;
}

// P1-6：从固定格式的下载路径 /api/documents/{run_id}/download 提取 run_id
function runIdFromDownloadUrl(url) {
  const parts = String(url || '').split('/').filter(Boolean);
  const idx = parts.indexOf('documents');
  return (idx >= 0 && parts[idx + 1]) ? parts[idx + 1] : null;
}

// P1-6：历史 artifact（仅 type/run_id/filename 等 public 字段）→ 可下载的 docFile
function documentArtifactToDocFile(artifact) {
  return {
    filename: artifact.filename,
    format: artifact.format,
    file_size: artifact.file_size,
    download_url: '/api/documents/' + artifact.run_id + '/download',
  };
}

// P1-5：通过 fetch 下载文书（支持认证头 + blob 触发浏览器保存）
async function downloadDocumentFile(docFile) {
  const resp = await apiFetch(docFile.download_url);
  if (!resp.ok) {
    let detail = '';
    try {
      const body = await resp.json();
      detail = body.detail || '';
    } catch (e) { /* ignore */ }
    throw new Error(detail || ('HTTP ' + resp.status));
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = docFile.filename || 'document.docx';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// 重新生成
function regenerateLast() {
  if (state.isRunning) {
    showToast('请等待当前运行完成');
    return;
  }
  if (!state.lastQuery) {
    showToast('无可重新生成的查询');
    return;
  }

  // 移除最后一条 Agent 消息
  const lastMsg = els.messages.lastElementChild;
  if (lastMsg && lastMsg.classList.contains('msg-agent')) {
    lastMsg.remove();
  }

  // 重新发送 lastQuery
  els.input.value = state.lastQuery;
  sendQuery();
}

// =========================================================================
// 简易 Markdown 渲染
// =========================================================================
function renderMarkdown(text) {
  if (!text) return '';
  let html = text;

  // 转义 HTML
  html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // 代码块
  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    return `<pre><code>${code.trim()}</code></pre>`;
  });

  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // 加粗
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // 标题
  html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');

  // 引用块
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

  // 无序列表
  html = html.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');

  // 有序列表
  html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

  // 分割线
  html = html.replace(/^---$/gm, '<hr>');

  // 段落分割
  html = html.split(/\n\n+/).map(p => {
    if (p.startsWith('<')) return p;
    return `<p>${p.replace(/\n/g, '<br>')}</p>`;
  }).join('');

  return html;
}

// =========================================================================
// 输入框自动高度与字数统计
// =========================================================================
function autoResize() {
  els.input.style.height = 'auto';
  els.input.style.height = Math.min(els.input.scrollHeight, 200) + 'px';
}

function updateCharCount() {
  const len = els.input.value.length;
  els.charCount.textContent = `${len.toLocaleString('zh-CN')} / 50,000 字`;
  els.sendBtn.disabled = state.isRunning || !els.input.value.trim();
}

// =========================================================================
// 历史记录管理
// =========================================================================
function persistHistory() {
  try {
    localStorage.setItem('lvyan_history', JSON.stringify(state.history));
  } catch {
    // localStorage 容量不足时，仅保留最近 5 个完整会话，其余保留摘要。
    state.history = state.history.slice(0, 30).map((item, index) => (
      index < 5 ? item : { ...item, messages: [] }
    ));
    try {
      localStorage.setItem('lvyan_history', JSON.stringify(state.history));
    } catch {
      // 服务端仍是持久化主来源，浏览器缓存失败不阻断对话。
    }
    showToast('浏览器空间不足，较早对话将从服务端按需加载', 'warning');
  }
}

function captureConversation() {
  return Array.from(els.messages.querySelectorAll('.msg'))
    .map(msg => ({
      role: msg.dataset.role || (
        msg.classList.contains('msg-user') ? 'user' : 'assistant'
      ),
      content: msg.dataset.content || '',
      structured_answer: msg.dataset.structuredAnswer ? JSON.parse(msg.dataset.structuredAnswer) : null,
      attachments: JSON.parse(msg.dataset.attachments || '[]'),
      artifacts: JSON.parse(msg.dataset.artifacts || '[]'),
      created_at: Date.now() / 1000,
    }))
    .filter(message => message.content);
}

async function loadHistory() {
  try {
    const raw = localStorage.getItem('lvyan_history');
    if (raw) state.history = JSON.parse(raw);
  } catch { state.history = []; }
  renderHistory();

  try {
    const resp = await apiFetch('/api/agent/threads');
    if (!resp.ok) throw new Error(await responseError(resp, '历史同步失败'));
    const data = await resp.json();
    const serverThreadIds = new Set(data.threads.map(thread => thread.thread_id));
    // 只保留仍可由服务端恢复的会话，或保留了完整本地消息的离线副本。
    // 早期版本仅保存了 thread_id/标题，服务重启后会留下无法打开的幽灵条目。
    const recoverableLocal = state.history.filter(item => (
      serverThreadIds.has(item.threadId)
      || (Array.isArray(item.messages) && item.messages.length > 0)
    ));
    const localById = new Map(recoverableLocal.map(item => [item.threadId, item]));
    data.threads.forEach(thread => {
      const local = localById.get(thread.thread_id);
      localById.set(thread.thread_id, {
        threadId: thread.thread_id,
        query: local?.query || thread.title || '未命名会话',
        fullQuery: local?.fullQuery || thread.title || '',
        mode: local?.mode || thread.complexity || 'light',
        ts: local?.ts
          || Number(thread.updated_at || thread.created_at || 0) * 1000
          || Date.now(),
        messages: Array.isArray(local?.messages) ? local.messages : [],
      });
    });
    state.history = Array.from(localById.values())
      .sort((a, b) => Number(b.ts || 0) - Number(a.ts || 0))
      .slice(0, 100);
    persistHistory();
    renderHistory();
  } catch (err) {
    console.warn('History sync failed:', err);
  }
}

function saveHistory(threadId) {
  if (!threadId) return;
  const messages = captureConversation();
  const firstUser = messages.find(message => message.role === 'user');
  const query = firstUser?.content || state.lastQuery || '';
  const existing = state.history.findIndex(h => h.threadId === threadId);
  const item = {
    threadId,
    query: query.slice(0, 40),
    fullQuery: query,
    mode: state.mode,
    ts: Date.now(),
    messages,
  };
  if (existing >= 0) {
    state.history[existing] = item;
  } else {
    state.history.unshift(item);
  }
  if (state.history.length > 100) state.history = state.history.slice(0, 100);
  persistHistory();
  renderHistory();
}

function renderHistory() {
  els.historyList.innerHTML = '';
  const searchTerm = (els.historySearch.value || '').toLowerCase().trim();

  state.history.forEach((item, i) => {
    // 搜索过滤
    if (searchTerm) {
      const text = (item.fullQuery || item.query || '').toLowerCase();
      if (!text.includes(searchTerm)) return;
    }

    const div = document.createElement('div');
    div.className = 'history-item';
    if (item.threadId === state.threadId) div.classList.add('active');

    const text = document.createElement('button');
    text.type = 'button';
    text.className = 'history-text history-open';
    text.textContent = item.query || `会话 ${i + 1}`;
    text.title = `${item.fullQuery || item.query}\n${new Date(item.ts).toLocaleString()}`;
    text.addEventListener('click', () => {
      loadThreadState(item.threadId, item);
    });

    const delBtn = document.createElement('button');
    delBtn.className = 'history-del';
    delBtn.type = 'button';
    delBtn.title = '删除此对话';
    delBtn.setAttribute('aria-label', `删除对话：${item.query || `会话 ${i + 1}`}`);
    delBtn.textContent = '×';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteHistoryItem(item.threadId);
    });

    div.appendChild(text);
    div.appendChild(delBtn);
    els.historyList.appendChild(div);
  });

  // 空状态
  if (els.historyList.children.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'history-empty';
    empty.textContent = searchTerm ? '无匹配对话' : '暂无历史对话';
    els.historyList.appendChild(empty);
  }
}

async function loadThreadState(threadId, item) {
  if (state.isRunning) {
    showToast('请先停止当前生成，再切换会话', 'warning');
    return;
  }
  state.threadId = threadId;
  setActiveMode();
  els.threadLabel.textContent = (item.fullQuery || item.query || '').slice(0, 20);
  renderHistory();
  els.welcome.style.display = 'none';
  els.messages.style.display = 'flex';
  els.exportBtn.style.display = 'block';
  els.messages.innerHTML = '';
  els.messages.setAttribute('aria-busy', 'true');
  addConversationNotice('正在加载完整对话…', 'loading');

  try {
    const resp = await apiFetch(`/api/agent/state/${threadId}`);
    const localMessages = Array.isArray(item.messages) ? item.messages : [];
    if (!resp.ok) {
      const reason = await responseError(resp, '无法加载会话');
      if (localMessages.length > 0) {
        renderConversation(localMessages);
        addConversationNotice(`${reason}，当前显示浏览器缓存。`, 'warning');
        return;
      }
      if (resp.status === 404) {
        state.history = state.history.filter(history => history.threadId !== threadId);
        persistHistory();
        renderHistory();
        state.threadId = null;
        addConversationNotice('该历史会话已不可恢复，已从列表移除。', 'warning');
        return;
      }
      throw new Error(reason);
    }
    const data = await resp.json();
    const serverMessages = Array.isArray(data.messages) ? data.messages : [];
    let messages = serverMessages.length > 0 ? serverMessages : localMessages;
    if (messages.length === 0 && data.final_output) {
      messages = [
        { role: 'user', content: item.fullQuery || item.query || '(历史会话)' },
        { role: 'assistant', content: data.final_output },
      ];
    }
    renderConversation(messages);
    if (messages.length === 0) {
      addConversationNotice('该会话暂无可显示的消息。', 'empty');
    }
    const lastUser = [...messages].reverse().find(message => message.role === 'user');
    state.lastQuery = lastUser?.content || '';
    item.messages = messages;
    persistHistory();
  } catch (err) {
    els.messages.innerHTML = '';
    addConversationNotice(err.message || '无法加载会话，请稍后重试。', 'error', () => {
      loadThreadState(threadId, item);
    });
  } finally {
    els.messages.setAttribute('aria-busy', 'false');
  }
  if (window.matchMedia('(max-width: 768px)').matches) {
    state.sidebarCollapsed = true;
    document.body.classList.add('sidebar-collapsed');
    els.sidebarToggle.setAttribute('aria-expanded', 'false');
  }
}

function renderConversation(messages) {
  els.messages.innerHTML = '';
  els.chatArea.classList.remove('la-active');
  messages.forEach(message => {
    const role = message.role === 'assistant' ? 'agent' : 'user';
    addMessage(
      role,
      message.content || '',
      false,
      Array.isArray(message.attachments) ? message.attachments : [],
    );
    // 恢复结构化法律分析视图
    if (message.structured_answer && role === 'agent'
        && message.structured_answer.meta
        && message.structured_answer.meta.analysis_mode !== 'light'
        && window.renderLegalAnswer) {
      const lastMsg = els.messages.querySelector('.msg-agent:last-child');
      const lastContent = lastMsg ? lastMsg.querySelector('.msg-content') : null;
      if (lastMsg && lastContent) {
        lastMsg.dataset.structuredAnswer = JSON.stringify(message.structured_answer);
        lastContent.innerHTML = window.renderLegalAnswer(message.structured_answer);
        lastMsg.classList.add('msg-structured');
        els.chatArea.classList.add('la-active');
      }
    }
    // P1-6：恢复历史会话中已产出的文书下载按钮
    if (role === 'agent' && Array.isArray(message.artifacts)) {
      message.artifacts.forEach(artifact => {
        if (artifact && artifact.type === 'document' && artifact.run_id) {
          appendDocumentDownload(documentArtifactToDocFile(artifact));
        }
      });
    }
  });
}

function addConversationNotice(message, type = 'info', retry = null) {
  const notice = document.createElement('div');
  notice.className = `conversation-notice notice-${type}`;
  notice.setAttribute('role', type === 'error' ? 'alert' : 'status');
  const text = document.createElement('span');
  text.textContent = message;
  notice.appendChild(text);
  if (retry) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '重试';
    button.addEventListener('click', retry);
    notice.appendChild(button);
  }
  els.messages.appendChild(notice);
}

async function deleteHistoryItem(threadId) {
  const index = state.history.findIndex(item => item.threadId === threadId);
  const item = state.history[index];
  if (!item) return;

  try {
    const resp = await apiFetch(`/api/agent/state/${item.threadId}`, { method: 'DELETE' });
    if (!resp.ok && resp.status !== 404) {
      throw new Error(await responseError(resp, '删除失败'));
    }
  } catch (err) {
    showToast(err.message || '删除失败，请稍后重试', 'error');
    return;
  }

  state.history.splice(index, 1);
  persistHistory();
  if (item.threadId === state.threadId) {
    startNewChat();
  }

  renderHistory();
  showToast('已删除对话');
}

async function clearAllHistory() {
  if (state.history.length === 0) {
    showToast('无历史可清空');
    return;
  }

  if (!confirm(`确定清空所有 ${state.history.length} 条历史对话？此操作不可撤销。`)) {
    return;
  }

  const results = await Promise.all(state.history.map(async item => {
    try {
      const resp = await apiFetch(`/api/agent/state/${item.threadId}`, { method: 'DELETE' });
      if (resp.ok || resp.status === 404) return { item, deleted: true };
      return {
        item,
        deleted: false,
        reason: await responseError(resp, '删除失败'),
      };
    } catch {
      return { item, deleted: false, reason: '网络不可用' };
    }
  }));
  state.history = results.filter(result => !result.deleted).map(result => result.item);
  persistHistory();
  renderHistory();
  if (state.history.length === 0) {
    startNewChat();
    showToast('已清空所有历史');
  } else {
    showToast(`${state.history.length} 个会话删除失败，已保留在列表中`, 'error');
  }
}

// =========================================================================
// 新对话
// =========================================================================
function startNewChat() {
  if (state.isRunning) {
    showToast('请先停止当前生成，再开始新对话', 'warning');
    return;
  }
  if (currentSSEController) {
    closeSSE();
  }
  state.threadId = null;
  state.runId = null;
  state.isRunning = false;
  setActiveMode();
  state.lastQuery = '';
  clearAttachments();
  els.welcome.style.display = 'flex';
  els.messages.style.display = 'none';
  els.messages.innerHTML = '';
  els.chatArea.classList.remove('la-active');
  els.threadLabel.textContent = '新对话';
  els.progressBar.style.display = 'none';
  els.hitlPanel.style.display = 'none';
  els.exportBtn.style.display = 'none';
  els.sendBtn.style.display = 'flex';
  els.stopBtn.style.display = 'none';
  els.uploadBtn.disabled = false;
  renderHistory();
}

// =========================================================================
// 导出对话
// =========================================================================
function exportConversation() {
  const msgs = els.messages.querySelectorAll('.msg');
  if (msgs.length === 0) {
    showToast('无对话可导出');
    return;
  }

  let md = `# 律言法律智能体 · 对话记录\n\n`;
  md += `> 导出时间：${new Date().toLocaleString()}\n`;
  md += `\n---\n\n`;

  msgs.forEach(msg => {
    const role = msg.classList.contains('msg-user') ? '👤 **用户**' : '⚖️ **律言 Agent**';
    const content = msg.querySelector('.msg-content')?.textContent || '';
    md += `${role}\n\n${content}\n\n---\n\n`;
  });

  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `lvyan_conversation_${new Date().toISOString().slice(0, 10)}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast('对话已导出');
}

// =========================================================================
// 侧栏折叠
// =========================================================================
function toggleSidebar() {
  state.sidebarCollapsed = !state.sidebarCollapsed;
  document.body.classList.toggle('sidebar-collapsed', state.sidebarCollapsed);
  els.sidebarToggle.setAttribute(
    'aria-expanded',
    String(!state.sidebarCollapsed),
  );
}

// =========================================================================
// Toast 提示
// =========================================================================
let toastTimer = null;
function showToast(message, type = 'info') {
  els.toast.textContent = message;
  els.toast.dataset.type = type;
  els.toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  els.toast.style.display = 'block';
  els.toast.classList.add('toast-show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    els.toast.classList.remove('toast-show');
    setTimeout(() => {
      els.toast.style.display = 'none';
    }, 300);
  }, 2500);
}

// =========================================================================
// 启动
// =========================================================================
init();
