extends Node

const CameraPublisher = preload("res://camera_publisher.gd")
const WIDTH := 640
const HEIGHT := 480
# Offset in the first camera's coordinates: +X is right, +Y is up, -Z is forward.
const SECOND_CAMERA_OFFSET := Vector3(1, 0, 0)
const THIRD_CAMERA_OFFSET := Vector3(2, 0, 0)

var viewport: SubViewport
var second_viewport: SubViewport
var third_viewport: SubViewport
var cube: MeshInstance3D
var elapsed := 0.0
var transport := ZenohImagePublisher.new()


func _ready() -> void:
	if not transport.open():
		get_tree().quit(1)
		return
	_build_scene()
	_start_publisher(viewport, 1, 10.0)
	_start_publisher(second_viewport, 2, 85.0)
	_start_publisher(third_viewport, 3, 160.0)


func _build_scene() -> void:
	viewport = SubViewport.new()
	viewport.size = Vector2i(WIDTH, HEIGHT)
	viewport.own_world_3d = true
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	add_child(viewport)
	var preview := TextureRect.new()
	preview.texture = viewport.get_texture()
	preview.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	preview.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	add_child(preview)
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
	_add_mesh(floor_mesh, Vector3(0, -0.1, 0), Color("71885e"))
	cube = _add_mesh(BoxMesh.new(), Vector3(0, 1, 0), Color("e85b42"))
	_add_mesh(SphereMesh.new(), Vector3(-2, 0.5, 0), Color("27c7d9"))
	var marker_mesh := BoxMesh.new()
	marker_mesh.size = Vector3(0.3, 2.5, 0.3)
	_add_mesh(marker_mesh, Vector3(2, 1.25, -1), Color("ffcf40"))
	var camera := Camera3D.new()
	camera.keep_aspect = Camera3D.KEEP_WIDTH
	camera.fov = 90
	camera.position = Vector3(4, 3, 6)
	viewport.add_child(camera)
	camera.look_at(Vector3(0, 0.8, 0))
	camera.current = true
	second_viewport = _add_offset_camera(camera, SECOND_CAMERA_OFFSET)
	third_viewport = _add_offset_camera(camera, THIRD_CAMERA_OFFSET)
	for view: SubViewport in [viewport, second_viewport, third_viewport]:
		assert(view.find_world_3d() == viewport.find_world_3d(),
			"All cameras must render the same world")
		var view_camera := view.get_camera_3d()
		var left_ray := view_camera.project_ray_normal(Vector2(0, HEIGHT / 2.0))
		var right_ray := view_camera.project_ray_normal(Vector2(WIDTH, HEIGHT / 2.0))
		assert(abs(rad_to_deg(left_ray.angle_to(right_ray)) - 90.0) < 0.01,
			"Camera must have a 90-degree horizontal field of view")


func _add_offset_camera(reference: Camera3D, offset: Vector3) -> SubViewport:
	var view := SubViewport.new()
	view.size = Vector2i(WIDTH, HEIGHT)
	view.world_3d = viewport.find_world_3d()
	view.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	add_child(view)
	var camera := Camera3D.new()
	camera.keep_aspect = reference.keep_aspect
	camera.fov = reference.fov
	camera.transform = reference.transform.translated_local(offset)
	view.add_child(camera)
	camera.current = true
	return view


func _add_mesh(mesh: Mesh, position: Vector3, color: Color) -> MeshInstance3D:
	var instance := MeshInstance3D.new()
	instance.mesh = mesh
	instance.position = position
	var material := StandardMaterial3D.new()
	material.albedo_color = color
	instance.material_override = material
	viewport.add_child(instance)
	return instance


func _start_publisher(view: SubViewport, camera_id: int, label_y: float) -> void:
	var label := Label.new()
	label.position = Vector2(10, label_y)
	label.add_theme_color_override("font_shadow_color", Color.BLACK)
	label.add_theme_constant_override("shadow_offset_x", 1)
	label.add_theme_constant_override("shadow_offset_y", 1)
	add_child(label)
	var publisher := CameraPublisher.new()
	publisher.viewport = view
	publisher.camera_id = camera_id
	publisher.transport = transport
	publisher.stats_updated.connect(func(text: String) -> void: label.text = text)
	add_child(publisher)


func _process(delta: float) -> void:
	elapsed += delta
	cube.rotation = Vector3(elapsed * 0.35, elapsed, 0)
	cube.position.y = 1.1 + sin(elapsed * 2.0) * 0.35


func _exit_tree() -> void:
	transport.close()
