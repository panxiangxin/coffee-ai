"""单房间唯一状态更新入口；异步模型结果也重新进入 inbox。"""

import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import logging
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .config import Settings
from .memory import MemoryStore
from .model import ModelGateway
from .model_context import action_choices, event_context, strategy_context, validate_action_choice
from .orders import Orders
from .schemas import EventPlan, Incoming, RECIPES
from .topics import Topics
from .tracing import TraceJournal, bind_model_trace

log = logging.getLogger("cafe.runtime")


class NpcRuntime:
    def __init__(self, settings: Settings, memory: MemoryStore, gateway: ModelGateway,
                 send: Callable[[dict], Awaitable[None]], trace: TraceJournal | None = None,
                 world_mode: str = "godot"):
        self.settings, self.memory, self.gateway, self.send = settings, memory, gateway, send
        self.trace = trace
        self.world_mode = world_mode
        self._trace_pending: dict[str, dict] = {}
        # Sidecar links never enter model context, business state or wire messages.
        self._flow_context: ContextVar[tuple[str, str]] = ContextVar("cafe_runtime_flow", default=("", ""))
        self._trace_links: dict[str, tuple[str, str]] = {}
        self._trace_history: dict[int, tuple[dict, str]] = {}
        self._trace_queue_states: dict[str, tuple] = {}
        self._trace_controls: dict[tuple[str, str], tuple[str, str] | None] = {}
        self._strategy_call_id = ""
        if trace:
            trace.add_secret(settings.api_key)
            async def traced_send(message: dict) -> None:
                trace_message = copy.deepcopy(message) if message.get("type") not in {"runtime_update", "memory_view"} else None
                flow_id, call_id = self._output_origin(message)
                try:
                    await send(message)
                except Exception as exc:
                    self._emit_trace("error", "运行时输出未送达",
                                     {"message": trace_message if trace_message is not None else message,
                                      "error": str(exc), "error_type": type(exc).__name__}, status="error",
                                     flow_id=flow_id, call_id=call_id)
                    raise
                if trace_message is not None:
                    record = self._emit_trace("output", str(trace_message.get("type", "output")), trace_message,
                                              status="emitted", flow_id=flow_id, call_id=call_id)
                    self.flow_step("output", "输出已送达展示或执行端", "emitted",
                                   {"record_seq": record["seq"], "type": trace_message.get("type"),
                                    **self._refs(trace_message)}, flow_id=flow_id, call_id=call_id)
            self.send = traced_send
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        self.registered = False
        self.scene: dict = {}
        self.players: dict[str, dict] = {}
        self.world: dict = {}
        self.world_view: dict = {"entities": {}, "visible_ids": [], "audible_player_ids": []}
        self.agenda: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        self.revisions: dict[str, int] = {}
        self.memory_revisions: dict[str, int] = {}
        self.actions: dict[str, dict] = {}
        self.body_action: str | None = None
        self.prompts: dict[str, dict] = {}
        self.speech_queue: deque[dict] = deque()
        self.current_speech: dict | None = None
        self.orders, self.topics = Orders(self), Topics(self)
        self.strategy: dict = {"active_goal_ids": [], "primary_goal_id": None, "interaction_mode": "quiet", "next_step": "等待顾客"}
        self.strategy_dirty = True
        self.strategy_revision = 0
        self.strategy_goal_signature: tuple[tuple[str, str], ...] | None = None
        self.lanes: dict[str, str] = {}
        self.tasks: set[asyncio.Task] = set()
        self.calls = {"event": 0, "strategy": 0, "episode": 0}
        self.successes = {"event": 0, "strategy": 0, "episode": 0}
        self.last_error = ""
        self.last_update = 0.0
        self.last_expiry_check = 0.0
        self.activity: dict | None = None
        self.maintenance: dict | None = None
        self.episode_offsets: dict[str, int] = {}

    @staticmethod
    def new_id(prefix: str) -> str:
        return prefix + "_" + uuid4().hex[:10]

    def _emit_trace(self, kind: str, summary: str, data: Any = None, **meta) -> dict | None:
        if self.trace:
            flow_id, call_id = self._flow_context.get()
            meta.setdefault("flow_id", flow_id)
            meta.setdefault("call_id", call_id)
            return self.trace.emit(kind, summary, data, **meta)

    @contextmanager
    def trace_scope(self, flow_id: str = "", call_id: str = ""):
        if not self.trace:
            yield
            return
        token = self._flow_context.set((flow_id, call_id))
        try:
            yield
        finally:
            self._flow_context.reset(token)

    def trace_link(self, reference_id: str, origin: tuple[str, str] | None = None) -> None:
        if self.trace and reference_id:
            self._trace_links[reference_id] = self._flow_context.get() if origin is None else origin

    def trace_origin(self, reference_id: Any) -> tuple[str, str]:
        return self._trace_links.get(reference_id, ("", "")) if self.trace and isinstance(reference_id, str) else ("", "")

    @staticmethod
    def _refs(value: dict) -> dict:
        if not isinstance(value, dict):
            return {}
        return {key: value[key] for key in ("player_id", "item_id", "event_id", "request_id", "speech_id",
                                           "prompt_id", "parent_skill_id", "order_id", "conversation_id", "segment_id")
                if key in value}

    def flow_step(self, stage: str, summary: str, status: str, data: dict | None = None, **meta) -> None:
        if self.trace and (meta.get("flow_id", self._flow_context.get()[0]) or (data or {}).get("related_flow_ids")):
            self._emit_trace("flow_step", summary, {**(data or {}), "stage": stage}, status=status, **meta)

    def _trace_requeued(self, item: dict, summary: str, feedback: dict) -> None:
        if self.trace:
            flow_id, _ = self.trace_origin(item["item_id"])
            self.flow_step("queue", summary, "pending",
                           {**self._refs(item), "trigger": self._refs(feedback), "trigger_flow_id": self._flow_context.get()[0]},
                           flow_id=flow_id, call_id="")

    def _trace_control_request(self, reference_id: str, operation: str) -> None:
        if self.trace:
            key, origin = (reference_id, operation), self._flow_context.get()
            # The wire protocol has no per-control token. Repeated operations from different
            # inputs cannot be uniquely matched to a late receipt, so leave its flow unknown.
            if key in self._trace_controls and self._trace_controls[key] != origin:
                self._trace_controls[key] = None
            else:
                self._trace_controls[key] = origin

    def _output_origin(self, message: dict) -> tuple[str, str]:
        kind = message.get("type")
        if kind in {"speech_control", "action_control"}:
            return self._flow_context.get()
        for key in ("speech_id", "request_id", "prompt_id"):
            if key in message:
                return self.trace_origin(message[key])
        return self._flow_context.get()

    def _input_origin(self, message: dict) -> tuple[str, str]:
        if not self.trace:
            return "", ""
        kind = message.get("type")
        if kind == "utterance":
            flow_id = self.new_id("flow")
            return flow_id, ""
        if kind == "interaction":
            prompt_id = message.get("prompt_id")
            prompt = self.prompts.get(prompt_id) if isinstance(prompt_id, str) else None
            if (prompt and prompt["player_id"] == message.get("player_id")
                    and prompt.get("expires_at", float("inf")) > time.time()
                    and any(option["kind"] == message.get("kind") for option in prompt["options"])
                    and all(message.get(key) == prompt[key] for key in ("order_id", "conversation_id", "activity_id") if key in prompt)):
                return self.trace_origin(prompt_id)
            return "", ""
        if kind == "_model_result":
            return self.trace_origin(message.get("decision_id"))
        if kind in {"speech_feedback", "action_feedback"}:
            reference_id = message.get("speech_id" if kind == "speech_feedback" else "request_id")
            status = message.get("status")
            operation = {"paused": "pause", "resumed": "resume", "cancelled": "cancel", "interrupted": "stop"}.get(status) if isinstance(status, str) else None
            if kind == "speech_feedback" and status == "cancelled":
                operation = "stop"
            if isinstance(reference_id, str) and operation and (reference_id, operation) in self._trace_controls:
                return self._trace_controls[reference_id, operation] or ("", "")
            return self.trace_origin(reference_id)
        return "", ""

    def _trace_state(self, player_ids: list[str]) -> dict:
        return copy.deepcopy({"players": self.players, "orders": self.orders.items, "conversations": self.topics.items,
                              "prompts": self.prompts, "actions": self.actions, "activity": self.activity,
                              "maintenance": self.maintenance,
                              "memory": {player_id: self.memory.view(player_id) for player_id in player_ids}})

    def _trace_changes(self, before: dict | None, player_ids: list[str], message: dict) -> None:
        if before is None:
            return
        after = self._trace_state(player_ids)
        current_origin = self._flow_context.get()
        direct_refs = {value for value in self._refs(message).values() if isinstance(value, str)}
        result = message.get("result", {})
        if message.get("type") == "_model_result" and isinstance(result, dict):
            proposed_action = result.get("action") or {}
            if isinstance(proposed_action, dict):
                direct_refs.update(value for value in self._refs(proposed_action.get("args", {})).values() if isinstance(value, str))
        request_id = message.get("request_id")
        if isinstance(request_id, str) and request_id in before["actions"]:
            direct_refs.add(before["actions"][request_id]["parent_skill_id"])
        grouped: dict[tuple[str, str], dict] = {}
        for section, previous in before.items():
            current = after[section]
            if previous == current:
                continue
            if isinstance(previous, dict) and isinstance(current, dict):
                for key in previous.keys() | current.keys():
                    if previous.get(key) == current.get(key):
                        continue
                    origin = current_origin
                    value = current.get(key) or previous.get(key) or {}
                    if section in {"orders", "conversations", "prompts", "actions"} and key in self._trace_links:
                        linked = {v for v in self._refs(value).values() if isinstance(v, str)}
                        if key not in direct_refs and not direct_refs.intersection(linked):
                            origin = self.trace_origin(key)
                    grouped.setdefault(origin, {}).setdefault(section, {})[key] = {"before": previous.get(key), "after": current.get(key)}
            else:
                grouped.setdefault(current_origin, {})[section] = {"before": previous, "after": current}
        for (flow_id, call_id), changes in grouped.items():
            summary = ("模型提议应用后的实际业务变化" if message.get("type") == "_model_result" else
                       "回执确认后的实际业务变化" if message.get("type") in {"action_feedback", "speech_feedback"} else
                       "玩家操作后的实际业务变化")
            self.flow_step("apply", summary, "applied",
                           {"trigger": message.get("type"), "trigger_flow_id": current_origin[0],
                            **self._refs(message), "changes": changes}, flow_id=flow_id, call_id=call_id)

    def _context_sources(self, lane: str, context: dict, meta: dict) -> dict:
        if lane == "episode":
            return {"actual_history": {"source": "Harness 实际经历记录", "player_id": meta.get("player_id"),
                                        "related_flow_ids": meta.get("related_flow_ids", [])},
                    "topic_progress": {"source": "SQLite topic_progress", "player_id": meta.get("player_id")}}
        sources = {
            "situation": {"source": "Godot 感知快照与 Harness 当前状态" if self.world_mode == "godot" else "调试手动世界与 Harness 当前状态",
                          "snapshot_id": self.world.get("snapshot_id"), "observed_ms": self.world_view.get("observed_ms"),
                          "world_mode": self.world_mode},
            "strategy": {"source": "已接受战略", "strategy_call_id": self._strategy_call_id,
                         "shared": True},
        }
        if lane == "strategy":
            sources["candidate_goals"] = {"source": "Harness 当前有效目标", "related_goals": meta.get("related_goals", [])}
            return sources
        sources.update({
            "event": {"source": "已感知事件的待答队列", "item_id": meta.get("item_id"),
                      "event_id": context.get("event", {}).get("event_id")},
            "memory": {"source": "SQLite 当前玩家资料、偏好、话题、约定与经历", "player_id": meta.get("player_id")},
            "available_actions": {"source": "action_choices 当前对象、在场条件与通道状态"},
        })
        for key, description in (("orders", "Harness 订单实际状态"), ("conversations", "Harness 会话实际状态"),
                                 ("recent_history", "当前玩家最近实际交互"), ("available_topics", "已实现话题配置")):
            if key in context:
                sources[key] = {"source": description}
        return sources

    def _safe_error(self, error: Any) -> str:
        return str(self.trace.redact(str(error))) if self.trace else str(error)

    def _trace_result(self, message: dict, status: str, reason: str, **details) -> None:
        if not self.trace:
            return
        call_id = message.get("decision_id", "")
        pending = self._trace_pending.pop(call_id, {})
        data = {"mode": self.settings.mode, "status": status, "reason": reason,
                **{key: message.get(key, pending.get(key)) for key in ("item_id", "player_id", "revision", "topic_id", "related_flow_ids")
                   if key in message or key in pending}, **details}
        if "result" in message:
            data["result"] = message["result"]
        flow_id, _ = self.trace_origin(call_id)
        lane = message.get("lane", pending.get("lane", ""))
        record = self._emit_trace("model_result", reason, data, call_id=call_id,
                                  lane=lane, status=status, flow_id=flow_id)
        deferred = details.get("item_status") == "deferred"
        self.flow_step("apply" if status == "accepted" else "validation",
                       "决策选择暂缓，待办保留且未执行提议" if deferred else reason,
                       "deferred" if deferred else status,
                       {"record_seq": record["seq"], "reason": reason,
                        **{key: value for key, value in data.items() if key != "result"}},
                       call_id=call_id, lane=lane, flow_id=flow_id)

    def record(self, player_id: str, event_type: str, **values) -> None:
        entry = {"type": event_type, "time": time.time(), **values}
        self.history.setdefault(player_id, []).append(entry)
        if self.trace:
            self._trace_history[id(entry)] = (entry, self._flow_context.get()[0])

    def enqueue(self, player_id: str, event_type: str, payload: dict) -> None:
        item = {"item_id": self.new_id("todo"), "event_id": self.new_id("event"),
                "player_id": player_id, "type": event_type, "payload": payload,
                "topic_id": payload.get("topic_id", ""), "created_at": time.time(),
                "status": "pending", "revision": self.revisions.get(player_id, 0)}
        self.agenda.append(item)
        if self.trace:
            self.trace_link(item["item_id"])
            self.trace_link(item["event_id"])
            self.flow_step("queue", "事件已进入待答队列", "pending",
                           {**self._refs(item), "event_type": event_type, "payload": payload,
                            "revision": item["revision"], "blocking_call_id": self.lanes.get("event"),
                            "queue_position": sum(i["status"] == "pending" for i in self.agenda)})

    async def _consume(self, message: dict) -> None:
        flow_id, call_id = self._input_origin(message)
        with self.trace_scope(flow_id, call_id):
            kind = message.get("type")
            if kind not in {"_tick", "world_snapshot", "_model_result"}:
                record = self._emit_trace("input", str(kind or "input"), message, status="received")
                if flow_id:
                    self.flow_step("feedback" if kind in {"action_feedback", "speech_feedback"} else "input",
                                   "收到执行或展示回执" if kind in {"action_feedback", "speech_feedback"} else "收到玩家输入，等待校验和感知判定",
                                   "received", {"record_seq": record["seq"], "message": message, **self._refs(message)})
            player_ids = list(self.players) if self.trace and flow_id else []
            before = self._trace_state(player_ids) if self.trace and flow_id else None
            try:
                await self.handle(message)
            except (ValueError, KeyError, TypeError) as exc:
                self.last_error = self._safe_error(exc)
                log.warning("业务输入未应用: %s", self.last_error)
                if kind == "_model_result" and message.get("decision_id") in self._trace_pending:
                    self._trace_result(message, "rejected", self.last_error, error_type=type(exc).__name__)
                self._emit_trace("error", "业务输入或提议未应用",
                                 {"message": message, "error": self.last_error, "error_type": type(exc).__name__}, status="rejected")
                self.flow_step("validation", "输入或业务条件校验未通过", "rejected",
                               {"reason": self.last_error, "error_type": type(exc).__name__, **self._refs(message)})
            except Exception as exc:
                if kind == "_model_result" and message.get("decision_id") in self._trace_pending:
                    self._trace_result(message, "error", "模型结果处理异常", error=str(exc), error_type=type(exc).__name__)
                self._emit_trace("error", "处理事件时发生异常",
                                 {"message": message, "error": str(exc), "error_type": type(exc).__name__}, status="error")
                self.flow_step("apply", "事件处理异常中止", "error", {"reason": str(exc), "error_type": type(exc).__name__})
                raise
            finally:
                self._trace_changes(before, player_ids, message)

    async def run(self) -> None:
        while True:
            try:
                message = await asyncio.wait_for(self.inbox.get(), timeout=1.0)
            except TimeoutError:
                message = {"type": "_tick"}
            await self._consume(message)
            try:
                now = time.monotonic()
                if now - self.last_expiry_check >= 1.0:
                    await self.expire_prompts()
                    self.last_expiry_check = now
                await self.schedule()
                if message["type"] != "world_snapshot" or time.monotonic() - self.last_update >= 1:
                    await self.publish()
            except Exception as exc:
                self._emit_trace("error", "调度或发布运行状态时发生异常",
                                 {"message": message, "error": str(exc), "error_type": type(exc).__name__}, status="error")
                raise

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for call_id, pending in list(self._trace_pending.items()):
            self._trace_result({"decision_id": call_id, **pending}, "discarded", "会话关闭，待处理结果不再应用")
        # 已展示的事实保留；模型工作不在断线后继续修改新的房间运行时。
        for player_id in self.players:
            for conv in self.topics.items.values():
                if conv["player_id"] == player_id and conv["status"] != "closed":
                    self.topics.save(conv, "paused")

    async def handle(self, message: dict) -> None:
        kind = message["type"]
        if kind == "_model_result":
            await self.model_result(message)
            return
        if kind == "_tick":
            return
        Incoming.model_validate(message)
        if kind == "scene_ready":
            if self.registered:
                raise ValueError("当前连接已经注册场景，请断开后重新注册")
            if (message.get("protocol"), message.get("room_id"), message.get("npc_id"), message.get("character_id")) != (
                    "cafe3d.v1", "cafe_demo", "npc_owner", "owner_01"):
                raise ValueError("场景协议或稳定 ID 与 cafe3d.v1 不匹配")
            if not all(key in message for key in ("entities", "anchors", "sockets", "supported_commands")):
                raise ValueError("scene_ready 缺少实体、站位、插槽或原子动作列表")
            self.scene = copy.deepcopy(message)
            self.registered = True
            await self.send({"type": "scene_accepted", "protocol": "cafe3d.v1", "room_id": "cafe_demo", "npc_id": "npc_owner",
                             "world_mode": self.world_mode, "mode": self.settings.mode,
                             "model_configured": self.gateway.configured, "model_id": self.settings.model_id})
            return
        if not self.registered:
            raise ValueError("先发送 scene_ready 并等待 scene_accepted")
        if kind == "world_snapshot":
            self.observe(message)
        elif kind == "player_entered":
            await self.enter(message)
        elif kind == "player_left":
            await self.leave(message["player_id"])
        elif kind == "utterance":
            await self.utterance(message)
        elif kind == "interaction":
            await self.interact(message)
        elif kind == "action_feedback":
            await self.feedback(message)
        elif kind == "speech_feedback":
            await self.speech_feedback(message)
        elif kind == "memory_request":
            player_id = self.valid_player_id(message.get("player_id"))
            await self.send(self.memory.view(player_id))

    @staticmethod
    def valid_player_id(player_id: Any) -> str:
        if not isinstance(player_id, str) or player_id not in {"p_A", "p_B", "p_C", "p_D", "p_E", "p_F", "p_G", "p_H"}:
            raise ValueError("仅支持固定测试身份 p_A 至 p_H")
        return player_id

    def present(self, player_id: str) -> dict:
        self.valid_player_id(player_id)
        if player_id not in self.players:
            raise ValueError("该玩家当前不在咖啡馆")
        return self.players[player_id]

    def observe(self, message: dict) -> None:
        if message.get("room_id") != "cafe_demo":
            raise ValueError("快照房间不匹配")
        if message.get("snapshot_id", -1) <= self.world.get("snapshot_id", -2):
            return
        self.world = message
        perception = message.get("perception", {})
        visible = perception.get("visible_ids", [])
        self.world_view["visible_ids"] = visible
        self.world_view["audible_player_ids"] = perception.get("audible_player_ids", [])
        self.world_view["observed_ms"] = message.get("observed_ms")
        entities = self.world_view["entities"]
        for entity in message.get("entities", []):
            entity_id = entity.get("entity_id")
            if entity_id == "npc_owner" or entity_id in visible:
                previous = entities.get(entity_id)
                if previous and "available" in entity and previous.get("available") != entity["available"]:
                    affected = next((o["player_id"] for o in self.orders.items.values() if o["status"] in {"running", "queued"} and o["player_id"] in self.players), next(iter(self.players), None))
                    if affected:
                        self.enqueue(affected, "world_changed", {"entity_id": entity_id, "available": entity["available"], "evidence_snapshot_id": message.get("snapshot_id")})
                        self.strategy_dirty = True
                entities[entity_id] = {**entity, "last_observed_ms": message.get("observed_ms")}
        # Unseen entities retain the last observation, never the current hidden location.

    async def enter(self, message: dict) -> None:
        player_id = self.valid_player_id(message.get("player_id"))
        if player_id in self.players:
            return
        name = str(message.get("display_name") or message.get("name") or player_id)
        visit_id = self.new_id("visit")
        self.players[player_id] = {"player_id": player_id, "display_name": name, "visit_id": visit_id,
                                   "give_space": False, "entered_at": time.time()}
        self.revisions[player_id] = self.revisions.get(player_id, 0) + 1
        self.history[player_id] = []
        self.episode_offsets[player_id] = 0
        memory = self.memory.visit(player_id, name)
        self.record(player_id, "entered", visit_id=visit_id)
        self.enqueue(player_id, "player_entered", {"display_name": name, "visit_count": memory["profile"]["visit_count"]})
        for promise in memory["commitments"]:
            if promise["status"] == "pending" and promise["created_visit_id"] != visit_id and promise["last_offered_visit_id"] != visit_id:
                self.memory.offered(promise["id"], visit_id)
                await self.topics.invite(player_id, promise["topic_id"])
        self.strategy_dirty = True
        await self.send(self.memory.view(player_id))

    async def leave(self, player_id: str) -> None:
        self.present(player_id)
        self.invalidate(player_id, abandon=True)
        await self.topics.leave(player_id)
        await self.stop_speech(player_id)
        for order in self.orders.items.values():
            if order["player_id"] == player_id and order["status"] in {"running", "queued", "ready"}:
                await self.orders.interact(player_id, "pause_order", {"order_id": order["order_id"]})
        self.record(player_id, "left")
        await self.summarize(player_id, "visit")
        self.players.pop(player_id)
        for prompt_id, prompt in list(self.prompts.items()):
            if prompt["player_id"] == player_id:
                self.dismiss_prompt(prompt_id)
        if self.activity and player_id in self.activity["participants"]:
            self.activity["participants"].remove(player_id)
            if not self.activity["participants"]:
                self.activity = None
        self.strategy_dirty = True

    def invalidate(self, player_id: str, conversation_id: str = "", abandon: bool = False) -> None:
        self.revisions[player_id] = self.revisions.get(player_id, 0) + 1
        for item in self.agenda:
            if item["player_id"] == player_id and item["status"] in {"pending", "generating", "deferred", "failed"}:
                old_status = item["status"]
                related_topic = conversation_id and item["payload"].get("conversation_id") == conversation_id
                stale_utterance = item["status"] == "generating" and item["type"] == "utterance"
                if abandon or related_topic or stale_utterance:
                    item["status"] = "superseded"
                    if item["type"] == "topic_segment":
                        conv = self.topics.items.get(item["payload"].get("conversation_id"))
                        if conv:
                            conv["pending_segment"] = False
                else:
                    # Keep unrelated questions and topic work; replan with the new state.
                    item["revision"] = self.revisions[player_id]
                    if item["status"] == "generating":
                        item["status"] = "pending"
                if self.trace:
                    flow_id, call_id = self.trace_origin(item["item_id"])
                    self.flow_step("queue", "玩家控制使待办失效" if item["status"] == "superseded" else "保留无关待办并更新控制版本",
                                   item["status"], {**self._refs(item), "previous_status": old_status,
                                                    "revision": item["revision"], "trigger_flow_id": self._flow_context.get()[0],
                                                    "conversation_id": conversation_id, "abandon": abandon},
                                   flow_id=flow_id, call_id=call_id)

    async def utterance(self, message: dict) -> None:
        player_id = message.get("player_id", "")
        self.present(player_id)
        text = str(message.get("text", "")).strip()
        if not text or len(text) > 2000:
            raise ValueError("发言须为 1 至 2000 字符")
        if message.get("target_id") not in {None, "", "npc_owner", "room"}:
            self.flow_step("perception", "发言对象不是老板娘或房间，不进入待答队列", "ignored",
                           {"player_id": player_id, "target_id": message.get("target_id"), "target_matches": False,
                            "audible": None, "audibility_checked": False, "reason": "target_is_other"})
            self.record(player_id, "player_speech_to_other", text=text, target_id=message.get("target_id"))
            return
        if "perceived_by" in message:
            if not isinstance(message["perceived_by"], list):
                raise ValueError("perceived_by 必须是场景计算的观察者 ID 列表")
            # Event-time geometry is newer than the periodic snapshot; do not AND stale data.
            audible = "npc_owner" in message["perceived_by"]
        else:
            audible = message.get("channel", "local") == "room" or player_id in self.world_view.get("audible_player_ids", [])
        self.flow_step("perception", "已听见且发言对象匹配，允许进入待答队列" if audible else "当前听不到这条发言，不进入待答队列",
                       "accepted" if audible else "rejected",
                       {"player_id": player_id, "target_id": message.get("target_id"), "target_matches": True,
                        "audible": audible, "channel": message.get("channel", "local"),
                        "source": "event.perceived_by" if "perceived_by" in message else "room_channel_or_last_snapshot",
                        "perceived_by": message.get("perceived_by"),
                        "snapshot_id": self.world.get("snapshot_id"), "observed_ms": self.world_view.get("observed_ms"),
                        "snapshot_audible_player_ids": self.world_view.get("audible_player_ids", []),
                        "world_mode": self.world_mode, "reason": "heard" if audible else "out_of_hearing_range"})
        if not audible:
            self.last_error = f"未回应 {player_id}：当前发言不在可听范围"
            self._emit_trace("error", "发言未进入角色听觉范围",
                             {"message": message, "error": self.last_error}, status="rejected")
            return
        self.record(player_id, "player_speech", text=text, target_id=message.get("target_id"), topic_id=message.get("topic_id", ""))
        self.enqueue(player_id, "utterance", {key: message.get(key) for key in ("text", "target_id", "topic_id", "reply_to", "channel")})

    async def interact(self, message: dict) -> None:
        player_id = message.get("player_id", "")
        self.present(player_id)
        kind = message.get("kind", "")
        value = message.get("value")
        immediate = {"pause_order", "cancel_order", "pause_topic", "next_visit", "end_topic", "decline_invitation", "stop_speech", "give_space", "forget_topic", "correct_preference"}
        if kind in immediate:
            conv_id = message.get("conversation_id", "")
            if kind in {"pause_topic", "next_visit", "end_topic", "decline_invitation", "stop_speech", "give_space", "forget_topic"} and not conv_id:
                conv = self.topics.find(player_id)
                conv_id = conv["conversation_id"] if conv else ""
            self.invalidate(player_id, conversation_id=conv_id)
        if kind == "order_drink":
            recipe = value.get("recipe_id", "latte") if isinstance(value, dict) else value or "latte"
            await self.orders.propose(player_id, recipe)
        elif kind in {"confirm_order", "collect_cup", "pause_order", "resume_order", "cancel_order", "retry_cleanup"}:
            await self.orders.interact(player_id, kind, message)
        elif kind == "invite_topic":
            self.players[player_id]["give_space"] = False
            await self.topics.invite(player_id, value if isinstance(value, str) else "pourover_intro")
        elif kind in {"accept_invitation", "decline_invitation", "pause_topic", "resume_topic", "next_topic", "next_visit", "end_topic"}:
            await self.topics.interact(player_id, kind, message)
        elif kind == "stop_speech":
            # Stop also invalidates a segment still being generated, without touching orders.
            await self.stop_speech(player_id, message.get("conversation_id", ""))
        elif kind == "give_space":
            self.players[player_id]["give_space"] = True
            conv = self.topics.find(player_id)
            if conv:
                await self.topics.interact(player_id, "end_topic", {"conversation_id": conv["conversation_id"]})
        elif kind == "correct_preference":
            if not isinstance(value, dict) or not value.get("key") or not isinstance(value.get("value"), str):
                raise ValueError("偏好修正需要 value:{key,value}")
            self.memory.preference(player_id, value["key"], value["value"])
            self.record(player_id, "preference_corrected", key=value["key"], value=value["value"])
            await self.send(self.memory.view(player_id))
        elif kind in {"forget_topic", "disable_reminder"}:
            if not isinstance(value, (str, dict)):
                raise ValueError("话题修改需要字符串 topic_id 或 value:{topic_id}")
            topic_id = value if isinstance(value, str) else value.get("topic_id", "")
            if not isinstance(topic_id, str) or not topic_id.strip():
                raise ValueError("请指定非空字符串 topic_id")
            if kind == "forget_topic":
                self.memory_revisions[player_id] = self.memory_revisions.get(player_id, 0) + 1
                self.memory.forget_topic(player_id, topic_id)
                for conv in self.topics.items.values():
                    if conv["player_id"] == player_id and conv["topic_id"] == topic_id:
                        conv["status"] = "closed"
                        conv["forgotten"] = True
                        await self.stop_speech(player_id, conv["conversation_id"])
                self.history[player_id] = [h for h in self.history.get(player_id, []) if h.get("topic_id") != topic_id]
                self.episode_offsets[player_id] = len(self.history[player_id])
            else:
                self.memory.close_promises(player_id, topic_id, "disabled")
            await self.send(self.memory.view(player_id))
        elif kind == "ask_menu":
            self.enqueue(player_id, "menu_question", {"recipes": RECIPES})
        elif kind in {"start_activity", "choose_activity", "leave_activity"}:
            await self.activity_interaction(player_id, kind, value)
        elif kind == "decline_activity":
            self.players[player_id]["activity_declined"] = True
            for prompt_id, prompt in list(self.prompts.items()):
                if prompt["player_id"] == player_id and prompt["kind"] == "activity_invitation":
                    self.dismiss_prompt(prompt_id)
        elif kind == "end_visit":
            await self.leave(player_id)
        elif kind == "retry_pending":
            for item in self.agenda:
                if item["player_id"] == player_id and item["status"] == "failed":
                    item["status"] = "pending"
            self.last_error = ""
        elif kind == "tidy_counter":
            await self.start_maintenance()
        else:
            raise ValueError(f"不支持的 interaction kind: {kind}")

    async def prompt(self, player_id: str, kind: str, text: str, options: list[dict], expires_in: float = 0, **refs) -> dict:
        # Each business object has only one active prompt; stale buttons cannot accumulate.
        for prompt_id, existing in list(self.prompts.items()):
            if existing["player_id"] == player_id and any(existing.get(k) == v for k, v in refs.items()):
                self.dismiss_prompt(prompt_id)
        prompt = {"type": "interaction_prompt", "prompt_id": self.new_id("prompt"), "player_id": player_id,
                  "kind": kind, "text": text, "options": options, **refs}
        if expires_in:
            prompt["expires_at"] = time.time() + expires_in
        self.prompts[prompt["prompt_id"]] = prompt
        self.trace_link(prompt["prompt_id"])
        await self.send(prompt)
        return prompt

    def dismiss_prompt(self, prompt_id: str) -> None:
        self.prompts.pop(prompt_id, None)

    def dismiss_related_prompts(self, key: str, value: str) -> None:
        for prompt_id, prompt in list(self.prompts.items()):
            if prompt.get(key) == value:
                self.dismiss_prompt(prompt_id)

    async def expire_prompts(self) -> None:
        for prompt_id, prompt in list(self.prompts.items()):
            if prompt.get("expires_at", float("inf")) > time.time():
                continue
            self.dismiss_prompt(prompt_id)
            if self.trace:
                flow_id, call_id = self.trace_origin(prompt_id)
                self.flow_step("apply", "交互提示已超时失效", "expired",
                               {"prompt_id": prompt_id, "expires_at": prompt["expires_at"], **self._refs(prompt)},
                               flow_id=flow_id, call_id=call_id)
            conv = self.topics.items.get(prompt.get("conversation_id"))
            if conv and conv["status"] == "waiting_reply":
                conv["status"] = "closed"
                self.strategy_dirty = True

    def goals(self) -> list[dict]:
        result = []
        for order in self.orders.items.values():
            if order["status"] not in {"confirming", "completed", "cancelled", "failed"}:
                result.append({"goal_id": "serve_" + order["order_id"], "kind": "serve_guest", "player_id": order["player_id"], "status": "suspended" if order["status"] == "paused" else "active", "stage": order["stage"]})
        for conv in self.topics.items.values():
            if conv["status"] != "closed":
                result.append({"goal_id": "topic_" + conv["conversation_id"], "kind": "continue_shared_topic", "player_id": conv["player_id"], "status": conv["status"], "stage": conv["stage"]})
        for player_id, player in self.players.items():
            if player["give_space"]:
                result.append({"goal_id": "space_" + player_id, "kind": "give_space", "player_id": player_id, "status": "active"})
        for item in self.agenda:
            if item["status"] in {"pending", "generating", "presenting", "deferred", "failed"}:
                result.append({"goal_id": "answer_" + item["item_id"], "kind": "answer_guest", "player_id": item["player_id"], "status": item["status"]})
        if not result and self.players:
            result.append({"goal_id": "maintain_cafe", "kind": "maintain_cafe", "status": "pending"})
        return result

    def situation(self) -> dict:
        npc = self.world_view["entities"].get("npc_owner", {})
        return {"present_players": list(self.players), "pending_questions": [i["item_id"] for i in self.agenda if i["status"] in {"pending", "generating", "deferred", "failed"}],
                "conversation_groups": [[c["player_id"], "npc_owner"] for c in self.topics.items.values() if c["status"] == "engaged"],
                "npc_activity": npc.get("body", {}), "available_channels": ["gaze"] + ([] if self.current_speech else ["speech"]) + ([] if self.body_action else ["movement", "hands"]),
                "give_space_players": [p for p in self.players if self.players[p]["give_space"]],
                "evidence_ids": [i["event_id"] for i in self.agenda[-8:]], "updated_at": time.time()}

    def context(self, item: dict) -> dict:
        return event_context(self, item)

    async def schedule(self) -> None:
        # A schedule can advance another player's work; never inherit the triggering input.
        with self.trace_scope():
            await self._schedule()

    async def _schedule(self) -> None:
        if not self.registered:
            return
        await self.orders.pump()
        await self.pump_speech()
        candidate_goals = self.goals()
        # Generation/presentation are internal stages of the same unfinished answer.
        signature = tuple(sorted((goal["goal_id"], "unfinished" if goal["kind"] == "answer_guest" and goal["status"] in {"pending", "generating", "presenting"} else goal["status"])
                                 for goal in candidate_goals))
        if signature != self.strategy_goal_signature:
            self.strategy_goal_signature = signature
            self.strategy_dirty = True
        if "event" not in self.lanes:
            item = next((i for i in self.agenda if i["status"] == "pending" and i["player_id"] in self.players), None)
            if item:
                item["status"] = "generating"
                context = self.context(item)
                if self.trace:
                    flow_id, _ = self.trace_origin(item["item_id"])
                    self.flow_step("queue", "调度器选中此待办，开始构建本轮决策", "generating",
                                   {**self._refs(item), "revision": item["revision"], "policy": "first_pending_present_player"},
                                   flow_id=flow_id, call_id="")
                self.launch_model("event", context, item_id=item["item_id"], player_id=item["player_id"], revision=item["revision"])
        if self.trace:
            for position, item in enumerate(i for i in self.agenda if i["status"] == "pending" and i["player_id"] in self.players):
                state = (self.lanes.get("event"), position)
                if self._trace_queue_states.get(item["item_id"]) != state:
                    self._trace_queue_states[item["item_id"]] = state
                    flow_id, _ = self.trace_origin(item["item_id"])
                    self.flow_step("queue", "等待事件决策通道空闲", "waiting",
                                   {**self._refs(item), "blocking_call_id": state[0], "queue_position": position + 1},
                                   flow_id=flow_id, call_id="")
        if self.strategy_dirty and "strategy" not in self.lanes:
            self.strategy_dirty = False
            self.strategy_revision += 1
            self.launch_model("strategy", strategy_context(self), revision=self.strategy_revision)

    def launch_model(self, lane: str, context: dict, **meta) -> None:
        decision_id = self.new_id("decision")
        flow_id = self.trace_origin(meta.get("item_id"))[0] if lane == "event" else ""
        if lane != "episode":
            self.lanes[lane] = decision_id
        if self.settings.mode == "model" and self.gateway.configured:
            self.calls[lane] += 1
        if self.trace:
            self.trace_link(decision_id, (flow_id, decision_id))
            if lane == "event":
                self.trace_link(meta["item_id"], (flow_id, decision_id))
            if lane == "strategy":
                dependencies = []
                for goal in context["candidate_goals"]:
                    prefix, _, reference_id = goal["goal_id"].partition("_")
                    if prefix in {"answer", "serve", "topic"}:
                        origin = self.trace_origin(reference_id)[0]
                        dependencies.append({"goal_id": goal["goal_id"], "reference_id": reference_id, "flow_id": origin})
                meta["related_goals"] = dependencies
                meta["related_flow_ids"] = sorted({row["flow_id"] for row in dependencies if row["flow_id"]})
            self._trace_pending[decision_id] = {"lane": lane, **meta}
            record = self._emit_trace("model_call", "规则联调决策（不发送模型请求）" if self.settings.mode == "rule" else "开始模型任务",
                             {"mode": self.settings.mode, "configured": self.gateway.configured,
                              "world_mode": self.world_mode, "context": context, **meta},
                             call_id=decision_id, lane=lane, flow_id=flow_id,
                             status="rule" if self.settings.mode == "rule" else "started" if self.gateway.configured else "unconfigured")
            self.flow_step("context", "已注入本次实际使用的情境" if lane == "event" else "构建共享情境，按明确依赖关联而不归给单条发言",
                           "built", {"record_seq": record["seq"], "context_ref": {"call_id": decision_id, "field": "data.context"},
                                     "context_keys": list(context), "sources": self._context_sources(lane, context, meta),
                                     "mode": self.settings.mode, **meta},
                           flow_id=flow_id, call_id=decision_id, lane=lane)
        async def work():
            with self.trace_scope(flow_id, decision_id), bind_model_trace(self.trace, decision_id, lane, flow_id):
                try:
                    result = self.rule_result(lane, context) if self.settings.mode == "rule" else await self.gateway.generate(lane, context)
                    self.flow_step("decision", "规则联调产生提议，等待运行时校验" if self.settings.mode == "rule" else "模型产生结构化提议，等待运行时校验",
                                   "proposed", {"proposal": result, "mode": self.settings.mode,
                                                "item_id": meta.get("item_id"), "related_flow_ids": meta.get("related_flow_ids", [])}, lane=lane)
                    await self.inbox.put({"type": "_model_result", "lane": lane, "decision_id": decision_id, "result": result, "context": context, **meta})
                except asyncio.CancelledError:
                    self._trace_result({"lane": lane, "decision_id": decision_id, **meta}, "discarded", "模型任务已取消", cancelled=True)
                    raise
                except Exception as exc:
                    log.warning("模型 %s 请求失败: %s", lane, type(exc).__name__)
                    # Pydantic/provider errors can contain input: keep user-facing diagnostics compact.
                    error = str(exc) if isinstance(exc, ValueError) and len(str(exc)) < 180 else f"模型 {lane} 请求或结构校验失败（{type(exc).__name__}）"
                    self._emit_trace("error", "模型任务执行失败",
                                     {"error": str(exc), "error_type": type(exc).__name__, "mode": self.settings.mode},
                                     call_id=decision_id, lane=lane, status="error")
                    self.flow_step("decision", "未取得可应用的决策提议", "error",
                                   {"reason": self._safe_error(error), "error_type": type(exc).__name__}, lane=lane)
                    await self.inbox.put({"type": "_model_result", "lane": lane, "decision_id": decision_id, "error": self._safe_error(error), "context": context, **meta})

        def finished(task: asyncio.Task) -> None:
            self.tasks.discard(task)
            if task.cancelled() and decision_id in self._trace_pending:
                self._trace_result({"lane": lane, "decision_id": decision_id, **meta}, "discarded", "模型任务启动前已取消", cancelled=True)
        task = asyncio.create_task(work())
        self.tasks.add(task)
        task.add_done_callback(finished)

    async def model_result(self, message: dict) -> None:
        lane = message["lane"]
        if lane != "episode":
            if self.lanes.get(lane) != message["decision_id"]:
                self._trace_result(message, "discarded", "结果不对应当前模型任务",
                                   current_decision_id=self.lanes.get(lane))
                return
            self.lanes.pop(lane, None)
        item = next((i for i in self.agenda if i["item_id"] == message.get("item_id")), None)
        if lane == "event" and (not item or item["status"] != "generating" or item["player_id"] not in self.players or message["revision"] != self.revisions.get(item["player_id"])):
            reason = "待答事项不存在" if not item else "待答事项已不处于生成状态" if item["status"] != "generating" else "目标玩家已离场" if item["player_id"] not in self.players else "玩家控制版本已变更"
            self._trace_result(message, "discarded", reason,
                               item_status=item.get("status") if item else None,
                               current_revision=self.revisions.get(item["player_id"]) if item else None)
            return
        if "error" in message:
            self.last_error = self._safe_error(message["error"])
            if item:
                item["status"] = "failed"
            self._trace_result(message, "error", self.last_error)
            return
        result = message["result"]
        if lane == "event":
            try:
                accepted = await self.apply_plan(item, EventPlan.model_validate(result).model_dump())
            except (ValueError, KeyError, TypeError) as exc:
                item["status"] = "failed"
                self.last_error = "提议未应用：" + self._safe_error(exc)
                self._trace_result(message, "rejected", self.last_error, error_type=type(exc).__name__)
                self._emit_trace("error", "事件提议被拒绝",
                                 {"message": message, "error": self.last_error},
                                 call_id=message["decision_id"], lane=lane, status="rejected")
            except Exception as exc:
                self._trace_result(message, "error", "应用事件提议时发生异常", error=str(exc), error_type=type(exc).__name__)
                raise
            else:
                if accepted and self.settings.mode == "model":
                    self.successes[lane] += 1
                self._trace_result(message, "accepted" if accepted else "discarded",
                                   "事件提议已接受" if accepted else "话题段落已经失效", item_status=item["status"])
        elif lane == "strategy":
            if message["revision"] != self.strategy_revision or self.strategy_dirty:
                self._trace_result(message, "discarded", "战略结果已过期，需要根据当前候选重新判断",
                                   current_revision=self.strategy_revision, strategy_dirty=self.strategy_dirty)
                return
            valid = {g["goal_id"] for g in self.goals()}
            chosen = [g for g in result["active_goal_ids"] if g in valid]
            # Accepted orders remain goals even if the proposal omits them.
            service = [g["goal_id"] for g in self.goals() if g["kind"] == "serve_guest" and g["status"] == "active"]
            chosen = list(dict.fromkeys(service + chosen))[:3]
            self.strategy = {**result, "active_goal_ids": chosen, "primary_goal_id": result["primary_goal_id"] if result["primary_goal_id"] in chosen else next(iter(chosen), None)}
            self._strategy_call_id = message["decision_id"]
            if self.settings.mode == "model":
                self.successes[lane] += 1
            self._trace_result(message, "accepted", "战略已更新", strategy=self.strategy)
        elif lane == "episode":
            player_id = message["player_id"]
            if message["memory_revision"] != self.memory_revisions.get(player_id, 0):
                self._trace_result(message, "discarded", "玩家记忆已被修改或遗忘，旧摘要不再写入",
                                   memory_revision=message["memory_revision"], current_memory_revision=self.memory_revisions.get(player_id, 0))
                return
            if result["participant_ids"] != [player_id]:
                self.last_error = "经历摘要参与者不匹配，未写入"
                self._trace_result(message, "rejected", self.last_error)
                self._emit_trace("error", self.last_error, {"message": message, "error": self.last_error},
                                 call_id=message["decision_id"], lane=lane, status="rejected")
                return
            try:
                self.memory.episode(player_id, message["topic_id"], result["summary"], message["visit_id"],
                                    self.settings.mode, result.get("resume_hint", ""))
            except Exception as exc:
                self._trace_result(message, "error", "经历写入失败", error=str(exc), error_type=type(exc).__name__)
                raise
            if self.settings.mode == "model":
                self.successes[lane] += 1
            self._trace_result(message, "accepted", "经历摘要已写入 SQLite", visit_id=message["visit_id"])
            await self.send(self.memory.view(player_id))

    async def apply_plan(self, item: dict, plan: dict) -> bool:
        player_id = item["player_id"]
        speech = plan.get("speech")
        extra = {}
        if item["type"] == "topic_segment":
            conv = self.topics.items.get(item["payload"]["conversation_id"])
            if not conv or conv["status"] != "engaged" or conv["segment_id"] != item["payload"]["segment_id"]:
                item["status"] = "superseded"
                return False
            extra = {"conversation_id": conv["conversation_id"], "segment_id": conv["segment_id"], "topic_id": conv["topic_id"]}
        if speech:
            targets = speech["target_player_ids"]
            if player_id not in targets or any(p not in self.players for p in targets):
                raise ValueError("回复对象不在场或未包含本轮提问者")
        # Re-check the current choices before any memory/action side effects.
        # Channels, perception and object states may change while the model runs.
        current_choices = action_choices(self, item)
        validate_action_choice(plan.get("action"), current_choices)
        # Check the entire evidence list before any memory or action side effects.
        for update in plan["memory_updates"]:
            if update["evidence_event_id"] != item["event_id"] or item["type"] != "utterance":
                raise ValueError("记忆修正缺少当前玩家发言证据")
        self.flow_step("validation", "当前对象、动作候选和记忆证据校验通过", "validated",
                       {**self._refs(item), "checks": ["current_item_revision", "player_present", "speech_targets", "action_choices", "memory_evidence"],
                        "available_actions": current_choices, "reaction": plan["reaction"]})
        if plan["reaction"] == "DEFER":
            item["status"] = "deferred"
            return True
        for update in plan["memory_updates"]:
            if update["kind"] == "preference":
                self.memory.preference(player_id, update["key"], update["value"])
            elif update["kind"] == "session_preference":
                self.players[player_id]["give_space"] = True
            else:
                await self.interact({"player_id": player_id, "kind": update["kind"], "value": update["key"]})
        if plan["memory_updates"]:
            await self.send(self.memory.view(player_id))
        action = plan.get("action")
        if action:
            kind, args = action["type"], action["args"]
            if kind == "order_drink":
                await self.orders.propose(player_id, args.get("recipe_id", "latte"))
            elif kind in {"pause_order", "resume_order", "cancel_order"}:
                await self.interact({**args, "player_id": player_id, "kind": kind})
            elif kind == "invite_topic":
                await self.topics.invite(player_id, args.get("topic_id", "pourover_intro"))
            elif kind == "continue_topic":
                await self.topics.continue_requested(player_id, args.get("conversation_id", ""))
            elif kind in {"pause_topic", "next_visit", "end_topic"}:
                await self.interact({**args, "player_id": player_id, "kind": kind})
            elif kind == "stop_speech":
                await self.stop_speech(player_id, args.get("conversation_id", ""))
            elif kind == "give_space":
                await self.interact({"player_id": player_id, "kind": "give_space"})
            elif kind == "look_at":
                target = args.get("target_id", player_id)
                if target not in self.world_view["entities"]:
                    raise ValueError("注视目标尚未感知")
                await self.action("look_at_entity", {"target_id": target, "hold_seconds": 2.0}, item["item_id"], "gaze")
            elif kind == "move_to":
                await self.action("move_to_anchor", {"anchor_id": args.get("anchor_id")}, item["item_id"], "local")
            elif kind == "tidy_counter":
                await self.start_maintenance()
            elif kind == "host_activity":
                if not self.players[player_id].get("give_space") and not self.players[player_id].get("activity_declined"):
                    await self.prompt(player_id, "activity_invitation", "想参加一轮咖啡口味猜想吗？",
                                      [{"label": "参加", "kind": "start_activity", "value": True},
                                       {"label": "暂不参加", "kind": "decline_activity", "value": False}])
        if speech:
            output = {"type": "npc_speech", "speech_id": self.new_id("speech"), "npc_id": "npc_owner",
                      "targets": targets, "text": speech["text"], "emotion": speech["emotion"], "interruptible": True,
                      "gaze_target": player_id, "item_id": item["item_id"], "mode": self.settings.mode, **extra}
            self.speech_queue.append(output)
            self.trace_link(output["speech_id"], self.trace_origin(item["item_id"]))
            item["status"] = "presenting"
            self.flow_step("output", "回复已排队，尚未送达或展示", "queued",
                           {**self._refs(output), "targets": targets, "text": speech["text"],
                            "queue_position": len(self.speech_queue), "blocking_speech_id": (self.current_speech or {}).get("speech_id")})
        else:
            item["status"] = "completed"
            if item["type"] == "topic_segment":
                conv = self.topics.items.get(item["payload"]["conversation_id"])
                if conv:
                    conv["pending_segment"] = False
            if not action and not plan["memory_updates"]:
                self.flow_step("apply", "提议不包含发言、动作或记忆修改，本待办已结束", "no_effect",
                               {**self._refs(item), "reaction": plan["reaction"], "item_status": item["status"]})
        if plan["reaction"] == "REPLAN":
            self.strategy_dirty = True
        return True

    async def pump_speech(self) -> None:
        if self.current_speech:
            return
        while self.speech_queue:
            speech = self.speech_queue.popleft()
            if not all(p in self.players for p in speech["targets"]):
                if self.trace:
                    flow_id, call_id = self.trace_origin(speech["speech_id"])
                    self.flow_step("output", "发言目标已经离场，排队回复未发送", "discarded",
                                   {**self._refs(speech), "targets": speech["targets"], "present_players": list(self.players)},
                                   flow_id=flow_id, call_id=call_id)
                continue
            self.current_speech = speech
            await self.send(speech)
            return

    async def stop_speech(self, player_id: str, conversation_id: str = "") -> None:
        def matches(speech):
            return player_id in speech["targets"] and (not conversation_id or speech.get("conversation_id") == conversation_id)
        retained = deque()
        for speech in self.speech_queue:
            if matches(speech):
                self.speech_item_status(speech, "superseded")
            else:
                retained.append(speech)
        self.speech_queue = retained
        if self.current_speech and matches(self.current_speech):
            self.current_speech["stop_requested"] = True
            self._trace_control_request(self.current_speech["speech_id"], "stop")
            await self.send({"type": "speech_control", "speech_id": self.current_speech["speech_id"], "operation": "stop"})

    def speech_item_status(self, speech: dict, status: str) -> None:
        for item in self.agenda:
            if item["item_id"] == speech.get("item_id"):
                item["status"] = status
                if self.trace:
                    flow_id, call_id = self.trace_origin(speech["speech_id"])
                    self.flow_step("queue", "发言对应待办状态已更新", status,
                                   {**self._refs(speech), "item_status": status, "trigger_flow_id": self._flow_context.get()[0]},
                                   flow_id=flow_id, call_id=call_id)

    async def speech_feedback(self, message: dict) -> None:
        speech = self.current_speech
        if not speech or speech["speech_id"] != message.get("speech_id"):
            self.flow_step("feedback", "字幕回执不对应当前发言，未应用", "discarded",
                           {"reason": "speech_not_current", **self._refs(message),
                            "current_speech_id": (speech or {}).get("speech_id")})
            return
        status = message.get("status")
        self.flow_step("feedback", "收到当前发言的实际展示状态", status or "unknown",
                       {**self._refs(speech), "feedback_status": status,
                        "source": message.get("source", "godot_feedback"), "targets": speech["targets"],
                        "already_recorded": bool(speech.get("recorded")), "stop_requested": bool(speech.get("stop_requested"))})
        if status == "displayed" and not speech.get("recorded"):
            speech["recorded"] = True
            conv = self.topics.items.get(speech.get("conversation_id"))
            if not conv or not conv.get("forgotten"):
                for player_id in speech["targets"]:
                    self.record(player_id, "npc_speech_displayed", text=speech["text"], topic_id=speech.get("topic_id", ""))
                await self.topics.speech_displayed(speech)
        if status not in {"finished", "interrupted", "cancelled", "failed"}:
            return
        self.current_speech = None
        if status == "finished" and not speech.get("stop_requested"):
            self.speech_item_status(speech, "completed")
        else:
            self.speech_item_status(speech, "superseded" if speech.get("stop_requested") else "failed")
        conv = self.topics.items.get(speech.get("conversation_id"))
        if conv and conv["segment_id"] == speech.get("segment_id"):
            # An ended but never displayed segment can be requested again without advancing.
            conv["pending_segment"] = False
        for item in self.agenda:
            if item["status"] == "deferred":
                item["status"] = "pending"
                self._trace_requeued(item, "发言结束后重新检查暂缓待办", message)

    async def action(self, command: str, args: dict, parent_skill_id: str, owner: str) -> str:
        if command not in self.scene.get("supported_commands", []):
            raise ValueError(f"场景不支持 {command}")
        if owner != "gaze" and self.body_action:
            raise ValueError("身体通道仍有动作，请等待真实回执")
        if command == "move_to_anchor":
            anchor = next((a for a in self.scene["anchors"] if a["anchor_id"] == args.get("anchor_id")), None)
            if not anchor or not anchor.get("available", True):
                raise ValueError("目标站位不存在或不可用")
        if command == "brew":
            station = next((e for e in self.world.get("entities", []) if e.get("entity_id") == args["station_id"]), None)
            station = station or next((e for e in self.scene["entities"] if e["entity_id"] == args["station_id"]), None)
            if not station or not station.get("available", True):
                raise ValueError("咖啡设备不可用")
        if command == "place_cup" and not any(s["socket_id"] == args["socket_id"] for s in self.scene["sockets"]):
            raise ValueError("放杯插槽不存在")
        if command == "collect_cup":
            self.present(args["player_id"])
        request_id = self.new_id("act")
        action = {"type": "action_command", "request_id": request_id, "parent_skill_id": parent_skill_id,
                  "npc_id": "npc_owner", "command": command, "args": args, "owner": owner, "status": "sent"}
        self.actions[request_id] = action
        self.trace_link(request_id, self.trace_origin(parent_skill_id))
        if owner != "gaze":
            self.body_action = request_id
        await self.send({k: v for k, v in action.items() if k not in {"owner", "status"}})
        return request_id

    async def control(self, request_id: str, operation: str) -> None:
        action = self.actions.get(request_id)
        if not action or action["status"] in {"completed", "failed", "cancelled"}:
            return
        action["control_requested"] = operation
        self._trace_control_request(request_id, operation)
        await self.send({"type": "action_control", "request_id": request_id, "operation": operation})

    async def feedback(self, message: dict) -> None:
        action = self.actions.get(message.get("request_id"))
        if not action or action["status"] in {"completed", "failed", "cancelled"}:
            self.flow_step("feedback", "动作回执没有对应的未结束动作，未应用", "discarded",
                           {**self._refs(message), "reason": "action_unknown_or_final", "action_status": (action or {}).get("status")})
            return
        if message.get("npc_id", "npc_owner") != "npc_owner":
            raise ValueError("动作回执 NPC 不匹配")
        status = message.get("status")
        if status not in {"started", "paused", "resumed", "completed", "failed", "cancelled"}:
            raise ValueError("动作回执状态无效")
        if status == "completed":
            self.validate_result(action, message.get("result", {}))
        action["status"] = status
        action["result"] = message.get("result", {})
        self.flow_step("feedback", "动作回执已通过实际对象与状态校验", status,
                       {**self._refs(action), "command": action["command"], "feedback_status": status,
                        "result": message.get("result", {}), "reason": message.get("reason", ""),
                        "source": message.get("source", "godot_feedback")})
        if status in {"completed", "failed", "cancelled"} and self.body_action == action["request_id"]:
            self.body_action = None
        if action["owner"] == "order":
            await self.orders.feedback(action, message)
        elif action["owner"] == "maintenance" and status in {"completed", "failed", "cancelled"}:
            if status == "completed" and action["command"] == "move_to_anchor":
                await self.action("tidy", {"station_id": "counter_01", "duration": 3.0}, action["parent_skill_id"], "maintenance")
            else:
                self.maintenance = None
        if status in {"completed", "failed", "cancelled"}:
            for item in self.agenda:
                if item["status"] == "deferred":
                    item["status"] = "pending"
                    self._trace_requeued(item, "动作结束后重新检查暂缓待办", message)
        if status == "failed":
            order = self.orders.items.get(action["parent_skill_id"])
            if order and order["player_id"] in self.players:
                self.enqueue(order["player_id"], "action_failed", {"order_id": order["order_id"], "command": action["command"], "reason": message.get("reason", "UNKNOWN")})
            self.strategy_dirty = True

    @staticmethod
    def validate_result(action: dict, result: dict) -> None:
        args, command = action["args"], action["command"]
        required = {"move_to_anchor": ["anchor_id"], "brew": ["cup_id", "order_id"],
                    "place_cup": ["cup_id", "socket_id"], "collect_cup": ["cup_id"], "retrieve_cup": ["cup_id"]}.get(command, [])
        if any(result.get(key) != args.get(key) for key in required):
            raise ValueError(f"{command} completed 缺少匹配的实际对象结果")
        if command == "move_to_anchor" and not isinstance(result.get("position"), list):
            raise ValueError("移动完成回执缺少实际 position")
        if command == "collect_cup" and result.get("holder_id", result.get("player_id")) != args["player_id"]:
            raise ValueError("取杯回执没有确认当前玩家持有")
        if command in {"brew", "retrieve_cup"} and result.get("holder_id") != "npc_owner":
            raise ValueError("制饮/取回杯子回执没有确认 NPC 持有")

    async def start_maintenance(self) -> None:
        if self.body_action or self.orders.active_id:
            raise ValueError("身体正在服务，暂不能整理柜台")
        skill_id = self.new_id("tidy")
        self.maintenance = {"skill_id": skill_id, "type": "tidy_counter"}
        self.trace_link(skill_id)
        await self.action("move_to_anchor", {"anchor_id": "counter"}, skill_id, "maintenance")

    async def activity_interaction(self, player_id: str, kind: str, value: Any) -> None:
        if kind == "start_activity":
            self.players[player_id]["activity_declined"] = False
            for prompt_id, prompt in list(self.prompts.items()):
                if prompt["player_id"] == player_id and prompt["kind"] == "activity_invitation":
                    self.dismiss_prompt(prompt_id)
            if not self.activity:
                self.activity = {"activity_id": self.new_id("activity"), "round": 1, "participants": [], "answers": {},
                                 "question": "明亮果酸、花香明显，更像浅烘还是深烘？", "answer": "light", "status": "collecting"}
            if player_id not in self.activity["participants"]:
                self.activity["participants"].append(player_id)
            await self.prompt(player_id, "activity", self.activity["question"],
                              [{"label": "浅烘", "kind": "choose_activity", "value": "light"},
                               {"label": "深烘", "kind": "choose_activity", "value": "dark"}], activity_id=self.activity["activity_id"])
        elif kind == "choose_activity":
            if not self.activity or player_id not in self.activity["participants"] or value not in {"light", "dark"}:
                raise ValueError("活动或选择已经失效")
            self.activity["answers"][player_id] = value
            for prompt_id, prompt in list(self.prompts.items()):
                if prompt.get("activity_id") == self.activity["activity_id"] and prompt["player_id"] == player_id:
                    self.dismiss_prompt(prompt_id)
            self.record(player_id, "activity_answer", topic_id="coffee_guess", choice=value, correct=value == self.activity["answer"])
            self.enqueue(player_id, "activity_result", {"question": self.activity["question"], "choice": value,
                                                       "answer": self.activity["answer"], "correct": value == self.activity["answer"]})
            self.activity["participants"].remove(player_id)
            if not self.activity["participants"]:
                self.activity["status"] = "completed"
                self.activity = None
            await self.summarize(player_id, "coffee_guess")
        elif self.activity and player_id in self.activity["participants"]:
            self.activity["participants"].remove(player_id)
            if not self.activity["participants"]:
                self.activity = None

    async def summarize(self, player_id: str, topic_id: str) -> None:
        history = self.history.get(player_id, [])
        offset = self.episode_offsets.get(player_id, 0)
        actual = copy.deepcopy(history[offset:])
        if not actual:
            return
        self.episode_offsets[player_id] = len(history)
        visit_id = self.players.get(player_id, {}).get("visit_id", "")
        context = {"player_id": player_id, "topic_id": topic_id, "actual_history": actual[-30:], "topic_progress": self.memory.view(player_id)["topics"]}
        related_flow_ids = sorted({self._trace_history.get(id(row), ({}, ""))[1] for row in history[offset:][-30:]} - {""}) if self.trace else []
        self.launch_model("episode", context, player_id=player_id, visit_id=visit_id, topic_id=topic_id,
                          memory_revision=self.memory_revisions.get(player_id, 0), related_flow_ids=related_flow_ids)

    def rule_result(self, lane: str, context: dict) -> dict:
        """显式 rule 联调模式，不对自由文本做伪语义推断。"""
        if lane == "strategy":
            ids = [g["goal_id"] for g in context["candidate_goals"]][:3]
            return {"active_goal_ids": ids, "primary_goal_id": next(iter(ids), None), "interaction_mode": "rule_debug", "next_step": "规则联调：按现有已接受目标推进", "review_on": ["goal_changed"]}
        if lane == "episode":
            kinds = list(dict.fromkeys(e["type"] for e in context["actual_history"]))
            return {"summary": "规则联调事实记录：" + "、".join(kinds), "topic_id": context["topic_id"], "participant_ids": [context["player_id"]], "resume_hint": "以已保存 topic_progress 为准"}
        event = context["event"]
        player_id = event["player_id"]
        texts = {"player_entered": "欢迎来到咖啡馆。当前是规则联调模式，请使用按钮体验点单和手冲讲解。",
                 "order_ready": "你的饮品已实际放在取餐柜台，走近后可以领取。",
                 "menu_question": "菜单有美式、拿铁、手冲和低因咖啡。请用点单按钮选择并确认。",
                 "activity_result": "答案是浅烘；这一轮的选择已经记录。"}
        if event["type"] == "topic_segment":
            text = {"beans": "先说咖啡豆与研磨：研磨粗细会影响水流通过速度，也会改变萃取程度。",
                    "water_temperature": "水温会影响萃取速度。调整时可以先固定粉水比和研磨，只比较水温变化。",
                    "pouring": "接着练习注水：先均匀润湿粉层，再稳定地分段注水，观察水位与流速。"}[event["stage"]]
        else:
            text = texts.get(event["type"], "当前是规则联调模式，未调用模型理解这段文字；请使用明确的交互按钮。")
        return {"reaction": "REACT", "goal_id": None, "speech": {"target_player_ids": [player_id], "text": text, "emotion": "warm"}, "action": None, "memory_updates": []}

    async def publish(self) -> None:
        model = {"configured": self.gateway.configured, "model_id": self.settings.model_id,
                 "status": "rule_debug" if self.settings.mode == "rule" else "ready" if self.gateway.configured else "unconfigured",
                 "calls": self.calls, "successes": self.successes, "in_flight": list(self.lanes), "error": self.last_error}
        await self.send({"type": "runtime_update", "npc_id": "npc_owner", "mode": self.settings.mode, "world_mode": self.world_mode,
                         "model": model, "present_players": list(self.players), "world_view": self.world_view,
                         "situation": self.situation(), "strategy": self.strategy, "goals": self.goals(),
                         "agenda": self.agenda[-30:], "conversations": list(self.topics.items.values()), "orders": list(self.orders.items.values()),
                         "active_skills": [a for a in self.actions.values() if a["status"] not in {"completed", "failed", "cancelled"}],
                         "body": {"action_request_id": self.body_action, "speech_id": (self.current_speech or {}).get("speech_id"),
                                  **self.world_view["entities"].get("npc_owner", {}).get("body", {})},
                         "memory_refs": {p: {"visit_id": data["visit_id"], "character_id": "owner_01"} for p, data in self.players.items()},
                         "prompts": list(self.prompts.values()), "activity": self.activity, "last_error": self.last_error})
        self.last_update = time.monotonic()
