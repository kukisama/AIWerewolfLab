// ═══ 狼人杀前端逻辑 ═══
const API = "http://127.0.0.1:8088";

// ─── 主题切换 ───
(function initTheme() {
  const saved = localStorage.getItem("werewolf-theme") || "light";
  document.documentElement.setAttribute("data-theme", saved);
})();

function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute("data-theme") || "light";
  const next = current === "dark" ? "light" : "dark";
  html.setAttribute("data-theme", next);
  localStorage.setItem("werewolf-theme", next);
  const btn = document.getElementById("btn-theme");
  if (btn) btn.textContent = next === "dark" ? "☀️" : "🌙";
}

// ─── 后端生命周期管理 ───
let backendReady = false;
const backendStatus = document.createElement("div");
backendStatus.id = "backend-status";
backendStatus.style.cssText = "text-align:center;color:var(--text-dim);font-size:13px;margin-top:12px;";

async function initBackend() {
  const statusEl = document.getElementById("backend-status");
  try {
    // 尝试调用 Tauri command 启动后端
    if (window.__TAURI__) {
      if (statusEl) statusEl.textContent = "⏳ 正在启动 Python 后端...";
      const msg = await window.__TAURI__.core.invoke("start_backend");
      console.log("[Tauri]", msg);
      if (statusEl) statusEl.textContent = "⏳ " + msg + " — 等待就绪...";
    }
  } catch (err) {
    console.warn("Tauri invoke 失败:", err);
    if (statusEl) statusEl.textContent = "⚠️ 启动后端失败: " + (err.message || err) + " — 尝试连接已有后端...";
  }
  // 轮询 health check
  for (let i = 0; i < 20; i++) {
    try {
      const resp = await fetch(`${API}/health`, { signal: AbortSignal.timeout(2000) });
      if (resp.ok) {
        backendReady = true;
        if (statusEl) statusEl.textContent = "✅ 后端已就绪";
        setTimeout(() => { if (statusEl) statusEl.style.display = "none"; }, 1500);
        return;
      }
    } catch {}
    await new Promise(r => setTimeout(r, 800));
    if (statusEl) statusEl.textContent = `⏳ 等待后端启动... (${i + 1}/20)`;
  }
  if (statusEl) {
    statusEl.textContent = "❌ 后端连接失败，请检查 Python 后端是否正常";
    statusEl.style.color = "var(--danger)";
  }
}

// ─── DOM 引用 ───
const startScreen   = document.getElementById("start-screen");
const gameScreen    = document.getElementById("game-screen");
const playerNameInput = document.getElementById("player-name");
const btnStart      = document.getElementById("btn-start");
const messagesEl    = document.getElementById("messages");
const chatPanel     = document.getElementById("chat-panel");
const actionPanel   = document.getElementById("action-panel");
const actionPrompt  = document.getElementById("action-prompt");
const actionOptions = document.getElementById("action-options");
const actionInput   = document.getElementById("action-input");
const speakInput    = document.getElementById("speak-input");
const btnSendSpeak  = document.getElementById("btn-send-speak");
const playerSeats   = document.getElementById("player-seats");
const aliveCount    = document.getElementById("alive-count");
const phaseBadge    = document.getElementById("phase-badge");
const roundBadge    = document.getElementById("round-badge");
const btnGodView    = document.getElementById("btn-god-view");
const godViewModal  = document.getElementById("god-view-modal");
const btnCloseGod   = document.getElementById("btn-close-god");

let gameState = null;
let eventSource = null;
let gameMode = "observer";  // "observer" or "player"

// ─── 座位位置（环形排列）───
const SEAT_POSITIONS = [
  { top: "10px",  left: "50%",  transform: "translateX(-50%)" },
  { top: "30px",  right: "20px" },
  { top: "120px", right: "0px" },
  { top: "220px", right: "10px" },
  { bottom: "20px", right: "60px" },
  { bottom: "10px", left: "50%", transform: "translateX(-50%)" },
  { bottom: "20px", left: "60px" },
  { top: "220px", left: "10px" },
  { top: "120px", left: "0px" },
];

const PHASE_LABELS = {
  night: "🌙 夜晚",
  day: "☀️ 白天",
  vote: "✋ 投票",
  ended: "🏆 结束",
};

// ─── 模式选择 ───
const modeSelector = document.getElementById("mode-selector");
const playerNameRow = document.getElementById("player-name-row");
if (modeSelector) {
  modeSelector.querySelectorAll(".btn-mode").forEach(btn => {
    btn.addEventListener("click", () => {
      modeSelector.querySelectorAll(".btn-mode").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      gameMode = btn.dataset.mode;
      playerNameRow.style.display = gameMode === "player" ? "block" : "none";
    });
  });
}

// ─── 开始游戏 ───
btnStart.addEventListener("click", async () => {
  if (!backendReady) {
    alert("后端尚未就绪，请稍候...");
    return;
  }
  const name = playerNameInput.value.trim() || "玩家";
  btnStart.disabled = true;
  btnStart.textContent = "正在开始...";

  try {
    const resp = await fetch(`${API}/game/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ player_name: name, game_mode: gameMode }),
    });
    if (!resp.ok) throw new Error(await resp.text());

    startScreen.style.display = "none";
    gameScreen.style.display = "flex";
    if (gameMode === "observer") {
      btnGodView.style.display = "inline-block";
    }
    document.getElementById("btn-stop-game").style.display = "inline-block";
    connectSSE();
  } catch (err) {
    alert("开始游戏失败！\n\n错误: " + err.message);
    btnStart.disabled = false;
    btnStart.textContent = "开始游戏";
  }
});

// ─── SSE 连接 ───
function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`${API}/game/stream`);

  eventSource.addEventListener("message", (e) => {
    const data = JSON.parse(e.data);
    addMessage(data.sender, data.content, data.type, data.visibility);
  });

  eventSource.addEventListener("state_update", (e) => {
    gameState = JSON.parse(e.data);
    renderSeats();
    updateHeader();
  });

  eventSource.addEventListener("human_input_request", (e) => {
    const data = JSON.parse(e.data);
    showActionPanel(data);
  });

  eventSource.addEventListener("game_over", (e) => {
    const data = JSON.parse(e.data);
    gameState = data;
    renderSeats(true);
    updateHeader();
    btnGodView.style.display = "inline-block";
    document.getElementById("btn-header-summary").style.display = "inline-block";
    document.getElementById("btn-stop-game").style.display = "none";
    hideActionPanel();
  });

  eventSource.onerror = () => {
    console.warn("SSE 连接断开，尝试重连...");
  };
}

// ─── 终止游戏按钮 ───
document.getElementById("btn-stop-game").addEventListener("click", async () => {
  if (!confirm("确定要终止当前游戏吗？")) return;
  const btn = document.getElementById("btn-stop-game");
  btn.disabled = true;
  btn.textContent = "⏳ 终止中...";
  try {
    const resp = await fetch(`${API}/game/stop`, { method: "POST" });
    const data = await resp.json();
    if (data.error) alert(data.error);
  } catch (e) {
    alert("终止失败: " + e.message);
  }
  btn.disabled = false;
  btn.textContent = "⛔ 终止";
});

// ─── 消息渲染 ───
function addMessage(sender, content, type, visibility) {
  // 旁观模式（上帝视角）显示所有消息；玩家模式隐藏 god/角色专属消息
  if (gameMode !== "observer" && visibility && visibility !== "public") {
    return; // 玩家模式下只显示 public 消息
  }

  const div = document.createElement("div");
  const isMonologue = type === "action" && content.startsWith("💭");
  if (isMonologue) {
    div.className = "msg msg-monologue";
  } else {
    div.className = `msg msg-${type || "chat"}`;
  }

  // 非公开消息在上帝视角中标记透明度
  if (visibility && visibility !== "public") {
    div.classList.add("msg-god-only");
  }

  if (type === "system" || type === "action") {
    div.textContent = content;
  } else {
    div.innerHTML = `<span class="msg-sender">${escapeHtml(sender)}</span>${escapeHtml(content)}`;
  }

  messagesEl.appendChild(div);
  chatPanel.scrollTop = chatPanel.scrollHeight;
}

function escapeHtml(text) {
  const d = document.createElement("div");
  d.textContent = text;
  return d.innerHTML;
}

/** 轻量 Markdown → HTML（先 escape 防 XSS，再转格式） */
function renderMarkdown(md) {
  let html = escapeHtml(md);
  // 标题
  html = html.replace(/^### (.+)$/gm, "<h4>$1</h4>");
  html = html.replace(/^## (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^# (.+)$/gm, "<h2>$1</h2>");
  // 粗体 / 斜体
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
  // 无序列表项
  html = html.replace(/^- (.+)$/gm, "<li>$1</li>");
  html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>");
  // 有序列表项
  html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
  // 分隔线
  html = html.replace(/^---$/gm, "<hr>");
  // 段落：连续两个换行
  html = html.replace(/\n{2,}/g, "</p><p>");
  // 单换行 → <br>
  html = html.replace(/\n/g, "<br>");
  return "<p>" + html + "</p>";
}

// ─── 座位渲染 ───
function renderSeats(showAllRoles = false) {
  if (!gameState) return;
  playerSeats.innerHTML = "";

  gameState.players.forEach((p, i) => {
    const pos = SEAT_POSITIONS[i] || {};
    const card = document.createElement("div");
    // 优先使用角色级样式（role-werewolf/role-seer/...），回退到阵营级
    const roleStyleClass = (gameMode === "observer" && p.role)
      ? `role-${p.role}`
      : (gameMode === "observer" ? (p.camp === "wolf" ? "wolf-camp" : "good-camp") : "");
    card.className = `seat-card ${p.alive ? "alive" : "dead"} ${p.is_human ? "human" : ""} ${roleStyleClass}`;

    Object.entries(pos).forEach(([k, v]) => { card.style[k] = v; });

    const avatar = p.is_human ? "🧑" : (p.alive ? "🤖" : "💀");
    const roleText = (showAllRoles || gameMode === "observer") ? (p.role_cn || "") : (p.is_human && p.role_cn ? p.role_cn : "");
    const nameColor = p.is_human ? "color: var(--accent-light)" : "";
    const roleClass = (gameMode === "observer" && p.role) ? `role-${p.role}` : (gameMode === "observer" ? (p.camp === "wolf" ? "wolf-role" : "good-role") : "");

    card.innerHTML = `
      <div class="seat-avatar">${avatar}</div>
      <div class="seat-name" style="${nameColor}">${escapeHtml(p.name)}</div>
      ${roleText ? `<div class="seat-role ${roleClass}">${escapeHtml(roleText)}</div>` : ""}
    `;

    playerSeats.appendChild(card);
  });

  const alive = gameState.players.filter(p => p.alive).length;
  aliveCount.textContent = `存活: ${alive}/9`;
}

function updateHeader() {
  if (!gameState) return;
  phaseBadge.textContent = PHASE_LABELS[gameState.phase] || gameState.phase;
  roundBadge.textContent = `第 ${gameState.round} 轮`;

  if (gameState.phase === "night") {
    phaseBadge.style.background = "#2c3e50";
  } else if (gameState.phase === "day") {
    phaseBadge.style.background = "#e67e22";
  } else if (gameState.phase === "vote") {
    phaseBadge.style.background = "#8e44ad";
  } else if (gameState.phase === "ended") {
    phaseBadge.style.background = "#27ae60";
  }
}

// ─── 人类操作面板 ───
function showActionPanel(data) {
  actionPanel.style.display = "block";
  actionPrompt.textContent = data.prompt;
  actionOptions.innerHTML = "";
  actionInput.style.display = "none";

  if (data.input_type === "speak") {
    actionInput.style.display = "flex";
    speakInput.value = "";
    speakInput.focus();
  } else if (data.input_type === "select_player") {
    (data.options || []).forEach(opt => {
      const btn = document.createElement("button");
      btn.className = "btn btn-option";
      btn.textContent = `${opt.name}`;
      btn.addEventListener("click", () => submitAction({ target_seat: opt.seat }));
      actionOptions.appendChild(btn);
    });
    const skipBtn = document.createElement("button");
    skipBtn.className = "btn btn-ghost";
    skipBtn.textContent = "跳过";
    skipBtn.style.marginTop = "6px";
    skipBtn.addEventListener("click", () => submitAction({}));
    actionOptions.appendChild(skipBtn);

  } else if (data.input_type === "vote") {
    (data.options || []).forEach(opt => {
      const btn = document.createElement("button");
      btn.className = "btn btn-option";
      btn.textContent = `投票给 ${opt.name}`;
      btn.addEventListener("click", () => submitAction({ target_seat: opt.seat }));
      actionOptions.appendChild(btn);
    });

  } else if (data.input_type === "witch_action") {
    (data.options || []).forEach(opt => {
      const btn = document.createElement("button");
      btn.className = "btn btn-option";
      btn.textContent = opt.label;
      btn.addEventListener("click", () => {
        const payload = { action: opt.action };
        if (opt.seat) payload.target_seat = opt.seat;
        submitAction(payload);
      });
      actionOptions.appendChild(btn);
    });
  }
}

function hideActionPanel() {
  actionPanel.style.display = "none";
}

async function submitAction(data) {
  hideActionPanel();
  try {
    await fetch(`${API}/game/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  } catch (err) {
    console.error("提交动作失败:", err);
  }
}

btnSendSpeak.addEventListener("click", () => {
  const text = speakInput.value.trim();
  if (text) submitAction({ content: text });
});

speakInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    btnSendSpeak.click();
  }
});

// ─── 上帝视角 ───
btnGodView.addEventListener("click", async () => {
  godViewModal.style.display = "flex";

  const godPlayers = document.getElementById("god-players");
  godPlayers.innerHTML = "";
  if (gameState && gameState.players) {
    gameState.players.forEach(p => {
      const card = document.createElement("div");
      const roleClass = p.role ? `role-${p.role}` : (p.camp === "wolf" ? "wolf" : "good");
      card.className = `god-player-card ${roleClass}`;
      const ROLE_ICONS = { werewolf: "🐺", seer: "🔮", witch: "🧪", guard: "🛡️", hunter: "🏹", villager: "👤" };
      const icon = ROLE_ICONS[p.role] || (p.camp === "wolf" ? "🐺" : "👤");
      card.innerHTML = `
        <div><strong>${escapeHtml(p.name)}</strong></div>
        <div>${p.role_cn || p.role} ${icon}</div>
        <div>${p.alive ? "✅ 存活" : "💀 死亡"}</div>
        ${p.is_human ? "<div>🧑 人类玩家</div>" : ""}
      `;
      godPlayers.appendChild(card);
    });
  }

  try {
    const resp = await fetch(`${API}/game/traces`);
    const traces = await resp.json();
    const tracesList = document.getElementById("traces-list");
    tracesList.innerHTML = "";

    traces.forEach(t => {
      const div = document.createElement("div");
      div.className = "trace-item";
      // 显示前 120 字符预览，点击展开全文
      const preview = t.detail.length > 120 ? t.detail.substring(0, 120) + "…" : t.detail;
      div.innerHTML = `<span class="t-agent">${escapeHtml(t.agent)}</span> <span class="t-type">[${escapeHtml(t.type)}]</span> ${escapeHtml(preview)}`;
      div.title = "点击展开完整内容";
      div.style.cursor = "pointer";
      div.addEventListener("click", () => {
        showTraceDetail(t);
      });
      tracesList.appendChild(div);
    });

    const statsPanel = document.getElementById("stats-panel");
    const llmCalls = traces.filter(t => t.type === "llm_call").length;
    const toolCalls = traces.filter(t => t.type === "tool_call").length;
    let totalTokens = 0;
    traces.filter(t => t.type === "llm_call").forEach(t => {
      try { totalTokens += JSON.parse(t.detail).tokens_approx || 0; } catch {}
    });

    statsPanel.innerHTML = `
      <div class="stat-card"><div class="stat-value">${llmCalls}</div><div class="stat-label">LLM 调用次数</div></div>
      <div class="stat-card"><div class="stat-value">${toolCalls}</div><div class="stat-label">工具调用次数</div></div>
      <div class="stat-card"><div class="stat-value">${totalTokens}</div><div class="stat-label">Token 消耗</div></div>
      <div class="stat-card"><div class="stat-value">${gameState?.players?.length || 9}</div><div class="stat-label">Agent 数量</div></div>
    `;
  } catch (err) {
    console.error("加载 traces 失败:", err);
  }
});

btnCloseGod.addEventListener("click", () => {
  godViewModal.style.display = "none";
});

// ─── Trace 详情弹窗 ───
const traceDetailModal = document.getElementById("trace-detail-modal");
const traceDetailTitle = document.getElementById("trace-detail-title");
const traceDetailContent = document.getElementById("trace-detail-content");

function showTraceDetail(trace) {
  traceDetailTitle.textContent = `🔍 ${trace.agent} [${trace.type}]`;
  // 尝试格式化 JSON，否则原样显示
  let text = trace.detail;
  try {
    const obj = JSON.parse(text);
    text = JSON.stringify(obj, null, 2);
  } catch {}
  traceDetailContent.textContent = text;
  traceDetailModal.style.display = "flex";
}

document.getElementById("btn-close-trace-detail").addEventListener("click", () => {
  traceDetailModal.style.display = "none";
});

document.getElementById("btn-copy-trace").addEventListener("click", () => {
  const text = traceDetailContent.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById("btn-copy-trace");
    btn.textContent = "✅ 已复制";
    setTimeout(() => { btn.textContent = "📋 复制"; }, 1500);
  });
});

// ─── AI 总结（防重复） ───
let _summaryInProgress = false;
const btnSummary = document.getElementById("btn-summary");
const summaryPanel = document.getElementById("summary-panel");

btnSummary.addEventListener("click", async () => {
  if (_summaryInProgress) return;
  // 如果已有总结内容且不是 loading 状态，不重新请求
  if (summaryPanel.style.display === "block" && !summaryPanel.classList.contains("summary-loading") && summaryPanel.innerHTML.length > 50) {
    return; // 已有总结，不重复请求
  }
  _summaryInProgress = true;
  summaryPanel.style.display = "block";
  summaryPanel.className = "summary-panel summary-loading";
  summaryPanel.textContent = "⏳ 正在调用 AI 生成总结，这需要一些时间，请耐心等待...";
  btnSummary.disabled = true;
  btnSummary.textContent = "⏳ 生成中...";

  try {
    const resp = await fetch(`${API}/game/summary`, { method: "POST" });
    const data = await resp.json();
    if (data.error) {
      summaryPanel.textContent = "❌ " + data.error;
    } else {
      summaryPanel.className = "summary-panel";
      summaryPanel.innerHTML = renderMarkdown(data.summary || "");
    }
  } catch (err) {
    summaryPanel.textContent = "❌ 请求失败: " + err.message;
  }
  btnSummary.disabled = false;
  btnSummary.textContent = "📊 AI 总结";
  _summaryInProgress = false;
});

// ─── 顶栏总结按钮 → 直接打开上帝视角并滚动到总结区 ───
const btnHeaderSummary = document.getElementById("btn-header-summary");

btnHeaderSummary.addEventListener("click", () => {
  // 打开上帝视角弹窗
  document.getElementById("btn-god-view").click();
  // 稍等 DOM 刷新后滚到总结区并自动触发生成
  setTimeout(() => {
    summaryPanel.scrollIntoView({ behavior: "smooth" });
    if (!_summaryInProgress && (summaryPanel.style.display !== "block" || summaryPanel.innerHTML.length < 50)) {
      btnSummary.click();
    }
  }, 300);
});

// ─── AI 配置管理 ───
const btnSettings       = document.getElementById("btn-settings");
const configModal       = document.getElementById("config-modal");
const btnCloseConfig    = document.getElementById("btn-close-config");
const btnSaveConfig     = document.getElementById("btn-save-config");
const btnRestartEngine  = document.getElementById("btn-restart-engine");
const cfgTemperature    = document.getElementById("cfg-temperature");
const cfgTemperatureVal = document.getElementById("cfg-temperature-val");
const cfgMaxTokens      = document.getElementById("cfg-max-tokens");
const cfgSpeakDelay     = document.getElementById("cfg-speak-delay");
const engineModeGroup   = document.getElementById("engine-mode-group");
const engineRestartHint = document.getElementById("engine-restart-hint");
const modelPoolEl       = document.getElementById("model-pool");
const btnAddModel       = document.getElementById("btn-add-model");
const agentBindings     = document.getElementById("agent-bindings");
const configStatus      = document.getElementById("config-status");

let _savedEngineMode = "manual";
let _poolData = [];   // 模型仓库数据

const API_TYPE_OPTIONS = {
  responses: "Responses API",
  azure: "Azure Chat Completions",
  openai: "OpenAI 兼容",
};

function _renderPoolCard(index, item) {
  const card = document.createElement("div");
  card.className = "model-pool-card";
  card.dataset.index = index;
  const apiTypeOpts = Object.entries(API_TYPE_OPTIONS)
    .map(([k, v]) => `<option value="${k}" ${k === (item.api_type || "responses") ? "selected" : ""}>${escapeHtml(v)}</option>`)
    .join("");
  card.innerHTML = `
    <div class="pool-header">
      <span class="pool-index">#${index + 1}</span>
      <input class="pool-name-input" placeholder="模型名称（如 GPT-4o）" value="${escapeHtml(item.name || "")}" />
      <button class="btn-remove-model" title="删除">✕</button>
    </div>
    <div class="pool-fields">
      <input class="pf-endpoint" placeholder="Endpoint" value="${escapeHtml(item.endpoint || "")}" />
      <input class="pf-model" placeholder="模型 / 部署名" value="${escapeHtml(item.model || "")}" />
      <input type="password" class="pf-apikey" placeholder="API Key" value="${escapeHtml(item.api_key || "")}" autocomplete="off" />
      <select class="pf-apitype">${apiTypeOpts}</select>
      <input class="pf-apiversion" placeholder="API Version" value="${escapeHtml(item.api_version || "2025-03-01-preview")}" />
    </div>
  `;
  card.querySelector(".btn-remove-model").addEventListener("click", () => {
    card.remove();
    _reindexPool();
    _refreshBindingOptions();
  });
  return card;
}

function _reindexPool() {
  modelPoolEl.querySelectorAll(".model-pool-card").forEach((c, i) => {
    c.dataset.index = i;
    c.querySelector(".pool-index").textContent = `#${i + 1}`;
  });
}

function _collectPool() {
  const pool = [];
  modelPoolEl.querySelectorAll(".model-pool-card").forEach(c => {
    pool.push({
      name: c.querySelector(".pool-name-input").value.trim(),
      endpoint: c.querySelector(".pf-endpoint").value.trim(),
      model: c.querySelector(".pf-model").value.trim(),
      api_key: c.querySelector(".pf-apikey").value.trim(),
      api_type: c.querySelector(".pf-apitype").value,
      api_version: c.querySelector(".pf-apiversion").value.trim(),
    });
  });
  return pool;
}

function _refreshBindingOptions() {
  const pool = _collectPool();
  agentBindings.querySelectorAll(".agent-binding-row select").forEach(sel => {
    const cur = sel.value;
    sel.innerHTML = pool.map((p, i) =>
      `<option value="${i}">#${i + 1} ${escapeHtml(p.name || p.model || "未命名")}</option>`
    ).join("");
    if (pool.length === 0) sel.innerHTML = '<option value="0">（请先添加模型）</option>';
    sel.value = cur < pool.length ? cur : "0";
  });
}

btnAddModel.addEventListener("click", () => {
  const idx = modelPoolEl.querySelectorAll(".model-pool-card").length;
  modelPoolEl.appendChild(_renderPoolCard(idx, {}));
  _refreshBindingOptions();
});

// Temperature slider 实时显示
cfgTemperature.addEventListener("input", () => {
  cfgTemperatureVal.textContent = parseFloat(cfgTemperature.value).toFixed(2);
});

btnSettings.addEventListener("click", async () => {
  configModal.style.display = "flex";
  configStatus.textContent = "";
  engineRestartHint.style.display = "none";
  btnRestartEngine.style.display = "none";
  await loadConfig();
});

btnCloseConfig.addEventListener("click", () => {
  configModal.style.display = "none";
});

async function loadConfig() {
  try {
    const resp = await fetch(`${API}/config`);
    const cfg = await resp.json();

    // 引擎模式
    const modes = cfg.engine_modes || { manual: "🔧 手搓版", sdk: "🧠 SDK 版" };
    const curMode = cfg.engine_mode || "manual";
    _savedEngineMode = curMode;
    engineModeGroup.innerHTML = "";
    for (const [key, label] of Object.entries(modes)) {
      const card = document.createElement("label");
      card.className = "engine-mode-card" + (key === curMode ? " active" : "");
      card.innerHTML = `
        <input type="radio" name="engine-mode" value="${key}" ${key === curMode ? "checked" : ""} />
        <div>
          <div class="mode-label">${escapeHtml(label)}</div>
          <div class="mode-desc">${key === "manual" ? "直接 REST API 调用" : "Semantic Kernel Agent + Plugin"}</div>
        </div>
      `;
      card.querySelector("input").addEventListener("change", () => {
        engineModeGroup.querySelectorAll(".engine-mode-card").forEach(c => c.classList.remove("active"));
        card.classList.add("active");
        const newMode = card.querySelector("input").value;
        engineRestartHint.style.display = newMode !== _savedEngineMode ? "block" : "none";
      });
      engineModeGroup.appendChild(card);
    }

    // 模型仓库
    const pool = cfg.model_pool || [];
    modelPoolEl.innerHTML = "";
    if (pool.length === 0) {
      // 兼容旧配置：把 default_endpoint/model/key 迁移成第一个池条目
      const legacy = {};
      if (cfg.default_endpoint) legacy.endpoint = cfg.default_endpoint;
      if (cfg.default_model) legacy.model = cfg.default_model;
      if (cfg.default_api_key) legacy.api_key = cfg.default_api_key;
      legacy.api_type = cfg.api_type || "responses";
      legacy.api_version = cfg.api_version || "2025-03-01-preview";
      legacy.name = cfg.default_model || "默认模型";
      if (legacy.endpoint) pool.push(legacy);
    }
    pool.forEach((item, i) => modelPoolEl.appendChild(_renderPoolCard(i, item)));

    // 游戏参数
    cfgTemperature.value = cfg.temperature ?? 0.8;
    cfgTemperatureVal.textContent = parseFloat(cfgTemperature.value).toFixed(2);
    cfgMaxTokens.value = cfg.max_tokens ?? 300;
    cfgSpeakDelay.value = cfg.speak_delay_ms ?? 300;

    // Agent 绑定（按人名）
    const aiNames = cfg.ai_names || [];
    const indices = cfg.agent_model_index || {};
    agentBindings.innerHTML = "";
    const poolOpts = pool.map((p, i) =>
      `<option value="${i}">#${i + 1} ${escapeHtml(p.name || p.model || "未命名")}</option>`
    ).join("") || '<option value="0">（请先添加模型）</option>';

    aiNames.forEach(name => {
      const row = document.createElement("div");
      row.className = "agent-binding-row";
      row.dataset.role = name;
      const curIdx = indices[name] ?? 0;
      row.innerHTML = `
        <span class="role-label">${escapeHtml(name)}</span>
        <select>${poolOpts}</select>
      `;
      row.querySelector("select").value = curIdx < pool.length ? curIdx : 0;
      agentBindings.appendChild(row);
    });

  } catch (err) {
    configStatus.textContent = "⚠ 加载配置失败: " + err.message;
    configStatus.style.color = "var(--danger)";
  }
}

btnSaveConfig.addEventListener("click", async () => {
  const pool = _collectPool();

  const agentIdx = {};
  agentBindings.querySelectorAll(".agent-binding-row").forEach(row => {
    agentIdx[row.dataset.role] = parseInt(row.querySelector("select").value, 10) || 0;
  });

  const selectedMode = engineModeGroup.querySelector("input[name='engine-mode']:checked");
  const engineMode = selectedMode ? selectedMode.value : "manual";

  // 用第一个池条目的 api_type 作为全局默认
  const globalApiType = pool.length > 0 ? pool[0].api_type : "responses";

  const payload = {
    engine_mode: engineMode,
    api_type: globalApiType,
    api_version: pool.length > 0 ? pool[0].api_version : "2025-03-01-preview",
    temperature: parseFloat(cfgTemperature.value),
    max_tokens: parseInt(cfgMaxTokens.value, 10),
    speak_delay_ms: parseInt(cfgSpeakDelay.value, 10),
    model_pool: pool,
    agent_model_index: agentIdx,
  };

  try {
    const resp = await fetch(`${API}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const result = await resp.json();
    configStatus.textContent = "✅ 已保存";
    configStatus.style.color = "var(--success)";
    _savedEngineMode = engineMode;
    engineRestartHint.style.display = "none";
    if (result.need_restart) {
      btnRestartEngine.style.display = "inline-block";
      configStatus.textContent = "✅ 已保存 — 请点击「重启引擎」使新引擎生效";
    } else {
      btnRestartEngine.style.display = "none";
    }
  } catch (err) {
    configStatus.textContent = "❌ 保存失败: " + err.message;
    configStatus.style.color = "var(--danger)";
  }
});

btnRestartEngine.addEventListener("click", async () => {
  try {
    configStatus.textContent = "🔄 正在重启引擎...";
    configStatus.style.color = "var(--text-dim)";
    const resp = await fetch(`${API}/engine/restart`, { method: "POST" });
    if (!resp.ok) throw new Error(await resp.text());
    const result = await resp.json();
    configStatus.textContent = `✅ 引擎已切换 → ${result.engine_mode === "sdk" ? "🧠 SK 版" : "🔧 手搓版"}`;
    configStatus.style.color = "var(--success)";
    btnRestartEngine.style.display = "none";
  } catch (err) {
    configStatus.textContent = "❌ 重启失败: " + err.message;
    configStatus.style.color = "var(--danger)";
  }
});

// ─── 页面加载时自动启动后端 ───
initBackend().then(() => {
  if (backendReady) {
    btnStart.disabled = false;
    btnStart.textContent = "开始游戏";
  }
});

// ─── 主题按钮 ───
const btnTheme = document.getElementById("btn-theme");
if (btnTheme) {
  btnTheme.textContent = (document.documentElement.getAttribute("data-theme") === "dark") ? "☀️" : "🌙";
  btnTheme.addEventListener("click", toggleTheme);
}

// ═══════════════════════════════════════════════════
// 历史记录
// ═══════════════════════════════════════════════════
const historyModal = document.getElementById("history-modal");
const historyList = document.getElementById("history-list");
const historyDetail = document.getElementById("history-detail");
const historyDetailTitle = document.getElementById("history-detail-title");
const historyPlayers = document.getElementById("history-players");
const historyMessages = document.getElementById("history-messages");
const historySummaryPanel = document.getElementById("history-summary-panel");
const historySummaryWrap = document.getElementById("history-summary-wrap");
const historyAnswer = document.getElementById("conv-messages");
const historyAnswerWrap = document.getElementById("conv-messages");
const perspectiveBar = document.getElementById("perspective-bar");
const perspectiveInfo = document.getElementById("perspective-info");

// 缓存当前对局数据
let _currentGame = null;
let _currentGameId = null;
let _currentConversationId = null;
let _currentPerspective = "god";

document.getElementById("btn-history").addEventListener("click", async () => {
  historyModal.style.display = "flex";
  historyList.style.display = "block";
  historyDetail.style.display = "none";
  historyList.innerHTML = "⏳ 加载中...";
  try {
    const resp = await fetch(`${API}/history`);
    const games = await resp.json();
    if (!games.length) {
      historyList.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-dim)">还没有历史对局记录。<br>完成一局游戏后会自动保存。</div>';
      return;
    }
    historyList.innerHTML = "";
    games.forEach(g => {
      const div = document.createElement("div");
      div.className = "history-item";
      const date = new Date(g.ended_at * 1000);
      const dateStr = `${date.getMonth()+1}/${date.getDate()} ${date.getHours()}:${String(date.getMinutes()).padStart(2,'0')}`;
      const winClass = g.winner === "wolf" ? "wolf" : g.winner === "aborted" ? "aborted" : "good";
      const winText = g.winner === "wolf" ? "🐺 狼胜" : g.winner === "good" ? "👤 好人胜" : g.winner === "aborted" ? "⛔ 终止" : "未结束";
      div.innerHTML = `
        <div class="hi-main">
          <span class="hi-id">#${g.id} · ${dateStr} · ${g.rounds}轮 ${g.observer ? "(观战)" : ""}</span>
          <span class="hi-info">${escapeHtml(g.players_preview)}</span>
        </div>
        <span class="hi-badge ${winClass}">${winText}</span>
      `;
      div.addEventListener("click", () => openHistoryDetail(g.id));
      historyList.appendChild(div);
    });
  } catch (err) {
    historyList.innerHTML = "❌ 加载失败: " + err.message;
  }
});

document.getElementById("btn-close-history").addEventListener("click", () => {
  historyModal.style.display = "none";
});

document.getElementById("btn-back-history").addEventListener("click", () => {
  historyList.style.display = "block";
  historyDetail.style.display = "none";
});

/** 渲染消息列表到 historyMessages */
function renderHistoryMessages(messages) {
  historyMessages.innerHTML = "";
  messages.forEach(m => {
    const div = document.createElement("div");
    const isMonologue = m.type === "action" && m.content && m.content.startsWith("💭");
    if (m.type === "system") {
      div.className = "hm-system";
      div.textContent = `[系统] ${m.content}`;
    } else if (isMonologue) {
      div.className = "hm-action";
      div.style.color = "var(--warning)";
      div.style.fontStyle = "italic";
      div.textContent = `[内心] ${m.content}`;
    } else if (m.type === "action") {
      div.className = "hm-action";
      div.textContent = `[行动] ${m.content}`;
    } else {
      div.className = "hm-chat";
      div.innerHTML = `<span class="hm-sender">${escapeHtml(m.sender)}:</span> ${escapeHtml(m.content)}`;
    }
    // 非公开消息标记半透明
    if (m.visibility && m.visibility !== "public") {
      div.style.opacity = "0.6";
    }
    historyMessages.appendChild(div);
  });
}

/** 切换到某个视角 */
async function switchPerspective(gameId, playerName) {
  // 更新按钮激活状态
  perspectiveBar.querySelectorAll(".btn-perspective").forEach(b => {
    b.classList.toggle("active", b.dataset.perspective === playerName);
  });

  _currentPerspective = playerName;
  // 切换视角时重置会话
  _currentConversationId = null;
  const convList = document.getElementById("conv-list");
  if (convList) convList.value = "";
  const convMsgs = document.getElementById("conv-messages");
  if (convMsgs) convMsgs.innerHTML = "";
  const hint = document.getElementById("conv-perspective-hint");
  if (hint) hint.textContent = playerName === "god" ? "当前：上帝视角（全知）" : `当前：${playerName} 视角`;

  // 刷新会话列表
  _refreshConvList(gameId, playerName);

  if (playerName === "god") {
    // 上帝视角 — 全部消息
    perspectiveInfo.style.display = "none";
    renderHistoryMessages(_currentGame.messages);
    return;
  }

  // 玩家视角 — 调用后端过滤
  historyMessages.innerHTML = "⏳ 加载视角...";
  perspectiveInfo.style.display = "none";
  try {
    const resp = await fetch(`${API}/history/${gameId}/perspective/${encodeURIComponent(playerName)}`);
    const data = await resp.json();
    if (data.error) {
      historyMessages.innerHTML = "❌ " + data.error;
      return;
    }
    renderHistoryMessages(data.messages);
    const p = data.player;
    const campText = p.camp === "wolf" ? "🐺 狼人阵营" : "👤 好人阵营";
    perspectiveInfo.style.display = "block";
    perspectiveInfo.textContent = `👤 ${p.name} · ${p.role_cn} · ${campText} · ${p.alive ? "存活" : "已死亡"} · 可见 ${data.visible_messages}/${data.total_messages} 条消息`;
  } catch (err) {
    historyMessages.innerHTML = "❌ 加载失败: " + err.message;
  }
}

/** 刷新会话列表下拉框 */
async function _refreshConvList(gameId, perspective) {
  const convList = document.getElementById("conv-list");
  if (!convList) return;
  try {
    const resp = await fetch(`${API}/history/${gameId}/conversations`);
    const convs = await resp.json();
    const filtered = convs.filter(c => c.perspective === perspective);
    convList.innerHTML = '<option value="">— 新对话 —</option>';
    filtered.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c.id;
      const date = new Date(c.created_at * 1000);
      const time = `${date.getHours()}:${String(date.getMinutes()).padStart(2,'0')}`;
      opt.textContent = `#${c.id} ${c.title || "未命名"} (${c.msg_count}条) ${time}`;
      convList.appendChild(opt);
    });
  } catch {}
}

/** 加载会话消息到气泡区域（始终从 DB 加载，保证切换不丢数据） */
async function _loadConvMessages(gameId, convId) {
  const convMsgs = document.getElementById("conv-messages");
  if (!convMsgs) return;
  convMsgs.innerHTML = "";
  if (!convId) return;
  try {
    const resp = await fetch(`${API}/history/${gameId}/conversations/${convId}`);
    const data = await resp.json();
    if (data.messages) {
      data.messages.forEach(m => {
        const bubble = document.createElement("div");
        bubble.className = `conv-bubble ${m.role}`;
        if (m.role === "assistant") {
          const content = m.content || "";
          if (!content) {
            // 流式输出还在进行中，显示加载状态并自动轮询
            bubble.innerHTML = '<span style="color:var(--text-dim);font-style:italic">✨ 生成中...</span>';
            bubble.dataset.polling = "true";
            bubble.dataset.msgId = m.id;
          } else {
            bubble.innerHTML = renderMarkdown(content);
          }
        } else {
          bubble.textContent = m.content;
        }
        convMsgs.appendChild(bubble);
      });
      convMsgs.scrollTop = convMsgs.scrollHeight;

      // 如果有正在生成的消息，启动轮询直到完成
      const pollingBubble = convMsgs.querySelector('[data-polling="true"]');
      if (pollingBubble) {
        _pollStreamingMessage(gameId, convId, pollingBubble);
      }
    }
  } catch {}
}

/** 轮询某条正在流式生成的消息，直到内容不再变化 */
async function _pollStreamingMessage(gameId, convId, bubble) {
  let lastLen = 0;
  let stableCount = 0;
  const poll = async () => {
    try {
      const resp = await fetch(`${API}/history/${gameId}/conversations/${convId}`);
      const data = await resp.json();
      if (!data.messages) return;
      // 找到最后一条 assistant 消息
      const msgs = data.messages.filter(m => m.role === "assistant");
      const last = msgs[msgs.length - 1];
      if (last && last.content) {
        bubble.innerHTML = renderMarkdown(last.content);
        const convMsgs = document.getElementById("conv-messages");
        if (convMsgs) convMsgs.scrollTop = convMsgs.scrollHeight;
        if (last.content.length === lastLen) {
          stableCount++;
          if (stableCount >= 3) {
            // 内容稳定了，停止轮询
            delete bubble.dataset.polling;
            _refreshConvList(gameId, _currentPerspective);
            return;
          }
        } else {
          stableCount = 0;
          lastLen = last.content.length;
        }
      }
    } catch {}
    // 继续轮询
    setTimeout(poll, 800);
  };
  setTimeout(poll, 800);
}

async function openHistoryDetail(gameId) {
  historyList.style.display = "none";
  historyDetail.style.display = "block";
  historySummaryWrap.style.display = "none";
  historyMessages.innerHTML = "⏳ 加载中...";
  perspectiveInfo.style.display = "none";
  // 重置对话状态
  _currentConversationId = null;
  _currentPerspective = "god";

  try {
    const resp = await fetch(`${API}/history/${gameId}`);
    const game = await resp.json();
    if (game.error) { historyMessages.innerHTML = "❌ " + game.error; return; }

    _currentGame = game;
    _currentGameId = gameId;

    const winText = game.winner === "wolf" ? "🐺 狼人阵营获胜" : game.winner === "good" ? "👤 好人阵营获胜" : game.winner === "aborted" ? "⛔ 被终止" : "未结束";
    historyDetailTitle.textContent = `对局 #${game.id} · ${game.rounds}轮 · ${winText}`;

    // 玩家卡片
    historyPlayers.innerHTML = "";
    game.players.forEach(p => {
      const card = document.createElement("div");
      const roleClass = p.role ? `role-${p.role}` : (p.camp === "wolf" ? "wolf" : "good");
      card.className = `god-player-card ${roleClass}`;
      const ROLE_ICONS = { werewolf: "🐺", seer: "🔮", witch: "🧪", guard: "🛡️", hunter: "🏹", villager: "👤" };
      const icon = ROLE_ICONS[p.role] || (p.camp === "wolf" ? "🐺" : "👤");
      card.innerHTML = `
        <div><strong>${escapeHtml(p.name)}</strong></div>
        <div>${p.role_cn} ${icon}</div>
        <div>${p.alive ? "✅ 存活" : "💀 死亡"}</div>
      `;
      historyPlayers.appendChild(card);
    });

    // 构建视角按钮栏
    perspectiveBar.innerHTML = '<span style="font-size:12px;color:var(--text-dim);margin-right:4px">视角:</span>';
    const godBtn = document.createElement("button");
    godBtn.className = "btn-perspective active";
    godBtn.dataset.perspective = "god";
    godBtn.textContent = "👁 上帝视角";
    godBtn.addEventListener("click", () => switchPerspective(gameId, "god"));
    perspectiveBar.appendChild(godBtn);

    game.players.forEach(p => {
      const btn = document.createElement("button");
      btn.className = "btn-perspective";
      btn.dataset.perspective = p.name;
      const icon = p.camp === "wolf" ? "🐺" : "👤";
      btn.textContent = `${icon} ${p.name}`;
      btn.title = `${p.role_cn} · ${p.alive ? "存活" : "已死亡"}`;
      btn.addEventListener("click", () => switchPerspective(gameId, p.name));
      perspectiveBar.appendChild(btn);
    });

    // 默认上帝视角
    renderHistoryMessages(game.messages);

    // 已有总结？
    if (game.summary) {
      historySummaryWrap.style.display = "block";
      historySummaryPanel.innerHTML = renderMarkdown(game.summary);
    }

    // 绑定总结按钮
    const btnSummary = document.getElementById("btn-history-summary");
    btnSummary.onclick = async () => {
      historySummaryWrap.style.display = "block";
      historySummaryPanel.className = "summary-panel summary-loading";
      historySummaryPanel.textContent = "⏳ 正在生成总结...";
      btnSummary.disabled = true;
      try {
        const r = await fetch(`${API}/history/${gameId}/summary`, { method: "POST" });
        const d = await r.json();
        if (d.error) { historySummaryPanel.textContent = "❌ " + d.error; }
        else {
          historySummaryPanel.className = "summary-panel";
          historySummaryPanel.innerHTML = renderMarkdown(d.summary || "");
        }
      } catch (e) { historySummaryPanel.textContent = "❌ " + e.message; }
      btnSummary.disabled = false;
    };

    // 绑定提问按钮（多轮对话）
    const btnAsk = document.getElementById("btn-ask-history");
    const inputQ = document.getElementById("history-question");
    const convMsgsEl = document.getElementById("conv-messages");
    const convListSel = document.getElementById("conv-list");
    const btnNewConv = document.getElementById("btn-new-conv");
    const convHint = document.getElementById("conv-perspective-hint");
    if (convHint) convHint.textContent = "当前：上帝视角（全知）";

    // 切换已有会话
    convListSel.onchange = () => {
      const val = convListSel.value;
      if (val) {
        _currentConversationId = parseInt(val);
        _loadConvMessages(gameId, _currentConversationId);
      } else {
        _currentConversationId = null;
        convMsgsEl.innerHTML = "";
      }
    };

    // 新建会话
    btnNewConv.onclick = () => {
      _currentConversationId = null;
      convListSel.value = "";
      convMsgsEl.innerHTML = "";
      inputQ.focus();
    };

    // 刷新会话列表
    _refreshConvList(gameId, _currentPerspective);

    btnAsk.onclick = async () => {
      const q = inputQ.value.trim();
      if (!q) return;
      inputQ.value = "";

      // 添加用户气泡
      const userBubble = document.createElement("div");
      userBubble.className = "conv-bubble user";
      userBubble.textContent = q;
      convMsgsEl.appendChild(userBubble);

      // 添加 AI 气泡（轮询填充）
      const aiBubble = document.createElement("div");
      aiBubble.className = "conv-bubble assistant";
      aiBubble.innerHTML = '<span style="color:var(--text-dim);font-style:italic">✨ 正在生成...</span>';
      convMsgsEl.appendChild(aiBubble);
      convMsgsEl.scrollTop = convMsgsEl.scrollHeight;

      try {
        const r = await fetch(`${API}/history/${gameId}/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question: q,
            conversation_id: _currentConversationId || undefined,
            perspective: _currentPerspective,
          }),
        });

        const data = await r.json();
        if (data.error) {
          aiBubble.innerHTML = '<span style="color:var(--danger)">❌ ' + escapeHtml(data.error) + '</span>';
          return;
        }
        if (!_currentConversationId) {
          _currentConversationId = data.conversation_id;
        }
        _refreshConvList(gameId, _currentPerspective);
        // 后台 Task 已启动，轮询 DB 直到完成
        aiBubble.dataset.polling = "true";
        _pollStreamingMessage(gameId, _currentConversationId, aiBubble);
      } catch (e) {
        aiBubble.innerHTML = '<span style="color:var(--danger)">❌ ' + escapeHtml(e.message) + '</span>';
      }
    };
    // 回车提交
    inputQ.onkeydown = (e) => { if (e.key === "Enter") btnAsk.click(); };

  } catch (err) {
    historyMessages.innerHTML = "❌ 加载失败: " + err.message;
  }
}

// ═══════════════════════════════════════════════════
// 复制功能（按钮 + 右键菜单）
// ═══════════════════════════════════════════════════

/** 复制元素的纯文本内容到剪贴板 */
function copyElementText(el) {
  const text = el.innerText || el.textContent || "";
  navigator.clipboard.writeText(text).then(() => {
    showCopyToast("✅ 已复制到剪贴板");
  }).catch(() => {
    // fallback
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;left:-9999px";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    showCopyToast("✅ 已复制到剪贴板");
  });
}

function showCopyToast(msg) {
  let toast = document.getElementById("copy-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "copy-toast";
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.className = "copy-toast show";
  setTimeout(() => { toast.className = "copy-toast"; }, 1500);
}

// 📋 按钮点击复制
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".btn-copy");
  if (!btn) return;
  const targetId = btn.dataset.copyTarget;
  const el = document.getElementById(targetId);
  if (el) {
    copyElementText(el);
    btn.textContent = "✅";
    setTimeout(() => { btn.textContent = "📋"; }, 1200);
  }
});

// 右键菜单
const ctxMenu = document.createElement("div");
ctxMenu.id = "copy-ctx-menu";
ctxMenu.className = "copy-ctx-menu";
ctxMenu.innerHTML = '<div class="ctx-item" data-action="copy">📋 复制内容</div>';
ctxMenu.style.display = "none";
document.body.appendChild(ctxMenu);

let _ctxTarget = null;

// 可复制区域的选择器
const COPYABLE_SELECTORS = ".summary-panel, .history-messages, .trace-detail-content";

document.addEventListener("contextmenu", (e) => {
  const panel = e.target.closest(COPYABLE_SELECTORS);
  if (!panel) {
    ctxMenu.style.display = "none";
    return;
  }
  e.preventDefault();
  _ctxTarget = panel;
  ctxMenu.style.display = "block";
  ctxMenu.style.left = Math.min(e.clientX, window.innerWidth - 160) + "px";
  ctxMenu.style.top = Math.min(e.clientY, window.innerHeight - 40) + "px";
});

ctxMenu.addEventListener("click", (e) => {
  const item = e.target.closest(".ctx-item");
  if (!item || !_ctxTarget) return;
  if (item.dataset.action === "copy") {
    copyElementText(_ctxTarget);
  }
  ctxMenu.style.display = "none";
});

document.addEventListener("click", (e) => {
  if (!ctxMenu.contains(e.target)) ctxMenu.style.display = "none";
});
