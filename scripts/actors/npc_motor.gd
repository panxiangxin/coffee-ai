extends CharacterBody3D
## Owns translation and Visual yaw; never infers arrival from a timer or path end.

signal motion_event(request_id: String, status: String, reason: String)

@export var npc_id: String = "npc_owner"
@export var walk_speed: float = 1.8
@export var gravity_acceleration: float = 9.8
@export var arrival_radius: float = 0.2
@export var height_tolerance: float = 0.4
@export var path_height_offset: float = 0.2
@export var turn_speed: float = 8.0
@export var max_move_seconds: float = 25.0
@export var blocked_seconds: float = 3.0

@onready var navigation: NavigationAgent3D = $NavigationAgent3D
@onready var visual: Node3D = $Visual
@onready var _locomotion: AnimationPlayer = get_node_or_null("LocomotionPlayer") as AnimationPlayer

var _anchors: Dictionary = {}
var _request_id: String = ""
var _anchor_id: String = ""
var _goal: Vector3 = Vector3.ZERO
var _paused: bool = false
var _needs_target: bool = false
var _elapsed: float = 0.0
var _stuck_time: float = 0.0
var _last_progress_position: Vector3 = Vector3.ZERO
var _facing_point: Vector3 = Vector3.ZERO
var _has_facing_point: bool = false


func _ready() -> void:
	collision_layer = 4
	collision_mask = 1
	navigation.avoidance_enabled = false
	# Subtract the baked surface elevation so path waypoints align with the feet.
	navigation.path_height_offset = path_height_offset
	navigation.path_desired_distance = 0.15
	navigation.target_desired_distance = arrival_radius * 0.75


func setup(anchors: Dictionary) -> void:
	_anchors = anchors


func start_move(request_id: String, anchor_id: String) -> bool:
	if not _request_id.is_empty() or request_id.is_empty():
		return false
	var anchor: Node3D = _anchors.get(anchor_id) as Node3D
	if not is_instance_valid(anchor) or not bool(anchor.get_meta("available", true)):
		return false
	_request_id = request_id
	_anchor_id = anchor_id
	_goal = anchor.global_position
	_paused = false
	_needs_target = true
	_elapsed = 0.0
	_stuck_time = 0.0
	_last_progress_position = global_position
	_has_facing_point = false
	motion_event.emit(_request_id, "started", "")
	return true


func pause_move(request_id: String) -> void:
	if request_id != _request_id or _request_id.is_empty() or _paused:
		return
	_paused = true
	velocity.x = 0.0
	velocity.z = 0.0
	motion_event.emit(_request_id, "paused", "")


func resume_move(request_id: String) -> void:
	if request_id != _request_id or _request_id.is_empty() or not _paused:
		return
	var anchor: Node3D = _anchors.get(_anchor_id) as Node3D
	if not is_instance_valid(anchor) or not bool(anchor.get_meta("available", true)):
		_finish("failed", "NO_ANCHOR")
		return
	_goal = anchor.global_position
	_paused = false
	_needs_target = true
	_stuck_time = 0.0
	_last_progress_position = global_position
	motion_event.emit(_request_id, "resumed", "")


func cancel_move(request_id: String) -> void:
	if request_id == _request_id and not _request_id.is_empty():
		_finish("cancelled", "")


func fail_move(request_id: String, reason: String) -> void:
	if request_id == _request_id and not _request_id.is_empty():
		_finish("failed", reason)


func face_toward(world_position: Vector3) -> void:
	_facing_point = world_position
	_has_facing_point = true


func clear_facing_target() -> void:
	_has_facing_point = false


func get_facing_direction() -> Vector3:
	return -visual.global_basis.z.normalized()


func get_body_state() -> Dictionary:
	return {
		"movement": "paused" if _paused else ("moving" if not _request_id.is_empty() else "idle"),
		"movement_request_id": _request_id,
		"target_anchor_id": _anchor_id,
		"speed": Vector2(velocity.x, velocity.z).length(),
	}


func _physics_process(delta: float) -> void:
	var desired: Vector3 = Vector3.ZERO
	if not is_on_floor():
		velocity.y -= gravity_acceleration * delta
	else:
		velocity.y = 0.0
	if not _request_id.is_empty() and not _paused:
		desired = _advance_move(delta)
	velocity.x = desired.x
	velocity.z = desired.z
	move_and_slide()
	var horizontal: Vector3 = Vector3(velocity.x, 0.0, velocity.z)
	if horizontal.length_squared() > 0.0025:
		_turn_to(horizontal, delta)
	elif _request_id.is_empty() and _has_facing_point:
		_turn_to(_facing_point - global_position, delta)
	_update_locomotion(horizontal.length())


func _advance_move(delta: float) -> Vector3:
	_elapsed += delta
	var anchor: Node3D = _anchors.get(_anchor_id) as Node3D
	if not is_instance_valid(anchor) or not bool(anchor.get_meta("available", true)):
		_finish("failed", "NO_ANCHOR")
		return Vector3.ZERO
	if _elapsed > max_move_seconds:
		_finish("failed", "MOVE_TIMEOUT")
		return Vector3.ZERO
	var nav_map: RID = navigation.get_navigation_map()
	if not nav_map.is_valid() or NavigationServer3D.map_get_iteration_id(nav_map) == 0:
		return Vector3.ZERO
	if _needs_target:
		navigation.target_position = _goal
		_needs_target = false
	# Query exactly once per physics frame while this move is active.
	var next_point: Vector3 = navigation.get_next_path_position()
	var remaining: Vector3 = _goal - global_position
	var height_ok: bool = absf(remaining.y) <= height_tolerance
	remaining.y = 0.0
	if remaining.length() <= arrival_radius and height_ok:
		_finish("completed", "")
		return Vector3.ZERO
	if navigation.is_navigation_finished():
		_finish("failed", "TARGET_UNREACHABLE")
		return Vector3.ZERO
	var progress: Vector3 = global_position - _last_progress_position
	progress.y = 0.0
	if progress.length() >= 0.06:
		_stuck_time = 0.0
		_last_progress_position = global_position
	else:
		_stuck_time += delta
	if _stuck_time > blocked_seconds:
		_finish("failed", "BLOCKED")
		return Vector3.ZERO
	var offset: Vector3 = next_point - global_position
	offset.y = 0.0
	if offset.length_squared() < 0.000001:
		return Vector3.ZERO
	var speed: float = minf(walk_speed, offset.length() / maxf(delta, 0.001))
	return offset.normalized() * speed


func _turn_to(direction: Vector3, delta: float) -> void:
	direction.y = 0.0
	if direction.length_squared() < 0.000001:
		return
	visual.global_rotation.y = lerp_angle(
		visual.global_rotation.y, atan2(-direction.x, -direction.z), minf(turn_speed * delta, 1.0)
	)


func _update_locomotion(speed: float) -> void:
	if not is_instance_valid(_locomotion):
		return
	var animation_name: StringName = &"walk" if speed > 0.05 else &"idle"
	if _locomotion.has_animation(animation_name) and _locomotion.current_animation != animation_name:
		_locomotion.play(animation_name)


func _finish(status: String, reason: String) -> void:
	var finished_id: String = _request_id
	_request_id = ""
	_anchor_id = ""
	_paused = false
	_needs_target = false
	velocity.x = 0.0
	velocity.z = 0.0
	motion_event.emit(finished_id, status, reason)
