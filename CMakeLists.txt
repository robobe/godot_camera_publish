cmake_minimum_required(VERSION 3.22)
project(godot_camera_publish LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "This shared-memory example supports Linux only")
endif()

if(NOT CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|amd64|AMD64)$")
  message(FATAL_ERROR "The pinned Zenoh SDK supports x86-64 Linux only")
endif()

include(FetchContent)
set(GODOTCPP_DISABLE_EXCEPTIONS OFF CACHE BOOL "Zenoh C++ requires exceptions" FORCE)
set(GODOTCPP_BUILD_PROFILE "${CMAKE_CURRENT_SOURCE_DIR}/build_profile.json" CACHE FILEPATH "" FORCE)
set(GODOTCPP_SYSTEM_HEADERS ON CACHE BOOL "Treat dependency headers as system headers" FORCE)
FetchContent_Declare(godot_cpp
  GIT_REPOSITORY https://github.com/godotengine/godot-cpp.git
  GIT_TAG e83fd0904c13356ed1d4c3d09f8bb9132bdc6b77
  GIT_SHALLOW FALSE)
FetchContent_MakeAvailable(godot_cpp)

FetchContent_Declare(zenoh_c
  URL https://github.com/eclipse-zenoh/zenoh-c/releases/download/1.10.1/zenoh-c-1.10.1-x86_64-unknown-linux-gnu-standalone.zip
  URL_HASH SHA256=9ee0f2d732b0f3042a7e1cd3076042a2bc3ac0415587c40bc3ed7b8b62fbde11
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_Declare(zenoh_cpp
  URL https://github.com/eclipse-zenoh/zenoh-cpp/releases/download/1.10.1/zenohcpp-1.10.1-standalone.zip
  URL_HASH SHA256=83f50d1b26d708da70da54862708a131cb903b4ce99ba5e12bbe88a383a8ea49
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(zenoh_c zenoh_cpp)
find_package(zenohc 1.10.1 EXACT CONFIG REQUIRED
  PATHS "${zenoh_c_SOURCE_DIR}/lib/cmake/zenohc" NO_DEFAULT_PATH)
find_package(zenohcxx 1.10.1 EXACT CONFIG REQUIRED
  PATHS "${zenoh_cpp_SOURCE_DIR}/lib/cmake/zenohcxx" NO_DEFAULT_PATH)

add_library(camera_zenoh SHARED src/zenoh_image_publisher.cpp)
target_link_libraries(camera_zenoh PRIVATE godot-cpp zenohcxx::zenohc)
target_compile_features(camera_zenoh PRIVATE cxx_std_20)
set_target_properties(camera_zenoh PROPERTIES
  CXX_STANDARD_REQUIRED ON
  CXX_EXTENSIONS OFF
  BUILD_RPATH "$ORIGIN"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/demo/bin")
target_compile_options(camera_zenoh PRIVATE -Wall -Wextra -Wpedantic)
add_custom_command(TARGET camera_zenoh POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${zenoh_c_SOURCE_DIR}/lib/libzenohc.so"
    "${CMAKE_CURRENT_SOURCE_DIR}/demo/bin/libzenohc.so")
