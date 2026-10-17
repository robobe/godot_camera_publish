#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/core/class_db.hpp>
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/packed_byte_array.hpp>
#include <godot_cpp/variant/utility_functions.hpp>
#include <zmq.h>

#include <array>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <unistd.h>

using namespace godot;

class ZmqImagePublisher : public RefCounted {
    GDCLASS(ZmqImagePublisher, RefCounted)
    static constexpr size_t frame_bytes = 640 * 480 * 3;
    static constexpr int default_buffer_bytes = 65536;
    void *context = nullptr;
    void *publisher = nullptr;
    std::string stream_id;
    static constexpr size_t header_bytes = 128;
    static constexpr size_t slot_bytes = header_bytes + frame_bytes;
    struct Slot {
        int fd = -1;
        uint8_t *data = nullptr;
        std::string name;
    };
    std::array<Slot, 9> slots;
    std::array<size_t, 3> next_slot{};

    static void check_posix(int result, const char *operation) {
        if (result < 0) throw std::runtime_error(std::string(operation) + ": " + std::strerror(errno));
    }
    struct SlotLock {
        int fd;
        ~SlotLock() { flock(fd, LOCK_UN); }
    };
    static void put_le(uint8_t *data, uint64_t value, size_t bytes) {
        for (size_t i = 0; i < bytes; ++i) data[i] = (value >> (8 * i)) & 0xff;
    }
    void create_slots() {
        for (size_t i = 0; i < slots.size(); ++i) {
            auto &slot = slots[i];
            const auto name = "/godot_camera_zmq_" + stream_id + "_c" + std::to_string(i / 3 + 1) + "_s" + std::to_string(i % 3);
            slot.fd = shm_open(name.c_str(), O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
            check_posix(slot.fd, "shm_open");
            slot.name = name;
            check_posix(ftruncate(slot.fd, slot_bytes), "ftruncate");
            void *mapping = mmap(nullptr, slot_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, slot.fd, 0);
            if (mapping == MAP_FAILED) check_posix(-1, "mmap");
            slot.data = static_cast<uint8_t *>(mapping);
            UtilityFunctions::print("SHM slot: ", String(slot.name.c_str()));
        }
    }
    Slot *write_slot(int64_t camera_id, const PackedByteArray &pixels, int64_t frame_id, int64_t capture_usec) {
        const size_t camera = camera_id - 1;
        for (size_t attempt = 0; attempt < 3; ++attempt) {
            const size_t index = (next_slot[camera] + attempt) % 3;
            auto &slot = slots[camera * 3 + index];
            if (flock(slot.fd, LOCK_EX | LOCK_NB) != 0) {
                if (errno == EWOULDBLOCK || errno == EAGAIN || errno == EINTR) continue;
                check_posix(-1, "flock writer");
            }
            SlotLock lock{slot.fd};
            std::array<uint8_t, header_bytes> header{};
            std::memcpy(header.data(), "GCSHM02", 8);
            const std::array<uint32_t, 6> fields{2, static_cast<uint32_t>(camera_id), static_cast<uint32_t>(index), 640, 480, 1};
            for (size_t i = 0; i < fields.size(); ++i) put_le(header.data() + 8 + i * 4, fields[i], 4);
            put_le(header.data() + 32, frame_bytes, 8);
            put_le(header.data() + 40, frame_id, 8);
            put_le(header.data() + 48, capture_usec, 8);
            std::memcpy(header.data() + 64, stream_id.data(), stream_id.size());
            // A syscall invalidates the old commit before any mapped pixels can be modified.
            const uint8_t incomplete[8]{};
            if (pwrite(slot.fd, incomplete, sizeof(incomplete), 56) != sizeof(incomplete))
                throw std::runtime_error("Could not invalidate SHM slot");
            std::atomic_thread_fence(std::memory_order_seq_cst);
            std::memcpy(slot.data, header.data(), header.size());
            std::memcpy(slot.data + header_bytes, pixels.ptr(), frame_bytes);
            std::atomic_thread_fence(std::memory_order_seq_cst);
            const uint8_t complete[8]{1};
            if (pwrite(slot.fd, complete, sizeof(complete), 56) != sizeof(complete))
                throw std::runtime_error("Could not commit SHM slot");
            next_slot[camera] = (index + 1) % 3;
            return &slot;
        }
        return nullptr;
    }

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
            create_slots();
            next_slot.fill(0);
            UtilityFunctions::print("ZeroMQ SHM PUB listening on tcp://127.0.0.1:5555 | buffer bytes: ", bytes);
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
            Slot *slot = write_slot(camera_id, pixels, frame_id, capture_usec);
            if (!slot) return false;
            const std::string topic = "godot/camera/" + std::to_string(camera_id) + "/image" + '\0';
            const std::string metadata = "{\"version\":2,\"camera_id\":" + std::to_string(camera_id) +
                ",\"width\":640,\"height\":480,\"format\":\"RGB8\",\"frame_id\":" + std::to_string(frame_id) +
                ",\"capture_usec\":" + std::to_string(capture_usec) + ",\"stream_id\":\"" + stream_id + "\",\"slot\":" + std::to_string((slot - slots.data()) % 3) +
                ",\"shm_name\":\"" + slot->name + "\",\"bytes\":921600}";
            zmq_msg_t message;
            check(zmq_msg_init_size(&message, topic.size() + 4 + metadata.size()), "message allocation");
            auto *data = static_cast<uint8_t *>(zmq_msg_data(&message));
            std::memcpy(data, topic.data(), topic.size());
            for (unsigned i = 0; i < 4; ++i) data[topic.size() + i] = (metadata.size() >> (i * 8)) & 0xff;
            std::memcpy(data + topic.size() + 4, metadata.data(), metadata.size());
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
        for (auto &slot : slots) {
            if (slot.data) { munmap(slot.data, slot_bytes); slot.data = nullptr; }
            if (slot.fd != -1) { ::close(slot.fd); slot.fd = -1; }
            if (!slot.name.empty()) { shm_unlink(slot.name.c_str()); slot.name.clear(); }
        }
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
