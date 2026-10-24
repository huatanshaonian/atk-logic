# 在 Linux 上把 atk_libsigrokdecode 编译成 libatk_decoder.so
#
# 用法:
#   cmake -S linux/decoder -B build/decoder -DATK_SRD_DIR=/path/to/atk_libsigrokdecode
#   cmake --build build/decoder -j
#
# 上游仓库: https://github.com/alientek-openedv/atk_libsigrokdecode
# 上游只提供 autotools 构建且不包含 atk_decoder.c，这里直接列出源文件。

cmake_minimum_required(VERSION 3.13)
project(atk_decoder C)

set(ATK_SRD_DIR "" CACHE PATH "atk_libsigrokdecode 源码目录")
if(NOT ATK_SRD_DIR OR NOT EXISTS "${ATK_SRD_DIR}/atk_decoder.c")
    message(FATAL_ERROR "请用 -DATK_SRD_DIR=<atk_libsigrokdecode 源码目录> 指定上游源码")
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB REQUIRED IMPORTED_TARGET glib-2.0>=2.34)

# 优先使用 python3-embed（Python >= 3.8 才会带 -lpython3.x）
set(ATK_PYTHON_PC "" CACHE STRING "指定 pkg-config 的 python 模块名，例如 python-3.12-embed")
if(ATK_PYTHON_PC)
    pkg_check_modules(PY REQUIRED IMPORTED_TARGET ${ATK_PYTHON_PC})
else()
    pkg_search_module(PY REQUIRED IMPORTED_TARGET python3-embed python3)
endif()
message(STATUS "Python for decoders: ${PY_VERSION} (${PY_LINK_LIBRARIES})")

# --- 生成 config.h / version.h -------------------------------------------
set(SRD_VER_MAJOR 0)
set(SRD_VER_MINOR 6)
set(SRD_VER_MICRO 0)
set(GEN_DIR "${CMAKE_CURRENT_BINARY_DIR}/gen")
file(MAKE_DIRECTORY "${GEN_DIR}")
file(WRITE "${GEN_DIR}/config.h"
"#ifndef ATK_SRD_CONFIG_H
#define ATK_SRD_CONFIG_H
#define PACKAGE_TARNAME \"libsigrokdecode\"
#define PACKAGE_NAME \"libsigrokdecode\"
#define PACKAGE_VERSION \"${SRD_VER_MAJOR}.${SRD_VER_MINOR}.${SRD_VER_MICRO}\"
#define CONF_HOST \"${CMAKE_SYSTEM_PROCESSOR}-linux\"
#define _GNU_SOURCE 1
#endif
")
file(WRITE "${GEN_DIR}/version.h"
"#ifndef LIBSIGROKDECODE_VERSION_H
#define LIBSIGROKDECODE_VERSION_H
#define SRD_PACKAGE_VERSION_MAJOR ${SRD_VER_MAJOR}
#define SRD_PACKAGE_VERSION_MINOR ${SRD_VER_MINOR}
#define SRD_PACKAGE_VERSION_MICRO ${SRD_VER_MICRO}
#define SRD_PACKAGE_VERSION_STRING \"${SRD_VER_MAJOR}.${SRD_VER_MINOR}.${SRD_VER_MICRO}\"
#define SRD_LIB_VERSION_CURRENT 4
#define SRD_LIB_VERSION_REVISION 0
#define SRD_LIB_VERSION_AGE 0
#define SRD_LIB_VERSION_STRING \"4:0:0\"
#endif
")

# --- Linux 修正: atk_decoder_init() 无条件把 PYTHONHOME 设成程序目录 ------------
# Windows 版在程序目录自带 Python，所以这样做没问题；Linux 用系统 Python，
# 若强行设置 PYTHONHOME，解释器会找不到 encodings 模块而初始化失败。
# 这里只在程序目录确实带有 Python 标准库时才设置 PYTHONHOME。
file(READ "${ATK_SRD_DIR}/atk_decoder.c" ATK_DECODER_SRC)
set(_orig "_decoder_setenv(\"PYTHONHOME\",path, 1);")
string(FIND "${ATK_DECODER_SRC}" "${_orig}" _pos)
if(_pos EQUAL -1)
    message(FATAL_ERROR "atk_decoder.c 与预期不符（找不到 PYTHONHOME 设置语句），请检查上游版本")
endif()
string(REPLACE "${_orig}"
    "if (atk_linux_has_bundled_python(path)) _decoder_setenv(\"PYTHONHOME\",path, 1);"
    ATK_DECODER_SRC "${ATK_DECODER_SRC}")
set(_helper "
/* ---- added by linux/decoder/CMakeLists.txt ---- */
#include <stdio.h>
#include <glib.h>
static int atk_linux_has_bundled_python(const char *path)
{
    char probe[1100];
    snprintf(probe, sizeof(probe), \"%s/lib/python%d.%d/encodings\", path, PY_MAJOR_VERSION, PY_MINOR_VERSION);
    return g_file_test(probe, G_FILE_TEST_IS_DIR);
}
/* ----------------------------------------------- */
")
string(REPLACE "/*********************  srd.c **************************/"
    "${_helper}/*********************  srd.c **************************/"
    ATK_DECODER_SRC "${ATK_DECODER_SRC}")
file(WRITE "${GEN_DIR}/atk_decoder_linux.c" "${ATK_DECODER_SRC}")

set(SRD_SOURCES
    srd.c session.c decoder.c instance.c log.c util.c exception.c
    module_sigrokdecode.c type_decoder.c error.c version.c)
list(TRANSFORM SRD_SOURCES PREPEND "${ATK_SRD_DIR}/")

add_library(atk_decoder SHARED ${SRD_SOURCES} "${GEN_DIR}/atk_decoder_linux.c")
target_include_directories(atk_decoder PRIVATE "${GEN_DIR}" "${ATK_SRD_DIR}")
target_compile_definitions(atk_decoder PRIVATE HAVE_CONFIG_H)
# 上游头文件已不再包含 version.h，这里强制包含
target_compile_options(atk_decoder PRIVATE -include "${GEN_DIR}/version.h"
    -Wno-deprecated-declarations -Wno-unused-parameter)
target_link_libraries(atk_decoder PRIVATE PkgConfig::GLIB PkgConfig::PY)
set_target_properties(atk_decoder PROPERTIES
    C_STANDARD 99
    C_EXTENSIONS ON
    VERSION 1.0.0
    SOVERSION 1)

install(TARGETS atk_decoder LIBRARY DESTINATION lib)
install(FILES "${ATK_SRD_DIR}/atk_decoder.h" DESTINATION include)
