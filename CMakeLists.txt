cmake_minimum_required(VERSION 3.22)
project(godot_camera_publish LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "This GPU-sharing example supports Linux only")
endif()

include(FetchContent)
set(GODOTCPP_DISABLE_EXCEPTIONS OFF CACHE BOOL "Catch errors before the Godot ABI" FORCE)
set(GODOTCPP_BUILD_PROFILE "${CMAKE_CURRENT_SOURCE_DIR}/build_profile.json" CACHE FILEPATH "" FORCE)
set(GODOTCPP_SYSTEM_HEADERS ON CACHE BOOL "Treat dependency headers as system headers" FORCE)
FetchContent_Declare(godot_cpp
  GIT_REPOSITORY https://github.com/godotengine/godot-cpp.git
  GIT_TAG e83fd0904c13356ed1d4c3d09f8bb9132bdc6b77
  GIT_SHALLOW FALSE)
FetchContent_MakeAvailable(godot_cpp)

find_package(Vulkan REQUIRED)
add_library(camera_gpu SHARED src/gpu_frame_publisher.cpp)
target_link_libraries(camera_gpu PRIVATE godot-cpp Vulkan::Vulkan)
target_compile_features(camera_gpu PRIVATE cxx_std_20)
set_target_properties(camera_gpu PROPERTIES
  CXX_STANDARD_REQUIRED ON
  CXX_EXTENSIONS OFF
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/demo/bin")
target_compile_options(camera_gpu PRIVATE -Wall -Wextra -Wpedantic -Wno-missing-field-initializers)

# Runtime templates do not perform editor discovery of GDExtensions.
file(MAKE_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/demo/.godot")
file(WRITE "${CMAKE_CURRENT_SOURCE_DIR}/demo/.godot/extension_list.cfg" "res://bin/camera_gpu.gdextension\n")
