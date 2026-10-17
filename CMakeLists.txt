cmake_minimum_required(VERSION 3.22)
project(godot_camera_publish LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "This shared-memory example supports Linux only")
endif()

include(FetchContent)
set(GODOTCPP_BUILD_PROFILE "${CMAKE_CURRENT_SOURCE_DIR}/build_profile.json" CACHE FILEPATH "" FORCE)
set(GODOTCPP_SYSTEM_HEADERS ON CACHE BOOL "Treat dependency headers as system headers" FORCE)
FetchContent_Declare(godot_cpp
  GIT_REPOSITORY https://github.com/godotengine/godot-cpp.git
  GIT_TAG e83fd0904c13356ed1d4c3d09f8bb9132bdc6b77
  GIT_SHALLOW FALSE)
FetchContent_MakeAvailable(godot_cpp)

add_library(camera_shm SHARED src/shared_frame_buffer.cpp)
target_link_libraries(camera_shm PRIVATE godot-cpp)
target_compile_features(camera_shm PRIVATE cxx_std_20)
set_target_properties(camera_shm PROPERTIES
  CXX_STANDARD_REQUIRED ON
  CXX_EXTENSIONS OFF
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/demo/bin")
target_compile_options(camera_shm PRIVATE -Wall -Wextra -Wpedantic)
