"""当前会话的本地观测日志；不参与业务调度，也不重放事件。"""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
from threading import RLock
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import uuid4

log = logging.getLogger("cafe.trace")
REDACTED = "[REDACTED]"
_SENSITIVE_NAMES = {
    "apikey", "authorization", "proxyauthorization", "accesstoken", "refreshtoken",
    "idtoken", "clientsecret", "password", "passwd", "credential", "credentials",
    "cookie", "setcookie", "privatekey", "secret", "auth", "token",
}
_SECRET_ASSIGNMENT = re.compile(
    r'''(?ix)(["']?(?:[a-z0-9_]*api[_-]?key|authorization|proxy[_-]?authorization|'''
    r'''access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|'''
    r'''credential|credentials|secret|cookie|set[_-]?cookie|token)["']?\s*[:=]\s*)'''
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)'''
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]+")


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_NAMES or any(
        normalized.endswith(suffix)
        for suffix in ("apikey", "authorization", "accesstoken", "refreshtoken", "clientsecret", "password", "privatekey")
    )


class TraceJournal:
    def __init__(self, scope: str, session_id: str, secrets: list[str] | None = None,
                 log_dir: str | Path | None = None):
        values = {value for value in (secrets or []) if isinstance(value, str) and value}
        self._secrets = tuple(sorted(
            values | {json.dumps(value, ensure_ascii=True)[1:-1] for value in values}
            | {quote(value, safe="") for value in values} | {quote_plus(value) for value in values},
            key=len, reverse=True,
        ))
        self.scope = self._text(scope)
        self.session_id = self._text(session_id)
        directory = Path(log_dir) if log_dir is not None else Path(__file__).resolve().parents[1] / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        safe_scope = re.sub(r"[^a-zA-Z0-9_-]", "_", self.scope)[:48] or "session"
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", self.session_id)[:80] or "session"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.path = directory / f"{stamp}_{safe_scope}_{safe_id}_{uuid4().hex[:8]}.jsonl"
        # Never append a new session to an old file, even if its user-visible ID is reused.
        with self.path.open("x", encoding="utf-8"):
            pass
        self.latest_seq = 0
        self.write_error = ""
        self._records: deque[dict[str, Any]] = deque(maxlen=500)
        self._lock = RLock()

    def add_secret(self, secret: str) -> None:
        """在开始记录前补充当前进程的凭证，不把凭证写入任何事件。"""
        if not secret:
            return
        with self._lock:
            variants = {secret, json.dumps(secret, ensure_ascii=True)[1:-1], quote(secret, safe=""), quote_plus(secret)}
            self._secrets = tuple(sorted(set(self._secrets) | variants, key=len, reverse=True))

    def _text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        value = _BEARER.sub(lambda match: match.group(1) + " " + REDACTED, value)
        return _SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + '"' + REDACTED + '"', value)

    def redact_url(self, value: str) -> str:
        """保留可定位的地址，移除 URL 凭证及敏感查询参数。"""
        try:
            parts = urlsplit(value)
            netloc = parts.netloc.rsplit("@", 1)[-1]
            query = [(key, REDACTED if _sensitive_key(key) or key.lower() == "key" else val)
                     for key, val in parse_qsl(parts.query, keep_blank_values=True)]
            return self._text(urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment)))
        except ValueError:
            return self._text(value)

    def redact(self, value: Any) -> Any:
        """返回可 JSON 序列化的脱敏副本，不改动业务对象。"""
        if isinstance(value, dict):
            result = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                safe_key = self._text(key)
                if _sensitive_key(key):
                    result[safe_key] = REDACTED
                elif key.lower().endswith("url") and isinstance(item, str):
                    result[safe_key] = self.redact_url(item)
                else:
                    result[safe_key] = self.redact(item)
            return result
        if isinstance(value, (list, tuple, set)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            # Chat Completions messages embed JSON context inside a string.
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    nested = json.loads(value)
                except (ValueError, TypeError):
                    pass
                else:
                    return self._text(json.dumps(self.redact(nested), ensure_ascii=False))
            return self._text(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        return self._text(str(value))

    def _record(self, kind: str, summary: str, data: Any, call_id: str, lane: str, status: str, flow_id: str = "") -> dict:
        self.latest_seq += 1
        return {
            "seq": self.latest_seq, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": self._text(kind), "summary": self._text(summary), "data": self.redact(data),
            "call_id": self._text(call_id), "lane": self._text(lane), "status": self._text(status),
            "flow_id": self._text(flow_id),
            "scope": self.scope, "session_id": self.session_id,
        }

    def emit(self, kind: str, summary: str = "", data: Any = None, *,
             call_id: str = "", lane: str = "", status: str = "", flow_id: str = "") -> dict:
        with self._lock:
            record = self._record(kind, summary, data, call_id, lane, status, flow_id)
            self._records.append(record)
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                self.write_error = ""
            except OSError as exc:
                # A failed observation sink must not turn a valid game action into a failure.
                error = f"本地日志写入失败（{type(exc).__name__}），当前仅保留内存事件"
                if error != self.write_error:
                    self.write_error = error
                    log.error("%s", error)
                    self._records.append(self._record("error", error, {"error": error}, "", "", "error"))
            return copy.deepcopy(record)

    def events(self, after: int = 0) -> dict:
        with self._lock:
            return {
                "events": copy.deepcopy([record for record in self._records if record["seq"] > after]),
                "latest_seq": self.latest_seq,
                "first_seq": self._records[0]["seq"] if self._records else 0,
            }


@dataclass(frozen=True)
class ModelTraceContext:
    journal: TraceJournal
    call_id: str
    lane: str
    flow_id: str = ""


_model_trace: ContextVar[ModelTraceContext | None] = ContextVar("cafe_model_trace", default=None)


@contextmanager
def bind_model_trace(journal: TraceJournal | None, call_id: str, lane: str, flow_id: str = "") -> Iterator[None]:
    token = _model_trace.set(ModelTraceContext(journal, call_id, lane, flow_id) if journal else None)
    try:
        yield
    finally:
        _model_trace.reset(token)


def emit_model_trace(kind: str, summary: str = "", data: Any = None, *, status: str = "") -> None:
    context = _model_trace.get()
    if context:
        record = context.journal.emit(kind, summary, data, call_id=context.call_id, lane=context.lane,
                                      status=status, flow_id=context.flow_id)
        if kind == "model_validation" and context.flow_id:
            context.journal.emit("flow_step", summary,
                                 {"stage": "validation", "source": "model_gateway", "record_seq": record["seq"],
                                  **{key: data[key] for key in ("error", "error_type", "schema") if isinstance(data, dict) and key in data}},
                                 call_id=context.call_id, lane=context.lane, status=status, flow_id=context.flow_id)
