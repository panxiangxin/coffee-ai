extends Node3D

@export var mouse_sensitivity: float = 0.004
@onready var arm: SpringArm3D = $SpringArm3D
@onready var camera: Camera3D = $SpringArm3D/Camera3D
var target: Node3D


func follow(player: Node3D) -> void:
	target = player
	global_position = player.global_position + Vector3(0, 1.4, 0)


func _process(delta: float) -> void:
	if is_instance_valid(target):
		global_position = global_position.lerp(
			target.global_position + Vector3(0, 1.4, 0), 1.0 - exp(-12.0 * delta)
		)


func _unhandled_input(event: InputEvent) -> void:
	if camera.current and event is InputEventMouseMotion and Input.is_mouse_button_pressed(MOUSE_BUTTON_RIGHT):
		rotation.y -= event.relative.x * mouse_sensitivity
		arm.rotation.x = clampf(arm.rotation.x - event.relative.y * mouse_sensitivity, -1.1, -0.1)
		get_viewport().set_input_as_handled()
