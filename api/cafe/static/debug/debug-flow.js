// Per-message evidence view. Correlation comes only from Harness IDs, never timing.
const STAGES = [
  ["input", "接收输入"], ["perception", "感知与对象"], ["queue", "待办调度"],
  ["context", "数据注入"], ["decision", "生成提议"], ["validation", "校验提议"],
  ["apply", "采纳与变更"], ["output", "发言与动作"], ["feedback", "执行回执"],
];
const KINDS = { input: "input", model_call: "context", model_request: "decision", model_response: "decision", model_validation: "validation", model_result: "apply", output: "output" };
const LABELS = { received: "已接收", accepted: "已采纳", applied: "已应用", completed: "已完成", validated: "通过", rejected: "未通过", discarded: "已丢弃", error: "失败", failed: "失败", skipped: "已跳过", ignored: "未处理", queued: "排队中", pending: "等待中", started: "已开始", emitted: "已下发", sent: "已下发", rule: "规则联调", displayed: "已展示", finished: "展示结束", interrupted: "已中断", deferred: "暂缓", superseded: "已替代", cancelled: "已取消", paused: "已暂停" };
Object.assign(LABELS, { generating: "生成中", dispatched: "已调度", waiting: "等待通道", waiting_feedback: "等待回执", built: "已注入", proposed: "已生成提议", no_effect: "无状态变更", observed: "已观察", resumed: "已继续", presenting: "展示中", expired: "已超时" });
const CONTEXT = { event: ["当前事件", "本条玩家输入与 Harness 事件 ID"], situation: ["环境与身体状态", "感知快照、在场顾客和通道占用"], memory: ["玩家记忆", "该玩家的 SQLite 资料与话题进度"], strategy: ["当前战略", "已采纳的目标和下一步安排"], orders: ["订单", "Harness 当前订单状态"], conversations: ["会话", "该玩家的话题状态"], recent_history: ["近期对话", "运行时实际记录的对话历史"], available_actions: ["可选动作", "本轮状态下可执行的动作及参数范围"] };
CONTEXT.available_topics = ["可聊话题", "已实现的话题配置"];
const pretty = (v) => JSON.stringify(v, null, 2);
const name = (id) => ({ p_A: "顾客 A", p_B: "顾客 B", npc_owner: "老板娘", room: "公共话题" }[id] || id || "未知玩家");
const el = (tag, cls, text) => { const n = document.createElement(tag); n.className = cls || ""; if (text != null) n.textContent = String(text); return n; };
const btn = (text, fn, cls = "button compact") => { const n = el("button", cls, text); n.type = "button"; n.addEventListener("click", fn); return n; };
const bad = (status) => ["rejected", "error", "failed", "invalid"].includes(status);
const stopped = (status) => bad(status) || ["discarded", "skipped", "ignored", "superseded", "cancelled", "expired"].includes(status);
const tone = (status) => bad(status) ? "error" : stopped(status) || ["paused", "deferred"].includes(status) ? "amber" : ["pending", "queued", "started", "running", "generating", "waiting", "waiting_feedback", "sent", "emitted"].includes(status) ? "blue" : "teal";
const textStatus = (s) => LABELS[s] || s || "已记录";
let selectedFlow = null;
let selectedStage = "context";

export function collectFlows(events) {
  const flows = new Map();
  for (const event of events) {
    const input = event.kind === "input" && event.data?.type === "utterance";
    const id = event.flow_id || (input ? `legacy_${event.seq}` : null);
    if (!id) continue;
    const flow = flows.get(id) || { id, seq: event.seq, at: event.at, input: null, events: [], calls: [], dependencies: [], legacy: !event.flow_id };
    flow.events.push(event);
    if (input) { flow.input = event; flow.seq = event.seq; flow.at = event.at; }
    flows.set(id, flow);
  }
  // Keep shared planning as a dependency, not as this player's decision chain.
  for (const event of events) {
    if (event.kind !== "model_call") continue;
    if (flows.has(event.flow_id)) flows.get(event.flow_id).calls.push(event);
    for (const id of event.data?.related_flow_ids || []) if (flows.has(id)) flows.get(id).dependencies.push(event);
  }
  return Array.from(flows.values()).map((flow) => ({ ...flow, title: flow.input ? `${name(flow.input.data.player_id)} · ${flow.input.data.text}` : "消息起点不在当前日志窗口" })).sort((a, b) => b.seq - a.seq);
}

function stageFor(event) {
  if (event.kind === "flow_step") {
    // Rejections before a decision exists belong at the input gate of the map.
    if (event.data?.stage === "validation" && !event.call_id && !event.data?.item_id) return "input";
    return event.data?.stage;
  }
  if (event.kind === "input" && ["speech_feedback", "action_feedback"].includes(event.data?.type)) return "feedback";
  if (event.kind === "input" && event.data?.type !== "utterance") return null;
  return KINDS[event.kind] || (event.kind === "error" ? (event.call_id ? "validation" : "input") : null);
}

function stageInfo(flow) {
  const stages = STAGES.map(([id, title]) => {
    const records = flow.events.filter((event) => stageFor(event) === id);
    const steps = records.filter((event) => event.kind === "flow_step");
    const latest = steps.at(-1) || records.at(-1);
    const status = id === "queue" && latest?.status === "generating" ? "dispatched" : latest?.status || "";
    return { id, title, records, latest, status, summary: latest?.summary || "" };
  });
  const end = stages.findIndex((stage) => stopped(stage.status));
  const deferred = stages.some((stage) => stage.status === "deferred");
  for (let i = 0; i < stages.length; i++) {
    if (stages[i].records.length) continue;
    const text = flow.legacy || !flow.input ? "缺少关联记录" : end >= 0 && i > end ? "未进入" : deferred ? "等待继续处理" : "尚无记录";
    stages[i].summary = text;
    stages[i].status = "unknown";
  }
  return stages;
}

export function flowStatus(flow) {
  if (flow.legacy) return { text: "旧记录", tone: "amber" };
  const latest = flow.events.filter((event) => event.kind === "flow_step").at(-1) || flow.events.at(-1);
  return { text: textStatus(latest?.status), tone: tone(latest?.status) };
}

function dataBlock(title, value, code = true) {
  const section = el("section", "flow-data-block");
  section.append(el("h4", "reading-label", title), el("pre", code ? "reader-code" : "reader-text", typeof value === "string" ? value : pretty(value)));
  return section;
}

function changedFields(before, after, path = "") {
  if (pretty(before) === pretty(after)) return [];
  const record = (value) => value && typeof value === "object" && !Array.isArray(value);
  if (record(before) || record(after)) {
    const keys = new Set([...Object.keys(record(before) ? before : {}), ...Object.keys(record(after) ? after : {})]);
    return Array.from(keys).flatMap((key) => changedFields(before?.[key], after?.[key], path ? `${path}.${key}` : key));
  }
  return [{ path, before, after }];
}

function changeSummary(event) {
  const changes = event.data?.changes;
  if (!changes) return null;
  const names = { memory: "玩家记忆", orders: "订单", conversations: "话题会话", prompts: "确认与邀请", actions: "动作", agenda: "待办", players: "玩家状态", activity: "活动", maintenance: "店务" };
  const block = el("section", "flow-change-summary");
  block.append(el("h4", "reading-label", `#${event.seq} · 实际变更`));
  for (const [section, objects] of Object.entries(changes)) {
    const entries = objects && ("before" in objects || "after" in objects) ? [[section, objects]] : Object.entries(objects || {});
    for (const [id, values] of entries) {
      if (!values || typeof values !== "object") continue;
      const rows = changedFields(values.before, values.after);
      const details = el("details", "change-object"); details.open = section === "memory";
      details.append(el("summary", "", `${names[section] || section} · ${id} · ${rows.length} 处变更`));
      const table = el("table", "change-table");
      const head = el("thead"); const headers = el("tr");
      for (const label of ["字段", "之前", "之后"]) headers.append(el("th", "", label)); head.append(headers); table.append(head);
      const body = el("tbody");
      for (const row of rows) {
        const tr = el("tr"); tr.append(el("td", "mono", row.path));
        for (const value of [row.before, row.after]) tr.append(el("td", "", value === undefined ? "无" : typeof value === "string" ? value : pretty(value)));
        body.append(tr);
      }
      table.append(body); details.append(table); block.append(details);
    }
  }
  return block;
}

function renderEvidence(holder, flow, stage, callbacks) {
  holder.replaceChildren();
  const heading = el("div", "flow-evidence-heading");
  heading.append(el("h3", "", stage.title), el("span", `badge ${stage.status === "unknown" ? "" : tone(stage.status)}`, stage.status === "unknown" ? stage.summary : textStatus(stage.status)));
  heading.append(btn("复制此阶段", () => callbacks.copy(stage.records)));
  heading.append(btn("↑ 流程图", () => holder.parentElement.querySelector(".flow-map")?.scrollIntoView({ block: "start" })));
  holder.append(heading);
  if (stage.id === "feedback") {
    holder.append(el("p", "reader-intro", "回执以实际展示或执行反馈为准。独立调试中，查看对话会回传发言展示；动作由交互区手动回传。"), btn("查看对话与动作", callbacks.onInteract));
  }
  if (!stage.records.length) { holder.append(el("p", "reader-intro", stage.summary === "未进入" ? "前序步骤已终止，这个阶段尚未执行。" : "当前日志没有这个阶段的证据。新消息会在实际处理后实时更新。")); return; }
  if (stage.id === "context") {
    for (const call of flow.calls) {
      const context = call.data?.context;
      if (!context) continue;
      const sourceFacts = stage.records.find((event) => event.kind === "flow_step" && event.call_id === call.call_id)?.data?.sources || {};
      holder.append(el("p", "flow-call-caption", `本次注入 · ${call.call_id}`));
      const sources = el("div", "injection-grid");
      for (const [key, value] of Object.entries(context)) {
        const [title, fallbackSource] = CONTEXT[key] || [key, "Harness 本轮上下文"];
        const source = sourceFacts[key];
        const card = el("details", "injection-card");
        const count = Array.isArray(value) ? `${value.length} 条` : value && typeof value === "object" ? `${Object.keys(value).length} 项` : "实际值";
        const summary = el("summary", ""); summary.append(el("strong", "", title), el("span", "", count));
        card.append(summary, el("p", "injection-source", source?.source || fallbackSource), el("pre", "reader-code", pretty(value)));
        if (source) card.append(el("p", "injection-source mono", Object.entries(source).filter(([field]) => field !== "source").map(([field, fact]) => `${field}: ${typeof fact === "object" ? pretty(fact) : fact}`).join(" · ")));
        if (source?.strategy_call_id) card.append(btn("查看本轮所用战略", () => callbacks.onCall(source.strategy_call_id, "result")));
        sources.append(card);
      }
      holder.append(sources, btn(call.data?.mode === "rule" ? "查看规则决策输入（未调用模型）" : "查看实际发送给模型的全文", () => callbacks.onCall(call.call_id, "input")));
    }
  }
  if (stage.id === "decision") {
    for (const event of stage.records.filter((r) => r.kind === "model_response")) {
      const content = event.data?.response?.choices?.[0]?.message?.content;
      if (content != null) holder.append(dataBlock("模型原始提议", content, false));
    }
  }
  if (stage.id === "output") {
    for (const event of stage.records.filter((r) => r.kind === "output")) {
      const message = event.data || {};
      if (message.type === "npc_speech") holder.append(dataBlock(`实际发言 → ${(message.targets || []).map(name).join("、")}`, message.text, false));
      else holder.append(dataBlock(message.type || "实际输出", message));
    }
  }
  for (const event of stage.records) { const changes = changeSummary(event); if (changes) holder.append(changes); }
  const evidence = el("div", "flow-records");
  for (const event of stage.records) {
    const row = el("details", "flow-record");
    // Stage facts are open by default; large requests remain one click away.
    row.open = (event.kind === "flow_step" && stage.id !== "context") || (stage.id === "input" && event.kind === "input");
    const summary = el("summary", "");
    summary.append(el("span", "mono", `#${event.seq}`), el("strong", "", event.summary || event.kind), el("span", `badge ${tone(event.status)}`, textStatus(event.status)));
    row.append(summary);
    const data = event.data || {};
    row.append(el("pre", "reader-code", pretty(data)));
    const links = el("div", "flow-record-links"); links.append(btn("完整事件", () => callbacks.onEvent(event.seq)));
    if (event.call_id) links.append(btn("关联调用", () => callbacks.onCall(event.call_id, stage.id === "decision" ? "output" : "result")));
    row.append(links); evidence.append(row);
  }
  holder.append(evidence);
}

export function renderFlow(holder, flow, callbacks) {
  const previousId = holder.dataset.flowId;
  const oldScroll = previousId === flow?.id ? holder.querySelector(".flow-scroll")?.scrollTop || 0 : 0;
  const expanded = previousId === flow?.id ? Array.from(holder.querySelectorAll("details[open]")).map((node) => node.querySelector("summary")?.textContent) : [];
  holder.replaceChildren(); holder.dataset.flowId = flow?.id || ""; holder.dataset.callId = "";
  if (!flow) {
    const empty = el("div", "empty-state");
    empty.append(el("span", "empty-symbol", "→"), el("h3", "", "从一条玩家消息追踪全过程"), el("p", "", "在交互测试中发送发言，或从消息旁的「查看处理流程」进入。"), btn("去交互测试", callbacks.onInteract, "button primary")); holder.append(empty); return;
  }
  const stages = stageInfo(flow);
  if (selectedFlow !== flow.id) { selectedFlow = flow.id; selectedStage = flow.legacy ? "input" : stages.find((stage) => stopped(stage.status))?.id || "context"; }
  const status = flowStatus(flow);
  const header = el("div", "reader-header");
  const title = el("div", "reader-title"); title.append(el("h3", "", flow.input?.data?.text || flow.title), el("span", `badge ${status.tone}`, status.text));
  const meta = el("div", "reader-metrics"); meta.append(el("span", "", name(flow.input?.data?.player_id)), el("span", "", `输入 #${flow.seq} · ${flow.events.length} 条关联记录`), el("span", "mono", flow.legacy ? "缺少 flow_id" : flow.id)); header.append(title, meta); holder.append(header);
  const content = el("div", "flow-scroll");
  if (flow.legacy || !flow.input) content.append(el("div", "trace-gap", flow.legacy ? "这条旧消息没有因果关联标识，只展示确定的输入。新消息将记录完整流程。" : "当前日志窗口未包含原始输入。可导出 JSONL 查看更早记录。"));
  const legend = el("div", "flow-legend"); legend.append(el("span", "", "按箭头查看；点击节点展开证据"), el("span", "teal", "● 已记录"), el("span", "blue", "● 进行中"), el("span", "error", "● 未通过"), el("span", "muted", "○ 尚无记录")); content.append(legend);
  const map = el("ol", "flow-map"); map.setAttribute("aria-label", "玩家消息处理流程");
  const detail = el("section", "flow-evidence"); detail.tabIndex = -1; detail.setAttribute("aria-label", "所选阶段的实际证据");
  for (const [i, stage] of stages.entries()) {
    const cell = el("li", `flow-cell flow-cell-${i + 1}`);
    const node = btn("", () => { selectedStage = stage.id; for (const item of map.querySelectorAll("button")) item.setAttribute("aria-pressed", String(item.dataset.stage === stage.id)); renderEvidence(detail, flow, stage, callbacks); detail.scrollIntoView({ block: "start" }); }, `flow-node ${stage.status === "unknown" ? "unknown" : tone(stage.status)}`);
    node.dataset.stage = stage.id; node.setAttribute("aria-pressed", String(selectedStage === stage.id));
    node.append(el("span", "flow-node-number mono", String(i + 1).padStart(2, "0")), el("strong", "", stage.title), el("span", "flow-node-status", stage.status === "unknown" ? stage.summary : textStatus(stage.status)), el("span", "flow-node-summary", stage.status === "unknown" ? "—" : stage.summary));
    cell.append(node); map.append(cell);
  }
  content.append(map);
  if (flow.dependencies.length) {
    const dependencies = el("div", "flow-dependencies"); dependencies.append(el("span", "", "关联的共享调用："));
    for (const event of flow.dependencies) dependencies.append(btn(`${event.lane === "episode" ? "经历" : "战略"} · ${event.call_id}`, () => callbacks.onCall(event.call_id, "result")));
    content.append(dependencies);
  }
  renderEvidence(detail, flow, stages.find((stage) => stage.id === selectedStage) || stages[0], callbacks);
  content.append(detail); holder.append(content);
  for (const node of holder.querySelectorAll("details")) if (expanded.includes(node.querySelector("summary")?.textContent)) node.open = true;
  content.scrollTop = oldScroll;
}
