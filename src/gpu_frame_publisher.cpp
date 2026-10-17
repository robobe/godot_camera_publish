#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/classes/rendering_device.hpp>
#include <godot_cpp/classes/rendering_server.hpp>
#include <godot_cpp/classes/rd_shader_source.hpp>
#include <godot_cpp/classes/rd_shader_spirv.hpp>
#include <godot_cpp/classes/rd_sampler_state.hpp>
#include <godot_cpp/classes/rd_uniform.hpp>
#include <godot_cpp/classes/os.hpp>
#include <godot_cpp/core/class_db.hpp>
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/utility_functions.hpp>
#include <vulkan/vulkan.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

using namespace godot;
namespace {
constexpr uint32_t width = 640, height = 480, bytes = width * height * 4, count = 3;
void check(VkResult result, const char *operation) {
    if (result != VK_SUCCESS) throw std::runtime_error(std::string(operation) + ": Vulkan error " + std::to_string(result));
}
void require(bool ok, const char *message) { if (!ok) throw std::runtime_error(message); }
void append32(std::vector<uint8_t> &out, uint32_t v) { for (int i=0;i<4;++i) out.push_back(uint8_t(v >> (i*8))); }
void append64(std::vector<uint8_t> &out, uint64_t v) { for (int i=0;i<8;++i) out.push_back(uint8_t(v >> (i*8))); }
uint32_t read32(const uint8_t *p) { uint32_t v=0; for(int i=0;i<4;++i) v |= uint32_t(p[i]) << (i*8); return v; }
uint64_t read64(const uint8_t *p) { uint64_t v=0; for(int i=0;i<8;++i) v |= uint64_t(p[i]) << (i*8); return v; }
const char *shader_text = R"GLSL(#version 450
layout(local_size_x=16, local_size_y=16, local_size_z=1) in;
layout(set=0,binding=0) uniform sampler2D camera_image;
layout(set=0,binding=1,std430) restrict writeonly buffer Pixels { uint pixels[]; } output_image;
layout(push_constant,std430) uniform Settings { uint pattern; } settings;
void main() {
    uvec2 p = gl_GlobalInvocationID.xy;
    if (p.x >= 640 || p.y >= 480) return;
    vec4 color = texelFetch(camera_image, ivec2(p), 0);
    if (settings.pattern != 0) color = vec4(float(p.x % 256)/255.0, float(p.y % 256)/255.0, 73.0/255.0, 1.0);
    output_image.pixels[p.y * 640 + p.x] = packUnorm4x8(vec4(color.rgb, 1.0));
}
)GLSL";
}

class GpuFramePublisher : public RefCounted {
    GDCLASS(GpuFramePublisher, RefCounted)
    struct Slot {
        VkBuffer buffer = VK_NULL_HANDLE;
        VkDeviceMemory memory = VK_NULL_HANDLE;
        VkDeviceSize allocation = 0;
        VkSemaphore ready = VK_NULL_HANDLE;
        VkCommandBuffer acquire = VK_NULL_HANDLE, release = VK_NULL_HANDLE;
        RID rd_buffer, uniforms;
        bool busy = false, external = false;
        uint64_t frame = 0, timestamp = 0;
        uint32_t pattern = 0;
    };
    struct Generation {
        std::array<Slot, count> slots;
        RID source;
    };
    RenderingDevice *rd = nullptr;
    VkDevice device = VK_NULL_HANDLE;
    VkPhysicalDevice physical = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    uint32_t family = 0;
    VkCommandPool pool = VK_NULL_HANDLE;
    PFN_vkGetMemoryFdKHR get_memory_fd = nullptr;
    PFN_vkGetSemaphoreFdKHR get_semaphore_fd = nullptr;
    RID shader, pipeline, sampler;
    int server = -1, client = -1, pending = -1;
    std::unique_ptr<Generation> generation;
    // shortcut: disconnected generations stay alive until shutdown, add bounded retirement for a long-running service.
    std::vector<std::unique_ptr<Generation>> retired;
    uint64_t frame_number = 0, submitted = 0, dropped = 0;
    std::array<uint8_t, VK_UUID_SIZE> uuid{};

    void init_device() {
        auto *rs = RenderingServer::get_singleton();
        require(rs->is_on_render_thread(), "Use single-thread rendering for this native queue integration");
        rd = rs->get_rendering_device();
        require(rd && rd->has_method("storage_buffer_create_from_extension"), "Run the patched Vulkan Godot from tools/build_gpu_engine.py");
        device = reinterpret_cast<VkDevice>(rd->get_driver_resource(RenderingDevice::DRIVER_RESOURCE_LOGICAL_DEVICE, RID(), 0));
        physical = reinterpret_cast<VkPhysicalDevice>(rd->get_driver_resource(RenderingDevice::DRIVER_RESOURCE_PHYSICAL_DEVICE, RID(), 0));
        queue = reinterpret_cast<VkQueue>(rd->get_driver_resource(RenderingDevice::DRIVER_RESOURCE_COMMAND_QUEUE, RID(), 0));
        family = rd->get_driver_resource(RenderingDevice::DRIVER_RESOURCE_QUEUE_FAMILY, RID(), 0);
        VkPhysicalDeviceIDProperties id{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};
        VkPhysicalDeviceProperties2 properties{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};
        properties.pNext = &id;
        vkGetPhysicalDeviceProperties2(physical, &properties);
        require(properties.properties.vendorID == 0x10de, "Select the NVIDIA Vulkan device");
        std::memcpy(uuid.data(), id.deviceUUID, uuid.size());
        get_memory_fd = reinterpret_cast<PFN_vkGetMemoryFdKHR>(vkGetDeviceProcAddr(device, "vkGetMemoryFdKHR"));
        get_semaphore_fd = reinterpret_cast<PFN_vkGetSemaphoreFdKHR>(vkGetDeviceProcAddr(device, "vkGetSemaphoreFdKHR"));
        require(get_memory_fd && get_semaphore_fd, "External memory/semaphore FD extensions are unavailable");
        VkPhysicalDeviceExternalBufferInfo query{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO};
        query.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
        query.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        VkExternalBufferProperties capabilities{VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES};
        vkGetPhysicalDeviceExternalBufferProperties(physical, &query, &capabilities);
        require(capabilities.externalMemoryProperties.externalMemoryFeatures & VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT, "GPU storage buffers are not exportable");
        VkPhysicalDeviceExternalSemaphoreInfo sem_query{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_SEMAPHORE_INFO};
        sem_query.handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_OPAQUE_FD_BIT;
        VkExternalSemaphoreProperties sem_caps{VK_STRUCTURE_TYPE_EXTERNAL_SEMAPHORE_PROPERTIES};
        vkGetPhysicalDeviceExternalSemaphoreProperties(physical, &sem_query, &sem_caps);
        require(sem_caps.externalSemaphoreFeatures & VK_EXTERNAL_SEMAPHORE_FEATURE_EXPORTABLE_BIT, "GPU semaphores are not exportable");
        VkCommandPoolCreateInfo pool_info{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
        pool_info.queueFamilyIndex = family;
        check(vkCreateCommandPool(device, &pool_info, nullptr, &pool), "vkCreateCommandPool");
        Ref<RDShaderSource> source;
        source.instantiate();
        source->set_stage_source(RenderingDevice::SHADER_STAGE_COMPUTE, shader_text);
        Ref<RDShaderSPIRV> spirv = rd->shader_compile_spirv_from_source(source);
        require(spirv->get_stage_compile_error(RenderingDevice::SHADER_STAGE_COMPUTE).is_empty(), "GPU copy shader compilation failed");
        shader = rd->shader_create_from_spirv(spirv);
        pipeline = rd->compute_pipeline_create(shader);
        Ref<RDSamplerState> state;
        state.instantiate();
        sampler = rd->sampler_create(state);
        require(shader.is_valid() && pipeline.is_valid() && sampler.is_valid(), "Cannot create GPU copy pipeline");
        UtilityFunctions::print("GPU publisher: ", properties.properties.deviceName, " | RGBA8 640x480 | 3 exportable slots");
    }

    void record_barrier(Slot &s, bool acquire) {
        VkCommandBuffer &command = acquire ? s.acquire : s.release;
        VkCommandBufferAllocateInfo alloc{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
        alloc.commandPool = pool;
        alloc.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        alloc.commandBufferCount = 1;
        check(vkAllocateCommandBuffers(device, &alloc, &command), "vkAllocateCommandBuffers");
        VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        check(vkBeginCommandBuffer(command, &begin), "vkBeginCommandBuffer");
        VkBufferMemoryBarrier barrier{VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER};
        barrier.srcAccessMask = acquire ? 0 : VK_ACCESS_SHADER_WRITE_BIT;
        barrier.dstAccessMask = acquire ? VK_ACCESS_SHADER_WRITE_BIT : 0;
        barrier.srcQueueFamilyIndex = acquire ? VK_QUEUE_FAMILY_EXTERNAL : family;
        barrier.dstQueueFamilyIndex = acquire ? family : VK_QUEUE_FAMILY_EXTERNAL;
        barrier.buffer = s.buffer;
        barrier.size = bytes;
        vkCmdPipelineBarrier(command, acquire ? VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT : VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                             acquire ? VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT : VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                             0, 0, nullptr, 1, &barrier, 0, nullptr);
        check(vkEndCommandBuffer(command), "vkEndCommandBuffer");
    }

    void make_slot(Slot &s) {
        VkExternalMemoryBufferCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO};
        external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        VkBufferCreateInfo buffer_info{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
        buffer_info.pNext = &external;
        buffer_info.size = bytes;
        buffer_info.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
        buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        check(vkCreateBuffer(device, &buffer_info, nullptr, &s.buffer), "vkCreateBuffer");
        VkMemoryRequirements requirements;
        vkGetBufferMemoryRequirements(device, s.buffer, &requirements);
        VkPhysicalDeviceMemoryProperties properties;
        vkGetPhysicalDeviceMemoryProperties(physical, &properties);
        uint32_t type = 0;
        for (; type < properties.memoryTypeCount; ++type)
            if ((requirements.memoryTypeBits & (1u << type)) && (properties.memoryTypes[type].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) break;
        require(type < properties.memoryTypeCount, "No device-local exportable memory type");
        VkMemoryDedicatedAllocateInfo dedicated{VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO};
        dedicated.buffer = s.buffer;
        VkExportMemoryAllocateInfo export_info{VK_STRUCTURE_TYPE_EXPORT_MEMORY_ALLOCATE_INFO};
        export_info.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        export_info.pNext = &dedicated;
        VkMemoryAllocateInfo allocation{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        allocation.pNext = &export_info;
        allocation.allocationSize = requirements.size;
        allocation.memoryTypeIndex = type;
        s.allocation = requirements.size;
        check(vkAllocateMemory(device, &allocation, nullptr, &s.memory), "vkAllocateMemory");
        check(vkBindBufferMemory(device, s.buffer, s.memory, 0), "vkBindBufferMemory");
        VkExportSemaphoreCreateInfo sem_export{VK_STRUCTURE_TYPE_EXPORT_SEMAPHORE_CREATE_INFO};
        sem_export.handleTypes = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_OPAQUE_FD_BIT;
        VkSemaphoreCreateInfo sem_info{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
        sem_info.pNext = &sem_export;
        check(vkCreateSemaphore(device, &sem_info, nullptr, &s.ready), "vkCreateSemaphore");
        s.rd_buffer = rd->call("storage_buffer_create_from_extension", uint64_t(s.buffer), bytes);
        require(s.rd_buffer.is_valid(), "Cannot register external buffer with Godot");
        record_barrier(s, true);
        record_barrier(s, false);
    }

    void disconnect_client() {
        if (client >= 0) ::close(client);
        client = -1;
        pending = -1;
        if (generation) retired.push_back(std::move(generation));
    }

    bool send_packet(const std::vector<uint8_t> &message) {
        ssize_t sent = ::send(client, message.data(), message.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
        if (sent == ssize_t(message.size())) return true;
        disconnect_client();
        return false;
    }

    void accept_client() {
        int incoming = ::accept4(server, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (incoming < 0) {
            require(errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR, "accept4 failed");
            return;
        }
        ucred credentials{};
        socklen_t length = sizeof(credentials);
        if (getsockopt(incoming, SOL_SOCKET, SO_PEERCRED, &credentials, &length) != 0 || credentials.uid != getuid() || client >= 0) {
            ::send(incoming, "BUSY", 4, MSG_NOSIGNAL | MSG_DONTWAIT);
            ::close(incoming);
            return;
        }
        client = incoming;
        generation = std::make_unique<Generation>();
        for (auto &slot : generation->slots) make_slot(slot);
        std::vector<uint8_t> message{'G','P','G','1'};
        for (uint32_t v : {1u, width, height, count, bytes}) append32(message, v);
        message.insert(message.end(), uuid.begin(), uuid.end());
        for (auto &slot : generation->slots) append64(message, slot.allocation);
        std::array<int, count * 2> fds;
        fds.fill(-1);
        try {
            for (uint32_t i = 0; i < count; ++i) {
                VkMemoryGetFdInfoKHR mem_info{VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR};
                mem_info.memory = generation->slots[i].memory;
                mem_info.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
                check(get_memory_fd(device, &mem_info, &fds[2*i]), "vkGetMemoryFdKHR");
                VkSemaphoreGetFdInfoKHR sem_info{VK_STRUCTURE_TYPE_SEMAPHORE_GET_FD_INFO_KHR};
                sem_info.semaphore = generation->slots[i].ready;
                sem_info.handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_OPAQUE_FD_BIT;
                check(get_semaphore_fd(device, &sem_info, &fds[2*i+1]), "vkGetSemaphoreFdKHR");
            }
            alignas(cmsghdr) char control[CMSG_SPACE(sizeof(fds))]{};
            iovec iov{message.data(), message.size()};
            msghdr header{};
            header.msg_iov = &iov;
            header.msg_iovlen = 1;
            header.msg_control = control;
            header.msg_controllen = sizeof(control);
            cmsghdr *cmsg = CMSG_FIRSTHDR(&header);
            cmsg->cmsg_level = SOL_SOCKET;
            cmsg->cmsg_type = SCM_RIGHTS;
            cmsg->cmsg_len = CMSG_LEN(sizeof(fds));
            std::memcpy(CMSG_DATA(cmsg), fds.data(), sizeof(fds));
            if (::sendmsg(client, &header, MSG_NOSIGNAL | MSG_DONTWAIT) != ssize_t(message.size())) disconnect_client();
        } catch (...) {
            for (int fd : fds) if (fd >= 0) ::close(fd);
            throw;
        }
        for (int fd : fds) if (fd >= 0) ::close(fd);
        if (client >= 0) UtilityFunctions::print("CUDA consumer connected | generation ", int64_t(retired.size()+1));
    }

    void receive_releases() {
        while (client >= 0) {
            std::array<uint8_t, 17> message{};
            ssize_t n = ::recv(client, message.data(), message.size(), MSG_DONTWAIT | MSG_TRUNC);
            if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) return;
            if (n <= 0) {
                disconnect_client();
                return;
            }
            if (n != 16 || std::memcmp(message.data(), "REL1", 4) || read32(message.data()+4) >= count) {
                UtilityFunctions::push_warning("Invalid GPU release packet, disconnecting consumer");
                disconnect_client();
                return;
            }
            auto &slot = generation->slots[read32(message.data()+4)];
            if (!slot.busy || int(read32(message.data()+4)) == pending || slot.frame != read64(message.data()+8)) {
                UtilityFunctions::push_warning("Stale or duplicate GPU release, disconnecting consumer");
                disconnect_client();
                return;
            }
            slot.busy = false;
        }
    }

    void finish_pending() {
        if (pending < 0) return;
        auto &s = generation->slots[pending];
        VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
        submit.commandBufferCount = 1;
        submit.pCommandBuffers = &s.release;
        submit.signalSemaphoreCount = 1;
        submit.pSignalSemaphores = &s.ready;
        check(vkQueueSubmit(queue, 1, &submit, VK_NULL_HANDLE), "vkQueueSubmit release");
        s.external = true;
        std::vector<uint8_t> packet{'R','D','Y','1'};
        append32(packet, pending);
        append32(packet, s.pattern);
        append64(packet, s.frame);
        append64(packet, s.timestamp);
        pending = -1;
        if (send_packet(packet)) ++submitted;
    }

    void schedule(RID source) {
        for (uint32_t i = 0; i < count; ++i) {
            auto &s = generation->slots[i];
            if (s.busy) continue;
            if (generation->source != source) {
                for (auto &old : generation->slots) {
                    if (old.uniforms.is_valid()) rd->free_rid(old.uniforms);
                    old.uniforms = RID();
                }
                generation->source = source;
            }
            if (!s.uniforms.is_valid()) {
                Ref<RDUniform> input; input.instantiate();
                input->set_uniform_type(RenderingDevice::UNIFORM_TYPE_SAMPLER_WITH_TEXTURE);
                input->set_binding(0); input->add_id(sampler); input->add_id(source);
                Ref<RDUniform> output; output.instantiate();
                output->set_uniform_type(RenderingDevice::UNIFORM_TYPE_STORAGE_BUFFER);
                output->set_binding(1); output->add_id(s.rd_buffer);
                TypedArray<RDUniform> uniforms; uniforms.push_back(input); uniforms.push_back(output);
                s.uniforms = rd->uniform_set_create(uniforms, shader, 0);
                require(s.uniforms.is_valid(), "Cannot bind camera image and GPU output buffer");
            }
            if (s.external) {
                VkSubmitInfo acquire{VK_STRUCTURE_TYPE_SUBMIT_INFO};
                acquire.commandBufferCount = 1;
                acquire.pCommandBuffers = &s.acquire;
                check(vkQueueSubmit(queue, 1, &acquire, VK_NULL_HANDLE), "vkQueueSubmit acquire");
            }
            s.busy = true;
            s.frame = ++frame_number;
            s.timestamp = std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
            s.pattern = s.frame <= 3 ? 1 : 0;
            // Godot rounds the shader push-constant block up to 16 bytes.
            PackedByteArray settings; settings.resize(16); settings.encode_u32(0, s.pattern);
            auto list = rd->compute_list_begin();
            rd->compute_list_bind_compute_pipeline(list, pipeline);
            rd->compute_list_bind_uniform_set(list, s.uniforms, 0);
            rd->compute_list_set_push_constant(list, settings, settings.size());
            rd->compute_list_dispatch(list, width/16, height/16, 1);
            rd->compute_list_end();
            pending = i;
            return;
        }
        ++dropped;
    }

    void destroy_generation(Generation &g) {
        for (auto &s : g.slots) {
            if (s.uniforms.is_valid()) rd->free_rid(s.uniforms);
            if (s.rd_buffer.is_valid()) rd->free_rid(s.rd_buffer);
            if (s.acquire) vkFreeCommandBuffers(device, pool, 1, &s.acquire);
            if (s.release) vkFreeCommandBuffers(device, pool, 1, &s.release);
            if (s.ready) vkDestroySemaphore(device, s.ready, nullptr);
            if (s.buffer) vkDestroyBuffer(device, s.buffer, nullptr);
            if (s.memory) vkFreeMemory(device, s.memory, nullptr);
        }
    }

protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("open", "channel"), &GpuFramePublisher::open, DEFVAL(String("camera1_a")));
        ClassDB::bind_method(D_METHOD("tick", "texture"), &GpuFramePublisher::tick);
        ClassDB::bind_method(D_METHOD("stats"), &GpuFramePublisher::stats);
        ClassDB::bind_method(D_METHOD("close"), &GpuFramePublisher::close);
    }
public:
    bool open(String channel = "camera1_a") {
        try {
            const std::string suffix = channel.utf8().get_data();
            require(!suffix.empty() && suffix.size() <= 48 && suffix.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") == std::string::npos,
                    "GPU channel must contain 1-48 letters, digits, underscores or hyphens");
            init_device();
            server = ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
            require(server >= 0, "Cannot create GPU control socket");
            sockaddr_un address{}; address.sun_family = AF_UNIX;
            std::string name = "godot-camera-gpu-" + std::to_string(getuid()) + "-" + suffix;
            std::memcpy(address.sun_path + 1, name.data(), name.size());
            require(::bind(server, reinterpret_cast<sockaddr *>(&address), offsetof(sockaddr_un, sun_path)+1+name.size()) == 0, "GPU publisher socket is already in use");
            require(::listen(server, 4) == 0, "Cannot listen on GPU control socket");
            return true;
        } catch (const std::exception &error) { UtilityFunctions::push_error(error.what()); return false; }
    }
    bool tick(RID texture) {
        try {
            require(RenderingServer::get_singleton()->is_on_render_thread(), "GPU tick must run on the render thread");
            // frame_post_draw follows Godot's submission, including last tick's compute dispatch.
            if (client >= 0) finish_pending();
            if (client >= 0) receive_releases();
            accept_client();
            if (client >= 0 && texture.is_valid()) schedule(texture);
            return true;
        } catch (const std::exception &error) { UtilityFunctions::push_error(error.what()); return false; }
    }
    String stats() const {
        return String("Submitted: ") + String::num_uint64(submitted) + " | Dropped: " + String::num_uint64(dropped)
            + (client >= 0 ? " | CUDA connected" : " | Waiting for CUDA");
    }
    void close() {
        if (server >= 0) ::close(server);
        server = -1;
        const bool queued_compute = pending >= 0;
        disconnect_client();
        if (!device) return;
        if (queued_compute) rd->call("gpu_flush", false);
        // Flush queued Godot work before destroying externally owned Vulkan objects.
        vkDeviceWaitIdle(device);
        for (auto &g : retired) destroy_generation(*g);
        retired.clear();
        for (RID resource : {pipeline, shader, sampler}) if (resource.is_valid()) rd->free_rid(resource);
        pipeline = shader = sampler = RID();
        if (pool) vkDestroyCommandPool(device, pool, nullptr);
        pool = VK_NULL_HANDLE;
        device = VK_NULL_HANDLE;
    }
    ~GpuFramePublisher() override { close(); }
};

static void initialize(ModuleInitializationLevel level) { if (level == MODULE_INITIALIZATION_LEVEL_SCENE) ClassDB::register_class<GpuFramePublisher>(); }
static void uninitialize(ModuleInitializationLevel) {}
extern "C" GDExtensionBool GDE_EXPORT camera_gpu_init(GDExtensionInterfaceGetProcAddress get_proc_address, GDExtensionClassLibraryPtr library, GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(get_proc_address, library, initialization);
    init.register_initializer(initialize); init.register_terminator(uninitialize);
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
