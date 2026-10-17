#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/core/class_db.hpp>
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/packed_byte_array.hpp>
#include <godot_cpp/variant/utility_functions.hpp>
#include <zmq.h>

#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <unistd.h>

using namespace godot;

class ZmqImagePublisher : public RefCounted {
    GDCLASS(ZmqImagePublisher, RefCounted)
    static constexpr size_t frame_bytes = 640 * 480 * 3;
    static constexpr int default_buffer_bytes = 65536;
    void *context = nullptr;
    void *publisher = nullptr;
    std::string stream_id;

    static void check(int result, const char *operation) {
        if (result < 0) throw std::runtime_error(std::string(operation) + ": " + zmq_strerror(zmq_errno()));
    }
    static void report_error() {
        try { throw; }
        catch (const std::exception &e) { UtilityFunctions::push_error(String("ZeroMQ: ") + e.what()); }
        catch (...) { UtilityFunctions::push_error("Unknown ZeroMQ error"); }
    }
    static int buffer_bytes() {
        const char *value = std::getenv("CAMERA_ZMQ_BUFFER_BYTES");
        if (!value) return default_buffer_bytes;
        int result = 0;
        const auto end = value + std::strlen(value);
        const auto parsed = std::from_chars(value, end, result);
        if (parsed.ec != std::errc() || parsed.ptr != end || result < 1024 || result > 4194304)
            throw std::runtime_error("CAMERA_ZMQ_BUFFER_BYTES must be an integer from 1024 through 4194304");
        return result;
    }
protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("open"), &ZmqImagePublisher::open);
        ClassDB::bind_method(D_METHOD("publish_frame", "camera_id", "pixels", "frame_id", "capture_usec"), &ZmqImagePublisher::publish_frame);
        ClassDB::bind_method(D_METHOD("close"), &ZmqImagePublisher::close);
    }
public:
    bool open() {
        close();
        try {
            const int bytes = buffer_bytes();
            context = zmq_ctx_new();
            if (!context) check(-1, "zmq_ctx_new");
            publisher = zmq_socket(context, ZMQ_PUB);
            if (!publisher) check(-1, "zmq_socket");
            const int one = 1, zero = 0;
            check(zmq_setsockopt(publisher, ZMQ_SNDHWM, &one, sizeof(one)), "SNDHWM");
            check(zmq_setsockopt(publisher, ZMQ_SNDTIMEO, &zero, sizeof(zero)), "SNDTIMEO");
            check(zmq_setsockopt(publisher, ZMQ_LINGER, &zero, sizeof(zero)), "LINGER");
            check(zmq_setsockopt(publisher, ZMQ_SNDBUF, &bytes, sizeof(bytes)), "SNDBUF");
            check(zmq_bind(publisher, "tcp://127.0.0.1:5555"), "bind");
            stream_id = std::to_string(getpid()) + "-" + std::to_string(
                std::chrono::steady_clock::now().time_since_epoch().count());
            UtilityFunctions::print("ZeroMQ PUB listening on tcp://127.0.0.1:5555 | buffer bytes: ", bytes);
            return true;
        } catch (...) { report_error(); close(); return false; }
    }

    bool publish_frame(int64_t camera_id, const PackedByteArray &pixels, int64_t frame_id, int64_t capture_usec) {
        if (!publisher || camera_id < 1 || camera_id > 3 ||
            pixels.size() != static_cast<int64_t>(frame_bytes) || frame_id <= 0 || capture_usec < 0) {
            UtilityFunctions::push_error("Expected an open publisher, camera 1/2/3, and 640*480*3 RGB8 bytes");
            return false;
        }
        try {
            const std::string topic = "godot/camera/" + std::to_string(camera_id) + "/image" + '\0';
            const std::string metadata = "{\"version\":1,\"camera_id\":" + std::to_string(camera_id) +
                ",\"width\":640,\"height\":480,\"format\":\"RGB8\",\"frame_id\":" + std::to_string(frame_id) +
                ",\"capture_usec\":" + std::to_string(capture_usec) + ",\"stream_id\":\"" + stream_id + "\"}";
            zmq_msg_t message;
            check(zmq_msg_init_size(&message, topic.size() + 4 + metadata.size() + frame_bytes), "message allocation");
            auto *data = static_cast<uint8_t *>(zmq_msg_data(&message));
            std::memcpy(data, topic.data(), topic.size());
            for (unsigned i = 0; i < 4; ++i) data[topic.size() + i] = (metadata.size() >> (i * 8)) & 0xff;
            std::memcpy(data + topic.size() + 4, metadata.data(), metadata.size());
            std::memcpy(data + topic.size() + 4 + metadata.size(), pixels.ptr(), frame_bytes);
            const int result = zmq_msg_send(&message, publisher, ZMQ_DONTWAIT);
            const int error = zmq_errno();
            zmq_msg_close(&message);
            if (result < 0) {
                if (error == EAGAIN) return false;
                throw std::runtime_error(std::string("send: ") + zmq_strerror(error));
            }
            return true;
        } catch (...) { report_error(); return false; }
    }

    void close() {
        if (publisher) { zmq_close(publisher); publisher = nullptr; }
        if (context) {
            while (zmq_ctx_term(context) == -1 && zmq_errno() == EINTR) {}
            context = nullptr;
        }
    }
    ~ZmqImagePublisher() override { close(); }
};
static void initialize(ModuleInitializationLevel level) {
    if (level == MODULE_INITIALIZATION_LEVEL_SCENE) ClassDB::register_class<ZmqImagePublisher>();
}
static void uninitialize(ModuleInitializationLevel) {}
extern "C" GDExtensionBool GDE_EXPORT camera_zmq_init(
    GDExtensionInterfaceGetProcAddress get_proc_address, GDExtensionClassLibraryPtr library,
    GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(get_proc_address, library, initialization);
    init.register_initializer(initialize);
    init.register_terminator(uninitialize);
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
