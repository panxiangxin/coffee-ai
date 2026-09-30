extends Node
## Executes one physical action at a time per channel. Harness owns all workflows.

signal feedback(message: Dictionary)

const NpcMotorScript = preload("res://scripts/actors/npc_motor.gd")
const SUPPORTED_COMMANDS: Array[String] = [
	"move_to_anchor", "look_at_entity", "gesture", "brew", "place_cup", "collect_cup", "retrieve_cup", "tidy"
]

@export var work_distance: float = 0.5
@export var pickup_distance: float = 1.6
@export var gaze_turn_speed: float = 8.0
@export var gaze_tolerance: float = 0.12
@export var gaze_limit_degrees: float = 60.0
@export var hand_socket_path: NodePath = ^"Visual/RightArm/HandSocket"

var _npc: NpcMotorScript
var _anchors: Dictionary = {}
var _entities: Dictionary = {}
var _players: Dictionary = {}
var _cups: Array[Node3D] = []
var _working_cup: Node3D
var _pickup: Marker3D
var _recycle: Marker3D
var _hand: Node3D
var _head: Node3D
var _animation: AnimationPlayer
var _active: Dictionary = {}
var _channels: Dictionary = {}
var _finished: Dictionary = {}
var _animation_request: String = ""
var _attention_target: String = ""
var _configured: bool = false


func setup(
	npc: NpcMotorScript,
	anchors: Dictionary,
	entities: Dictionary,
	cup_rack: Node3D,
	pickup: Marker3D,
	players: Dictionary
) -> void:
	_npc = npc
	_anchors = anchors
	_entities = entities
	_pickup = pickup
	_players = players
	_hand = _npc.get_node_or_null(hand_socket_path) as Node3D
	_head = _npc.get_node_or_null("Visual/Head") as Node3D
	_animation = _npc.get_node_or_null("AnimationPlayer") as AnimationPlayer
	_npc.setup(anchors)
	if not _npc.motion_event.is_connected(_on_motion_event):
		_npc.motion_event.connect(_on_motion_event)
	if is_instance_valid(_animation) and not _animation.animation_finished.is_connected(_on_animation_finished):
		_animation.animation_finished.connect(_on_animation_finished)
	# Keep references when cups leave the rack; all cup entities are scene-authored.
	_cups.clear()
	if is_instance_valid(cup_rack):
		for child: Node in cup_rack.get_children():
			var cup: Node3D = child as Node3D
			if not is_instance_valid(cup):
				continue
			_cups.append(cup)
			if not cup.has_meta("state"):
				cup.set_meta("state", "empty")
				cup.set_meta("cup_id", "")
				cup.set_meta("order_id", "")
				cup.set_meta("holder_id", "")
				cup.set_meta("socket_id", "")
				cup.visible = false
	_configured = true


func set_recycle_socket(socket: Marker3D) -> void:
	_recycle = socket


func handle_command(message: Dictionary) -> void:
	var request_id: String = str(message.get("request_id", ""))
	if _active.has(request_id) or _finished.has(request_id):
		push_warning("Duplicate action request ignored: " + request_id)
		return
	if not _configured:
		_reject(message, "WORLD_NOT_READY")
		return
	if request_id.is_empty() or str(message.get("npc_id", "")) != _npc.npc_id:
		_reject(message, "INVALID_REQUEST")
		return
	var command: String = str(message.get("command", ""))
	if command not in SUPPORTED_COMMANDS:
		_reject(message, "UNKNOWN_COMMAND")
		return
	if not message.get("args", {}) is Dictionary:
		_reject(message, "INVALID_ARGUMENTS")
		return
	var args: Dictionary = message.get("args", {}).duplicate(true)
	var required: Array[String] = _required_channels(command, args)
	for channel: String in required:
		if _channels.has(channel):
			_reject(message, "CHANNEL_BUSY")
			return
	var reason: String = _validate_start(command, args)
	if not reason.is_empty():
		_reject(message, reason)
		return
	var context: Dictionary = {
		"command": command, "args": args, "channels": required,
		"parent_skill_id": str(message.get("parent_skill_id", "")),
		"paused": false, "progress": 0.0, "gaze_held": 0.0, "gaze_elapsed": 0.0,
	}
	_active[request_id] = context
	for channel: String in required:
		_channels[channel] = request_id
	if command == "move_to_anchor":
		if not _npc.start_move(request_id, str(args.get("anchor_id", ""))):
			_finish(request_id, "failed", "MOVE_REJECTED")
		return
	if command == "brew":
		_working_cup = _find_cup(str(args["cup_id"]))
		if not is_instance_valid(_working_cup):
			_working_cup = _unused_cup()
		_working_cup.set_meta("cup_id", str(args["cup_id"]))
		_working_cup.set_meta("order_id", str(args["order_id"]))
		_working_cup.set_meta("state", "brewing")
		_move_cup_to(_working_cup, _hand, _npc.npc_id, "npc_hand_socket")
	_emit(request_id, "started")
	match command:
		"look_at_entity":
			_attention_target = str(args.get("target_id", ""))
		"collect_cup":
			_collect_cup(request_id)
		"brew":
			_face_station(args, "machine_01")
			_play_action(request_id, &"brew")
		"place_cup":
			var socket: Marker3D = _recycle if str(args["socket_id"]) == "recycle_socket" else _pickup
			_npc.face_toward(socket.global_position)
			_play_action(request_id, &"serve")
		"retrieve_cup":
			_npc.face_toward(_pickup.global_position)
			_play_action(request_id, &"serve")
		"tidy":
			_face_station(args, "counter_01")
			_play_action(request_id, &"tidy")
		"gesture":
			_play_action(request_id, StringName(str(args.get("animation_key", "greet"))))


func handle_control(message: Dictionary) -> void:
	var request_id: String = str(message.get("request_id", ""))
	if not _active.has(request_id):
		if not _finished.has(request_id):
			_reject(message, "UNKNOWN_REQUEST")
		return
	var context: Dictionary = _active[request_id]
	var operation: String = str(message.get("operation", ""))
	if operation not in ["pause", "resume", "cancel"]:
		if context["command"] == "move_to_anchor":
			_npc.fail_move(request_id, "UNKNOWN_CONTROL")
		else:
			_finish(request_id, "failed", "UNKNOWN_CONTROL")
		return
	if context["command"] == "move_to_anchor":
		match operation:
			"pause": _npc.pause_move(request_id)
			"resume": _npc.resume_move(request_id)
			"cancel": _npc.cancel_move(request_id)
		return
	if operation == "cancel":
		_finish(request_id, "cancelled")
		return
	var paused: bool = bool(context["paused"])
	if (operation == "pause" and paused) or (operation == "resume" and not paused):
		return
	if operation == "resume":
		var reason: String = _continuation_reason(context)
		if not reason.is_empty():
			_finish(request_id, "failed", reason)
			return
	context["paused"] = operation == "pause"
	if context["command"] == "look_at_entity" and operation == "pause" and not _channels.has("hands") and not _channels.has("movement"):
		_npc.clear_facing_target()
	if _animation_request == request_id:
		if operation == "pause":
			_animation.pause()
		else:
			_animation.play()
	_emit(request_id, "paused" if operation == "pause" else "resumed")


func set_attention(target_id: String) -> void:
	# Speech may update free gaze without touching movement or hand actions.
	if not _channels.has("gaze"):
		_attention_target = target_id if _target_available(target_id) else ""


func get_body_state() -> Dictionary:
	if not _configured:
		return {"movement": "idle", "hands": "idle", "gaze": "idle"}
	var result: Dictionary = _npc.get_body_state()
	var hands_id: String = str(_channels.get("hands", ""))
	result["hands"] = "idle"
	if _active.has(hands_id):
		var context: Dictionary = _active[hands_id]
		result["hands"] = "paused" if context["paused"] else str(context["command"])
		result["hands_request_id"] = hands_id
		result["action_progress"] = context["progress"]
	result["gaze"] = "idle" if _attention_target.is_empty() else _attention_target
	return result


func get_cup_state(cup_id: String = "") -> Dictionary:
	var cup: Node3D = _working_cup if cup_id.is_empty() else _find_cup(cup_id)
	if not is_instance_valid(cup):
		return {}
	return {
		"cup_id": str(cup.get_meta("cup_id", "")),
		"order_id": str(cup.get_meta("order_id", "")),
		"holder_id": str(cup.get_meta("holder_id", "")),
		"socket_id": str(cup.get_meta("socket_id", "")),
		"state": str(cup.get_meta("state", "empty")),
		"cup_ready": str(cup.get_meta("state", "empty")) in ["ready", "placed", "collected"],
		"position": _vector_array(cup.global_position),
	}


func get_cup_states() -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for cup: Node3D in _cups:
		if not is_instance_valid(cup):
			continue
		var cup_id: String = str(cup.get_meta("cup_id", ""))
		if not cup_id.is_empty():
			result.append(get_cup_state(cup_id))
	return result


func get_active_actions() -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for request_id: String in _active:
		var context: Dictionary = _active[request_id]
		result.append({
			"request_id": request_id, "command": context["command"],
			"paused": context["paused"], "progress": context["progress"],
		})
	return result


func _required_channels(command: String, args: Dictionary) -> Array[String]:
	match command:
		"move_to_anchor": return ["movement"]
		"look_at_entity": return ["gaze"]
		"gesture": return ["hands"]
		"brew": return ["hands", "movement", "station:" + str(args.get("station_id", "machine_01")), "cup:" + str(args.get("cup_id", ""))]
		"place_cup", "retrieve_cup": return ["hands", "movement", "cup:" + str(args.get("cup_id", ""))]
		"tidy": return ["hands", "movement", "station:" + str(args.get("station_id", "counter_01"))]
		"collect_cup": return ["cup:" + str(args.get("cup_id", "")), "carry:" + str(args.get("player_id", ""))]
	return []


func _validate_start(command: String, args: Dictionary) -> String:
	match command:
		"move_to_anchor":
			if not _anchor_available(str(args.get("anchor_id", ""))):
				return "NO_ANCHOR"
		"look_at_entity":
			if not is_instance_valid(_head):
				return "HEAD_MISSING"
			if not _target_available(str(args.get("target_id", ""))):
				return "TARGET_LEFT"
		"brew":
			if str(args.get("order_id", "")).is_empty() or str(args.get("cup_id", "")).is_empty():
				return "INVALID_ARGUMENTS"
			if not is_instance_valid(_hand):
				return "NO_HAND_SOCKET"
			var cup: Node3D = _find_cup(str(args["cup_id"]))
			if is_instance_valid(cup):
				var retry_unfinished: bool = str(cup.get_meta("state", "")) == "unfinished" and str(cup.get_meta("order_id", "")) == str(args["order_id"]) and str(cup.get_meta("holder_id", "")) == _npc.npc_id
				if not retry_unfinished:
					return "CUP_IN_USE"
			else:
				cup = _unused_cup()
				if not is_instance_valid(cup):
					return "CUPS_UNAVAILABLE"
			var held_cup: Node3D = _held_cup()
			if is_instance_valid(held_cup) and held_cup != cup:
				return "HANDS_HOLDING_CUP"
			var station_error: String = _station_error(args, "machine_01", "coffee_machine")
			if not station_error.is_empty():
				return station_error
			return _animation_error(&"brew")
		"place_cup":
			var place_error: String = _placement_error(args)
			if not place_error.is_empty():
				return place_error
			return _animation_error(&"serve")
		"retrieve_cup":
			var retrieve_error: String = _retrieval_error(args)
			if not retrieve_error.is_empty():
				return retrieve_error
			return _animation_error(&"serve")
		"collect_cup":
			return _collection_error(args)
		"tidy":
			var station_error: String = _station_error(args, "counter_01", "counter")
			if not station_error.is_empty():
				return station_error
			if is_instance_valid(_held_cup()):
				return "HANDS_HOLDING_CUP"
			return _animation_error(&"tidy")
		"gesture":
			var key: String = str(args.get("animation_key", "greet"))
			if key not in ["greet", "point"]:
				return "UNSUPPORTED_GESTURE"
			if is_instance_valid(_held_cup()):
				return "HANDS_HOLDING_CUP"
			return _animation_error(StringName(key))
	return ""


func _continuation_reason(context: Dictionary) -> String:
	var args: Dictionary = context["args"]
	match str(context["command"]):
		"look_at_entity":
			if not _target_available(str(args.get("target_id", ""))):
				return "TARGET_LEFT"
		"brew":
			var cup: Node3D = _find_cup(str(args.get("cup_id", "")))
			if not is_instance_valid(cup) or str(cup.get_meta("holder_id", "")) != _npc.npc_id or str(cup.get_meta("state", "")) != "brewing":
				return "CUP_NOT_HELD"
			return _station_error(args, "machine_01", "coffee_machine")
		"tidy": return _station_error(args, "counter_01", "counter")
		"place_cup": return _placement_error(args)
		"retrieve_cup": return _retrieval_error(args)
	return ""


func _animation_error(animation_name: StringName) -> String:
	if not is_instance_valid(_animation) or not _animation.has_animation(animation_name):
		return "ANIMATION_MISSING"
	var clip: Animation = _animation.get_animation(animation_name)
	if clip.loop_mode != Animation.LOOP_NONE or clip.length <= 0.0:
		return "ANIMATION_MUST_BE_FINITE"
	if clip.get_track_count() == 0:
		return "ANIMATION_EMPTY"
	return ""


func _station_error(args: Dictionary, station_id: String, anchor_id: String) -> String:
	if str(args.get("station_id", station_id)) != station_id:
		return "WRONG_STATION"
	var station: Node3D = _entities.get(station_id) as Node3D
	if not is_instance_valid(station) or not bool(station.get_meta("available", true)):
		return "STATION_UNAVAILABLE"
	if not _at_anchor(anchor_id):
		return "NOT_AT_STATION"
	return ""


func _anchor_available(anchor_id: String) -> bool:
	var anchor: Node3D = _anchors.get(anchor_id) as Node3D
	return is_instance_valid(anchor) and bool(anchor.get_meta("available", true))


func _at_anchor(anchor_id: String) -> bool:
	if not _anchor_available(anchor_id):
		return false
	var anchor: Node3D = _anchors[anchor_id] as Node3D
	var distance: Vector3 = anchor.global_position - _npc.global_position
	return Vector2(distance.x, distance.z).length() <= work_distance and absf(distance.y) <= 0.5


func _find_cup(cup_id: String) -> Node3D:
	if cup_id.is_empty():
		return null
	for cup: Node3D in _cups:
		if is_instance_valid(cup) and str(cup.get_meta("cup_id", "")) == cup_id:
			return cup
	return null


func _unused_cup() -> Node3D:
	for cup: Node3D in _cups:
		if not is_instance_valid(cup):
			continue
		var state: String = str(cup.get_meta("state", ""))
		if state == "empty" and str(cup.get_meta("cup_id", "")).is_empty():
			return cup
		# A cancelled cup only becomes reusable after the actual recycle placement.
		if state == "recycled" and str(cup.get_meta("socket_id", "")) == "recycle_socket" and cup.get_parent() == _recycle:
			return cup
	return null


func _held_cup() -> Node3D:
	for cup: Node3D in _cups:
		if is_instance_valid(cup) and str(cup.get_meta("holder_id", "")) == _npc.npc_id:
			return cup
	return null


func _pickup_occupied() -> bool:
	for cup: Node3D in _cups:
		if is_instance_valid(cup) and str(cup.get_meta("socket_id", "")) == "pickup_socket":
			return true
	return false


func _placement_error(args: Dictionary) -> String:
	var cup: Node3D = _find_cup(str(args.get("cup_id", "")))
	if not is_instance_valid(cup):
		return "CUP_NOT_FOUND"
	var holder_id: String = str(cup.get_meta("holder_id", ""))
	if _players.has(holder_id):
		return "CUP_ALREADY_COLLECTED"
	if holder_id != _npc.npc_id or cup.get_parent() != _hand:
		return "CUP_NOT_HELD"
	var socket_id: String = str(args.get("socket_id", ""))
	var state: String = str(cup.get_meta("state", ""))
	if socket_id == "recycle_socket":
		if not is_instance_valid(_recycle):
			return "NO_SOCKET"
		if state not in ["ready", "unfinished"]:
			return "CUP_NOT_RECYCLABLE"
		if not _at_anchor("recycle_spot"):
			return "NOT_AT_STATION"
		return ""
	if socket_id != "pickup_socket" or not is_instance_valid(_pickup):
		return "NO_SOCKET"
	if state != "ready":
		return "CUP_NOT_READY"
	if not _at_anchor("counter"):
		return "NOT_AT_STATION"
	if _pickup_occupied():
		return "PICKUP_OCCUPIED"
	return ""


func _retrieval_error(args: Dictionary) -> String:
	var cup: Node3D = _find_cup(str(args.get("cup_id", "")))
	if not is_instance_valid(cup):
		return "CUP_NOT_FOUND"
	if _players.has(str(cup.get_meta("holder_id", ""))):
		return "CUP_ALREADY_COLLECTED"
	if str(cup.get_meta("state", "")) != "placed" or str(cup.get_meta("socket_id", "")) != "pickup_socket" or cup.get_parent() != _pickup:
		return "CUP_NOT_AT_PICKUP"
	if not is_instance_valid(_hand) or not is_instance_valid(_pickup):
		return "CUP_OR_SOCKET_MISSING"
	if is_instance_valid(_held_cup()):
		return "HANDS_HOLDING_CUP"
	if not _at_anchor("counter"):
		return "NOT_AT_STATION"
	return ""


func _collection_error(args: Dictionary) -> String:
	var cup: Node3D = _find_cup(str(args.get("cup_id", "")))
	if not is_instance_valid(cup) or str(cup.get_meta("state", "")) != "placed":
		return "CUP_NOT_READY"
	if str(cup.get_meta("socket_id", "")) != "pickup_socket" or not is_instance_valid(_pickup):
		return "CUP_NOT_AT_PICKUP"
	var player_id: String = str(args.get("player_id", ""))
	var player: Node3D = _players.get(player_id) as Node3D
	if not is_instance_valid(player) or not bool(player.get("inside_cafe")):
		return "TARGET_LEFT"
	if not player.get_node_or_null("CarrySocket") is Marker3D:
		return "NO_CARRY_SOCKET"
	var distance: Vector3 = _pickup.global_position - player.global_position
	if Vector2(distance.x, distance.z).length() > pickup_distance or absf(distance.y) > 2.0:
		return "OUT_OF_REACH"
	var socket: Marker3D = player.get_node("CarrySocket") as Marker3D
	if socket.get_child_count() > 0:
		return "CARRY_SLOT_OCCUPIED"
	return ""


func _play_action(request_id: String, animation_name: StringName) -> void:
	_animation_request = request_id
	_animation.speed_scale = 1.0
	var context: Dictionary = _active[request_id]
	if context["command"] == "tidy":
		var args: Dictionary = context["args"]
		var duration: float = clampf(float(args.get("duration", 3.0)), 1.0, 15.0)
		_animation.speed_scale = _animation.get_animation(animation_name).length / duration
	_animation.play(animation_name)


func _collect_cup(request_id: String) -> void:
	var args: Dictionary = _active[request_id]["args"]
	# Repeat the check at the physical transfer, not just command admission.
	var reason: String = _collection_error(args)
	if not reason.is_empty():
		_finish(request_id, "failed", reason)
		return
	var player_id: String = str(args["player_id"])
	var player: Node3D = _players[player_id] as Node3D
	var cup: Node3D = _find_cup(str(args["cup_id"]))
	_move_cup_to(cup, player.get_node("CarrySocket") as Node3D, player_id, player_id + "_carry_socket")
	cup.set_meta("state", "collected")
	_finish(request_id, "completed")


func _move_cup_to(cup: Node3D, socket: Node3D, holder_id: String, socket_id: String) -> void:
	cup.reparent(socket, false)
	cup.transform = Transform3D.IDENTITY
	cup.visible = true
	cup.set_meta("holder_id", holder_id)
	cup.set_meta("socket_id", socket_id)


func _face_station(args: Dictionary, default_id: String) -> void:
	var station: Node3D = _entities.get(str(args.get("station_id", default_id))) as Node3D
	if is_instance_valid(station):
		_npc.face_toward(station.global_position)


func _target_node(target_id: String) -> Node3D:
	if _players.has(target_id):
		return _players[target_id] as Node3D
	return _entities.get(target_id) as Node3D


func _target_available(target_id: String) -> bool:
	var target: Node3D = _target_node(target_id)
	if not is_instance_valid(target):
		return false
	if _players.has(target_id) and not bool(target.get("inside_cafe")):
		return false
	return bool(target.get_meta("available", true))


func _look_position(target_id: String) -> Vector3:
	var target: Node3D = _target_node(target_id)
	var eye: Node3D = target.get_node_or_null("Visual/Head") as Node3D
	if not is_instance_valid(eye):
		eye = target.get_node_or_null("LookPoint") as Node3D
	return eye.global_position if is_instance_valid(eye) else target.global_position + Vector3.UP


func _process(delta: float) -> void:
	if not _configured:
		return
	for request_id: String in _active.keys():
		if not _active.has(request_id):
			continue
		var context: Dictionary = _active[request_id]
		if bool(context["paused"]):
			continue
		if context["command"] == "look_at_entity":
			_advance_gaze(request_id, delta)
		elif _animation_request == request_id:
			var reason: String = _continuation_reason(context)
			if not reason.is_empty():
				_finish(request_id, "failed", reason)
				continue
			context["progress"] = clampf(_animation.current_animation_position / maxf(_animation.current_animation_length, 0.001), 0.0, 1.0)
	if not _channels.has("gaze"):
		if _target_available(_attention_target):
			_turn_head(_attention_target, delta)
		else:
			_attention_target = ""
			if is_instance_valid(_head):
				_head.rotation = _head.rotation.lerp(Vector3.ZERO, minf(gaze_turn_speed * delta, 1.0))


func _advance_gaze(request_id: String, delta: float) -> void:
	var context: Dictionary = _active[request_id]
	var args: Dictionary = context["args"]
	var target_id: String = str(args.get("target_id", ""))
	if not _target_available(target_id):
		_finish(request_id, "failed", "TARGET_LEFT")
		return
	if not _channels.has("hands") and not _channels.has("movement"):
		_npc.face_toward(_look_position(target_id))
	var aligned: bool = _turn_head(target_id, delta)
	context["gaze_elapsed"] = float(context["gaze_elapsed"]) + delta
	if aligned:
		context["gaze_held"] = float(context["gaze_held"]) + delta
	else:
		context["gaze_held"] = 0.0
	var hold: float = clampf(float(args.get("hold_seconds", 0.5)), 0.0, 15.0)
	if aligned and float(context["gaze_held"]) >= hold:
		_finish(request_id, "completed")
	elif float(context["gaze_elapsed"]) > hold + 5.0:
		_finish(request_id, "failed", "GAZE_OUT_OF_RANGE")


func _turn_head(target_id: String, delta: float) -> bool:
	if not is_instance_valid(_head):
		return false
	var local_direction: Vector3 = _npc.visual.global_basis.inverse() * (_look_position(target_id) - _head.global_position)
	if local_direction.length_squared() < 0.000001:
		return true
	var yaw: float = atan2(-local_direction.x, -local_direction.z)
	var pitch: float = atan2(local_direction.y, Vector2(local_direction.x, local_direction.z).length())
	var yaw_limit: float = deg_to_rad(gaze_limit_degrees)
	var desired_yaw: float = clampf(yaw, -yaw_limit, yaw_limit)
	var desired_pitch: float = clampf(pitch, -0.5, 0.5)
	var blend: float = minf(gaze_turn_speed * delta, 1.0)
	_head.rotation.y = lerp_angle(_head.rotation.y, desired_yaw, blend)
	_head.rotation.x = lerp_angle(_head.rotation.x, desired_pitch, blend)
	return absf(wrapf(yaw - _head.rotation.y, -PI, PI)) <= gaze_tolerance and absf(pitch - _head.rotation.x) <= gaze_tolerance


func _on_motion_event(request_id: String, status: String, reason: String) -> void:
	if not _active.has(request_id):
		return
	if status in ["completed", "failed", "cancelled"]:
		_finish(request_id, status, reason)
		return
	_active[request_id]["paused"] = status == "paused"
	_emit(request_id, status, reason)


func _on_animation_finished(_animation_name: StringName) -> void:
	var request_id: String = _animation_request
	if request_id.is_empty() or not _active.has(request_id):
		return
	var context: Dictionary = _active[request_id]
	var reason: String = _continuation_reason(context)
	if not reason.is_empty():
		_finish(request_id, "failed", reason)
		return
	var args: Dictionary = context["args"]
	var cup: Node3D = _find_cup(str(args.get("cup_id", "")))
	match str(context["command"]):
		"brew":
			cup.set_meta("state", "ready")
		"place_cup":
			if str(args["socket_id"]) == "recycle_socket":
				_move_cup_to(cup, _recycle, "counter_01", "recycle_socket")
				cup.set_meta("state", "recycled")
			else:
				_move_cup_to(cup, _pickup, "counter_01", "pickup_socket")
				cup.set_meta("state", "placed")
		"retrieve_cup":
			_move_cup_to(cup, _hand, _npc.npc_id, "npc_hand_socket")
			cup.set_meta("state", "ready")
	context["progress"] = 1.0
	_finish(request_id, "completed")


func _finish(request_id: String, status: String, reason: String = "") -> void:
	if not _active.has(request_id):
		return
	var context: Dictionary = _active[request_id]
	var args: Dictionary = context["args"]
	var cup: Node3D = _find_cup(str(args.get("cup_id", "")))
	if context["command"] == "brew" and status != "completed" and is_instance_valid(cup):
		cup.set_meta("state", "unfinished")
	if context["command"] == "look_at_entity":
		_attention_target = ""
		if not _channels.has("hands") and not _channels.has("movement"):
			_npc.clear_facing_target()
	if _animation_request == request_id:
		_animation_request = ""
		_animation.stop(true)
		_animation.speed_scale = 1.0
		if _animation.has_animation(&"idle"):
			_animation.play(&"idle")
	for channel: String in context["channels"]:
		if str(_channels.get(channel, "")) == request_id:
			_channels.erase(channel)
	# Emit after channel release; the result describes the actual current world.
	_emit(request_id, status, reason)
	_active.erase(request_id)
	_finished[request_id] = true


func _emit(request_id: String, status: String, reason: String = "") -> void:
	var context: Dictionary = _active.get(request_id, {})
	var args: Dictionary = context.get("args", {})
	# Never let an overlapping collection report the latest brewing cup's identity.
	var requested_cup_id: String = str(args.get("cup_id", ""))
	var cup_state: Dictionary = get_cup_state(requested_cup_id) if not requested_cup_id.is_empty() else {}
	var result: Dictionary = {
		"position": _vector_array(_npc.global_position), "body": get_body_state(),
		"cup": cup_state, "progress": context.get("progress", 0.0),
	}
	for key: String in ["anchor_id", "target_id", "station_id", "player_id"]:
		if args.has(key):
			result[key] = args[key]
	if str(context.get("command", "")) in ["brew", "place_cup", "collect_cup", "retrieve_cup"]:
		for key: String in ["cup_id", "order_id", "holder_id", "socket_id", "cup_ready", "state"]:
			result[key] = cup_state.get(key)
	var message: Dictionary = {
		"type": "action_feedback", "request_id": request_id, "npc_id": _npc.npc_id,
		"status": status, "result": result,
	}
	if not reason.is_empty():
		message["reason"] = reason
	feedback.emit(message)


func _reject(message: Dictionary, reason: String) -> void:
	var request_id: String = str(message.get("request_id", ""))
	if not request_id.is_empty():
		_finished[request_id] = true
	var args: Dictionary = message["args"] if message.get("args") is Dictionary else {}
	var requested_cup_id: String = str(args.get("cup_id", ""))
	var cup_state: Dictionary = get_cup_state(requested_cup_id) if not requested_cup_id.is_empty() else {}
	feedback.emit({
		"type": "action_feedback", "request_id": request_id,
		"npc_id": _npc.npc_id if is_instance_valid(_npc) else str(message.get("npc_id", "npc_owner")),
		"status": "failed", "reason": reason, "result": {"cup": cup_state},
	})


func _vector_array(value: Vector3) -> Array[float]:
	return [value.x, value.y, value.z]
