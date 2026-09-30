"""把 Harness 的事实整理成模型情境；保留完整运行状态供执行与调试。"""

import copy
import json
import math
from typing import TYPE_CHECKING

from .schemas import RECIPES, TOPIC_LABELS

if TYPE_CHECKING:
    from .runtime import NpcRuntime


TOPICS = {"pourover_intro": "手冲咖啡入门"}
ENDED_ORDERS = {"completed", "cancelled", "failed"}


def action_choices(runtime: "NpcRuntime", item: dict) -> dict[str, dict[str, list[str]]]:
    """每个动作只列当前玩家可用的参数值；null 始终表示不新增动作。"""
    player_id = item["player_id"]
    player = runtime.players.get(player_id)
    if not player or item["type"] in {"topic_segment", "activity_result"}:
        return {}
    choices = {}
    orders = [order for order in runtime.orders.items.values() if order["player_id"] == player_id]
    # Creating a confirmed-later order does not occupy the body channel.
    if not any(order["status"] not in ENDED_ORDERS for order in orders):
        choices["order_drink"] = {"recipe_id": list(RECIPES)}
    for kind, statuses in (
        ("pause_order", {"running", "queued", "ready"}),
        ("resume_order", {"paused"}),
        ("cancel_order", {"confirming", "queued", "running", "ready", "paused", "failed"}),
    ):
        ids = [order["order_id"] for order in orders if order["status"] in statuses]
        if ids:
            choices[kind] = {"order_id": ids}

    conversations = [conv for conv in runtime.topics.items.values()
                     if conv["player_id"] == player_id and not conv.get("forgotten")]
    active = [conv for conv in conversations if conv["status"] != "closed"]
    if not active and not player.get("give_space"):
        choices["invite_topic"] = {"topic_id": list(TOPICS)}
    continuable = [conv["conversation_id"] for conv in active
                   if conv["stage"] != "finished" and not conv["pending_segment"]]
    # Explicitly resuming is allowed even after give_space/next_visit closed a topic.
    # No conversation_id asks the existing Topics implementation to create one.
    if not active and item["type"] == "utterance":
        if conversations and conversations[-1]["stage"] != "finished":
            continuable = [conversations[-1]["conversation_id"]]
        elif player.get("give_space") or any(topic["stage"] != "finished" for topic in runtime.memory.view(player_id)["topics"]):
            continuable = [""]
    if continuable:
        choices["continue_topic"] = {"conversation_id": continuable}
    for kind, statuses in (
        ("pause_topic", {"waiting_reply", "engaged"}),
        ("next_visit", {"waiting_reply", "engaged", "paused"}),
        ("end_topic", {"waiting_reply", "engaged", "paused"}),
    ):
        ids = [conv["conversation_id"] for conv in active
               if conv["status"] in statuses and conv["stage"] != "finished"]
        if ids:
            choices[kind] = {"conversation_id": ids}
    speeches = [*runtime.speech_queue, *([runtime.current_speech] if runtime.current_speech else [])]
    if any(player_id in speech["targets"] for speech in speeches):
        choices["stop_speech"] = {}
    if not player.get("give_space"):
        choices["give_space"] = {}

    commands = runtime.scene.get("supported_commands", [])
    visible = [entity_id for entity_id in runtime.world_view.get("visible_ids", [])
               if entity_id != "npc_owner" and entity_id in runtime.world_view["entities"]]
    if "look_at_entity" in commands and visible:
        choices["look_at"] = {"target_id": visible}
    anchors = [anchor["anchor_id"] for anchor in runtime.scene.get("anchors", []) if anchor.get("available", True)]
    if not runtime.body_action and "move_to_anchor" in commands and anchors:
        choices["move_to"] = {"anchor_id": anchors}
        if "tidy" in commands and "counter" in anchors and not runtime.orders.active_id:
            choices["tidy_counter"] = {}
    invited = any(prompt["player_id"] == player_id and prompt["kind"] == "activity_invitation"
                  for prompt in runtime.prompts.values())
    participating = runtime.activity and player_id in runtime.activity["participants"]
    if not player.get("give_space") and not player.get("activity_declined") and not invited and not participating:
        choices["host_activity"] = {}
    return choices


def validate_action_choice(action: dict | None, choices: dict) -> None:
    if action is None:
        return
    kind, args = action["type"], action["args"]
    if kind not in choices:
        raise ValueError(f"当前没有可选动作 {kind}")
    allowed = choices[kind]
    if set(args) != set(allowed) or any(args[key] not in values for key, values in allowed.items()):
        raise ValueError(f"{kind} 的参数不在当前候选中")


def fields(value: dict, names: tuple[str, ...]) -> dict:
    return {name: copy.deepcopy(value[name]) for name in names if name in value and value[name] not in (None, "", [], {})}


def near_anchor(entity: dict, scene: dict) -> str | None:
    """只把已观察坐标转成附近站位；不据此宣告抵达或动作完成。"""
    position = entity.get("position")
    if not isinstance(position, list) or len(position) != 3:
        return None
    nearby = []
    for anchor in scene.get("anchors", []):
        point = anchor.get("work_position")
        if not isinstance(point, list) or len(point) != 3:
            continue
        try:
            distance = math.dist(position, point)
        except (TypeError, ValueError):
            continue
        if math.isfinite(distance) and distance <= 1.5:
            nearby.append((distance, anchor["anchor_id"]))
    return min(nearby)[1] if nearby else None


def recent_history(history: list[dict], event: dict) -> list[dict]:
    rows = list(history[-12:])
    if rows:
        latest = rows[-1]
        if (event["type"] == "player_entered" and latest["type"] == "entered") or (
            event["type"] == "utterance" and latest["type"] == "player_speech" and latest.get("text") == event.get("text")
        ):
            rows.pop()
    result = []
    for row in rows:
        entry = fields(row, ("type", "text", "target_id", "topic_id", "recipe_id", "order_id", "command",
                             "stage", "from_status", "to_status", "next_stage", "choice", "correct", "key", "value", "source"))
        if row.get("result"):
            entry["result"] = fields(row["result"], ("anchor_id", "holder_id", "socket_id", "progress", "source"))
        result.append(entry)
    return result


def event_context(runtime: "NpcRuntime", item: dict) -> dict:
    player_id = item["player_id"]
    memory = runtime.memory.view(player_id)
    event = {"event_id": item["event_id"], "type": item["type"], "player_id": player_id,
             **fields(item["payload"], ("text", "target_id", "channel", "reply_to", "topic_id", "conversation_id",
                                        "stage", "stage_label", "order_id", "recipe_id", "command", "reason",
                                        "entity_id", "available", "question", "choice", "answer", "correct", "recipes"))}
    if item["type"] == "utterance":
        event["heard"] = True
    view = runtime.world_view
    visible, audible = view.get("visible_ids", []), view.get("audible_player_ids", [])
    guests = {}
    for guest_id, player in runtime.players.items():
        guest = {"name": player["display_name"], "visible": guest_id in visible, "audible": guest_id in audible}
        if player.get("give_space"):
            guest["give_space"] = True
        if guest_id in view["entities"]:
            anchor = near_anchor(view["entities"][guest_id], runtime.scene)
            if anchor:
                guest["near_anchor" if guest_id in visible else "last_seen_near"] = anchor
        guests[guest_id] = guest
    own = view["entities"].get("npc_owner", {})
    npc = {"body": fields(own.get("body", {}), ("movement", "hands", "gaze")),
           "free_channels": runtime.situation()["available_channels"]}
    body_action = runtime.actions.get(runtime.body_action)
    if body_action:
        npc["action"] = fields(body_action, ("command", "status", "control_requested"))
        order = runtime.orders.items.get(body_action["parent_skill_id"])
        if order:
            npc["action"].update(player_id=order["player_id"], order_id=order["order_id"])
    if runtime.current_speech:
        npc["speaking_to"] = list(runtime.current_speech["targets"])
    anchor = near_anchor(own, runtime.scene)
    if anchor:
        npc["near_anchor"] = anchor
    facilities = {}
    for entity_id, entity in view["entities"].items():
        if entity.get("kind") not in {"npc", "player"}:
            facilities[entity_id] = {**fields(entity, ("kind", "available")),
                                     "observation": "current" if entity_id in visible else "last_seen"}
    context = {
        "event": event,
        "situation": {"guests": guests, "npc": npc},
        "strategy": fields(runtime.strategy, ("active_goal_ids", "primary_goal_id", "interaction_mode", "next_step")),
        "memory": {"visit_count": memory["profile"]["visit_count"]},
        "available_actions": action_choices(runtime, item),
    }
    if facilities:
        context["situation"]["facilities"] = facilities
    pending = [fields(other, ("player_id", "type")) for other in runtime.agenda
               if other["item_id"] != item["item_id"] and other["status"] in {"pending", "generating", "deferred", "failed"}]
    if pending:
        context["situation"]["waiting"] = pending
    if memory["profile"]["preferences"]:
        context["memory"]["preferences"] = copy.deepcopy(memory["profile"]["preferences"])
    for name, names in (
        ("topics", ("topic_id", "stage", "status", "summary", "next_step")),
        ("commitments", ("kind", "topic_id", "trigger", "status")),
        ("episodes", ("topic_id", "summary", "resume_hint")),
    ):
        if memory[name]:
            context["memory"][name] = [fields(row, names) for row in memory[name]]
    orders = list(runtime.orders.items.values())
    last_order = next((order for order in reversed(orders) if order["player_id"] == player_id), None)
    selected_orders = [fields(order, ("order_id", "player_id", "recipe_id", "status", "stage", "preferences",
                                      "holder_id", "cleanup_pending", "cleanup_failed", "reason")) for order in orders
                       if order["status"] not in ENDED_ORDERS or order.get("cleanup_pending") or order is last_order
                       or order["order_id"] == event.get("order_id")]
    if selected_orders:
        context["orders"] = selected_orders
    conversations = list(runtime.topics.items.values())
    last_conv = next((conv for conv in reversed(conversations) if conv["player_id"] == player_id and not conv.get("forgotten")), None)
    selected_convs = [fields(conv, ("conversation_id", "player_id", "topic_id", "status", "stage", "pending_segment", "summary"))
                      for conv in conversations if not conv.get("forgotten") and (conv["status"] != "closed" or conv is last_conv)]
    if selected_convs:
        context["conversations"] = selected_convs
    if any(kind in context["available_actions"] for kind in ("invite_topic", "continue_topic")):
        context["available_topics"] = TOPICS.copy()
    history = recent_history(runtime.history.get(player_id, []), event)
    if history:
        context["recent_history"] = history
    return context


def strategy_context(runtime: "NpcRuntime") -> dict:
    situation = runtime.situation()
    return {"candidate_goals": runtime.goals(),
            "strategy": fields(runtime.strategy, ("active_goal_ids", "primary_goal_id", "interaction_mode", "next_step")),
            "situation": fields(situation, ("present_players", "conversation_groups", "npc_activity", "available_channels", "give_space_players"))}


EVENT_NAMES = {
    "player_entered": "顾客入店", "utterance": "玩家发言", "topic_segment": "已接受的讲解段落",
    "activity_result": "活动结果", "menu_question": "询问菜单", "order_ready": "饮品已放到取餐位",
    "action_failed": "动作失败", "world_changed": "观察到环境变化",
}
FIELD_NAMES = {
    "player_id": "玩家", "target_id": "对象", "order_id": "订单", "conversation_id": "会话",
    "topic_id": "话题", "recipe_id": "饮品", "command": "动作", "status": "状态", "stage": "阶段",
    "text": "原话", "question": "题目", "choice": "玩家选择", "answer": "答案", "correct": "答对",
    "reason": "原因", "entity_id": "实体", "available": "可用", "reply_to": "回复目标", "channel": "频道",
    "stage_label": "本段名称", "recipes": "菜单", "preferences": "口味", "holder_id": "持有者",
    "cleanup_pending": "等待回收", "cleanup_failed": "回收失败", "pending_segment": "段落待展示",
    "summary": "已发生摘要", "next_step": "下一步", "resume_hint": "旧经历续谈提示",
    "kind": "类型", "trigger": "触发条件", "goal_id": "目标", "active_goal_ids": "当前目标",
    "primary_goal_id": "首要目标", "interaction_mode": "互动方向", "control_requested": "已请求控制",
    "movement": "移动", "hands": "双手", "gaze": "视线", "speed": "速度", "anchor_id": "站位",
    "socket_id": "插槽", "cup_id": "杯子", "progress": "进度", "from_status": "原状态",
    "to_status": "新状态", "next_stage": "下一阶段", "source": "事实来源", "key": "记忆键", "value": "值",
    "drink": "饮品偏好", "flavor": "风味", "sweetness": "甜度", "milk": "奶", "address": "称呼",
    "invitation_style": "邀请偏好", "request_id": "动作请求", "segment_id": "段落", "result": "实际回执",
}


def fact_value(value) -> str:
    """引用字符串保留原话边界；对象只转为短事实，不发送对象树。"""
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return fact_details(value) or "无"
    if isinstance(value, list):
        return "、".join(fact_value(item) for item in value) or "无"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def fact_details(value: dict) -> str:
    parts = []
    for key, item in value.items():
        if key in {"time", "visit_id", "source_event_id"}:
            continue
        if key in {"stage", "next_stage"} and item in TOPIC_LABELS:
            item = f"{TOPIC_LABELS[item]}({item})"
        parts.append(f"{FIELD_NAMES.get(key, key)}={fact_value(item)}")
    return "；".join(parts)


def history_lines(rows: list[dict]) -> list[str]:
    return [f"- {row['type']}：{fact_details({key: value for key, value in row.items() if key != 'type'})}"
            for row in rows]


def event_text(context: dict) -> str:
    """生成真正发给事件模型的中文情境，内部字典仍用于追踪和校验。"""
    event, situation, memory = context["event"], context["situation"], context["memory"]
    lines = [f"事件：{EVENT_NAMES.get(event['type'], event['type'])}（{event['type']}）；"
             f"本轮玩家={event['player_id']}；event_id={event['event_id']}"]
    payload = {key: value for key, value in event.items() if key not in {"type", "player_id", "event_id", "heard"}}
    if payload:
        lines.append("事件内容：" + fact_details(payload))
    if event.get("heard"):
        lines.append("这次发言已实际听到；下方可听范围来自最近观察。")
    lines.append("在场与感知：")
    for player_id, guest in situation["guests"].items():
        facts = ["当前看见" if guest["visible"] else "当前未看见",
                 "在可听范围" if guest["audible"] else "不在当前可听范围"]
        if guest.get("near_anchor"):
            facts.append(f"观察到在 {guest['near_anchor']} 附近")
        elif guest.get("last_seen_near"):
            facts.append(f"上次看见在 {guest['last_seen_near']} 附近，当前位置未知")
        if guest.get("give_space"):
            facts.append("本次不主动邀请，仍可正常服务和回答")
        lines.append(f"- {fact_value(guest['name'])}（{player_id}）：{'；'.join(facts)}。")
    npc = situation["npc"]
    state = []
    if npc.get("near_anchor"):
        state.append(f"自己在 {npc['near_anchor']} 附近")
    if npc.get("body"):
        state.append("观察到身体：" + fact_details(npc["body"]))
    state.append("当前可用通道：" + "、".join(FIELD_NAMES.get(channel, "发言" if channel == "speech" else channel)
                                         for channel in npc["free_channels"]))
    if npc.get("action"):
        state.append("已下发动作：" + fact_details(npc["action"]))
    if npc.get("speaking_to"):
        state.append("正在对 " + "、".join(npc["speaking_to"]) + " 发言")
    lines.append("自身：" + "；".join(state) + "。")
    if situation.get("facilities"):
        facts = []
        for entity_id, facility in situation["facilities"].items():
            observation = "当前看见" if facility["observation"] == "current" else "仅上次看见，当前情况未知"
            availability = "可用" if facility.get("available") else "不可用" if "available" in facility else "可用性未知"
            facts.append(f"{entity_id}（{facility.get('kind', '设施')}）：{observation}；观察时{availability}")
        lines.append("设施：" + "。".join(facts) + "。")
    lines.append("当前打算：" + (fact_details(context["strategy"]) or "尚未形成战略") + "。")
    if situation.get("waiting"):
        lines.append("其他待答：" + "。".join(fact_details(row) for row in situation["waiting"]))
    orders, conversations = context.get("orders", []), context.get("conversations", [])
    lines.append("相关订单：" + ("\n" + "\n".join("- " + fact_details(row) for row in orders) if orders else "无"))
    lines.append("相关会话：" + ("\n" + "\n".join("- " + fact_details(row) for row in conversations) if conversations else "无"))
    lines.append(f"当前玩家记忆：第 {memory['visit_count']} 次到店。")
    lines.append("偏好：" + (fact_details(memory["preferences"]) if memory.get("preferences") else "无已知偏好"))
    for key, name in (("topics", "话题进度（阶段为下一段）"), ("commitments", "约定"), ("episodes", "共同经历")):
        if memory.get(key):
            lines.append(name + "：\n" + "\n".join("- " + fact_details(row) for row in memory[key]))
        else:
            lines.append(name + "：无")
    if context.get("recent_history"):
        lines.extend(["最近交互（按发生顺序）：", *history_lines(context["recent_history"])])
    if context.get("available_topics"):
        lines.append("可聊话题：" + "、".join(f"{name}（{topic_id}）" for topic_id, name in context["available_topics"].items()))
    lines.append("本轮可选动作（每个参数仅选一个允许值；未列出的动作不可选）：")
    for kind, args in context["available_actions"].items():
        parameters = "；".join(f"{key} 可选 {' / '.join(fact_value(value) for value in values)}" for key, values in args.items())
        lines.append(f"- {kind}：{parameters or '无参数'}")
    lines.append("- 不新增动作：action=null")
    return "\n".join(lines)


def model_input_text(task: str, context: dict) -> str:
    if task == "event":
        return event_text(context)
    if task == "strategy":
        situation = context["situation"]
        lines = ["候选目标（只能从下列 goal_id 选择）："]
        lines.extend(["- " + fact_details(goal) for goal in context["candidate_goals"]] or ["无"])
        lines.append("现有战略：" + (fact_details(context["strategy"]) or "无"))
        lines.append("在场玩家：" + "、".join(situation.get("present_players", [])))
        if situation.get("conversation_groups"):
            lines.append("正在交谈的分组：" + "；".join("、".join(group) for group in situation["conversation_groups"]))
        lines.append("身体观察：" + fact_details(situation.get("npc_activity", {})))
        lines.append("可用通道：" + "、".join(situation.get("available_channels", [])))
        if situation.get("give_space_players"):
            lines.append("本次不主动邀请：" + "、".join(situation["give_space_players"]))
        return "\n".join(lines)
    lines = [f"经历归属玩家：{context['player_id']}；话题：{context['topic_id']}", "实际经历（按发生顺序）：",
             *history_lines(context["actual_history"]), "已保存的话题进度（阶段为下一段）："]
    lines.extend(["- " + fact_details(fields(row, ("topic_id", "stage", "status", "summary", "next_step")))
                  for row in context["topic_progress"]] or ["无"])
    return "\n".join(lines)
