/* =========================================================================
   律言法律智能体 · 前端应用逻辑 v6
   功能：对话 / 文件上传 / 历史管理 / 消息操作 / 导出 / 快捷键
   ========================================================================= */

// --- 全局状态 ---
const state = {
  threadId: null,
  runId: null,
  mode: 'light',
  isRunning: false,
  history: [],        // [{threadId, query, output, mode, ts}]
  completedNodes: new Set(),
  totalNodes: 12,
  runStartTime: 0,
  attachments: [],    // [{file_id, filename, size, content_type, text_preview}]
  lastQuery: '',      // 用于重新生成
  sidebarCollapsed: false,
};

// 全局 EventSource 引用
let currentEventSource = null;

// --- 节点中文名映射 ---
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
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }

  // 模式选择
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      if (state.isRunning) return;
      document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.mode = btn.dataset.mode;
    });
  });

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
  // Esc: 关闭菜单/HITL
  if (e.key === 'Escape') {
    els.msgMenu.style.display = 'none';
    els.hitlPanel.style.display = 'none';
  }
}

// =========================================================================
// 健康检查
// =========================================================================
async function checkHealth() {
  try {
    const resp = await fetch('/api/health');
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
  els.sendBtn.style.display = 'none';
  els.stopBtn.style.display = 'flex';
  els.uploadBtn.disabled = true;

  // 重置进度
  state.completedNodes.clear();
  showProgress(true);

  // 超时保护：60 秒后自动重置
  const timeoutId = setTimeout(() => {
    if (state.isRunning && state.runStartTime && (Date.now() - state.runStartTime > 60000)) {
      console.warn('Agent 运行超时（60s），自动重置状态');
      stopGeneration();
      updateLastAgentMessage('运行超时，请重试。');
    }
  }, 61000);

  try {
    // 启动 Agent
    const body = {
      query,
      thread_id: state.threadId,
      complexity: state.mode,
    };
    // 附加 file_id 列表
    if (state.attachments.length > 0) {
      body.attachments = state.attachments.map(a => a.file_id);
    }

    const resp = await fetch('/api/agent/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    state.runId = data.run_id;
    state.threadId = data.thread_id;

    // 更新顶栏
    els.threadLabel.textContent = query.length > 20
      ? query.slice(0, 20) + '…'
      : query;

    // 添加 Agent 占位消息
    addMessage('agent', '', true);

    // 清空已上传附件（已提交给 Agent）
    clearAttachments();

    // 启动 SSE 监听
    listenSSE(data.run_id);
  } catch (err) {
    addMessage('agent', `请求失败: ${err.message}`);
    resetRunState();
  } finally {
    clearTimeout(timeoutId);
  }
}

// 停止生成
function stopGeneration() {
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }
  resetRunState();
  // 如果 Agent 消息还是空的，移除占位
  const lastMsg = els.messages.lastElementChild;
  if (lastMsg && lastMsg.classList.contains('msg-agent')) {
    const content = lastMsg.querySelector('.msg-content');
    if (!content.textContent.trim() || content.querySelector('.typing-dots')) {
      content.innerHTML = '<p style="color: var(--text-muted);">已停止生成。</p>';
    }
  }
}

// =========================================================================
// SSE 监听
// =========================================================================
function listenSSE(runId) {
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }

  const es = new EventSource(`/api/agent/stream/${runId}`);
  currentEventSource = es;

  es.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handleSSEEvent(event);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };

  es.onerror = () => {
    es.close();
    if (currentEventSource === es) currentEventSource = null;
    if (state.isRunning) {
      updateLastAgentMessage('连接中断，请重试。');
      resetRunState();
    }
  };
}

function handleSSEEvent(event) {
  switch (event.event) {
    case 'node_start':
      updateNodeChip(event.node, 'running');
      break;

    case 'node_end':
      updateNodeChip(event.node, 'done');
      state.completedNodes.add(event.node);
      updateProgressFill();
      break;

    case 'hitl_required':
      showHitlPanel(event.message || 'Agent 尝试执行不可逆操作，请确认是否批准。');
      break;

    case 'final_output':
      if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
      }
      updateLastAgentMessage(event.output || '(无输出)');
      finalizeRun();
      break;

    case 'error':
      if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
      }
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
}

function finalizeRun() {
  resetRunState();
  // 保存到历史
  const lastMsg = els.messages.lastElementChild;
  const output = lastMsg ? lastMsg.querySelector('.msg-content')?.textContent : '';
  saveHistory(state.threadId, state.history.length, output);
  checkHealth();
}

// =========================================================================
// 进度条
// =========================================================================
function showProgress(show) {
  els.progressBar.style.display = show ? 'block' : 'none';
  if (show) {
    els.progressFill.style.width = '0%';
    els.progressNodes.innerHTML = '';
    Object.entries(NODE_LABELS).forEach(([key, label]) => {
      const chip = document.createElement('span');
      chip.className = 'node-chip';
      chip.id = `chip-${key}`;
      chip.textContent = label;
      els.progressNodes.appendChild(chip);
    });
  }
}

function updateNodeChip(node, status) {
  const chip = $(`chip-${node}`);
  if (!chip) return;
  chip.className = `node-chip ${status}`;
}

function updateProgressFill() {
  const pct = (state.completedNodes.size / state.totalNodes) * 100;
  els.progressFill.style.width = `${Math.min(pct, 100)}%`;
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
    await fetch(`/api/agent/hitl/${state.runId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, edited_output: editedOutput }),
    });
  } catch (err) {
    console.error('HITL resolve failed:', err);
    showToast('HITL 响应失败');
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
      const resp = await fetch('/api/upload', {
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
    remove.textContent = '×';
    remove.title = '移除';
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
  const lastMsg = els.messages.lastElementChild;
  if (!lastMsg || !lastMsg.classList.contains('msg-agent')) return;
  const contentDiv = lastMsg.querySelector('.msg-content');
  contentDiv.innerHTML = renderMarkdown(content);

  // 如果还没有操作按钮，添加
  if (!lastMsg.querySelector('.msg-actions')) {
    const body = lastMsg.querySelector('.msg-body');
    body.appendChild(createMsgActions(content));
  }

  els.chatArea.scrollTop = els.chatArea.scrollHeight;
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
  els.charCount.textContent = `${len} 字`;
}

// =========================================================================
// 历史记录管理
// =========================================================================
function loadHistory() {
  try {
    const raw = localStorage.getItem('lvyan_history');
    if (raw) state.history = JSON.parse(raw);
  } catch { state.history = []; }
  renderHistory();
}

function saveHistory(threadId, idx, output) {
  const query = state.lastQuery || '';
  const existing = state.history.findIndex(h => h.threadId === threadId);
  const item = {
    threadId,
    query: query.slice(0, 40),
    fullQuery: query,
    mode: state.mode,
    ts: Date.now(),
  };
  if (existing >= 0) {
    state.history[existing] = item;
  } else {
    state.history.unshift(item);
  }
  if (state.history.length > 50) state.history = state.history.slice(0, 50);
  localStorage.setItem('lvyan_history', JSON.stringify(state.history));
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

    const text = document.createElement('span');
    text.className = 'history-text';
    text.textContent = item.query || `会话 ${i + 1}`;
    text.title = `${item.fullQuery || item.query}\n${new Date(item.ts).toLocaleString()}`;
    text.addEventListener('click', () => {
      loadThreadState(item.threadId, item);
    });

    const delBtn = document.createElement('button');
    delBtn.className = 'history-del';
    delBtn.title = '删除此对话';
    delBtn.textContent = '×';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteHistoryItem(i);
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
  state.threadId = threadId;
  state.mode = item.mode || 'light';
  document.querySelectorAll('.mode-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === state.mode);
  });
  els.threadLabel.textContent = (item.fullQuery || item.query || '').slice(0, 20);
  renderHistory();

  try {
    const resp = await fetch(`/api/agent/state/${threadId}`);
    if (resp.ok) {
      const data = await resp.json();
      els.welcome.style.display = 'none';
      els.messages.style.display = 'flex';
      els.exportBtn.style.display = 'block';
      els.messages.innerHTML = '';
      if (data.final_output) {
        addMessage('user', item.fullQuery || item.query || '(历史会话)');
        addMessage('agent', data.final_output);
      } else {
        addMessage('agent', '该会话暂无最终输出。');
      }
    }
  } catch { /* 忽略 */ }
}

function deleteHistoryItem(index) {
  const item = state.history[index];
  if (!item) return;

  state.history.splice(index, 1);
  localStorage.setItem('lvyan_history', JSON.stringify(state.history));

  if (item.threadId) {
    fetch(`/api/agent/state/${item.threadId}`, { method: 'DELETE' })
      .catch(() => {});
  }

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

  const promises = state.history
    .filter(h => h.threadId)
    .map(h => fetch(`/api/agent/state/${h.threadId}`, { method: 'DELETE' }).catch(() => {}));
  await Promise.all(promises);

  state.history = [];
  localStorage.removeItem('lvyan_history');
  renderHistory();
  startNewChat();
  showToast('已清空所有历史');
}

// =========================================================================
// 新对话
// =========================================================================
function startNewChat() {
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }
  state.threadId = null;
  state.runId = null;
  state.isRunning = false;
  state.lastQuery = '';
  clearAttachments();
  els.welcome.style.display = 'flex';
  els.messages.style.display = 'none';
  els.messages.innerHTML = '';
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
  md += `> 模式：${state.mode}\n\n---\n\n`;

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
}

// =========================================================================
// Toast 提示
// =========================================================================
let toastTimer = null;
function showToast(message) {
  els.toast.textContent = message;
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
