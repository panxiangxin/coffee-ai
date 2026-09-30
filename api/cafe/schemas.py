from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Speech(StrictModel):
    target_player_ids: list[str] = Field(min_length=1, max_length=8)
    text: str = Field(min_length=1, max_length=1200)
    emotion: str = "warm"


class Action(StrictModel):
    type: Literal[
        "order_drink", "invite_topic", "continue_topic", "pause_topic", "next_visit",
        "end_topic", "pause_order", "resume_order", "cancel_order", "stop_speech",
        "give_space", "look_at", "move_to", "tidy_counter", "host_activity", "wait",
    ]
    args: dict[str, Any] = Field(default_factory=dict)


class MemoryUpdate(StrictModel):
    kind: Literal["preference", "session_preference", "disable_reminder", "forget_topic"]
    key: str
    value: str = ""
    evidence_event_id: str


class EventPlan(StrictModel):
    reaction: Literal["CONTINUE", "REACT", "DEFER", "PREEMPT", "REPLAN"]
    goal_id: str | None = None
    speech: Speech | None = None
    action: Action | None = None
    memory_updates: list[MemoryUpdate] = Field(default_factory=list, max_length=5)


class StrategyProposal(StrictModel):
    active_goal_ids: list[str] = Field(default_factory=list, max_length=3)
    primary_goal_id: str | None = None
    interaction_mode: str = "quiet"
    next_step: str = ""
    review_on: list[str] = Field(default_factory=list)


class EpisodeSummary(StrictModel):
    summary: str = Field(min_length=1, max_length=1600)
    topic_id: str = "visit"
    participant_ids: list[str] = Field(min_length=1, max_length=8)
    resume_hint: str = ""


class Incoming(BaseModel):
    """消息共有字段；类型特有的业务条件在唯一事件入口检查。"""

    model_config = ConfigDict(extra="allow")
    type: Literal[
        "scene_ready", "world_snapshot", "player_entered", "player_left", "utterance",
        "interaction", "action_feedback", "speech_feedback", "memory_request",
    ]


RECIPES = {
    "americano": "美式",
    "latte": "拿铁",
    "pourover": "手冲",
    "decaf": "低因咖啡",
}
TOPIC_STAGES = ["beans", "water_temperature", "pouring", "finished"]
TOPIC_LABELS = {"beans": "咖啡豆和研磨", "water_temperature": "水温", "pouring": "注水", "finished": "已讲完"}
