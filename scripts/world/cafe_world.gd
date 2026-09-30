extends Node3D
## Scene-root wiring. Godot owns geometry and single actions; Harness owns intent.

const ClientScript = preload("res://scripts/network/harness_client.gd")
const CameraScript = preload("res://scripts/world/cafe_camera.gd")
const HudScript = preload("res://scripts/ui/cafe_hud.gd")
const ObservationScript = preload("res://scripts/world/observation_exporter.gd")
const DriverScript = preload("res://scripts/actions/world_action_driver.gd")
const PlayerScript = preload("res://scripts/actors/test_player.gd")
const MotorScript = preload("res://scripts/actors/npc_motor.gd")

@onready var client: ClientScript = $HarnessClient
@onready var camera_rig: CameraScript = $CameraRig
@onready var overview_camera: Camera3D = $OverviewCamera
@onready var hud: HudScript = $Hud
@onready var observation: ObservationScript = $Observation
@onready var driver: DriverScript = $WorldActionDriver
@onready var npc: MotorScript = $Owner
@onready var zone: Area3D = $CafeZone

var players: Dictionary = {}
var anchors: Dictionary = {}
var entities: Dictionary = {}
var controlled_player_id: String = "p_A"
var latest_snapshot: Dictionary = {}
var latest_runtime: Dictionary = {}
var feedback_history: Array[Dictionary] = []
var visit_events: Array[Dictionary] = []
var _overview: bool = false
var _sample_elapsed: float = 0.0
var _leave_revision: Dictionary = {}
var _ready_for_sampling: bool = false


func _ready() -> void:
	players = {"p_A": $PlayerA, "p_B": $PlayerB}
	for node: Node in $Anchors.get_children():
		anchors[str(node.name)] = node
	entities = {
		"machine_01": $Navigation/Geometry/CoffeeMachine,
		"counter_01": $Navigation/Geometry/Counter,
		"table_1": $Navigation/Geometry/Table1,
		"table_2": $Navigation/Geometry/Table2,
		"menu_board": $Navigation/Geometry/MenuBoard,
	}
	driver.setup(npc, anchors, entities, $CupRack, $Sockets/Pickup, players)
	driver.set_recycle_socket($Sockets/Recycle)
	observation.setup(npc, players, entities, anchors, driver)
	observation.set_pickup_socket($Sockets/Pickup)
	observation.set_recycle_socket($Sockets/Recycle)
	client.message_received.connect(_route_message)
	client.status_changed.connect(hud.set_connection)
	driver.feedback.connect(_on_action_feedback)
	hud.switch_player_requested.connect(select_player)
	hud.utterance_requested.connect(_send_utterance)
	hud.interaction_requested.connect(_send_interaction)
	hud.memory_requested.connect(_request_memory)
	hud.reconnect_requested.connect(_connect_backend)
	hud.speech_feedback.connect(func(message: Dictionary): client.send_message(message))
	zone.body_entered.connect(_on_body_entered)
	zone.body_exited.connect(_on_body_exited)
	select_player("p_A")
	_ready_for_sampling = true
	await get_tree().physics_frame
	await get_tree().physics_frame
	_connect_backend()


func select_player(id: String) -> void:
	if not players.has(id):
		return
	for player: PlayerScript in players.values():
		player.controlled = player.player_id == id
	controlled_player_id = id
	var selected: PlayerScript = players[id]
	camera_rig.follow(selected)
	_update_camera()
	hud.select_player(id, selected.inside_cafe)


func _update_camera() -> void:
	var active: Camera3D = overview_camera if _overview else camera_rig.camera
	active.make_current()
	for player: PlayerScript in players.values():
		player.set_camera(active)


func _physics_process(delta: float) -> void:
	if not _ready_for_sampling:
		return
	_sample_elapsed += delta
	if _sample_elapsed < 0.25:
		return
	_sample_elapsed = 0.0
	latest_snapshot = observation.sample_snapshot()
	if client.accepted:
		client.send_message(latest_snapshot)


func _unhandled_input(event: InputEvent) -> void:
	var focus: Control = get_viewport().gui_get_focus_owner()
	if focus is LineEdit or focus is TextEdit:
		return
	if event.is_action_pressed("player_a"):
		select_player("p_A")
	elif event.is_action_pressed("player_b"):
		select_player("p_B")
	elif event.is_action_pressed("toggle_camera"):
		_overview = not _overview
		_update_camera()
	elif event.is_action_pressed("toggle_debug"):
		hud.tabs.current_tab = 2 if hud.tabs.current_tab != 2 else 0
	elif event.is_action_pressed("interact"):
		hud._intent("collect_cup")
	else:
		return
	get_viewport().set_input_as_handled()


func _connect_backend() -> void:
	client.connect_to_harness(observation.make_registration())


func _route_message(message: Dictionary) -> void:
	match str(message.get("type", "")):
		"scene_accepted":
			if not latest_snapshot.is_empty():
				client.send_message(latest_snapshot)
			for player: PlayerScript in players.values():
				if player.inside_cafe:
					_send_visit("player_entered", player)
			hud.log_line("咖啡馆", "共享场景已连接。进店后可点单、交谈或查看记忆。")
		"action_command":
			driver.handle_command(message)
		"action_control":
			driver.handle_control(message)
		"npc_speech":
			driver.set_attention(str(message.get("gaze_target", "")))
			hud.present_speech(message)
		"speech_control":
			hud.control_speech(message)
		"interaction_prompt":
			hud.show_prompt(message)
		"runtime_update":
			latest_runtime = message
			hud.render_runtime(message)
		"memory_view":
			hud.render_memory(message)
		"error":
			hud.log_line("提示", str(message.get("message", message.get("detail", message.get("reason", "请求失败")))))
		_:
			push_warning("Unhandled Harness message: %s" % str(message.get("type", "")))


func _on_action_feedback(message: Dictionary) -> void:
	feedback_history.append(message.duplicate(true))
	if feedback_history.size() > 100:
		feedback_history.pop_front()
	client.send_message(message)
	if message.get("status", "") == "failed":
		hud.log_line("动作未完成", str(message.get("reason", "UNKNOWN")))
	print("ACTION_FEEDBACK ", JSON.stringify(message))


func _send_utterance(text: String, target_id: String, channel: String) -> void:
	if not _can_interact():
		return
	var perceived: Array[String] = []
	if observation.can_hear(controlled_player_id, channel):
		perceived.append("npc_owner")
	var message: Dictionary = {
		"type": "utterance", "player_id": controlled_player_id,
		"text": text, "target_id": target_id, "channel": channel,
		"topic_id": "", "reply_to": null, "perceived_by": perceived,
	}
	if client.send_message(message):
		hud.log_line("顾客 %s → %s" % [controlled_player_id.trim_prefix("p_"), target_id], text)
		if perceived.is_empty():
			hud.log_line("距离提示", "老板娘听不到本地发言。请走近些，或选择公共话题。")


func _send_interaction(kind: String, payload: Dictionary) -> void:
	if not _can_interact():
		return
	var message: Dictionary = payload.duplicate(true)
	message.merge({"type": "interaction", "kind": kind, "player_id": controlled_player_id}, true)
	client.send_message(message)


func _can_interact() -> bool:
	if not client.accepted:
		hud.log_line("提示", "后端尚未连接，请启动服务后点击重连。")
		return false
	var player: PlayerScript = players[controlled_player_id]
	if not player.inside_cafe:
		hud.log_line("提示", "请先从入口走进咖啡馆。")
		return false
	return true


func _request_memory() -> void:
	if client.accepted:
		client.send_message({"type": "memory_request", "player_id": controlled_player_id})
	else:
		hud.log_line("提示", "连接后才能读取保存的记忆。")


func _on_body_entered(body: Node3D) -> void:
	if not body is PlayerScript:
		return
	var player: PlayerScript = body
	_leave_revision[player.player_id] = int(_leave_revision.get(player.player_id, 0)) + 1
	if player.inside_cafe:
		return
	player.inside_cafe = true
	_send_visit("player_entered", player)
	if player.player_id == controlled_player_id:
		hud.select_player(player.player_id, true)


func _on_body_exited(body: Node3D) -> void:
	if not body is PlayerScript:
		return
	var player: PlayerScript = body
	var revision: int = int(_leave_revision.get(player.player_id, 0)) + 1
	_leave_revision[player.player_id] = revision
	await get_tree().create_timer(0.5).timeout
	if not is_instance_valid(player) or revision != _leave_revision.get(player.player_id, 0):
		return
	if zone.overlaps_body(player) or not player.inside_cafe:
		return
	player.inside_cafe = false
	_send_visit("player_left", player)
	if player.player_id == controlled_player_id:
		hud.select_player(player.player_id, false)


func _send_visit(type: String, player: PlayerScript) -> void:
	var message: Dictionary = {"type": type, "player_id": player.player_id, "display_name": player.display_name}
	visit_events.append(message)
	if visit_events.size() > 40:
		visit_events.pop_front()
	if client.accepted:
		client.send_message(message)
	hud.log_line(player.display_name, "走进了咖啡馆。" if type == "player_entered" else "离开了咖啡馆。")
	print("VISIT_EVENT ", JSON.stringify(message))
