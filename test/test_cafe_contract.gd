extends "res://addons/gut/test.gd"
## Saved-scene contract checks, run through Godot MCP Native's run_project_test.
## Physical movement and Harness/model flows still need MCP runtime input.

const ObservationScript = preload("res://scripts/world/observation_exporter.gd")
const NPC_SCRIPT_PATH: String = "res://scripts/actors/npc_motor.gd"
const PLAYER_SCRIPT_PATH: String = "res://scripts/actors/test_player.gd"
const FACILITY_PATHS: Dictionary = {
	"machine_01": "Navigation/Geometry/CoffeeMachine",
	"counter_01": "Navigation/Geometry/Counter",
	"table_1": "Navigation/Geometry/Table1",
	"table_2": "Navigation/Geometry/Table2",
	"menu_board": "Navigation/Geometry/MenuBoard",
}
const REQUIRED_ANCHORS: Array[String] = [
	"entrance", "counter", "coffee_machine", "pickup_spot",
	"menu_board", "table_1", "table_2", "demo_spot", "recycle_spot",
]

var _root: Node
var _npc: Node3D
var _players: Dictionary = {}


func before_all() -> void:
	var scene: PackedScene = load("res://scenes/cafe_world.tscn") as PackedScene
	if scene == null:
		return
	_root = scene.instantiate()
	# Inspect the saved scene without starting its Harness connection or gameplay loop.
	_root.set_script(null)
	_root.process_mode = Node.PROCESS_MODE_DISABLED
	add_child(_root)


func after_all() -> void:
	if is_instance_valid(_root):
		_root.free()
	_root = null
	_npc = null
	_players.clear()


func _prepare_scene() -> bool:
	if _root == null or _root.scene_file_path != "res://scenes/cafe_world.tscn":
		fail_test("Could not instantiate res://scenes/cafe_world.tscn.")
		return false
	_players.clear()
	_npc = null
	for node: Node in _all_nodes(_root):
		var script: Script = node.get_script() as Script
		if script == null:
			continue
		if script.resource_path == NPC_SCRIPT_PATH:
			_npc = node as Node3D
		elif script.resource_path == PLAYER_SCRIPT_PATH:
			var player_id: String = str(node.get("player_id"))
			if _players.has(player_id):
				fail_test("Duplicate player_id in the scene: " + player_id)
				return false
			_players[player_id] = node
	if _npc == null:
		fail_test("The scene must contain the NPC motor before testing the full contract.")
		return false
	return true


func test_navigation_is_baked_and_obstacles_have_collision() -> void:
	if not _prepare_scene():
		return
	var region: NavigationRegion3D = _root.get_node_or_null("Navigation") as NavigationRegion3D
	assert_true(region != null, "Navigation must be a NavigationRegion3D.")
	if region == null:
		return
	var mesh: NavigationMesh = region.navigation_mesh
	assert_true(mesh != null, "NavigationRegion3D must have its saved NavigationMesh.")
	if mesh != null:
		assert_true(mesh.get_polygon_count() > 0, "The delivered scene needs baked polygons, not empty bake settings.")
		assert_true(mesh.get_vertices().size() > 0, "Baked navigation vertices must persist.")
		assert_true(mesh.agent_radius > 0.3, "Navigation clearance must cover the NPC collision radius.")
	for node_name: String in ["Floor", "BackWall", "LeftWall", "RightWall", "Counter", "Table1", "Table2"]:
		var body: StaticBody3D = _root.get_node_or_null("Navigation/Geometry/" + node_name) as StaticBody3D
		assert_true(body != null, node_name + " needs a real static collision body.")
		if body == null:
			continue
		assert_true((body.collision_layer & 1) != 0, node_name + " must participate in environment collision.")
		var shape: CollisionShape3D = body.get_node_or_null("Shape") as CollisionShape3D
		assert_true(shape != null and shape.shape != null and not shape.disabled, node_name + " needs an enabled collision shape.")


func test_actor_identities_and_action_resources() -> void:
	if not _prepare_scene():
		return
	assert_true(_npc is CharacterBody3D, "NPC movement requires CharacterBody3D.")
	assert_true(_npc.get_node_or_null("NavigationAgent3D") is NavigationAgent3D)
	assert_true(_npc.get_node_or_null("Visual/Head") is Node3D, "Gaze needs an actual head node.")
	assert_true(_npc.get_node_or_null("Visual/RightArm/HandSocket") is Node3D)
	assert_true(_players.size() >= 2 and _players.size() <= 8, "Keep 2–8 stable test identities.")
	assert_true(_players.has("p_A"), "Missing player p_A.")
	assert_true(_players.has("p_B"), "Missing player p_B.")
	for player_id: String in _players:
		var player: Node3D = _players[player_id] as Node3D
		assert_false(player_id.is_empty(), "A player's stable identity cannot be empty.")
		assert_true(player is CharacterBody3D, player_id + " needs real movement.")
		assert_true(player.get_node_or_null("CarrySocket") is Marker3D, player_id + " needs a cup socket.")
	var animator: AnimationPlayer = _npc.get_node_or_null("AnimationPlayer") as AnimationPlayer
	assert_true(animator != null, "The physical action driver needs its AnimationPlayer.")
	if animator == null:
		return
	for clip_name: StringName in [&"brew", &"serve", &"tidy", &"greet", &"point"]:
		assert_true(animator.has_animation(clip_name), "Missing action animation: " + str(clip_name))
		if not animator.has_animation(clip_name):
			continue
		var clip: Animation = animator.get_animation(clip_name)
		assert_true(clip.length > 0.0, str(clip_name) + " must have a duration.")
		assert_true(clip.get_track_count() > 0, str(clip_name) + " must change real properties.")
		assert_eq(clip.loop_mode, Animation.LOOP_NONE, str(clip_name) + " must emit a natural completion.")
		_check_animation_targets(animator, clip, str(clip_name))
	var locomotion: AnimationPlayer = _npc.get_node_or_null("LocomotionPlayer") as AnimationPlayer
	assert_true(locomotion != null, "Movement animation has its own controller.")
	if locomotion != null:
		for clip_name: StringName in [&"idle", &"walk"]:
			assert_true(locomotion.has_animation(clip_name), "Missing locomotion animation: " + str(clip_name))


func test_workpoints_zone_and_camera_exist() -> void:
	if not _prepare_scene():
		return
	for anchor_id: String in REQUIRED_ANCHORS:
		assert_true(_root.get_node_or_null("Anchors/" + anchor_id) is Marker3D, "Missing workpoint: " + anchor_id)
	assert_true(_root.get_node_or_null("Sockets/Pickup") is Marker3D)
	assert_true(_root.get_node_or_null("Sockets/Recycle") is Marker3D)
	var zone: Area3D = _root.get_node_or_null("CafeZone") as Area3D
	assert_true(zone != null, "Visits must use a real CafeZone.")
	if zone != null:
		assert_true(zone.monitoring and (zone.collision_mask & 2) != 0, "CafeZone must detect players.")
	assert_true(_root.get_node_or_null("CameraRig/SpringArm3D") is SpringArm3D)
	assert_true(_root.get_node_or_null("CameraRig/SpringArm3D/Camera3D") is Camera3D)
	assert_true(_root.get_node_or_null("OverviewCamera") is Camera3D)
	var workpoint: Node3D = _root.get_node_or_null("Anchors/coffee_machine") as Node3D
	var machine: Node3D = _root.get_node_or_null(FACILITY_PATHS["machine_01"]) as Node3D
	if workpoint != null and machine != null:
		var separation: Vector3 = workpoint.global_position - machine.global_position
		assert_true(Vector2(separation.x, separation.z).length() > 0.3, "The machine workpoint must not be its model center.")


func test_registration_exports_real_scene_positions_and_socket_ids() -> void:
	if not _prepare_scene():
		return
	var entities: Dictionary = {}
	for entity_id: String in FACILITY_PATHS:
		var node: Node3D = _root.get_node_or_null(FACILITY_PATHS[entity_id]) as Node3D
		assert_true(node != null, "Missing registered facility: " + entity_id)
		if node != null:
			entities[entity_id] = node
	var anchors: Dictionary = {}
	for anchor_id: String in REQUIRED_ANCHORS:
		var anchor: Node3D = _root.get_node_or_null("Anchors/" + anchor_id) as Node3D
		if anchor != null:
			anchors[anchor_id] = anchor
	var exporter = ObservationScript.new()
	exporter.setup(_npc, _players, entities, anchors, _root.get_node_or_null("WorldActionDriver"))
	exporter.set_pickup_socket(_root.get_node_or_null("Sockets/Pickup") as Marker3D)
	exporter.set_recycle_socket(_root.get_node_or_null("Sockets/Recycle") as Marker3D)
	var registration: Dictionary = exporter.make_registration()
	exporter.free()
	var decoded: Variant = JSON.parse_string(JSON.stringify(registration))
	assert_true(decoded is Dictionary, "The registration must be ordinary serializable JSON.")
	assert_eq(registration.get("type"), "scene_ready")
	assert_eq(registration.get("protocol"), "cafe3d.v1")
	assert_eq(registration.get("npc_id"), "npc_owner")
	assert_eq(registration.get("character_id"), "owner_01")
	assert_eq(registration.get("units"), "meters")
	assert_eq(registration["entities"].size(), 5)
	assert_eq(registration["anchors"].size(), REQUIRED_ANCHORS.size())
	for row: Dictionary in registration["anchors"]:
		var anchor_id: String = str(row["anchor_id"])
		var position: Array = row["work_position"]
		assert_eq(position.size(), 3)
		if position.size() == 3 and anchors.has(anchor_id):
			var actual := Vector3(position[0], position[1], position[2])
			assert_true(actual.is_equal_approx(anchors[anchor_id].global_position), anchor_id + " must export its real workpoint.")
	var socket_ids: Array[String] = []
	for row: Dictionary in registration["sockets"]:
		socket_ids.append(str(row["socket_id"]))
	assert_true(socket_ids.has("pickup_socket"), "Missing pickup_socket.")
	assert_true(socket_ids.has("recycle_socket"), "Missing recycle_socket.")
	assert_true(socket_ids.has("npc_hand_socket"), "Missing npc_hand_socket.")
	for player_id: String in _players:
		assert_true(socket_ids.has(player_id + "_carry_socket"), "Missing carry socket for " + player_id)
	for command: String in ["move_to_anchor", "look_at_entity", "gesture", "brew", "place_cup", "retrieve_cup", "collect_cup", "tidy"]:
		assert_true(registration["supported_commands"].has(command), "Missing command: " + command)


func _all_nodes(parent: Node) -> Array[Node]:
	var nodes: Array[Node] = [parent]
	for child: Node in parent.get_children():
		nodes.append_array(_all_nodes(child))
	return nodes


func _check_animation_targets(animator: AnimationPlayer, clip: Animation, clip_name: String) -> void:
	var animation_root: Node = animator.get_node_or_null(animator.root_node)
	assert_true(animation_root != null, "Animation root is missing for " + clip_name)
	if animation_root == null:
		return
	for index: int in range(clip.get_track_count()):
		var track_path: NodePath = clip.track_get_path(index)
		var node_path := NodePath(str(track_path.get_concatenated_names()))
		assert_true(animation_root.get_node_or_null(node_path) != null, clip_name + " has a broken track: " + str(track_path))
