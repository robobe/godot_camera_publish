extends Node

var viewport: SubViewport
var channel := "camera1"
var transport := GpuFramePublisher.new()
var started := 0
var stopping := false
var exit_code := 0
var frame_limit := 0
var frames := 0


func _ready() -> void:
	if not transport.open(channel):
		get_tree().quit(1)
		return
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--gpu-frames="):
			frame_limit = int(argument.trim_prefix("--gpu-frames="))
	started = Time.get_ticks_usec()
	RenderingServer.frame_post_draw.connect(_publish)
	get_tree().auto_accept_quit = false


func _publish() -> void:
	if stopping:
		transport.close()
		get_tree().quit(exit_code)
		return
	var texture := RenderingServer.texture_get_rd_texture(viewport.get_texture().get_rid())
	if not transport.tick(texture):
		exit_code = 1
		stopping = true
	frames += 1
	if frame_limit > 0 and frames >= frame_limit:
		stopping = true
	var now := Time.get_ticks_usec()
	if now - started >= 1000000:
		var text := transport.stats()
		print(channel, " | ", text)
		started = now


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST:
		# One more rendered frame submits the last queued GPU copy before resource cleanup.
		stopping = true


func _exit_tree() -> void:
	if RenderingServer.frame_post_draw.is_connected(_publish):
		RenderingServer.frame_post_draw.disconnect(_publish)
	transport.close()
