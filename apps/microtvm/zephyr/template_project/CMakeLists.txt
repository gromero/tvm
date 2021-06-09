# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.13.1)

set(ENV{QEMU_BIN_PATH} "${CMAKE_SOURCE_DIR}/qemu-hack")

set(QEMU_PIPE "\${QEMU_PIPE}")  # QEMU_PIPE is set by the calling TVM instance.

find_package(Zephyr HINTS $ENV{ZEPHYR_BASE})
project(microtvm_zephyr_runtime)

set(CRT_LIBS utvm_rpc_server utvm_rpc_common common)
set(CRT_LIB_BASE crt/src/runtime/crt)
#file(GLOB CRT_LIB_DIRS crt/src/runtime/crt/*)
foreach(crt_lib_name ${CRT_LIBS})
  zephyr_library_named(${crt_lib_name})
  file(GLOB_RECURSE crt_lib_srcs ${CRT_LIB_BASE}/${crt_lib_name}/*.c ${CRT_LIB_BASE}/${crt_lib_name}/*.cc)
  target_sources(${crt_lib_name} PRIVATE ${crt_lib_srcs})
  zephyr_library_include_directories(${crt_lib_name} PRIVATE crt_config crt/include)
  target_link_libraries(app PRIVATE ${crt_lib_name})
endforeach(crt_lib_name ${CRT_LIBS})

# define a library for the model sources.
zephyr_library_named(tvm_model)
file(GLOB_RECURSE tvm_model_srcs model/codegen/host/src/*.c model/codegen/host/lib/*.o)
target_sources(tvm_model PRIVATE ${tvm_model_srcs})
target_include_directories(tvm_model PRIVATE crt_config crt/include)
target_compile_options(tvm_model PRIVATE -Wno-unused-variable)  # TVM-generated code tends to include lots of these.
target_link_libraries(app PRIVATE tvm_model)

target_sources(app PRIVATE src/main.c)
target_include_directories(app PRIVATE crt_config crt/include)
