#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/core/class_db.hpp>
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/packed_byte_array.hpp>
#include <godot_cpp/variant/utility_functions.hpp>
#include <zenoh.hxx>
#include <cstring>
#include <memory>
#include <optional>
#include <string>
#include <vector>

using namespace godot;

class ZenohImagePublisher : public RefCounted {
    GDCLASS(ZenohImagePublisher, RefCounted)
    static constexpr size_t frame_bytes = 640 * 480 * 3;
    std::optional<zenoh::Session> session;
    std::unique_ptr<zenoh::PosixShmProvider> pool;
    std::vector<zenoh::Publisher> publishers;
    std::string stream_id;

    static void report_error() {
        try { throw; }
        catch (const std::exception &e) { UtilityFunctions::push_error(String("Zenoh: ") + e.what()); }
        catch (...) { UtilityFunctions::push_error("Unknown Zenoh error"); }
    }

protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("open"), &ZenohImagePublisher::open);
        ClassDB::bind_method(D_METHOD("has_subscribers", "camera_id"), &ZenohImagePublisher::has_subscribers);
        ClassDB::bind_method(D_METHOD("publish_frame", "camera_id", "pixels", "frame_id", "capture_usec"), &ZenohImagePublisher::publish_frame);
        ClassDB::bind_method(D_METHOD("close"), &ZenohImagePublisher::close);
    }

public:
    bool open() {
        close();
        try {
            auto config = zenoh::Config::create_default();
            config.insert_json5("mode", "\"peer\"");
            config.insert_json5("listen/endpoints", "[\"tcp/127.0.0.1:7447\"]");
            config.insert_json5("scouting/multicast/enabled", "false");
            config.insert_json5("transport/shared_memory/enabled", "true");
            session.emplace(zenoh::Session::open(std::move(config)));
            stream_id = session->get_zid().to_string();
            pool = std::make_unique<zenoh::PosixShmProvider>(32 * 1024 * 1024);
            for (int id = 1; id <= 3; ++id) {
                auto options = zenoh::Session::PublisherOptions::create_default();
                options.congestion_control = Z_CONGESTION_CONTROL_DROP;
                publishers.push_back(session->declare_publisher(
                    zenoh::KeyExpr("godot/camera/" + std::to_string(id) + "/image"), std::move(options)));
            }
            UtilityFunctions::print("Zenoh SHM publisher listening on tcp/127.0.0.1:7447");
            return true;
        } catch (...) { report_error(); close(); return false; }
    }

    bool has_subscribers(int64_t camera_id) {
        if (camera_id < 1 || camera_id > static_cast<int64_t>(publishers.size())) return false;
        try { return publishers[camera_id - 1].get_matching_status().matching; }
        catch (...) { report_error(); return false; }
    }

    bool publish_frame(int64_t camera_id, const PackedByteArray &pixels, int64_t frame_id, int64_t capture_usec) {
        if (!pool || camera_id < 1 || camera_id > static_cast<int64_t>(publishers.size()) ||
            pixels.size() != static_cast<int64_t>(frame_bytes) || frame_id <= 0 || capture_usec < 0) {
            UtilityFunctions::push_error("Invalid camera, RGB8 frame length, or timestamp");
            return false;
        }
        try {
            // Never wait for readers: exhausted pools drop the current frame.
            auto result = pool->alloc_gc(frame_bytes);
            auto *buffer = std::get_if<zenoh::ZShmMut>(&result);
            if (!buffer) return false;
            std::memcpy(buffer->data(), pixels.ptr(), frame_bytes);
            auto options = zenoh::Publisher::PutOptions::create_default();
            options.attachment = zenoh::Bytes("{\"version\":1,\"camera_id\":" + std::to_string(camera_id) +
                ",\"width\":640,\"height\":480,\"format\":\"RGB8\",\"frame_id\":" + std::to_string(frame_id) +
                ",\"capture_usec\":" + std::to_string(capture_usec) + ",\"stream_id\":\"" + stream_id + "\"}");
            publishers[camera_id - 1].put(std::move(*buffer), std::move(options));
            return true;
        } catch (...) { report_error(); return false; }
    }

    void close() {
        try { publishers.clear(); pool.reset(); session.reset(); }
        catch (...) { report_error(); }
    }
    ~ZenohImagePublisher() override { close(); }
};

static void initialize(ModuleInitializationLevel level) {
    if (level == MODULE_INITIALIZATION_LEVEL_SCENE) ClassDB::register_class<ZenohImagePublisher>();
}
static void uninitialize(ModuleInitializationLevel) {}
extern "C" GDExtensionBool GDE_EXPORT camera_zenoh_init(
    GDExtensionInterfaceGetProcAddress get_proc_address, GDExtensionClassLibraryPtr library,
    GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(get_proc_address, library, initialization);
    init.register_initializer(initialize);
    init.register_terminator(uninitialize);
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
