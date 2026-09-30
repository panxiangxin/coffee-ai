import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from .config import Settings
from .debug_console import DebugService, router as debug_router
from .memory import MemoryStore
from .model import ModelGateway
from .runtime import NpcRuntime

log = logging.getLogger("cafe.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.memory = MemoryStore(settings.db_path)
    app.state.gateway = ModelGateway(settings)
    app.state.runtime = None
    app.state.debug = DebugService(settings, app.state.gateway)
    yield
    if app.state.runtime:
        await app.state.runtime.close()
    await app.state.debug.close()
    await app.state.gateway.close()
    app.state.memory.close()


app = FastAPI(title="咖啡馆 NPC Harness", version="0.1.0", lifespan=lifespan)
app.include_router(debug_router)
app.mount("/debug", StaticFiles(directory=Path(__file__).parent / "static" / "debug", html=True, check_dir=False), name="debug")


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse("/debug/")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "world_mode": "godot", "mode": app.state.settings.mode,
            "model_configured": app.state.gateway.configured, "model_id": app.state.settings.model_id,
            "scene_connected": app.state.runtime is not None}


@app.websocket("/ws/cafe")
async def cafe_socket(socket: WebSocket):
    await socket.accept()
    if app.state.runtime is not None:
        await socket.send_json({"type": "error", "message": "当前已有一个 Godot 世界连接；请先关闭该连接。"})
        await socket.close(code=1008)
        return
    session = app.state.debug.new_session("godot")

    async def send_to_world(message: dict):
        await session.capture(message)
        await socket.send_json(message)

    runtime = NpcRuntime(app.state.settings, app.state.memory, app.state.gateway, send_to_world, trace=session.trace)
    session.runtime = runtime
    app.state.debug.godot = session
    app.state.runtime = runtime
    worker = asyncio.create_task(runtime.run())
    receiver = None
    try:
        while True:
            receiver = asyncio.create_task(socket.receive_json())
            done, _ = await asyncio.wait({worker, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                await worker
                break
            message = receiver.result()
            if not isinstance(message, dict):
                await runtime.inbox.put({"type": "invalid"})
            else:
                await runtime.inbox.put(message)
    except WebSocketDisconnect:
        log.info("Godot 世界正常断开")
    except Exception as exc:
        session.trace.emit("error", "Godot 连接或运行时异常，连接将关闭",
                           {"error": str(exc), "error_type": type(exc).__name__}, status="error")
        log.exception("Godot 连接或运行时异常")
    finally:
        if receiver and not receiver.done():
            receiver.cancel()
        worker.cancel()
        await asyncio.gather(worker, *([receiver] if receiver else []), return_exceptions=True)
        await runtime.close()
        if socket.client_state == WebSocketState.CONNECTED and socket.application_state == WebSocketState.CONNECTED:
            try:
                await socket.close(code=1011)
            except (OSError, RuntimeError):
                # The peer can disconnect while the close frame is being sent.
                pass
        session.connected = False
        session.trace.emit("session", "Godot 世界已断开", {"operation": "disconnect"})
        if app.state.runtime is runtime:
            app.state.runtime = None
