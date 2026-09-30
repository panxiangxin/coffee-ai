import { collectFlows, renderFlow, flowStatus } from "./debug-flow.js";

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const LANES = { event: "事件决策", strategy: "战略规划", episode: "经历摘要" };
  const PLAYERS = { p_A: "顾客 A", p_B: "顾客 B", npc_owner: "老板娘", room: "公共话题" };
  const RECIPES = { americano: "美式", latte: "拿铁", pourover: "手冲", decaf: "低因咖啡" };
  const STATUS_LABELS = {
    sent: "已下发", started: "已开始", running: "执行中", paused: "已暂停", resumed: "已继续",
    completed: "已完成", cancelled: "已取消", failed: "失败", error: "错误", accepted: "已采纳",
    applied: "已应用", discarded: "已丢弃", rejected: "未采纳", stale: "已过期", superseded: "已替代",
    pending: "等待中", queued: "排队中", ready: "可领取", confirming: "待确认", waiting_reply: "等待回应",
    engaged: "交谈中", closed: "已结束", success: "成功", received: "已返回", presenting: "展示中",
    pause: "暂停", resume: "继续", cancel: "取消", displayed: "已展示", finished: "展示结束", interrupted: "已中断",
    rule: "规则决策", validated: "校验通过", unconfigured: "未配置",
  };
  const KIND_LABELS = { input: "输入", output: "输出", model_call: "决策调度", model_request: "模型请求", model_response: "模型响应", model_validation: "结构校验", model_result: "处理结果", flow_step: "流程步骤", session: "会话", error: "错误" };
  const app = {
    scope: "lab", generation: 0, sessionId: null, snapshot: null, online: false, busy: false,
    player: "p_A", tab: "dialogue", view: "trace", traceMode: "flows", lane: "event", search: "", follow: true,
    detailTab: "input", rawSection: "request", filteredTrace: [], listOpen: false,
    events: [], seen: new Set(), calls: new Map(), messages: new Map(), lastSeq: 0,
    selectedCall: null, selectedEvent: null, selectedFlow: null, gap: "", pollTimer: null, pollController: null,
    pollingGeneration: null, refreshRequested: false, speech: null, speechHistory: new Map(),
    renderKeys: new Map(), toastTimer: null,
  };

  function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }
  function pretty(value) { return JSON.stringify(value === undefined ? null : value, null, 2); }
  function label(value) { return STATUS_LABELS[value] || String(value || "—"); }
  function playerName(id) { return PLAYERS[id] || id || "—"; }
  function array(value) { return Array.isArray(value) ? value : []; }
  function runtime() { return app.snapshot?.runtime || {}; }
  function presentPlayers() { return array(runtime().present_players).map((item) => typeof item === "string" ? item : item.player_id); }
  function connected() { return app.online && Boolean(app.snapshot?.connected); }
  function writable(playerId = app.player) { return connected() && !app.busy && presentPlayers().includes(playerId); }
  function statusClass(status) {
    if (["failed", "error", "rejected", "invalid"].includes(status)) return "error";
    if (["paused", "discarded", "stale", "superseded", "cancelled", "confirming", "waiting_reply"].includes(status)) return "amber";
    if (["completed", "accepted", "applied", "success", "ready", "displayed", "finished", "engaged"].includes(status)) return "teal";
    return "blue";
  }
  function badge(text, kind = "") { return element("span", `badge ${kind}`, text); }
  function button(text, action, { disabled = false, className = "button compact" } = {}) {
    const item = element("button", className, text);
    item.type = "button";
    item.disabled = disabled;
    item.addEventListener("click", action);
    return item;
  }
  function empty(text) { return element("p", "empty-inline", text); }
  function timestamp(at) {
    if (at === undefined || at === null) return "—";
    const value = typeof at === "number" ? (at < 1e12 ? at * 1000 : at) : at;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(at) : date.toLocaleTimeString("zh-CN", { hour12: false });
  }
  function setText(id, text) { $(id).textContent = text; }
  function renderChanged(key, value, render) {
    const signature = typeof value === "string" ? value : JSON.stringify(value);
    if (app.renderKeys.get(key) === signature) return;
    app.renderKeys.set(key, signature);
    render();
  }
  function toast(message, error = false) {
    clearTimeout(app.toastTimer);
    $("toast").className = `toast${error ? " error" : ""}`;
    $("toast").textContent = message;
    $("toast").hidden = false;
    app.toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 6500 : 3000);
  }
  async function copy(value) {
    try { await navigator.clipboard.writeText(typeof value === "string" ? value : pretty(value)); toast("已复制完整内容"); }
    catch { toast("浏览器未允许复制，请选择内容后复制。", true); }
  }

  async function api(endpoint, { method = "GET", body, scope = app.scope, sessionId, query = {}, signal } = {}) {
    const url = new URL(`/debug/api/${endpoint}`, window.location.origin);
    if (endpoint !== "session") url.searchParams.set("scope", scope);
    if (sessionId) url.searchParams.set("session_id", sessionId);
    for (const [key, value] of Object.entries(query)) url.searchParams.set(key, String(value));
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener("abort", cancel, { once: true });
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    try {
      const response = await fetch(url, {
        method, cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      let result;
      try { result = text ? JSON.parse(text) : {}; }
      catch { throw new Error(`后端返回了非 JSON 内容（HTTP ${response.status}），请确认调试服务已启动。`); }
      if (!response.ok) {
        const detail = typeof result.detail === "string" ? result.detail : pretty(result.detail || result);
        const error = new Error(detail || `请求失败：HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return result;
    } catch (error) {
      if (timedOut) throw new Error("后端请求超过 15 秒，请检查服务状态后重试。");
      if (error instanceof TypeError) throw new Error("无法连接后端，请确认服务正在运行。");
      throw error;
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", cancel);
    }
  }

  function rememberSpeechTime() {
    const delivery = app.speech;
    if (!delivery) return;
    clearTimeout(delivery.timer);
    delivery.timer = null;
    if (delivery.visibleSince !== null) {
      delivery.record.remaining = Math.max(0, delivery.record.remaining - (performance.now() - delivery.visibleSince));
      delivery.visibleSince = null;
    }
  }
  function clearSpeech() { rememberSpeechTime(); app.speech = null; }
  function resetSession(sessionId) {
    clearSpeech();
    app.sessionId = sessionId || null;
    app.events = [];
    app.seen.clear();
    app.calls.clear();
    app.messages.clear();
    app.lastSeq = 0;
    app.selectedCall = null;
    app.selectedEvent = null;
    app.selectedFlow = null;
    app.gap = "";
    app.renderKeys.clear();
  }
  function schedulePoll(delay = 950) {
    clearTimeout(app.pollTimer);
    if (app.pollingGeneration === app.generation) { app.refreshRequested = true; return; }
    const generation = app.generation;
    app.pollTimer = setTimeout(() => poll(generation), delay);
  }
  async function poll(generation) {
    if (generation !== app.generation || app.pollingGeneration === generation) return;
    app.pollingGeneration = generation;
    app.refreshRequested = false;
    const scope = app.scope;
    const controller = new AbortController();
    app.pollController = controller;
    try {
      const snapshot = await api("state", { scope, signal: controller.signal });
      if (generation !== app.generation) return;
      if ((snapshot.session_id || null) !== app.sessionId) resetSession(snapshot.session_id);
      app.snapshot = snapshot;
      const requestedAfter = app.lastSeq;
      const response = await api("events", { scope, query: { after: requestedAfter }, signal: controller.signal });
      if (generation !== app.generation) return;
      const firstSeq = Number(response.first_seq || snapshot.trace_first_seq || 0);
      if (firstSeq > requestedAfter + 1) app.gap = `服务当前保留的日志从 #${firstSeq} 开始；更早记录请导出 JSONL 查看。`;
      ingest(array(response.events));
      app.online = true;
      $("connection-error").hidden = true;
      setText("sync-label", `已同步 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
      renderAll();
      syncSpeech();
    } catch (error) {
      if (generation !== app.generation || error.name === "AbortError") return;
      app.online = false;
      rememberSpeechTime();
      $("connection-error").hidden = false;
      setText("connection-error-text", error.message);
      setText("sync-label", "同步中断 · 自动重试");
      renderControls();
    } finally {
      if (app.pollingGeneration === generation) app.pollingGeneration = null;
      if (generation === app.generation) schedulePoll(app.refreshRequested ? 0 : (app.online ? 950 : 1800));
    }
  }
  function switchScope(scope) {
    if (scope === app.scope || !["lab", "godot"].includes(scope)) return;
    app.generation += 1;
    clearTimeout(app.pollTimer);
    app.pollController?.abort();
    clearSpeech();
    app.scope = scope;
    app.snapshot = null;
    app.online = false;
    app.busy = false;
    app.follow = true;
    app.search = "";
    $("trace-search").value = "";
    resetSession(null);
    $("connection-error").hidden = true;
    setText("sync-label", "切换范围，等待同步");
    renderAll();
    schedulePoll(0);
  }

  async function mutate(operation, successText = "") {
    if (app.busy) return false;
    const context = { scope: app.scope, sessionId: app.sessionId, generation: app.generation };
    app.busy = true;
    renderControls();
    renderPrompts();
    renderActions();
    try {
      await operation(context);
      if (context.generation !== app.generation) return false;
      if (successText) toast(successText);
      return true;
    } catch (error) {
      if (context.generation === app.generation) toast(error.message, true);
      return false;
    } finally {
      if (context.generation === app.generation) {
        app.busy = false;
        renderControls();
        renderPrompts();
        renderActions();
        schedulePoll(0);
      }
    }
  }
  function validateInput(message, scope) {
    if (!message || typeof message !== "object" || Array.isArray(message)) throw new Error("事件必须是一个 JSON 对象。");
    const allowed = scope === "lab"
      ? ["player_entered", "player_left", "utterance", "interaction", "memory_request"]
      : ["utterance", "interaction", "memory_request"];
    if (!allowed.includes(message.type)) throw new Error(scope === "godot" ? "实时 Godot 仅接受发言、交互与记忆查询；出入店和动作反馈由真实场景产生。" : "这里只接受玩家出入店、发言、交互与记忆查询；请在动作区发送模拟回执。");
    if (scope === "godot" && message.type === "interaction" && message.kind === "end_visit") throw new Error("实时 Godot 的离店必须由角色实际走出场景区域触发。");
    if (!connected()) throw new Error("当前会话未连接，暂时不能发送事件。");
    if (["utterance", "interaction", "player_left"].includes(message.type) && !presentPlayers().includes(message.player_id)) throw new Error("这个玩家当前不在店内，请先入店。");
    if (message.type === "utterance" && (!String(message.text || "").trim() || String(message.text).length > 2000)) throw new Error("发言需要 1 至 2000 个字符。");
  }
  function sendInput(message, successText = "") {
    return mutate(async ({ scope, sessionId }) => {
      validateInput(message, scope);
      await api("input", { method: "POST", body: message, scope, sessionId });
    }, successText);
  }
  function interact(kind, refs = {}, playerId = app.player) {
    return sendInput({ ...refs, type: "interaction", player_id: playerId, kind });
  }

  function harnessMessage(event) {
    const data = event.data || {};
    return data.message || data.payload || data;
  }
  function ingest(events) {
    for (const event of events.sort((a, b) => a.seq - b.seq)) {
      if (app.seen.has(event.seq)) continue;
      app.seen.add(event.seq);
      app.events.push(event);
      app.lastSeq = Math.max(app.lastSeq, Number(event.seq) || 0);
      if (["model_call", "model_request", "model_response", "model_validation", "model_result"].includes(event.kind) || (event.kind === "error" && event.call_id)) {
        const id = event.call_id || event.data?.call_id || `unlinked_${event.seq}`;
        const call = app.calls.get(id) || { id, lane: event.lane || event.data?.lane || "", seq: event.seq, at: event.at, events: [] };
        call.lane = event.lane || call.lane;
        call.events.push(event);
        call[event.kind] = event;
        call.status = call.model_result?.status || call.model_result?.data?.status || event.status || event.data?.status || (event.kind === "model_request" ? "pending" : "received");
        if (event.kind === "model_response") call.elapsed = event.data?.elapsed_ms;
        app.calls.set(id, call);
      }
      if (["input", "output"].includes(event.kind)) {
        const message = harnessMessage(event);
        if (message.type === "utterance") app.messages.set(`input:${event.seq}`, { ...message, seq: event.seq, flow_id: event.flow_id, at: event.at, role: "player" });
        if (message.type === "npc_speech") app.messages.set(`speech:${message.speech_id}`, { ...message, seq: event.seq, at: event.at, role: "npc" });
        if (["player_entered", "player_left"].includes(message.type)) app.messages.set(`visit:${event.seq}`, { ...message, seq: event.seq, at: event.at, role: "system" });
      }
    }
  }

  function renderAll() {
    renderControls();
    renderConversation();
    renderPrompts();
    renderActions();
    renderBusiness();
    renderStrategy();
    renderMemory();
    renderTrace();
    renderChanged("raw", app.snapshot || {}, () => setText("runtime-json", pretty(app.snapshot || {})));
  }
  function renderControls() {
    const isLab = app.scope === "lab";
    const isConnected = connected();
    const present = presentPlayers();
    const active = present.includes(app.player);
    document.querySelectorAll("[data-scope]").forEach((item) => {
      const selected = item.dataset.scope === app.scope;
      item.classList.toggle("is-active", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    document.querySelectorAll("[data-player]").forEach((item) => {
      const selected = item.dataset.player === app.player;
      item.classList.toggle("is-active", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    document.querySelectorAll("[data-presence]").forEach((item) => item.classList.toggle("present", present.includes(item.dataset.presence)));
    $("mode-select").disabled = !isLab || app.busy || Boolean(app.snapshot?.connected);
    if (app.snapshot?.connected && app.snapshot.mode) $("mode-select").value = app.snapshot.mode;
    $("start-session").disabled = !isLab || !app.online || app.busy || Boolean(app.snapshot?.connected);
    $("stop-session").disabled = !isLab || !isConnected || app.busy;
    $("export-log").href = `/debug/api/export?scope=${app.scope}${app.sessionId ? `&session_id=${encodeURIComponent(app.sessionId)}` : ""}`;
    $("export-log").classList.toggle("is-disabled", !app.sessionId || !app.online);
    $("export-log").setAttribute("aria-disabled", String(!app.sessionId || !app.online));
    $("connection-status").className = `connection-status ${isConnected ? "online" : (!app.online && app.snapshot ? "error" : "")}`;
    setText("connection-text", !app.online ? (app.snapshot ? "后端连接中断" : "正在连接后端") : isConnected ? (isLab ? "独立会话运行中" : "Godot 已连接") : (isLab ? "独立会话未启动" : "Godot 未连接"));
    const mode = app.snapshot?.mode;
    setText("model-status", mode === "rule" ? "规则联调 · 不调用模型" : `${app.snapshot?.model_id || "未指定模型"} · ${app.snapshot?.model_configured ? "模型已配置" : "模型未配置"}`);
    setText("session-label", `session ${app.sessionId || "—"}`);
    $("session-label").title = app.sessionId || "暂无会话";
    setText("scope-notice-text", isLab ? "只需后端，无需启动 Godot。动作可手动回传完成、失败或暂停。" : "观察真实场景；仅向在场玩家发送发言与交互。位置、出入店、动作和发言回执由 Godot 产生。");
    $("scope-notice").querySelector(".notice-label").textContent = isLab ? "独立运行" : "真实场景";
    $("enter-player").disabled = !isLab || !isConnected || active || app.busy;
    $("leave-player").disabled = !isLab || !isConnected || !active || app.busy;
    $("player-presence").className = `badge${active ? " teal" : ""}`;
    setText("player-presence", active ? "在店内" : "未入店");
    setText("presence-help", !isLab ? "入店和离店以 Godot 场景为准。切换身份不会结束访问。" : !isConnected ? "启动独立会话后，选择玩家并入店。" : active ? "该玩家已入店，可以发言或回应提示。" : "点击「入店」开始该玩家的访问。");
    $("utterance-text").disabled = !isConnected || !active;
    $("send-utterance").disabled = !writable() || !$("utterance-text").value.trim();
    $("target-select").disabled = !isConnected || app.busy;
    for (const id of ["order-drink", "invite-topic", "ask-menu"]) $(id).disabled = !writable();
    $("recipe-select").disabled = !writable();
    $("send-raw").disabled = !isConnected || app.busy;
    $("refresh-memory").disabled = !isConnected || app.busy;
    setText("raw-input-help", isLab ? "支持 player_entered / player_left / utterance / interaction / memory_request。" : "仅支持 utterance / interaction / memory_request；不能伪造真实场景事件。");
    setText("runtime-tag", isConnected ? `${present.length} 位在场 · ${mode === "rule" ? "规则" : "模型"}` : app.sessionId ? "保留最近状态" : "等待会话");
    const error = runtime().last_error || "";
    $("runtime-error").hidden = !error;
    setText("runtime-error", error);
    setText("action-mode-tag", isLab ? "手动模拟" : "真实回执");
    $("action-mode-tag").className = `badge ${isLab ? "amber" : "teal"}`;
    setText("action-help", isLab ? "手动回传动作结果，仅用于 Harness 逻辑调试。" : "由 Godot 执行动作并反馈，这里仅观察。");
    setText("character-count", `${$("utterance-text").value.length} / 2000`);
    const attention = array(runtime().prompts).length + array(app.snapshot?.pending_actions).length + Number(Boolean(app.snapshot?.speech?.speech_id));
    setText("interaction-count", attention);
    $("interaction-count").hidden = !attention;
    $("interaction-count").title = "待回应提示、动作回执或待展示发言";
    renderContextActions();
  }

  function renderContextActions() {
    const orders = array(runtime().orders).filter((order) => order.player_id === app.player && !["completed", "cancelled", "failed"].includes(order.status));
    const conversations = array(runtime().conversations).filter((item) => item.player_id === app.player && ["engaged", "paused"].includes(item.status));
    renderChanged("context", { orders, conversations, writable: writable(), player: app.player }, () => {
      const holder = $("context-actions");
      holder.replaceChildren();
      for (const order of orders) {
        const card = element("div", "context-action");
        card.append(element("p", "", `${RECIPES[order.recipe_id] || order.recipe_id} · ${label(order.status)} · ${order.order_id}`));
        const row = element("div", "button-row");
        if (["running", "queued", "ready"].includes(order.status)) row.append(button("暂停订单", () => interact("pause_order", { order_id: order.order_id }), { disabled: !writable() }));
        if (order.status === "paused") row.append(button("继续订单", () => interact("resume_order", { order_id: order.order_id }), { disabled: !writable() }));
        row.append(button("取消订单", () => interact("cancel_order", { order_id: order.order_id }), { disabled: !writable(), className: "button compact danger" }));
        card.append(row); holder.append(card);
      }
      for (const conversation of conversations) {
        const card = element("div", "context-action");
        card.append(element("p", "", `手冲话题 · ${label(conversation.status)} · ${conversation.conversation_id}`));
        const row = element("div", "button-row");
        const paused = conversation.status === "paused";
        row.append(button(paused ? "继续话题" : "暂停话题", () => interact(paused ? "resume_topic" : "pause_topic", { conversation_id: conversation.conversation_id }), { disabled: !writable() }));
        row.append(button("下次继续", () => interact("next_visit", { conversation_id: conversation.conversation_id }), { disabled: !writable() }));
        card.append(row); holder.append(card);
      }
    });
  }

  function renderConversation() {
    const current = app.snapshot?.speech;
    if (current?.speech_id && !app.messages.has(`speech:${current.speech_id}`)) app.messages.set(`speech:${current.speech_id}`, { ...current, role: "npc", seq: null, at: null });
    const messages = Array.from(app.messages.values()).sort((a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER));
    renderChanged("conversation", { messages, speechId: current?.speech_id, stopped: current?.stop_requested }, () => {
      const feed = $("conversation-feed");
      const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
      feed.replaceChildren();
      if (!messages.length) {
        const placeholder = element("div", "empty-state");
        placeholder.append(element("span", "empty-symbol", "“"), element("h3", "", "从一句话开始"), element("p", "", "启动会话，让顾客入店。玩家发言与老板娘的回应会按实际顺序出现在这里。"));
        feed.append(placeholder);
      }
      for (const message of messages) {
        if (message.role === "system") {
          const line = element("div", "message-system");
          line.append(seqButton(message.seq), element("span", "", `${playerName(message.player_id)} ${message.type === "player_entered" ? "走进咖啡馆" : "离开咖啡馆"}`), element("span", "mono", timestamp(message.at)));
          feed.append(line); continue;
        }
        const npc = message.role === "npc";
        const row = element("article", `message${npc ? " npc" : ""}${message.speech_id === current?.speech_id ? " active" : ""}`);
        if (message.speech_id) row.dataset.speechId = message.speech_id;
        row.append(element("span", `avatar message-avatar ${npc ? "avatar-npc" : message.player_id === "p_B" ? "avatar-b" : "avatar-a"}`, npc ? "店" : (message.player_id || "?").replace("p_", "")));
        const content = element("div", "message-content");
        const meta = element("div", "message-meta");
        const target = npc ? array(message.targets).map(playerName).join("、") : playerName(message.target_id || "npc_owner");
        meta.append(element("span", "message-author", npc ? "老板娘" : playerName(message.player_id)), element("span", "", `→ ${target || "—"}`), seqButton(message.seq), element("time", "mono", timestamp(message.at)));
        if (!npc) meta.append(button("查看处理流程 →", () => openFlow(message.flow_id || `legacy_${message.seq}`), { className: "message-flow-link" }));
        content.append(meta, element("div", "message-text", message.text || ""));
        if (message.mode === "rule") content.append(element("p", "message-note", "规则联调回应 · 未调用模型"));
        row.append(content); feed.append(row);
      }
      if (nearBottom || messages.length <= 2) feed.scrollTop = feed.scrollHeight;
    });
  }
  function seqButton(seq) {
    const item = button(seq === null || seq === undefined ? "同步中" : `#${seq}`, () => {
      if (seq === null || seq === undefined) return;
      app.traceMode = "events"; app.selectedEvent = seq; app.follow = false; app.search = ""; $("trace-search").value = ""; setWorkspace("trace"); renderTrace();
    }, { className: "message-seq", disabled: seq === null || seq === undefined });
    item.title = "查看关联日志";
    return item;
  }
  function renderPrompts() {
    const prompts = array(runtime().prompts);
    setText("prompt-count", String(prompts.length));
    renderChanged("prompts", { prompts, connected: connected(), busy: app.busy, present: presentPlayers() }, () => {
      const holder = $("prompt-list"); holder.replaceChildren();
      if (!prompts.length) holder.append(empty("当前没有需要回应的邀请或确认。"));
      for (const prompt of prompts) {
        const card = element("article", "prompt-card");
        const meta = element("div", "prompt-meta");
        meta.append(badge(playerName(prompt.player_id), prompt.player_id === "p_B" ? "amber" : "blue"), element("span", "prompt-id mono", prompt.prompt_id));
        card.append(meta, element("p", "", prompt.text || ""));
        const row = element("div", "button-row");
        for (const option of array(prompt.options)) {
          const expired = prompt.expires_at && Number(prompt.expires_at) * 1000 < Date.now();
          row.append(button(option.label || option.kind || "未定义选项", () => {
            const activePrompt = array(runtime().prompts).find((item) => item.prompt_id === prompt.prompt_id);
            if (!activePrompt) { toast("这条提示已经失效，请等待状态同步。", true); return; }
            const refs = Object.fromEntries(Object.entries(activePrompt).filter(([key]) => key.endsWith("_id")));
            interact(option.kind, { ...refs, value: option.value }, prompt.player_id);
          }, { disabled: !option.kind || !writable(prompt.player_id) || Boolean(expired), className: "button compact teal" }));
        }
        card.append(row); holder.append(card);
      }
    });
  }

  function renderActions() {
    const actions = array(app.snapshot?.pending_actions);
    setText("action-count", String(actions.length));
    renderChanged("actions", { actions, scope: app.scope, connected: connected(), busy: app.busy }, () => {
      const holder = $("action-list"); holder.replaceChildren();
      if (!actions.length) holder.append(empty("没有等待回执的动作。"));
      for (const action of actions) {
        const card = element("article", "action-card");
        const title = element("div", "action-title");
        title.append(element("strong", "", action.command || "未命名动作"), badge(label(action.status || "sent"), statusClass(action.status)));
        card.append(title, element("p", "action-id", action.request_id || "—"), element("pre", "action-args", pretty(action.args || {})));
        const controlAcknowledged = (action.control_requested === "pause" && action.status === "paused")
          || (action.control_requested === "resume" && action.status === "resumed");
        if (action.control_requested && !controlAcknowledged) card.append(element("p", "action-notice", `Harness 请求${label(action.control_requested)}，等待执行端回执。`));
        if (app.scope === "lab") {
          const row = element("div", "button-row");
          const available = connected() && !app.busy && Boolean(action.request_id);
          const statuses = action.status === "paused" ? [["resumed", "已继续"], ["failed", "失败"], ["cancelled", "已取消"]] : [["completed", "已完成"], ["failed", "失败"], ["paused", "已暂停"], ["cancelled", "已取消"]];
          if ([undefined, "pending", "sent", "queued"].includes(action.status)) statuses.unshift(["started", "已开始"]);
          const reason = element("input", ""); reason.type = "text"; reason.placeholder = "失败原因（可选）"; reason.setAttribute("aria-label", `${action.request_id} 的模拟失败原因`); reason.style.cssText = "width:100%;font-size:10px;padding:5px 7px;min-height:27px;border:1px solid var(--line);border-radius:4px;margin:0 0 7px";
          for (const [status, text] of statuses) row.append(button(`回执：${text}`, () => mutate(async ({ scope, sessionId }) => {
            if (scope !== "lab") throw new Error("实时 Godot 禁止发送模拟回执。");
            const body = { request_id: action.request_id, status };
            if (status === "failed") body.reason = reason.value.trim() || "手动模拟失败";
            await api("feedback", { method: "POST", body, scope, sessionId });
          }, "已发送手动模拟回执"), { disabled: !available, className: `button compact${status === "failed" ? " danger" : ""}` }));
          card.append(reason, row);
        }
        holder.append(card);
      }
    });
  }

  function keyValues(entries) {
    const list = element("dl", "key-values");
    for (const [key, value] of entries) list.append(element("dt", "", key), element("dd", "", typeof value === "object" && value !== null ? pretty(value) : value ?? "—"));
    return list;
  }
  function jsonDetails(title, value, open = false) {
    const details = element("details", "json-details"); details.open = open;
    details.append(element("summary", "", title), element("pre", "code-block", pretty(value)));
    return details;
  }
  function objectCard(title, id, status, entries, original) {
    const card = element("article", "object-card");
    const header = element("div", "object-header");
    const text = element("div", ""); text.append(element("h3", "object-title", title), element("p", "object-id", id));
    header.append(text); if (status) header.append(badge(label(status), statusClass(status)));
    card.append(header, keyValues(entries));
    if (original !== undefined) card.append(jsonDetails("完整数据", original));
    return card;
  }
  function renderBusiness() {
    const data = { orders: array(runtime().orders), conversations: array(runtime().conversations), activity: runtime().activity };
    renderChanged("business", data, () => {
      const holder = $("business-content"); holder.replaceChildren(element("h3", "view-section-title", "订单"));
      if (!data.orders.length) holder.append(empty("尚无订单。先点单，再回应真实的确认提示。"));
      for (const order of data.orders) holder.append(objectCard(`${playerName(order.player_id)} · ${RECIPES[order.recipe_id] || order.recipe_id}`, order.order_id, order.status, [["当前阶段", order.stage], ["杯子", order.cup_id], ["持有人", playerName(order.holder_id)], ["插槽", order.socket_id || "—"], ["原因", order.reason || "—"]], order));
      holder.append(element("h3", "view-section-title", "会话"));
      if (!data.conversations.length) holder.append(empty("尚无话题会话。可以从手冲邀请开始。"));
      for (const conversation of data.conversations) holder.append(objectCard(`${playerName(conversation.player_id)} · ${conversation.topic_id}`, conversation.conversation_id, conversation.status, [["当前阶段", conversation.stage], ["已显示段落", conversation.displayed_segment_id || "—"], ["摘要", conversation.summary || "尚无摘要"]], conversation));
      if (data.activity && Object.keys(data.activity).length) holder.append(element("h3", "view-section-title", "多人活动"), jsonDetails("活动当前数据", data.activity, true));
    });
  }
  function renderStrategy() {
    const data = { strategy: runtime().strategy || {}, goals: array(runtime().goals), agenda: array(runtime().agenda), situation: runtime().situation || {}, model: runtime().model || {} };
    renderChanged("strategy", data, () => {
      const holder = $("strategy-content"); holder.replaceChildren();
      holder.append(objectCard("当前战略", data.strategy.primary_goal_id || "暂无主要目标", "", [["交互方式", data.strategy.interaction_mode || "—"], ["下一步", data.strategy.next_step || "等待玩家行为与战略决策"], ["在途模型", array(data.model.in_flight).join("、") || "无"], ["活跃目标", array(data.strategy.active_goal_ids).join("、") || "无"]], data.strategy));
      holder.append(element("h3", "view-section-title", `目标 · ${data.goals.length}`));
      if (!data.goals.length) holder.append(empty("当前没有持续目标。"));
      for (const goal of data.goals) holder.append(objectCard(goal.kind || "目标", goal.goal_id || "", goal.status, [["相关玩家", playerName(goal.player_id)], ["阶段", goal.stage || "—"]], goal));
      holder.append(element("h3", "view-section-title", `待办与话轮 · ${data.agenda.length}`));
      for (const item of data.agenda) holder.append(objectCard(item.type || "待办", item.item_id || "", item.status, [["相关玩家", playerName(item.player_id)]], item));
      holder.append(jsonDetails("完整情境理解", data.situation));
    });
  }
  function renderMemory() {
    const memory = app.snapshot?.memory?.[app.player];
    setText("memory-heading", `${playerName(app.player)} 的记忆`);
    renderChanged("memory", { memory, player: app.player }, () => {
      const holder = $("memory-content"); holder.replaceChildren();
      if (!memory) { holder.append(empty("尚未读取这个玩家的记忆。点击「查询记忆」查看后台实际保存的数据。")); return; }
      const profile = memory.profile || {};
      holder.append(keyValues([["身份", profile.display_name || app.player], ["累计到店", profile.visit_count ?? "—"], ["角色", memory.character_id || "—"]]));
      const preferences = element("div", "memory-preferences");
      for (const [key, value] of Object.entries(profile.preferences || {})) { const chip = element("span", "memory-chip"); chip.append(element("span", "", key), element("strong", "", value)); preferences.append(chip); }
      holder.append(preferences);
      for (const [key, title] of [["topics", "话题进度"], ["commitments", "未来约定"], ["episodes", "共同经历"]]) {
        const items = array(memory[key]); holder.append(element("h3", "view-section-title", `${title} · ${items.length}`));
        if (!items.length) holder.append(empty(`暂无${title}。`));
        for (const item of items) holder.append(objectCard(item.topic_id || title, item.id || item.created_at || "", item.status, [["摘要", item.summary || item.kind || "—"], ["下一步", item.next_step || item.resume_hint || item.trigger || "—"]], item));
      }
      holder.append(jsonDetails("完整记忆响应", memory));
    });
  }

  function setWorkspace(view) {
    if (!["trace", "interact"].includes(view)) return;
    rememberSpeechTime();
    app.view = view;
    document.body.dataset.workspace = view;
    document.querySelectorAll("[data-workspace].workspace-button").forEach((item) => {
      const selected = item.dataset.workspace === view;
      item.classList.toggle("is-active", selected); item.setAttribute("aria-pressed", String(selected));
    });
    setText("workspace-hint", view === "trace" ? "从玩家输入追到数据、决策与输出" : "发送玩家输入，查看回应并推进动作");
    syncSpeech();
  }
  function setCallList(open) {
    app.listOpen = open;
    document.querySelector(".trace-workspace").classList.toggle("list-open", open);
    $("toggle-call-list").setAttribute("aria-expanded", String(open));
    $("close-call-list").hidden = !open;
  }
  function moveTrace(direction) {
    const selected = app.traceMode === "flows" ? app.selectedFlow : app.traceMode === "calls" ? app.selectedCall : app.selectedEvent;
    const index = app.filteredTrace.findIndex((item) => (app.traceMode !== "events" ? item.id : item.seq) === selected);
    const item = app.filteredTrace[index + direction];
    if (!item) return;
    app.follow = false;
    if (app.traceMode === "flows") app.selectedFlow = item.id; else if (app.traceMode === "calls") app.selectedCall = item.id; else app.selectedEvent = item.seq;
    renderTrace();
    $("trace-list").querySelector(".is-selected")?.scrollIntoView({ block: "nearest" });
  }
  function callTitle(call) {
    const context = call.model_call?.data?.context || {};
    if (call.lane === "event") {
      const event = context.event || {};
      const names = { player_entered: "顾客入店", utterance: "玩家发言", topic_segment: "讲解段落", order_ready: "饮品可领取", menu_question: "询问菜单", activity_result: "活动结果", action_failed: "动作失败", world_changed: "环境变化" };
      return `${playerName(event.player_id)} · ${event.text || names[event.type] || event.type || "事件决策"}`;
    }
    if (call.lane === "strategy") return `安排当前目标 · ${array(context.candidate_goals).length} 个候选`;
    if (call.lane === "episode") return `${playerName(context.player_id)} · ${context.topic_id === "pourover_intro" ? "手冲经历" : context.topic_id || "共同经历"}`;
    return call.id;
  }
  function callSearch(call) { return `${callTitle(call)} ${call.id} ${call.lane} ${call.events.map((event) => `${event.seq} ${event.summary || ""} ${pretty(event.data)}`).join(" ")}`.toLocaleLowerCase(); }
  function openFlow(id) {
    app.traceMode = "flows"; app.selectedFlow = id; app.follow = false; app.search = ""; $("trace-search").value = "";
    setWorkspace("trace"); setCallList(false); renderTrace();
  }
  function openFlowCall(id, tab = "input") {
    if (!app.calls.has(id)) { toast("当前日志窗口没有这次调用，可导出 JSONL 查看完整记录。", true); return; }
    app.traceMode = "calls"; app.selectedCall = id; app.detailTab = tab; app.lane = "all"; app.follow = false; app.search = ""; $("trace-search").value = ""; renderTrace();
  }
  function renderTrace() {
    document.querySelectorAll("[data-trace-mode]").forEach((item) => { const selected = item.dataset.traceMode === app.traceMode; item.classList.toggle("is-active", selected); item.setAttribute("aria-pressed", String(selected)); });
    document.querySelectorAll("[data-lane]").forEach((item) => { const selected = item.dataset.lane === app.lane; item.classList.toggle("is-active", selected); item.setAttribute("aria-pressed", String(selected)); });
    $("lane-filters").hidden = app.traceMode !== "calls";
    $("follow-latest").setAttribute("aria-pressed", String(app.follow));
    setText("follow-latest", app.follow ? "跟随最新 ✓" : "跟随最新");
    const flows = collectFlows(app.events);
    setText("flow-count", String(flows.length));
    setText("call-count", String(app.calls.size)); setText("event-count", String(app.events.length));
    setText("trace-heading", app.traceMode === "flows" ? "消息处理流程" : app.traceMode === "calls" ? "模型调用" : "事件日志");
    $("trace-gap").hidden = !app.gap; setText("trace-gap", app.gap);
    setText("trace-sequence", `seq ${app.lastSeq || "—"}`);
    setText("log-path", app.snapshot?.log_file || "每个会话独立保存日志"); $("log-path").title = app.snapshot?.log_file || "";
    const search = app.search.toLocaleLowerCase();
    const calls = Array.from(app.calls.values()).filter((call) => (app.lane === "all" || call.lane === app.lane) && (!search || callSearch(call).includes(search))).sort((a, b) => b.seq - a.seq);
    const events = app.events.filter((event) => !search || `${event.seq} ${event.kind} ${event.call_id || ""} ${event.summary || ""} ${pretty(event.data)}`.toLocaleLowerCase().includes(search)).slice().reverse();
    const filteredFlows = flows.filter((flow) => !search || `${flow.title} ${flow.id} ${pretty(flow.events)}`.toLocaleLowerCase().includes(search));
    if (app.traceMode === "calls" && (app.follow || !calls.some((call) => call.id === app.selectedCall))) app.selectedCall = calls[0]?.id || null;
    if (app.traceMode === "events" && (app.follow || !events.some((event) => event.seq === app.selectedEvent))) app.selectedEvent = events[0]?.seq ?? null;
    if (app.traceMode === "flows" && (app.follow || !filteredFlows.some((flow) => flow.id === app.selectedFlow))) app.selectedFlow = filteredFlows[0]?.id || null;
    const items = app.traceMode === "flows" ? filteredFlows : app.traceMode === "calls" ? calls : events;
    app.filteredTrace = items;
    const selectedIndex = items.findIndex((item) => app.traceMode === "flows" ? item.id === app.selectedFlow : app.traceMode === "calls" ? item.id === app.selectedCall : item.seq === app.selectedEvent);
    $("previous-call").disabled = selectedIndex <= 0;
    $("next-call").disabled = selectedIndex < 0 || selectedIndex >= items.length - 1;
    setText("toggle-call-list", `${app.traceMode === "flows" ? "消息" : "调用"}列表 · ${items.length}`);
    renderChanged("trace-list", { mode: app.traceMode, items, selected: app.traceMode === "flows" ? app.selectedFlow : app.traceMode === "calls" ? app.selectedCall : app.selectedEvent }, () => {
      const holder = $("trace-list"); const oldScroll = holder.scrollTop; holder.replaceChildren();
      if (!items.length) holder.append(empty(search ? "没有匹配的记录，请调整搜索内容。" : app.traceMode === "flows" ? "发送玩家发言后，可在这里查看处理流程。" : app.traceMode === "calls" ? "暂无决策调用。入店或发言后会出现模型或规则决策。" : "暂无事件。启动会话后，从玩家入店开始。"));
      for (const item of items) {
        if (app.traceMode === "flows") {
          const state = flowStatus(item); const selected = item.id === app.selectedFlow;
          const row = button("", () => openFlow(item.id), { className: `trace-item${selected ? " is-selected" : ""}` }); row.setAttribute("aria-pressed", String(selected));
          const top = element("div", "trace-item-top"); top.append(badge(state.text, state.tone), element("span", "trace-time", timestamp(item.at)));
          row.append(top, element("div", "trace-item-title", item.title), element("div", "trace-item-meta", `#${item.seq} · ${item.events.length} 条关联记录`)); holder.append(row); continue;
        }
        const isCall = app.traceMode === "calls";
        const selected = isCall ? app.selectedCall === item.id : app.selectedEvent === item.seq;
        const row = button("", () => { app.follow = false; if (isCall) app.selectedCall = item.id; else app.selectedEvent = item.seq; setCallList(false); renderTrace(); }, { className: `trace-item${selected ? " is-selected" : ""}` });
        row.setAttribute("aria-pressed", String(selected));
        const top = element("div", "trace-item-top");
        if (isCall) {
          const lane = element("span", ""); lane.append(element("i", `lane-dot ${["event", "strategy", "episode"].includes(item.lane) ? item.lane : ""}`), document.createTextNode(` ${LANES[item.lane] || item.lane || "未关联类型"}`));
          top.append(lane, badge(label(item.status), statusClass(item.status)));
          if (item.model_call?.data?.mode === "rule") top.append(badge("规则 · 无 HTTP", "amber"));
          top.append(element("span", "trace-time", timestamp(item.at)));
          const title = element("div", "trace-item-title", callTitle(item)); title.title = `${callTitle(item)}\n${item.id}`;
          row.append(top, title);
          const usage = item.model_response?.data?.response?.usage;
          const meta = element("div", "trace-item-meta"); meta.append(element("span", "", `#${item.seq}`), element("span", "", item.elapsed !== undefined ? `${(item.elapsed / 1000).toFixed(2)} s` : "等待返回"));
          if (usage?.prompt_tokens !== undefined) meta.append(element("span", "", `${usage.prompt_tokens} 输入 token`)); row.append(meta);
        } else {
          top.append(element("span", "mono", `#${item.seq}`), badge(KIND_LABELS[item.kind] || item.kind, item.kind === "error" ? "error" : ""), element("span", "trace-time", timestamp(item.at)));
          row.append(top, element("div", "trace-item-summary", item.summary || harnessMessage(item).type || item.kind));
          if (item.call_id) row.append(element("div", "trace-item-meta", item.call_id));
        }
        holder.append(row);
      }
      holder.scrollTop = app.follow ? 0 : oldScroll;
    });
    if (app.traceMode === "flows") {
      const flow = flows.find((item) => item.id === app.selectedFlow);
      renderChanged("trace-detail", { mode: "flows", flow }, () => renderFlow($("trace-detail"), flow, { copy, onCall: openFlowCall, onEvent: (seq) => { app.traceMode = "events"; app.selectedEvent = seq; app.follow = false; app.search = ""; $("trace-search").value = ""; renderTrace(); }, onInteract: () => setWorkspace("interact") }));
    } else if (app.traceMode === "calls") renderCallDetail(app.calls.get(app.selectedCall));
    else renderEventDetail(app.events.find((event) => event.seq === app.selectedEvent));
  }

  function readingSection(title, text, code = false) {
    const section = element("section", "reading-section");
    if (title) section.append(element("h4", "reading-label", title));
    section.append(element("pre", code ? "reader-code" : "reader-text", text));
    return section;
  }
  function modelOutput(call) {
    const content = call.model_response?.data?.response?.choices?.[0]?.message?.content;
    // Malformed model JSON is a normal debugging case. Only render fields from
    // the schema-validated result; always retain the unmodified provider text.
    const validated = call.model_validation?.status === "validated" ? call.model_validation.data?.result : null;
    const rule = call.model_call?.data?.mode === "rule" ? call.model_result?.data?.result : null;
    return { raw: content === undefined ? undefined : typeof content === "string" ? content : pretty(content), parsed: validated || rule || null };
  }
  function selectDetailTab(tab, focus = false) {
    app.detailTab = tab;
    renderTrace();
    if (focus) document.querySelector(`[data-detail-tab="${tab}"]`)?.focus();
  }
  function renderCallDetail(call) {
    renderChanged("trace-detail", { mode: "calls", call: call || null, tab: app.detailTab, raw: app.rawSection }, () => {
      const holder = $("trace-detail");
      const sameView = holder.dataset.callId === call?.id && holder.dataset.detailTab === app.detailTab && holder.dataset.rawSection === app.rawSection;
      const oldScroll = sameView ? holder.querySelector(".trace-content")?.scrollTop || 0 : 0;
      holder.replaceChildren(); holder.dataset.callId = call?.id || ""; holder.dataset.detailTab = app.detailTab; holder.dataset.rawSection = app.rawSection;
      if (!call) { const placeholder = element("div", "empty-state"); placeholder.append(element("span", "empty-symbol mono", "{ }"), element("h3", "", app.search ? "没有匹配的调用" : "等待一次模型调用"), element("p", "", app.search ? "打开调用列表，修改搜索或筛选条件。" : "在「交互测试」中入店或发言，再到这里查看模型的输入与输出。"), button("去交互测试", () => setWorkspace("interact"), { className: "button primary" })); holder.append(placeholder); return; }
      const result = call.model_result?.data;
      const status = result?.status || call.model_result?.status || call.status;
      const isRule = call.model_call?.data?.mode === "rule";
      const header = element("div", "reader-header");
      const title = element("div", "reader-title"); title.append(element("h3", "", callTitle(call)), badge(label(status), statusClass(status))); header.append(title);
      const meta = element("div", "reader-metrics"); meta.append(element("span", "", LANES[call.lane] || "模型调用"), element("span", "", timestamp(call.at)), element("span", "", call.elapsed !== undefined ? `${(call.elapsed / 1000).toFixed(2)} s` : "等待响应"));
      const usage = call.model_response?.data?.response?.usage;
      if (usage?.prompt_tokens !== undefined) meta.append(element("span", "", `输入 ${usage.prompt_tokens} tokens`));
      if (usage?.completion_tokens !== undefined) meta.append(element("span", "", `输出 ${usage.completion_tokens} tokens`));
      if (isRule) meta.append(badge("规则 · 无 HTTP", "amber"));
      header.append(meta); holder.append(header);
      if (call.model_call?.flow_id) header.append(button("← 返回消息流程", () => openFlow(call.model_call.flow_id), { className: "text-button" }));
      const tabs = element("div", "reader-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "调用内容");
      const tabLabels = [["input", "实际输入"], ["output", "模型输出"], ["result", "处理结果"], ["choices", "可选动作"], ["system", "系统规则"], ["raw", "原始 JSON"]];
      for (const [id, name] of tabLabels) {
        const selected = app.detailTab === id;
        const tab = button(name, () => selectDetailTab(id, true), { className: `reader-tab${selected ? " is-active" : ""}` });
        tab.id = `detail-tab-${id}`; tab.dataset.detailTab = id; tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(selected)); tab.setAttribute("aria-controls", "trace-content"); tab.tabIndex = selected ? 0 : -1; tabs.append(tab);
      }
      tabs.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); const current = tabLabels.findIndex(([id]) => id === app.detailTab);
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabLabels.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabLabels.length) % tabLabels.length;
        selectDetailTab(tabLabels[next][0], true);
      });
      holder.append(tabs);
      const toolbar = element("div", "reader-toolbar");
      const idText = element("span", "reader-call-id mono", call.id); idText.title = call.id; toolbar.append(idText);
      let copyValue = "";
      toolbar.append(button("复制当前内容", () => copy(copyValue), { className: "button compact" })); holder.append(toolbar);
      const content = element("div", "trace-content"); content.id = "trace-content"; content.setAttribute("role", "tabpanel"); content.setAttribute("aria-labelledby", `detail-tab-${app.detailTab}`); content.tabIndex = 0;
      const request = call.model_request?.data?.request;
      const messages = array(request?.messages);
      const choices = call.model_call?.data?.context?.available_actions;
      if (app.detailTab === "input" || app.detailTab === "system") {
        const selected = messages.filter((message) => app.detailTab === "system" ? message.role === "system" : message.role !== "system");
        copyValue = selected.map((message) => typeof message.content === "string" ? message.content : pretty(message.content)).join("\n\n");
        if (!selected.length) {
          if (isRule && app.detailTab === "input") { copyValue = pretty(call.model_call?.data?.context); content.append(readingSection("规则输入 · 未发送给模型", copyValue, true)); }
          else content.append(empty(app.detailTab === "system" ? "这次调用没有系统消息。" : "请求尚未发送，实际输入将在发送后显示。"));
        }
        selected.forEach((message) => content.append(readingSection(message.role === "system" ? "SYSTEM · 固定规则与输出格式" : `${String(message.role).toUpperCase()} · 实际发送内容`, typeof message.content === "string" ? message.content : pretty(message.content))));
      } else if (app.detailTab === "output") {
        const output = modelOutput(call); const plan = output.parsed; copyValue = output.raw ?? pretty(plan);
        if (isRule) content.append(element("p", "reader-intro", "以下为规则联调结果，未调用模型。"));
        if (!plan) content.append(readingSection("原始输出", output.raw ?? call.model_response?.data?.error ?? (call.model_result || call.model_response ? "没有可读输出，请查看处理结果或原始响应。" : isRule ? "等待规则结果。" : "等待模型返回。")));
        else {
          if (plan.speech) content.append(readingSection(`回复 → ${array(plan.speech.target_player_ids).map(playerName).join("、")}`, plan.speech.text));
          if (plan.reaction) content.append(keyValues([["反应", plan.reaction], ["动作", plan.action?.type || "不新增动作"], ["目标", plan.goal_id || "无"]]));
          if (plan.action) content.append(readingSection("动作参数", pretty(plan.action.args), true));
          if (array(plan.memory_updates).length) content.append(readingSection("记忆修改", pretty(plan.memory_updates), true));
          if (plan.summary) content.append(readingSection("经历摘要", plan.summary), readingSection("续谈提示", plan.resume_hint || "无"));
          if (plan.active_goal_ids) content.append(readingSection("下一步", plan.next_step || "无"), keyValues([["主要目标", plan.primary_goal_id || "无"], ["活跃目标", plan.active_goal_ids.join("、") || "无"], ["互动方向", plan.interaction_mode || "无"]]));
          content.append(element("p", "reader-footnote", "完整字段及 HTTP 响应可在「原始 JSON」中查看。"));
        }
      } else if (app.detailTab === "choices") {
        copyValue = pretty(choices);
        if (choices === undefined) content.append(empty("这类调用不选择执行动作。"));
        else {
          content.append(element("p", "reader-intro", `本轮共 ${Object.keys(choices).length} 个可选动作。参数只能使用下方列出的值。`));
          for (const [name, args] of Object.entries(choices)) {
            const card = element("section", "choice-card"); card.append(element("h4", "mono", name));
            for (const [key, values] of Object.entries(args)) card.append(keyValues([[key, values.map((value) => value === "" ? '""（从保存进度重建）' : value).join(" / ")]]));
            if (!Object.keys(args).length) card.append(element("p", "muted", "无参数")); content.append(card);
          }
          content.append(element("p", "reader-footnote", "不新增动作时使用 action=null。"));
        }
      } else if (app.detailTab === "result") {
        copyValue = pretty({ result: call.model_result?.data, validation: call.model_validation?.data });
        content.append(element("div", `result-note ${statusClass(status)}`, result?.reason || (isRule ? "规则联调，没有 HTTP 请求。" : "等待 Harness 处理结果。")));
        const stages = [["model_call", "调度"], ["model_request", "发送请求"], ["model_response", "模型返回"], ["model_validation", "结构校验"], ["model_result", "采纳结果"]];
        const stageList = element("ol", "call-stages");
        for (const [key, name] of stages) { const event = call[key]; const row = element("li", event ? "complete" : ""); row.append(element("strong", "", name), badge(event ? label(event.status || event.data?.status || "received") : isRule && ["model_request", "model_response", "model_validation"].includes(key) ? "不适用" : "等待", event ? statusClass(event.status) : ""), element("span", "mono muted", event ? `#${event.seq}` : "—")); stageList.append(row); }
        content.append(stageList);
        const validation = call.model_validation?.data;
        if (validation?.error) content.append(readingSection("校验错误", validation.error));
        if (call.model_response?.data?.error) content.append(readingSection("请求错误", call.model_response.data.error));
        content.append(keyValues([["HTTP", call.model_response?.data?.http_status ?? "未收到响应"], ["调用 ID", call.id], ["处理状态", label(status)]]));
      } else {
        const sources = { request: ["完整请求", isRule ? call.model_call?.data : request], response: ["HTTP 响应", call.model_response?.data], validation: ["结构校验", call.model_validation?.data], result: ["采纳结果", call.model_result?.data], all: ["完整调用日志", call.events] };
        const select = element("select", "raw-source-select"); select.setAttribute("aria-label", "原始数据类型");
        for (const [id, [name]] of Object.entries(sources)) { const option = element("option", "", name); option.value = id; select.append(option); }
        select.value = app.rawSection; select.addEventListener("change", () => { app.rawSection = select.value; renderTrace(); }); toolbar.prepend(select);
        const [name, value] = sources[app.rawSection]; copyValue = value === undefined ? "尚无记录" : pretty(value); content.append(readingSection(name, copyValue, true));
      }
      holder.append(content); content.scrollTop = oldScroll;
    });
  }
  function renderEventDetail(event) {
    renderChanged("trace-detail", { mode: "events", event: event || null }, () => {
      const holder = $("trace-detail"); holder.replaceChildren(); holder.dataset.callId = "";
      if (!event) { holder.append(empty("选择一条事件，查看完整输入、输出及关联 ID。")); return; }
      const header = element("div", "reader-header");
      const top = element("div", "detail-header-top"); top.append(badge(KIND_LABELS[event.kind] || event.kind, "blue"), button("复制完整事件", () => copy(event), { className: "text-button" }));
      header.append(top, element("h3", "detail-call-id", `#${event.seq} · ${event.summary || event.kind}`), element("p", "detail-meta", timestamp(event.at)));
      if (event.call_id) header.append(button("查看关联调用", () => { app.traceMode = "calls"; app.lane = "all"; app.search = ""; $("trace-search").value = ""; app.selectedCall = event.call_id; app.follow = false; renderTrace(); }, { className: "button compact" }));
      if (event.flow_id) header.append(button("← 返回消息流程", () => openFlow(event.flow_id), { className: "button compact" }));
      const content = element("div", "trace-content"); content.append(readingSection("事件原始数据", pretty(event), true)); holder.append(header, content);
    });
  }

  function speechIsCurrent(delivery) {
    return app.scope === "lab" && delivery === app.speech && delivery.generation === app.generation && delivery.sessionId === app.sessionId && connected() && app.snapshot?.speech?.speech_id === delivery.id;
  }
  function speechVisible(delivery) {
    return speechIsCurrent(delivery) && app.view === "interact" && app.tab === "dialogue" && document.visibilityState === "visible";
  }
  async function sendSpeechFeedback(delivery, status) {
    if (!speechIsCurrent(delivery) || delivery.pending || delivery.record.terminal) return;
    if (status === "displayed" && delivery.record.displayed) return;
    delivery.pending = true;
    try {
      await api("speech", { method: "POST", scope: "lab", sessionId: delivery.sessionId, body: { speech_id: delivery.id, status } });
      if (status === "displayed") delivery.record.displayed = true;
      else delivery.record.terminal = status;
      if (speechIsCurrent(delivery)) { renderSpeechStatus(); if (status === "displayed") armSpeechTimer(delivery); schedulePoll(0); }
    } catch (error) {
      if (speechIsCurrent(delivery)) toast(`发言回执未送达：${error.message}`, true);
    } finally { delivery.pending = false; }
  }
  function armSpeechTimer(delivery) {
    if (!speechVisible(delivery) || !delivery.record.displayed || delivery.record.terminal || delivery.timer) return;
    delivery.visibleSince = performance.now();
    delivery.timer = setTimeout(() => {
      delivery.timer = null;
      if (!speechVisible(delivery)) { rememberSpeechTime(); return; }
      delivery.record.remaining = 0; delivery.visibleSince = null;
      sendSpeechFeedback(delivery, "finished");
    }, delivery.record.remaining);
  }
  function syncSpeech() {
    const speech = app.snapshot?.speech;
    if (app.scope !== "lab" || !connected() || !speech?.speech_id) { clearSpeech(); renderSpeechStatus(); return; }
    if (!app.speech || app.speech.id !== speech.speech_id || app.speech.sessionId !== app.sessionId) {
      clearSpeech();
      const key = `${app.sessionId}:${speech.speech_id}`;
      let record = app.speechHistory.get(key);
      if (!record) {
        record = { displayed: Boolean(speech.recorded), terminal: null, remaining: Math.min(10000, Math.max(2000, String(speech.text || "").length * 70)) };
        app.speechHistory.set(key, record);
      }
      app.speech = { id: speech.speech_id, sessionId: app.sessionId, generation: app.generation, record, pending: false, timer: null, visibleSince: null };
    }
    const delivery = app.speech;
    if (speech.stop_requested) { rememberSpeechTime(); sendSpeechFeedback(delivery, "interrupted"); renderSpeechStatus(); return; }
    if (!speechVisible(delivery)) { rememberSpeechTime(); renderSpeechStatus(); return; }
    if (!delivery.record.displayed && !delivery.pending) {
      // Two animation frames ensure the DOM containing this speech has reached a paint.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (speechVisible(delivery) && app.snapshot?.speech?.speech_id === delivery.id) sendSpeechFeedback(delivery, "displayed");
      }));
    } else armSpeechTimer(delivery);
    renderSpeechStatus();
  }
  function renderSpeechStatus() {
    const holder = $("speech-status"); holder.replaceChildren();
    const speech = app.snapshot?.speech;
    holder.hidden = !speech?.speech_id;
    if (!speech?.speech_id) return;
    if (app.scope === "godot") { holder.append(element("span", "", "真实场景正在展示这段发言"), element("span", "mono", speech.speech_id)); return; }
    const delivery = app.speech;
    const message = delivery?.record.terminal ? label(delivery.record.terminal) : delivery?.record.displayed ? "浏览器已实际展示 · 展示结束后回传 finished" : "等待对话面板实际展示";
    holder.append(element("span", "", message), button("中断展示", () => { if (app.speech) { rememberSpeechTime(); sendSpeechFeedback(app.speech, "interrupted"); } }, { className: "text-button", disabled: !delivery || delivery.pending || Boolean(delivery.record.terminal) }));
  }

  function setTab(tab, focus = false) {
    if (app.tab !== tab) rememberSpeechTime();
    app.tab = tab;
    document.querySelectorAll("[data-tab]").forEach((item) => {
      const selected = item.dataset.tab === tab;
      item.classList.toggle("is-active", selected); item.setAttribute("aria-selected", String(selected)); item.tabIndex = selected ? 0 : -1;
      $(item.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) item.focus();
    });
    syncSpeech();
  }

  document.querySelectorAll(".workspace-button").forEach((item) => item.addEventListener("click", () => setWorkspace(item.dataset.workspace)));
  $("toggle-call-list").addEventListener("click", () => setCallList(!app.listOpen));
  $("close-call-list").addEventListener("click", () => setCallList(false));
  $("previous-call").addEventListener("click", () => moveTrace(-1));
  $("next-call").addEventListener("click", () => moveTrace(1));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && app.listOpen) { setCallList(false); $("toggle-call-list").focus(); } });
  document.querySelectorAll("[data-scope]").forEach((item) => item.addEventListener("click", () => switchScope(item.dataset.scope)));
  document.querySelectorAll("[data-player]").forEach((item) => item.addEventListener("click", () => { app.player = item.dataset.player; renderControls(); renderMemory(); }));
  document.querySelectorAll("[data-example]").forEach((item) => item.addEventListener("click", () => { $("utterance-text").value = item.dataset.example; renderControls(); $("utterance-text").focus(); }));
  document.querySelectorAll("[data-tab]").forEach((item) => item.addEventListener("click", () => setTab(item.dataset.tab)));
  $("tab-dialogue").parentElement.addEventListener("keydown", (event) => {
    const tabs = Array.from(document.querySelectorAll("[data-tab]")); const index = tabs.indexOf(document.activeElement);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    setTab(tabs[next].dataset.tab, true);
  });
  document.querySelectorAll("[data-trace-mode]").forEach((item) => item.addEventListener("click", () => { app.traceMode = item.dataset.traceMode; renderTrace(); }));
  document.querySelectorAll("[data-lane]").forEach((item) => item.addEventListener("click", () => { app.lane = item.dataset.lane; app.selectedCall = null; renderTrace(); }));
  $("trace-search").addEventListener("input", () => { app.search = $("trace-search").value; renderTrace(); });
  $("follow-latest").addEventListener("click", () => { app.follow = !app.follow; renderTrace(); });
  $("retry-connection").addEventListener("click", () => schedulePoll(0));
  $("copy-state").addEventListener("click", () => copy(app.snapshot || {}));
  $("start-session").addEventListener("click", () => mutate(async ({ scope }) => {
    if (scope !== "lab") throw new Error("只能在独立调试范围启动会话。");
    await api("session", { method: "POST", body: { op: "start", mode: $("mode-select").value } });
  }, "独立调试会话已启动"));
  $("stop-session").addEventListener("click", () => mutate(async ({ scope, sessionId }) => {
    if (scope !== "lab") throw new Error("浏览器不能停止 Godot 会话。");
    clearSpeech();
    await api("session", { method: "POST", body: { op: "stop", session_id: sessionId } });
  }, "独立调试会话已停止"));
  $("enter-player").addEventListener("click", () => sendInput({ type: "player_entered", player_id: app.player, display_name: playerName(app.player) }));
  $("leave-player").addEventListener("click", () => sendInput({ type: "player_left", player_id: app.player }));
  $("utterance-text").addEventListener("input", renderControls);
  $("utterance-text").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); $("utterance-form").requestSubmit(); } });
  $("utterance-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("utterance-text").value.trim(); const playerId = app.player; const target = $("target-select").value;
    if (!text || !writable()) return;
    const sent = await sendInput({ type: "utterance", player_id: playerId, target_id: target, text, channel: target === "room" ? "room" : "local" });
    if (sent && app.player === playerId && $("utterance-text").value.trim() === text) $("utterance-text").value = "";
    renderControls();
  });
  $("order-drink").addEventListener("click", () => interact("order_drink", { value: { recipe_id: $("recipe-select").value } }));
  $("invite-topic").addEventListener("click", () => interact("invite_topic", { value: "pourover_intro" }));
  $("ask-menu").addEventListener("click", () => interact("ask_menu"));
  $("refresh-memory").addEventListener("click", () => sendInput({ type: "memory_request", player_id: app.player }));
  $("send-raw").addEventListener("click", () => { try { const message = JSON.parse($("raw-input").value); sendInput(message, "事件已发送"); } catch (error) { toast(`JSON 格式错误：${error.message}`, true); } });
  $("export-log").addEventListener("click", (event) => { if ($( "export-log").getAttribute("aria-disabled") === "true") event.preventDefault(); });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState !== "visible") rememberSpeechTime(); else { syncSpeech(); schedulePoll(0); } });
  window.addEventListener("pagehide", () => { clearTimeout(app.pollTimer); app.pollController?.abort(); clearSpeech(); });

  renderAll();
  schedulePoll(0);
})();
