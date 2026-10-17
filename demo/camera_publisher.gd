extends Node

signal stats_updated(text: String)

var viewport: SubViewport
var camera_id := 1
var transport: ZmqImagePublisher
var render_frame := 0
var published := 0
var dropped := 0
var capture_usec := 0
var stats_started := 0


func _ready() -> void:
	stats_started = Time.get_ticks_usec()
	RenderingServer.frame_post_draw.connect(_publish)


func _process(_delta: float) -> void:
	var now := Time.get_ticks_usec()
	if now - stats_started >= 1000000:
		var seconds := (now - stats_started) / 1000000.0
		var average_ms := capture_usec / 1000.0 / published if published > 0 else 0.0
		var text := "Camera %d | Submitted: %.1f FPS | capture + copy: %.2f ms\nPublish failures: %d | %s" % [
			camera_id, published / seconds, average_ms, dropped,
			"ZeroMQ PUB, no delivery ACK"]
		stats_updated.emit(text)
		print(text.replace("\n", " | "))
		published = 0
		dropped = 0
		capture_usec = 0
		stats_started = now


func _publish() -> void:
	render_frame += 1
	var started := Time.get_ticks_usec()
	var image := viewport.get_texture().get_image()
	if image == null or image.is_empty():
		return
	image.convert(Image.FORMAT_RGB8)
	if transport.publish_frame(camera_id, image.get_data(), render_frame, started):
		capture_usec += Time.get_ticks_usec() - started
		published += 1
	else:
		dropped += 1


func _exit_tree() -> void:
	if RenderingServer.frame_post_draw.is_connected(_publish):
		RenderingServer.frame_post_draw.disconnect(_publish)
