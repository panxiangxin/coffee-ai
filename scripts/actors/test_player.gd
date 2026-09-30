extends CharacterBody3D
## A stable test identity. CafeWorld alone switches control and visit membership.

@export var player_id: String = "p_A"
@export var display_name: String = "顾客 A"
@export var controlled: bool = false
@export var input_enabled: bool = true
@export var inside_cafe: bool = false
@export var walk_speed: float = 3.0
@export var fast_speed: float = 5.0
@export var turn_speed: float = 12.0
@export var gravity_acceleration: float = 9.8

@onready var visual: Node3D = $Visual
@onready var carry_socket: Marker3D = $CarrySocket

var _camera: Camera3D


func _ready() -> void:
	collision_layer = 2
	collision_mask = 1
	var name_label: Label3D = $NameLabel
	name_label.text = display_name
	var torso: MeshInstance3D = $Visual/Torso
	var source_material: BaseMaterial3D = torso.get_active_material(0) as BaseMaterial3D
	if not is_instance_valid(source_material):
		push_error("Player torso requires a BaseMaterial3D: " + player_id)
		return
	# Each identity owns its override; imported scene materials remain shared and unchanged.
	var identity_material: BaseMaterial3D = source_material.duplicate() as BaseMaterial3D
	match player_id:
		"p_A": identity_material.albedo_color = Color("#627e9a")
		"p_B": identity_material.albedo_color = Color("#d18b47")
	torso.material_override = identity_material


func set_camera(camera: Camera3D) -> void:
	_camera = camera


func get_facing_direction() -> Vector3:
	return -visual.global_basis.z.normalized()


func _physics_process(delta: float) -> void:
	var direction: Vector3 = Vector3.ZERO
	var focus: Control = get_viewport().gui_get_focus_owner()
	var editing_text: bool = focus is LineEdit or focus is TextEdit
	if controlled and input_enabled and not editing_text:
		var input_axis: Vector2 = Input.get_vector(
			"move_left", "move_right", "move_forward", "move_back"
		)
		if is_instance_valid(_camera):
			var forward: Vector3 = -_camera.global_basis.z
			forward.y = 0.0
			# An exactly vertical overview camera still has a stable screen-up direction.
			if forward.length_squared() < 0.001:
				forward = _camera.global_basis.y
				forward.y = 0.0
			forward = forward.normalized()
			var right: Vector3 = forward.cross(Vector3.UP).normalized()
			direction = right * input_axis.x - forward * input_axis.y
		else:
			direction = Vector3(input_axis.x, 0.0, input_axis.y)
	var speed: float = fast_speed if Input.is_physical_key_pressed(KEY_SHIFT) else walk_speed
	velocity.x = direction.x * speed
	velocity.z = direction.z * speed
	if not is_on_floor():
		velocity.y -= gravity_acceleration * delta
	else:
		velocity.y = 0.0
	move_and_slide()
	var horizontal: Vector3 = Vector3(velocity.x, 0.0, velocity.z)
	if horizontal.length_squared() > 0.0025:
		visual.global_rotation.y = lerp_angle(
			visual.global_rotation.y,
			atan2(-horizontal.x, -horizontal.z),
			minf(turn_speed * delta, 1.0)
		)
