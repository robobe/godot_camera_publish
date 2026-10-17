extends Node

signal stats_updated(text: String)

const WIDTH := 640
const HEIGHT := 480

var viewport: SubViewport
var port := 5001
var buffer := SharedFrameBuffer.new()
var shm_name := ""
var server := TCPServer.new()
var peer: StreamPeerTCP
var outgoing := PackedByteArray()
var ack_bytes := PackedByteArray()
var awaiting_ack := false
var pending_frame := 0
var render_frame := 0
var published := 0
var capture_usec := 0
var stats_started := 0


func _ready() -> void:
	shm_name = buffer.open_buffer()
	if shm_name.is_empty():
		get_tree().quit(1)
		return
	var error := server.listen(port, "127.0.0.1")
	if error != OK:
		push_error("Cannot listen on 127.0.0.1:%d: %s" % [port, error_string(error)])
		buffer.close_buffer()
		get_tree().quit(1)
		return
	stats_started = Time.get_ticks_usec()
	RenderingServer.frame_post_draw.connect(_publish)
	print("Camera publisher listening on 127.0.0.1:%d; shared memory: %s" % [port, shm_name])
	stats_updated.emit("Camera %d: waiting for Python" % port)


func _process(_delta: float) -> void:
	_poll_connection()
	_flush_outgoing()
	var now := Time.get_ticks_usec()
	if now - stats_started >= 1000000:
		var seconds := (now - stats_started) / 1000000.0
		var average_ms := capture_usec / 1000.0 / published if published > 0 else 0.0
		var text := "Camera %d | Published: %.1f FPS | capture + copy: %.2f ms\n%s" % [
			port, published / seconds, average_ms, "Python connected" if peer != null else "Waiting for Python"]
		stats_updated.emit(text)
		print(text.replace("\n", " | "))
		published = 0
		capture_usec = 0
		stats_started = now


func _poll_connection() -> void:
	if peer == null and server.is_connection_available():
		peer = server.take_connection()
		peer.set_no_delay(true)
		var metadata := JSON.stringify({"version": 1, "shm": shm_name,
			"width": WIDTH, "height": HEIGHT, "format": "RGB8", "bytes": WIDTH * HEIGHT * 3}).to_utf8_buffer()
		outgoing.resize(4)
		outgoing.encode_u32(0, metadata.size())
		outgoing.append_array(metadata)
	if peer == null:
		return
	peer.poll()
	if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
		_disconnect()
		return
	var available := peer.get_available_bytes()
	if available == 0:
		return
	if not awaiting_ack or available + ack_bytes.size() > 8:
		push_warning("Invalid acknowledgment length")
		_disconnect()
		return
	var result := peer.get_data(available)
	if result[0] != OK:
		_disconnect()
		return
	ack_bytes.append_array(result[1])
	if ack_bytes.size() == 8:
		if ack_bytes.decode_u64(0) != pending_frame:
			push_warning("Invalid acknowledgment frame ID")
			_disconnect()
			return
		ack_bytes.clear()
		awaiting_ack = false


func _flush_outgoing() -> void:
	if peer == null or outgoing.is_empty():
		return
	var result := peer.put_partial_data(outgoing)
	if result[0] != OK:
		_disconnect()
		return
	outgoing = outgoing.slice(result[1])


func _publish() -> void:
	render_frame += 1
	if peer == null or awaiting_ack or not outgoing.is_empty():
		return
	var started := Time.get_ticks_usec()
	var image := viewport.get_texture().get_image()
	if image == null or image.is_empty():
		return
	image.convert(Image.FORMAT_RGB8)
	if not buffer.write_frame(image.get_data()):
		_disconnect()
		return
	capture_usec += Time.get_ticks_usec() - started
	published += 1
	pending_frame = render_frame
	awaiting_ack = true
	outgoing.resize(16)
	outgoing.encode_u64(0, pending_frame)
	outgoing.encode_u64(8, started)
	_flush_outgoing()


func _disconnect() -> void:
	if peer != null:
		peer.disconnect_from_host()
	peer = null
	outgoing.clear()
	ack_bytes.clear()
	awaiting_ack = false


func _exit_tree() -> void:
	if RenderingServer.frame_post_draw.is_connected(_publish):
		RenderingServer.frame_post_draw.disconnect(_publish)
	_disconnect()
	server.stop()
	buffer.close_buffer()
