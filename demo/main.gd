extends Node

const CameraPublisher = preload("res://camera_publisher.gd")
const WIDTH := 640
const HEIGHT := 480
const CAMERA_CHANNELS := ["camera1_a", "camera1_b", "camera2", "camera3"]
const CAMERA_OFFSETS := [0.0, 1.0, 2.0]
const CAMERA_POSITION := Vector3(4, 3, 6)
const CAMERA_RIGHT := Vector3(0.83205, 0, -0.55470)

var cubes: Array[MeshInstance3D] = []
var elapsed := 0.0


func _ready() -> void:
	# Keep the rendered views offscreen to avoid a second presentation path through X11.
	RenderingServer.render_loop_enabled = false
	RenderingServer.viewport_set_update_mode(get_tree().root.get_viewport_rid(),
		RenderingServer.VIEWPORT_UPDATE_DISABLED)
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_MINIMIZED)
	var views: Array[SubViewport] = []
	for offset in CAMERA_OFFSETS:
		views.append(_build_camera(offset))
	_start_publisher(views[0], CAMERA_CHANNELS[0])
	_start_publisher(views[0], CAMERA_CHANNELS[1])
	_start_publisher(views[1], CAMERA_CHANNELS[2])
	_start_publisher(views[2], CAMERA_CHANNELS[3])


func _build_camera(offset: float) -> SubViewport:
	var viewport := SubViewport.new()
	viewport.size = Vector2i(WIDTH, HEIGHT)
	viewport.own_world_3d = true
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	add_child(viewport)
	var environment := WorldEnvironment.new()
	environment.environment = Environment.new()
	environment.environment.background_mode = Environment.BG_COLOR
	environment.environment.background_color = Color("819eb5")
	environment.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.environment.ambient_light_color = Color.WHITE
	environment.environment.ambient_light_energy = 0.5
	viewport.add_child(environment)
	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-55, -30, 0)
	light.shadow_enabled = true
	viewport.add_child(light)
	var floor_mesh := BoxMesh.new()
	floor_mesh.size = Vector3(12, 0.2, 12)
	_add_mesh(viewport, floor_mesh, Vector3(0, -0.1, 0), Color("71885e"))
	cubes.append(_add_mesh(viewport, BoxMesh.new(), Vector3(0, 1, 0), Color("e85b42")))
	_add_mesh(viewport, SphereMesh.new(), Vector3(-2, 0.5, 0), Color("27c7d9"))
	var marker_mesh := BoxMesh.new()
	marker_mesh.size = Vector3(0.3, 2.5, 0.3)
	_add_mesh(viewport, marker_mesh, Vector3(2, 1.25, -1), Color("ffcf40"))
	var camera := Camera3D.new()
	camera.keep_aspect = Camera3D.KEEP_WIDTH
	camera.fov = 90
	camera.position = CAMERA_POSITION + CAMERA_RIGHT * offset
	viewport.add_child(camera)
	camera.look_at(Vector3(0, 0.8, 0) + CAMERA_RIGHT * offset)
	camera.current = true
	var left_ray := camera.project_ray_normal(Vector2(0, HEIGHT / 2.0))
	var right_ray := camera.project_ray_normal(Vector2(WIDTH, HEIGHT / 2.0))
	assert(abs(rad_to_deg(left_ray.angle_to(right_ray)) - 90.0) < 0.01,
		"Camera must have a 90-degree horizontal field of view")
	return viewport


func _add_mesh(viewport: SubViewport, mesh: Mesh, position: Vector3,
		color: Color) -> MeshInstance3D:
	var instance := MeshInstance3D.new()
	instance.mesh = mesh
	instance.position = position
	var material := StandardMaterial3D.new()
	material.albedo_color = color
	instance.material_override = material
	viewport.add_child(instance)
	return instance


func _start_publisher(view: SubViewport, channel: String) -> void:
	var publisher := CameraPublisher.new()
	publisher.viewport = view
	publisher.channel = channel
	add_child(publisher)


func _process(delta: float) -> void:
	elapsed += delta
	for cube in cubes:
		cube.rotation = Vector3(elapsed * 0.35, elapsed, 0)
		cube.position.y = 1.1 + sin(elapsed * 2.0) * 0.35
	RenderingServer.force_draw(false, delta)
