@tool
extends Node
## Exports Godot facts. Harness alone owns visits, interpretation and remembered locations.

const ActionDriverScript = preload("res://scripts/actions/world_action_driver.gd")
const NPC_ID: String = "npc_owner"
const CHARACTER_ID: String = "owner_01"
const ROOM_ID: String = "cafe_demo"
const SCENE_ID: String = "cafe_3d_01"
const ENTITY_KINDS: Dictionary = {
	"machine_01": "coffee_station", "counter_01": "counter",
	"table_1": "table", "table_2": "table", "menu_board": "menu",
}
const ANCHOR_ENTITIES: Dictionary = {
	"coffee_machine": "machine_01", "counter": "counter_01",
	"pickup_spot": "counter_01", "recycle_spot": "counter_01", "menu_board": "menu_board",
	"table_1": "table_1", "table_2": "table_2",
}

@export var visual_range: float = 8.0
@export_range(1.0, 360.0) var field_of_view_degrees: float = 120.0
@export var hearing_range: float = 6.0
@export var occlusion_enabled: bool = true
@export_flags_3d_physics var occlusion_mask: int = 1

var _npc: Node3D
var _players: Dictionary = {}
var _entities: Dictionary = {}
var _anchors: Dictionary = {}
var _driver: Node
var _pickup_socket: Marker3D
var _recycle_socket: Marker3D
var _snapshot_id: int = 0
var _perception: Dictionary = {
	"observer_id": NPC_ID, "visible_ids": [], "audible_player_ids": [],
	"sample_time_ms": -1,
}


func setup(
	npc: Node3D, players: Dictionary, entities: Dictionary,
	anchors: Dictionary, driver: Node
) -> void:
	_npc = npc
	_players = players
	_entities = entities
	_anchors = anchors
	_driver = driver


func set_pickup_socket(socket: Marker3D) -> void:
	_pickup_socket = socket


func set_recycle_socket(socket: Marker3D) -> void:
	_recycle_socket = socket


func make_registration() -> Dictionary:
	var entity_rows: Array[Dictionary] = []
	for entity_id: String in _entities:
		var entity: Node3D = _entities[entity_id] as Node3D
		if is_instance_valid(entity):
			entity_rows.append(_facility_state(entity_id, entity))
	var anchor_rows: Array[Dictionary] = []
	for anchor_id: String in _anchors:
		var anchor: Node3D = _anchors[anchor_id] as Node3D
		if not is_instance_valid(anchor):
			continue
		var entity_id: String = str(ANCHOR_ENTITIES.get(anchor_id, ""))
		var entity: Node3D = _entities.get(entity_id) as Node3D
		var look_position: Vector3 = anchor.global_position - anchor.global_basis.z + Vector3.UP * 1.2
		if is_instance_valid(entity):
			look_position = entity.global_position
		if anchor.has_meta("look_position"):
			look_position = anchor.get_meta("look_position")
		var row: Dictionary = {
			"anchor_id": anchor_id,
			"work_position": _vector_array(anchor.global_position),
			"look_position": _vector_array(look_position),
			"available": bool(anchor.get_meta("available", true)),
		}
		if not entity_id.is_empty():
			row["entity_id"] = entity_id
		anchor_rows.append(row)
	return {
		"type": "scene_ready", "protocol": "cafe3d.v1", "room_id": ROOM_ID,
		"scene_id": SCENE_ID, "npc_id": NPC_ID, "character_id": CHARACTER_ID,
		"units": "meters", "entities": entity_rows, "anchors": anchor_rows,
		"sockets": _socket_registration(),
		"supported_commands": ActionDriverScript.SUPPORTED_COMMANDS.duplicate(),
	}


## The caller samples from _physics_process so direct_space_state is safe to read.
func sample_snapshot() -> Dictionary:
	if not is_instance_valid(_npc) or not is_instance_valid(_driver):
		push_error("ObservationExporter.setup requires an NPC and action driver before sampling.")
		return {}
	var sampled_at: int = Time.get_ticks_msec()
	var eye_position: Vector3 = _eye_position()
	var eye_forward: Vector3 = _eye_forward()
	var visible_ids: Array[String] = []
	var audible_ids: Array[String] = []
	var body: Dictionary = _driver.call("get_body_state")
	var rows: Array[Dictionary] = [{
		"entity_id": NPC_ID, "kind": "npc", "position": _vector_array(_npc.global_position),
		"forward": _vector_array(eye_forward), "body": body.duplicate(true),
	}]
	for player_id: String in _players:
		var player: Node3D = _players[player_id] as Node3D
		if not is_instance_valid(player):
			continue
		rows.append({
			"entity_id": player_id, "player_id": player_id, "kind": "player",
			"display_name": str(player.get("display_name")),
			"position": _vector_array(player.global_position),
			"forward": _vector_array(_node_forward(player)),
			"inside_cafe": bool(player.get("inside_cafe")),
		})
		if _can_see(eye_position, eye_forward, _player_eye(player), player):
			visible_ids.append(player_id)
		if can_hear(player_id, "local"):
			audible_ids.append(player_id)
	for entity_id: String in _entities:
		var entity: Node3D = _entities[entity_id] as Node3D
		if not is_instance_valid(entity):
			continue
		rows.append(_facility_state(entity_id, entity))
		if _can_see(eye_position, eye_forward, entity.global_position, entity):
			visible_ids.append(entity_id)
	var cups: Array = _driver.call("get_cup_states")
	for cup: Dictionary in cups:
		var cup_id: String = str(cup.get("cup_id", ""))
		if cup_id.is_empty():
			continue
		var row: Dictionary = cup.duplicate(true)
		row["entity_id"] = cup_id
		row["kind"] = "cup"
		rows.append(row)
		var position: Array = cup.get("position", [])
		if position.size() == 3:
			var cup_position := Vector3(float(position[0]), float(position[1]), float(position[2]))
			if _can_see(eye_position, eye_forward, cup_position):
				visible_ids.append(cup_id)
	_snapshot_id += 1
	_perception = {
		"observer_id": NPC_ID, "visible_ids": visible_ids,
		"audible_player_ids": audible_ids, "sample_time_ms": sampled_at,
		"eye_position": _vector_array(eye_position), "eye_forward": _vector_array(eye_forward),
		"occlusion_enabled": occlusion_enabled,
	}
	return {
		"type": "world_snapshot", "room_id": ROOM_ID,
		"snapshot_id": _snapshot_id, "observed_ms": sampled_at,
		"entities": rows, "perception": get_perception(),
	}


func can_hear(player_id: String, channel: String = "local") -> bool:
	var player: Node3D = _players.get(player_id) as Node3D
	if not is_instance_valid(player) or not is_instance_valid(_npc):
		return false
	if channel == "room":
		return bool(player.get("inside_cafe"))
	if channel != "local":
		return false
	return _npc.global_position.distance_to(player.global_position) <= hearing_range


func get_perception() -> Dictionary:
	return _perception.duplicate(true)


func _facility_state(entity_id: String, entity: Node3D) -> Dictionary:
	return {
		"entity_id": entity_id, "kind": str(ENTITY_KINDS.get(entity_id, "facility")),
		"position": _vector_array(entity.global_position),
		"forward": _vector_array(-entity.global_basis.z.normalized()),
		"available": bool(entity.get_meta("available", true)),
	}


func _socket_registration() -> Array[Dictionary]:
	var rows: Array[Dictionary] = []
	if is_instance_valid(_pickup_socket):
		rows.append(_socket_row("pickup_socket", "counter_01", _pickup_socket))
	if is_instance_valid(_recycle_socket):
		rows.append(_socket_row("recycle_socket", "counter_01", _recycle_socket))
	if is_instance_valid(_npc):
		var hand: Node3D = _npc.get_node_or_null("Visual/RightArm/HandSocket") as Node3D
		if is_instance_valid(hand):
			rows.append(_socket_row("npc_hand_socket", NPC_ID, hand))
	for player_id: String in _players:
		var player: Node3D = _players[player_id] as Node3D
		if not is_instance_valid(player):
			continue
		var carry: Node3D = player.get_node_or_null("CarrySocket") as Node3D
		if is_instance_valid(carry):
			rows.append(_socket_row(player_id + "_carry_socket", player_id, carry))
	return rows


func _socket_row(socket_id: String, entity_id: String, socket: Node3D) -> Dictionary:
	return {
		"socket_id": socket_id, "entity_id": entity_id,
		"position": _vector_array(socket.global_position),
	}


func _eye_position() -> Vector3:
	var head: Node3D = _npc.get_node_or_null("Visual/Head") as Node3D
	return head.global_position if is_instance_valid(head) else _npc.global_position + Vector3.UP * 1.6


func _eye_forward() -> Vector3:
	var head: Node3D = _npc.get_node_or_null("Visual/Head") as Node3D
	if is_instance_valid(head):
		return -head.global_basis.z.normalized()
	return _node_forward(_npc)


func _node_forward(node: Node3D) -> Vector3:
	# In editor-only contract checks, non-tool actor scripts do not execute.
	if not Engine.is_editor_hint() and node.has_method("get_facing_direction"):
		var facing: Vector3 = node.call("get_facing_direction")
		return facing.normalized()
	var visual: Node3D = node.get_node_or_null("Visual") as Node3D
	return -visual.global_basis.z.normalized() if is_instance_valid(visual) else -node.global_basis.z.normalized()


func _player_eye(player: Node3D) -> Vector3:
	var head: Node3D = player.get_node_or_null("Visual/Head") as Node3D
	return head.global_position if is_instance_valid(head) else player.global_position + Vector3.UP * 1.5


func _can_see(origin: Vector3, forward: Vector3, target: Vector3, target_node: Node3D = null) -> bool:
	var offset: Vector3 = target - origin
	if offset.length() > visual_range:
		return false
	var horizontal: Vector3 = Vector3(offset.x, 0.0, offset.z)
	var facing: Vector3 = Vector3(forward.x, 0.0, forward.z)
	if horizontal.length_squared() > 0.0001:
		if facing.length_squared() < 0.0001:
			return false
		if facing.normalized().dot(horizontal.normalized()) < cos(deg_to_rad(field_of_view_degrees * 0.5)):
			return false
	if not occlusion_enabled or offset.length_squared() < 0.0001:
		return true
	var query := PhysicsRayQueryParameters3D.create(origin, target, occlusion_mask)
	query.collide_with_areas = false
	if _npc is CollisionObject3D:
		query.exclude = [(_npc as CollisionObject3D).get_rid()]
	var hit: Dictionary = _npc.get_world_3d().direct_space_state.intersect_ray(query)
	if hit.is_empty():
		return true
	var collider: Node = hit.get("collider") as Node
	return is_instance_valid(target_node) and is_instance_valid(collider) and (
		collider == target_node or target_node.is_ancestor_of(collider)
	)


func _vector_array(value: Vector3) -> Array[float]:
	return [value.x, value.y, value.z]
