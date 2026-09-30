"""Local Harness workbench: inspect Godot or drive an explicitly manual test world."""

import asyncio
import copy
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .memory import MemoryStore
from .model import ModelGateway
from .runtime import NpcRuntime
from .schemas import Incoming
from .tracing import TraceJournal


Scope = Literal["lab", "godot"]
router = APIRouter(prefix="/debug/api", tags=["debug"])
FINAL_ACTION_STATES = {"completed", "failed", "cancelled"}
ANCHORS = {
    "coffee_machine": [-1.8, 0, -2.8], "counter": [0, 0, -2.8],
    "recycle_spot": [-2.7, 0, -2.8], "pickup_spot": [0, 0, -0.6],
    "entrance": [0, 0, 4.3], "table_1": [-3.5, 0, 2.7],
    "table_2": [3.5, 0, 2.7], "demo_spot": [4.7, 0, -2.6],
    "menu_board": [-5.25, 0, 2.8],
}


class SessionCommand(BaseModel):
    op: Literal["start", "stop"]
    mode: Literal["model", "rule"] = "model"
    session_id: str | None = None


class ManualFeedback(BaseModel):
    request_id: str
    status: Literal["started", "completed", "failed", "paused", "resumed", "cancelled"]
    reason: str = Field(default="DEBUG_MANUAL_FAILURE", max_length=500)


class SpeechFeedback(BaseModel):
    speech_id: str
    status: Literal["displayed", "finished", "interrupted"]


@dataclass
class DebugSession:
    scope: str
    session_id: str
    trace: TraceJournal
    runtime: NpcRuntime | None = None
    worker: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    connected: bool = True
    latest: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    manual_players: dict = field(default_factory=dict)
    npc_position: list = field(default_factory=lambda: [0, 0, -2.8])
    manual_body: dict = field(default_factory=lambda: {"movement": "idle", "hands": "idle", "gaze": "idle", "speed": 0})
    snapshot_id: int = 0
    acknowledged_speech: set = field(default_factory=set)

    async def capture(self, message: dict) -> None:
        if message.get("type") == "runtime_update":
            self.latest = copy.deepcopy(message)
        elif message.get("type") == "memory_view":
            self.memory[message["player_id"]] = copy.deepcopy(message)


class DebugService:
    def __init__(self, settings: Settings, gateway: ModelGateway):
        self.settings = settings
        self.gateway = gateway
        self.lab: DebugSession | None = None
        self.godot: DebugSession | None = None
        self.lab_memory: MemoryStore | None = None
        self.session_lock = asyncio.Lock()

    def new_session(self, scope: str) -> DebugSession:
        session_id = scope + "_" + uuid4().hex[:10]
        trace = TraceJournal(scope, session_id, secrets=[self.settings.api_key],
                             log_dir=os.getenv("CAFE_TRACE_DIR") or None)
        trace.emit("session", "独立调试会话已建立" if scope == "lab" else "Godot 世界已连接",
                   {"world_mode": "debug_manual" if scope == "lab" else "godot"})
        return DebugSession(scope, session_id, trace)

    async def start_lab(self, mode: str) -> DebugSession:
        async with self.session_lock:
            if self.lab and self.lab.connected:
                raise HTTPException(409, "独立调试正在运行，请先停止再切换模式。")
            if self.lab and self.lab.runtime:
                if self.lab.cleanup:
                    await self.lab.cleanup
                await self.lab.runtime.close()
            if self.lab_memory is None:
                path = os.getenv("CAFE_DEBUG_DB_PATH") or str(Path(__file__).resolve().parents[1] / "data" / "harness_debug.sqlite3")
                self.lab_memory = MemoryStore(path)
            session = self.new_session("lab")
            session.runtime = NpcRuntime(replace(self.settings, mode=mode), self.lab_memory,
                                         self.gateway, session.capture, trace=session.trace,
                                         world_mode="debug_manual")
            self.lab = session
            await session.runtime.inbox.put(lab_registration())
            await session.runtime.inbox.put(lab_snapshot(session))
            session.worker = asyncio.create_task(session.runtime.run())
            session.worker.add_done_callback(lambda task: self.worker_done(session, task))
            return session

    @staticmethod
    def worker_done(session: DebugSession, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error:
            session.connected = False
            session.trace.emit("error", "调试运行时已停止", {"error": str(error)}, status="error")
            if session.runtime:
                session.cleanup = asyncio.create_task(session.runtime.close())

    async def stop_lab(self, session_id: str | None = None) -> None:
        async with self.session_lock:
            session = self.lab
            if not session:
                return
            check_session_id(session, session_id)
            if not session.connected and (not session.worker or session.worker.done()):
                # A failed worker can still own in-flight model tasks.
                if session.cleanup:
                    await session.cleanup
                elif session.runtime:
                    await session.runtime.close()
                return
            session.connected = False
            if session.worker:
                session.worker.cancel()
                await asyncio.gather(session.worker, return_exceptions=True)
            if session.runtime:
                await session.runtime.close()
            session.trace.emit("session", "独立调试已停止，调试记忆和日志已保留", {"operation": "stop"})

    async def close(self) -> None:
        await self.stop_lab()
        if self.lab_memory:
            self.lab_memory.close()

    def session(self, scope: str, *, active: bool = False, session_id: str | None = None) -> DebugSession | None:
        session = self.lab if scope == "lab" else self.godot
        if session:
            check_session_id(session, session_id)
        if active and (not session or not session.connected or not session.runtime):
            raise HTTPException(409, "请先启动独立调试。" if scope == "lab" else "Godot 世界未连接，请先运行咖啡馆场景。")
        return session


def check_session_id(session: DebugSession, expected: str | None) -> None:
    if expected and expected != session.session_id:
        raise HTTPException(409, "会话已更换，请刷新当前状态后重试。")


def service(request: Request) -> DebugService:
    return request.app.state.debug


def lab_registration() -> dict:
    return {
        "type": "scene_ready", "protocol": "cafe3d.v1", "room_id": "cafe_demo",
        "scene_id": "debug_manual", "npc_id": "npc_owner", "character_id": "owner_01",
        "entities": [
            {"entity_id": "machine_01", "kind": "coffee_station", "position": [-1.8, 1.38, -1.7], "available": True},
            {"entity_id": "counter_01", "kind": "counter", "position": [0, 0.5, -1.7], "available": True},
        ],
        "anchors": [{"anchor_id": key, "work_position": position, "available": True} for key, position in ANCHORS.items()],
        "sockets": [{"socket_id": "pickup_socket", "holder_id": "counter_01"},
                    {"socket_id": "recycle_socket", "holder_id": "recycle_01"}],
        "supported_commands": ["move_to_anchor", "look_at_entity", "gesture", "brew", "place_cup", "collect_cup", "retrieve_cup", "tidy"],
    }


def lab_snapshot(session: DebugSession) -> dict:
    session.snapshot_id += 1
    entities = [{"entity_id": "npc_owner", "kind": "npc", "position": session.npc_position,
                 "forward": [0, 0, 1], "body": copy.deepcopy(session.manual_body), "source": "debug_manual"},
                *lab_registration()["entities"], *session.manual_players.values()]
    return {"type": "world_snapshot", "room_id": "cafe_demo", "source": "debug_manual",
            "snapshot_id": session.snapshot_id, "observed_ms": int(time.monotonic() * 1000),
            "entities": entities,
            "perception": {"visible_ids": [row["entity_id"] for row in entities],
                           "audible_player_ids": list(session.manual_players)}}


@router.get("/state")
async def debug_state(request: Request, scope: Scope = "lab") -> dict:
    manager = service(request)
    session = manager.session(scope)
    runtime = session.runtime if session else None
    return {
        "scope": scope, "session_id": session.session_id if session else None,
        "connected": bool(session and session.connected),
        "mode": runtime.settings.mode if runtime else manager.settings.mode,
        "model_id": manager.settings.model_id, "model_configured": manager.gateway.configured,
        "runtime": session.trace.redact(session.latest) if session else {},
        "memory": session.trace.redact(session.memory) if session else {},
        "pending_actions": session.trace.redact([a for a in runtime.actions.values() if a["status"] not in FINAL_ACTION_STATES]) if runtime else [],
        "speech": session.trace.redact(runtime.current_speech) if runtime and runtime.current_speech else None,
        "log_file": str(session.trace.path) if session else None,
        "latest_seq": session.trace.latest_seq if session else 0,
        "trace_first_seq": session.trace.events(session.trace.latest_seq)["first_seq"] if session else 0,
    }


@router.get("/events")
async def debug_events(request: Request, scope: Scope = "lab", after: int = 0) -> dict:
    session = service(request).session(scope)
    return session.trace.events(after) if session else {"events": [], "latest_seq": 0, "first_seq": 0}


@router.get("/export")
async def debug_export(request: Request, scope: Scope = "lab"):
    session = service(request).session(scope)
    if not session:
        raise HTTPException(404, "此模式还没有会话日志。")
    return Response(session.trace.path.read_bytes(), media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="{session.trace.path.name}"'})


@router.post("/session")
async def debug_session(command: SessionCommand, request: Request) -> dict:
    manager = service(request)
    if command.op == "start":
        await manager.start_lab(command.mode)
    else:
        await manager.stop_lab(command.session_id)
    return await debug_state(request, "lab")


@router.post("/input")
async def debug_input(message: dict, request: Request, scope: Scope = "lab", session_id: str | None = None) -> dict:
    session = service(request).session(scope, active=True, session_id=session_id)
    try:
        return await _debug_input(message, session, scope)
    except HTTPException as exc:
        # Rejected utterances never reach the runtime inbox but still need an honest terminal chain.
        if message.get("type") == "utterance":
            flow_id = NpcRuntime.new_id("flow")
            record = session.trace.emit("input", "Web 发言输入", message, status="received", flow_id=flow_id)
            session.trace.emit("flow_step", "收到 Web 发言输入，尚未进入运行时",
                               {"stage": "input", "message": message, "record_seq": record["seq"]}, status="received", flow_id=flow_id)
            session.trace.emit("flow_step", "Web 输入校验拒绝，未进入待答队列",
                               {"stage": "validation", "reason": str(exc.detail), "http_status": exc.status_code,
                                "source": "debug_input", "player_id": message.get("player_id")}, status="rejected", flow_id=flow_id)
        raise


async def _debug_input(message: dict, session: DebugSession, scope: Scope) -> dict:
    try:
        Incoming.model_validate(message)
    except ValidationError:
        raise HTTPException(422, "缺少或无效的事件 type。") from None
    allowed = {"utterance", "interaction", "memory_request"}
    if scope == "lab":
        allowed.update({"player_entered", "player_left"})
    kind = message["type"]
    if kind not in allowed:
        raise HTTPException(422, "此入口仅接收玩家事件；动作与字幕回执请使用对应调试控件。")
    try:
        player_id = NpcRuntime.valid_player_id(message.get("player_id"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    runtime = session.runtime
    if kind not in {"player_entered", "memory_request"} and player_id not in runtime.players:
        raise HTTPException(409, "此玩家尚未入店。")
    if scope == "godot" and kind == "interaction" and message.get("kind") == "end_visit":
        raise HTTPException(422, "Godot 模式下请让角色实际离店。")
    # Do not accept internal queue messages or caller-supplied perception as Godot facts.
    message = {k: v for k, v in message.items() if not k.startswith("_")}
    message["debug_source"] = "web_console"
    if scope == "lab" and kind == "interaction" and message.get("kind") == "end_visit":
        message = {"type": "player_left", "player_id": player_id,
                   "debug_source": "web_console", "debug_original_kind": "end_visit"}
        kind = "player_left"
    if kind == "utterance":
        message.setdefault("target_id", "npc_owner")
        message.setdefault("channel", "local")
        message["perceived_by"] = ["npc_owner"] if player_id in runtime.world_view.get("audible_player_ids", []) else []
    if scope == "lab" and kind in {"player_entered", "player_left"}:
        if kind == "player_entered":
            name = message.get("display_name") or "顾客 " + player_id.removeprefix("p_")
            message["display_name"] = name
            session.manual_players[player_id] = {"entity_id": player_id, "player_id": player_id, "kind": "player",
                                                  "display_name": name, "inside_cafe": True,
                                                  "position": [0.6 * (ord(player_id[-1]) - ord("A")), 0, 0.5]}
        else:
            session.manual_players.pop(player_id, None)
        await runtime.inbox.put(lab_snapshot(session))
    await runtime.inbox.put(message)
    return {"accepted": True, "session_id": session.session_id}


@router.post("/feedback")
async def debug_feedback(message: ManualFeedback, request: Request, scope: Scope = "lab", session_id: str | None = None) -> dict:
    if scope != "lab":
        raise HTTPException(422, "真实 Godot 动作只能由游戏回报结果。")
    session = service(request).session(scope, active=True, session_id=session_id)
    runtime = session.runtime
    action = runtime.actions.get(message.request_id)
    if not action or action["status"] in FINAL_ACTION_STATES:
        raise HTTPException(409, "该动作不存在或已经结束。")
    result = {"source": "debug_manual", "position": list(session.npc_position),
              "progress": 1.0 if message.status == "completed" else 0.0}
    order = runtime.orders.items.get(action.get("parent_skill_id"))
    # Failed/cancelled actions report the last confirmed object state, never their requested destination.
    if order and order.get("cup_exists"):
        result.update({key: order[key] for key in ("cup_id", "order_id", "holder_id", "socket_id")})
    command = action["command"]
    if command == "look_at_entity":
        if message.status in {"started", "resumed", "completed"}:
            session.manual_body["gaze"] = action["args"].get("target_id", "idle")
    else:
        channel = "movement" if command == "move_to_anchor" else "hands"
        session.manual_body[channel] = ("idle" if message.status in FINAL_ACTION_STATES else
                                        "paused" if message.status == "paused" else
                                        "moving" if channel == "movement" else command)
    if message.status == "completed":
        if command == "move_to_anchor":
            session.npc_position = list(ANCHORS[action["args"]["anchor_id"]])
            result["anchor_id"] = action["args"]["anchor_id"]
            result["position"] = session.npc_position
        elif command in {"brew", "retrieve_cup"}:
            result.update(action["args"])
            result.update(holder_id="npc_owner", socket_id="npc_hand_socket", cup_ready=True)
        elif command == "place_cup":
            result.update(action["args"])
            result["holder_id"] = "counter_01" if result["socket_id"] == "pickup_socket" else "recycle_01"
        elif command == "collect_cup":
            result.update(action["args"])
            result.update(holder_id=result["player_id"], socket_id=result["player_id"] + "_carry_socket")
        else:
            result.update(action["args"])
    await runtime.inbox.put({"type": "action_feedback", "source": "debug_manual", "npc_id": "npc_owner",
                             "request_id": message.request_id, "status": message.status,
                             "reason": message.reason if message.status == "failed" else "MANUAL_TEST_RECEIPT", "result": result})
    await runtime.inbox.put(lab_snapshot(session))
    return {"accepted": True, "simulation": True, "session_id": session.session_id}


@router.post("/speech")
async def debug_speech(message: SpeechFeedback, request: Request, scope: Scope = "lab", session_id: str | None = None) -> dict:
    if scope != "lab":
        raise HTTPException(422, "真实 Godot 字幕只能由游戏回报展示结果。")
    session = service(request).session(scope, active=True, session_id=session_id)
    speech = session.runtime.current_speech
    marker = (message.speech_id, message.status)
    if marker in session.acknowledged_speech:
        return {"accepted": True, "duplicate": True}
    if not speech or speech["speech_id"] != message.speech_id:
        raise HTTPException(409, "该段发言已结束或已被新的发言替换。")
    session.acknowledged_speech.add(marker)
    # Enough to deduplicate recent browser tabs without retaining a transcript forever.
    if len(session.acknowledged_speech) > 300:
        session.acknowledged_speech = {entry for entry in session.acknowledged_speech if entry[0] == message.speech_id}
    feedback = {"type": "speech_feedback", "source": "debug_browser", "speech_id": message.speech_id, "status": message.status}
    for key in ("conversation_id", "segment_id"):
        if key in speech:
            feedback[key] = speech[key]
    await session.runtime.inbox.put(feedback)
    return {"accepted": True, "session_id": session.session_id}
