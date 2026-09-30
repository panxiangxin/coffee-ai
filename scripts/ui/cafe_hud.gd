extends CanvasLayer
## Displays backend facts and emits player intentions; owns no NPC strategy or memory.

signal switch_player_requested(player_id: String)
signal utterance_requested(text: String, target_id: String, channel: String)
signal interaction_requested(kind: String, payload: Dictionary)
signal reconnect_requested
signal memory_requested
signal speech_feedback(message: Dictionary)

@onready var overlay: Control = $Overlay
@onready var status_label: Label = $Overlay/Header/Status
@onready var player_label: Label = $Overlay/Header/Identity
@onready var transcript: RichTextLabel = $Overlay/Chat/Content/Transcript
@onready var input_box: LineEdit = $Overlay/Chat/Content/Compose/Input
@onready var target_select: OptionButton = $Overlay/Chat/Content/Compose/Target
@onready var tabs: TabContainer = $Overlay/Sidebar/Content/Tabs
@onready var prompt_text: RichTextLabel = $Overlay/Sidebar/Content/Tabs/Interaction/PromptText
@onready var prompt_buttons: Array[Button] = [
	$Overlay/Sidebar/Content/Tabs/Interaction/Choices/Choice0,
	$Overlay/Sidebar/Content/Tabs/Interaction/Choices/Choice1,
	$Overlay/Sidebar/Content/Tabs/Interaction/Choices/Choice2,
]
@onready var memory_text: RichTextLabel = $Overlay/Sidebar/Content/Tabs/Memory/MemoryText
@onready var preference_input: LineEdit = $Overlay/Sidebar/Content/Tabs/Memory/Preference
@onready var state_text: RichTextLabel = $Overlay/Sidebar/Content/Tabs/State/StateText
@onready var mode_label: Label = $Overlay/Sidebar/Content/Mode
@onready var action_label: Label = $Overlay/Sidebar/Content/Activity

var player_id: String = "p_A"
var runtime_state: Dictionary = {}
var _prompts: Dictionary = {}
var _speech: Dictionary = {}
var _speech_remaining: float = 0.0
var _lines: Array[String] = []


func _ready() -> void:
	$Overlay/Header/PlayerA.pressed.connect(func(): switch_player_requested.emit("p_A"))
	$Overlay/Header/PlayerB.pressed.connect(func(): switch_player_requested.emit("p_B"))
	$Overlay/Header/Reconnect.pressed.connect(func(): reconnect_requested.emit())
	$Overlay/Chat/Content/Compose/Send.pressed.connect(_submit)
	input_box.text_submitted.connect(func(_text: String): _submit())
	target_select.add_item("老板娘")
	target_select.add_item("公共话题")
	target_select.add_item("顾客 A")
	target_select.add_item("顾客 B")
	tabs.set_tab_title(0, "互动")
	tabs.set_tab_title(1, "记忆")
	tabs.set_tab_title(2, "状态")
	var interaction: Node = tabs.get_node("Interaction")
	interaction.get_node("Drinks/Americano").pressed.connect(func(): _intent("order_drink", {"value": "americano"}))
	interaction.get_node("Drinks/Latte").pressed.connect(func(): _intent("order_drink", {"value": "latte"}))
	interaction.get_node("Topic").pressed.connect(func(): _intent("invite_topic", {"value": "pourover_intro"}))
	interaction.get_node("Progress/Next").pressed.connect(func(): _intent("next_topic"))
	interaction.get_node("Progress/Pause").pressed.connect(func(): _intent("pause_topic"))
	interaction.get_node("Progress/Resume").pressed.connect(func(): _intent("resume_topic"))
	interaction.get_node("NextVisit").pressed.connect(func(): _intent("next_visit"))
	interaction.get_node("Order/Collect").pressed.connect(func(): _intent("collect_cup"))
	interaction.get_node("Order/Pause").pressed.connect(func(): _intent("pause_order"))
	interaction.get_node("Order/Resume").pressed.connect(func(): _intent("resume_order"))
	interaction.get_node("CancelOrder").pressed.connect(func(): _intent("cancel_order"))
	interaction.get_node("Activity").pressed.connect(func(): _intent("start_activity"))
	for index: int in range(prompt_buttons.size()):
		prompt_buttons[index].pressed.connect(_choose.bind(index))
	tabs.get_node("Memory/Refresh").pressed.connect(func(): memory_requested.emit())
	tabs.get_node("Memory/SavePreference").pressed.connect(_save_preference)
	tabs.get_node("Memory/ForgetTopic").pressed.connect(func(): _intent("forget_topic", {"value": "pourover_intro"}))
	tabs.get_node("State/Recovery/RetryPending").pressed.connect(func(): _intent("retry_pending"))
	tabs.get_node("State/Recovery/RetryCleanup").pressed.connect(func(): _intent("retry_cleanup"))
	_show_prompt()
	log_line("慢杯", "欢迎来到咖啡馆。WASD 行走，Ctrl+1 / 2 切换顾客，F2 切换镜头。")


func select_player(id: String, inside: bool) -> void:
	player_id = id
	player_label.text = "当前：顾客 %s · %s" % [id.trim_prefix("p_"), "店内" if inside else "店外"]
	_show_prompt()
	memory_text.text = "点击“查看记录”读取当前顾客的真实记忆。"


func set_connection(status: String) -> void:
	status_label.text = status


func log_line(speaker: String, text: String) -> void:
	_lines.append("%s  ·  %s" % [speaker, text])
	if _lines.size() > 60:
		_lines.pop_front()
	transcript.text = "\n\n".join(_lines)


func render_runtime(message: Dictionary) -> void:
	runtime_state = message
	_prompts.clear()
	for prompt: Dictionary in message.get("prompts", []):
		_prompts[str(prompt.get("player_id", ""))] = prompt
	var model: Dictionary = message.get("model", {})
	var model_status: String = str(model.get("status", "unconfigured"))
	var status_names: Dictionary = {"unconfigured": "请配置 API Key", "ready": "已就绪", "calling": "思考中", "error": "请求失败，可在状态页重试"}
	mode_label.text = "DeepSeek · %s" % str(status_names.get(model_status, model_status)) if message.get("mode", "") == "model" else "规则联调 · 非模型决策"
	var orders: Array = message.get("orders", [])
	var activities: Array[String] = []
	var stages: Dictionary = {"CONFIRM_ORDER": "等待确认", "MOVE_TO_MACHINE": "前往咖啡机", "BREW": "制作中", "MOVE_TO_COUNTER": "送往柜台", "PLACE": "放杯中", "WAIT_PICKUP": "等待取杯", "COLLECT": "取杯中", "RECYCLE_PICKUP_MOVE": "准备回收", "RECYCLE_RETRIEVE": "取回杯子", "RECYCLE_MOVE": "送往回收位", "RECYCLE_PLACE": "回收中"}
	for order: Dictionary in orders:
		if order.get("status", "") not in ["completed", "cancelled"]:
			var stage: String = str(order.get("stage", ""))
			var description: String = str(stages.get(stage, stage))
			if order.get("status", "") == "paused":
				description = "已暂停 · " + description
			elif order.get("status", "") == "failed":
				description = "未完成 · " + str(order.get("reason", ""))
			activities.append("%s %s" % [str(order.get("player_id", "")).trim_prefix("p_"), description])
	action_label.text = "订单：" + (" / ".join(activities) if not activities.is_empty() else "暂无进行中的订单")
	state_text.text = JSON.stringify(message, "  ")
	_show_prompt()


func show_prompt(message: Dictionary) -> void:
	_prompts[str(message.get("player_id", ""))] = message
	_show_prompt()


func render_memory(message: Dictionary) -> void:
	if message.get("player_id", "") != player_id:
		return
	memory_text.text = JSON.stringify(message, "  ")


func present_speech(message: Dictionary) -> void:
	_interrupt_speech()
	_speech = message
	var targets: Array = message.get("targets", [])
	var names: Array[String] = []
	for target: String in targets:
		names.append("顾客 " + target.trim_prefix("p_"))
	log_line("老板娘 → " + "、".join(names), str(message.get("text", "")))
	_speech_remaining = clampf(str(message.get("text", "")).length() * 0.09, 2.0, 10.0)
	_mark_speech_displayed(str(message.get("speech_id", "")))


func control_speech(message: Dictionary) -> void:
	if not _speech.is_empty() and message.get("speech_id", "") == _speech.get("speech_id", ""):
		_interrupt_speech()


func _process(delta: float) -> void:
	if _speech.is_empty():
		return
	_speech_remaining -= delta
	if _speech_remaining <= 0.0:
		_emit_speech_feedback("finished")
		_speech = {}


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("open_chat") and not input_box.has_focus():
		input_box.grab_focus()
		get_viewport().set_input_as_handled()
	elif event.is_action_pressed("ui_cancel"):
		input_box.release_focus()
		get_viewport().set_input_as_handled()
	elif event.is_action_pressed("switch_target"):
		target_select.select((target_select.selected + 1) % target_select.item_count)
		get_viewport().set_input_as_handled()


func _submit() -> void:
	var text: String = input_box.text.strip_edges()
	if text.is_empty():
		return
	var targets: Array[String] = ["npc_owner", "room", "p_A", "p_B"]
	var target: String = targets[target_select.selected]
	utterance_requested.emit(text, target, "room" if target == "room" else "local")
	input_box.clear()
	input_box.release_focus()
	get_viewport().set_input_as_handled()


func _intent(kind: String, payload: Dictionary = {}) -> void:
	var data: Dictionary = payload.duplicate(true)
	for conversation: Dictionary in runtime_state.get("conversations", []):
		if conversation.get("player_id", "") == player_id and conversation.get("status", "") in ["waiting_reply", "engaged", "paused"]:
			data["conversation_id"] = conversation.get("conversation_id", "")
	for order: Dictionary in runtime_state.get("orders", []):
		var relevant: bool = bool(order.get("cleanup_failed", false)) if kind == "retry_cleanup" else order.get("status", "") not in ["completed", "cancelled"]
		if order.get("player_id", "") == player_id and relevant:
			data["order_id"] = order.get("order_id", "")
	interaction_requested.emit(kind, data)


func _show_prompt() -> void:
	var prompt: Dictionary = _prompts.get(player_id, {})
	prompt_text.text = str(prompt.get("text", "走进店里，向老板娘点单或开启一个话题。"))
	var options: Array = prompt.get("options", [])
	for index: int in range(prompt_buttons.size()):
		prompt_buttons[index].visible = index < options.size()
		if index < options.size():
			prompt_buttons[index].text = str(options[index].get("label", "选择"))


func _choose(index: int) -> void:
	var prompt: Dictionary = _prompts.get(player_id, {})
	var options: Array = prompt.get("options", [])
	if index >= options.size():
		return
	var payload: Dictionary = {}
	for key: String in ["prompt_id", "conversation_id", "order_id", "activity_id"]:
		if prompt.has(key):
			payload[key] = prompt[key]
	if options[index].has("value"):
		payload["value"] = options[index]["value"]
	interaction_requested.emit(str(options[index].get("kind", "")), payload)
	_prompts.erase(player_id)
	_show_prompt()


func _save_preference() -> void:
	var value: String = preference_input.text.strip_edges()
	if not value.is_empty():
		_intent("correct_preference", {"value": {"key": "coffee", "value": value}})
		preference_input.clear()


func _interrupt_speech() -> void:
	if not _speech.is_empty():
		_emit_speech_feedback("interrupted")
		_speech = {}


func _mark_speech_displayed(id: String) -> void:
	await RenderingServer.frame_post_draw
	if not _speech.is_empty() and _speech.get("speech_id", "") == id:
		_emit_speech_feedback("displayed")


func _emit_speech_feedback(status: String) -> void:
	var feedback: Dictionary = {"type": "speech_feedback", "status": status}
	for key: String in ["speech_id", "conversation_id", "segment_id"]:
		if _speech.has(key):
			feedback[key] = _speech[key]
	speech_feedback.emit(feedback)
