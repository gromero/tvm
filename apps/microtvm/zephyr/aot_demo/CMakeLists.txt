# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.13.1)

set(ENV{QEMU_BIN_PATH} "${CMAKE_SOURCE_DIR}/qemu-hack")

set(QEMU_PIPE "\${QEMU_PIPE}")  # QEMU_PIPE is set by the calling TVM instance.

find_package(Zephyr HINTS $ENV{ZEPHYR_BASE})
project(microtvm_zephyr_runtime)

# define a library for the model sources.
zephyr_library_named(tvm_model)
file(GLOB_RECURSE tvm_model_srcs model/codegen/host/src/*.c model/codegen/host/lib/*.o)
target_sources(tvm_model PRIVATE ${tvm_model_srcs})
target_include_directories(tvm_model PRIVATE crt_config crt/include)
target_compile_options(tvm_model PRIVATE -Wno-unused-variable)  # TVM-generated code tends to include lots of these.
target_link_libraries(app PRIVATE tvm_model)

target_sources(app PRIVATE src/main.c)
target_include_directories(app PRIVATE crt_config crt/include)
