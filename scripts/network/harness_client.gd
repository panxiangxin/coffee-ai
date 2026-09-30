extends Node
## One JSON WebSocket connection. Failures remain visible until an explicit reconnect.

signal message_received(message: Dictionary)
signal status_changed(status: String)

@export var endpoint: String = "ws://127.0.0.1:8765/ws/cafe"
@export var connect_timeout: float = 6.0

var accepted: bool = false
var _socket: WebSocketPeer = WebSocketPeer.new()
var _connecting: bool = false
var _connected: bool = false
var _elapsed: float = 0.0
var _registration: Dictionary = {}


func connect_to_harness(registration: Dictionary) -> void:
	_socket.close()
	_socket = WebSocketPeer.new()
	accepted = false
	_connected = false
	_registration = registration
	_elapsed = 0.0
	var error: Error = _socket.connect_to_url(endpoint)
	_connecting = error == OK
	status_changed.emit("连接中…" if _connecting else "连接失败：%s" % error_string(error))


func send_message(message: Dictionary) -> bool:
	if _socket.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return false
	var error: Error = _socket.send_text(JSON.stringify(message))
	if error != OK:
		status_changed.emit("发送失败：%s" % error_string(error))
	return error == OK


func _process(delta: float) -> void:
	if not _connecting and not _connected:
		return
	_socket.poll()
	_elapsed += delta
	var state: int = _socket.get_ready_state()
	if state == WebSocketPeer.STATE_OPEN:
		if not _connected:
			_connected = true
			_connecting = false
			status_changed.emit("注册场景…")
			send_message(_registration)
		while _socket.get_available_packet_count() > 0:
			var packet: String = _socket.get_packet().get_string_from_utf8()
			var json := JSON.new()
			if json.parse(packet) != OK or not json.data is Dictionary:
				status_changed.emit("后端消息格式错误")
				continue
			var message: Dictionary = json.data
			if message.get("type", "") == "scene_accepted":
				accepted = true
				status_changed.emit("已连接")
			message_received.emit(message)
	elif state == WebSocketPeer.STATE_CLOSED or (_connecting and _elapsed > connect_timeout):
		var reason: String = _socket.get_close_reason()
		_socket.close()
		_connecting = false
		_connected = false
		accepted = false
		status_changed.emit("未连接 · 启动后端后点重连" if reason.is_empty() else "连接关闭：" + reason)


func _exit_tree() -> void:
	_socket.close()
