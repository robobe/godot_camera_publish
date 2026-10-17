#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/core/class_db.hpp>
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/packed_byte_array.hpp>
#include <godot_cpp/variant/utility_functions.hpp>

#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <sys/mman.h>
#include <unistd.h>

using namespace godot;

class SharedFrameBuffer : public RefCounted {
    GDCLASS(SharedFrameBuffer, RefCounted)

    static constexpr size_t frame_bytes = 640 * 480 * 3;
    void *pixels = MAP_FAILED;
    std::string name;

    void report_error(const char *operation) {
        UtilityFunctions::push_error(String(operation) + ": " + String(std::strerror(errno)));
    }

protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("open_buffer"), &SharedFrameBuffer::open_buffer);
        ClassDB::bind_method(D_METHOD("write_frame", "bytes"), &SharedFrameBuffer::write_frame);
        ClassDB::bind_method(D_METHOD("close_buffer"), &SharedFrameBuffer::close_buffer);
    }

public:
    String open_buffer() {
        close_buffer();
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        name = "/godot_camera_" + std::to_string(getpid()) + "_" + std::to_string(stamp);
        const int fd = shm_open(name.c_str(), O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
        if (fd == -1) {
            report_error("shm_open");
            name.clear();
            return {};
        }
        if (ftruncate(fd, frame_bytes) == -1) {
            report_error("ftruncate");
            ::close(fd);
            close_buffer();
            return {};
        }
        pixels = mmap(nullptr, frame_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (pixels == MAP_FAILED) {
            report_error("mmap");
        }
        ::close(fd);
        if (pixels == MAP_FAILED) {
            close_buffer();
            return {};
        }
        return String(name.c_str());
    }

    bool write_frame(const PackedByteArray &bytes) {
        if (pixels == MAP_FAILED || bytes.size() != static_cast<int64_t>(frame_bytes)) {
            UtilityFunctions::push_error("Expected an open buffer and exactly 640*480*3 RGB8 bytes");
            return false;
        }
        // The caller owns the slot until it sends READY, and cannot write again until ACK.
        std::memcpy(pixels, bytes.ptr(), frame_bytes);
        return true;
    }

    void close_buffer() {
        if (pixels != MAP_FAILED) {
            munmap(pixels, frame_bytes);
            pixels = MAP_FAILED;
        }
        if (!name.empty()) {
            if (shm_unlink(name.c_str()) == -1 && errno != ENOENT) {
                report_error("shm_unlink");
            }
            name.clear();
        }
    }

    ~SharedFrameBuffer() override { close_buffer(); }
};

static void initialize(ModuleInitializationLevel level) {
    if (level == MODULE_INITIALIZATION_LEVEL_SCENE) {
        ClassDB::register_class<SharedFrameBuffer>();
    }
}

static void uninitialize(ModuleInitializationLevel) {}

extern "C" GDExtensionBool GDE_EXPORT camera_shm_init(
    GDExtensionInterfaceGetProcAddress get_proc_address,
    GDExtensionClassLibraryPtr library,
    GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(get_proc_address, library, initialization);
    init.register_initializer(initialize);
    init.register_terminator(uninitialize);
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
