QT += core gui quick network gui-private widgets

CONFIG += c++11
CONFIG += precompile_header
CONFIG += thread exceptions rtti stl
CONFIG += debug_and_release

TARGET = ATK-Logic

PRECOMPILED_HEADER = $$PWD/stdafx.h

VERSION = 1.0.6.6
DEFINES += APP_VERSION_NUM=1066
DEFINES += APP_VERSION=\\\"$$VERSION\\\"
DEFINES += FPGA_MIN_VERSION_NUM=115

QMAKE_TARGET_COMPANY ="ALIENTEK"
QMAKE_TARGET_DESCRIPTION = "ATK-LogicView"
QMAKE_TARGET_COPYRIGHT = "Copyright (C) 2022. ALIENTEK All Rights Reserved."
QMAKE_TARGET_PRODUCT = "ATK-LogicView"
RC_ICONS = app.ico
RC_LANG = 0x0004

Debug: DST_DIR_DLL = $$OUT_PWD\\debug
Release: DST_DIR_DLL = $$OUT_PWD\\Release
DST_DIR_DLL = $$replace(DST_DIR_DLL, /, \\)

SRC_DIR_DLL = $$PWD\\runtime
SRC_DIR_DLL = $$replace(SRC_DIR_DLL, /, \\)

SOURCES += \
        main.cpp \
        pv/controller/session_error.cpp \
        pv/controller/setting.cpp \
        pv/data/analysis.cpp \
        pv/data/decode_controller.cpp \
        pv/draw/double_buffering.cpp \
        pv/draw/draw_background.cpp \
        pv/draw/draw_background_floor.cpp \
        pv/model/decode_list_model.cpp \
        pv/model/decode_table_model.cpp \
        pv/model/measure_tree_view_model.cpp \
        pv/model/search_table_model.cpp \
        pv/model/test_file_table_model.cpp \
        pv/model/vernier_list_model.cpp \
        pv/static/clipboard.cpp \
        pv/static/decode_service.cpp \
        pv/static/fpsitem.cpp \
        pv/static/log_help.cpp \
        pv/static/menustyle.cpp \
        pv/static/shared_memory_helper.cpp \
        pv/static/shortcut_listener.cpp \
        pv/static/util.cpp \
        pv/static/data_service.cpp \
        pv/controller/session_controller.cpp \
        pv/data/session.cpp \
        pv/data/session_config.cpp \
        pv/data/session_show_config.cpp \
        pv/data/segment.cpp \
        pv/draw/frameless_window.cpp \
        pv/draw/draw_channel.cpp \
        pv/draw/draw_channel_header.cpp \
        pv/static/window_error.cpp \
        pv/static/zip_helper.cpp \
        pv/thread/connect.cpp \
        pv/thread/sharedthread.cpp \
        pv/thread/thread_download.cpp \
        pv/thread/thread_read.cpp \
        pv/thread/thread_work.cpp \
        pv/usb/usb_base.cpp \
        pv/usb/usb_control.cpp \
        pv/usb/usb_hotplug.cpp \
        pv/usb/usb_server.cpp \
        pv/utils/qtlockedfile/qtlockedfile.cpp

# QtLockedFile 的平台实现
win32: SOURCES += pv/utils/qtlockedfile/qtlockedfile_win.cpp
unix:  SOURCES += pv/utils/qtlockedfile/qtlockedfile_unix.cpp

HEADERS += \
        lib/include/atk_decoder.h \
        pv/controller/session_error.h \
        pv/controller/setting.h \
        pv/data/analysis.h \
        pv/data/decode_controller.h \
        pv/draw/double_buffering.h \
        pv/draw/draw_background.h \
        pv/draw/draw_background_floor.h \
        pv/model/decode_list_model.h \
        pv/model/decode_table_model.h \
        pv/model/measure_tree_view_model.h \
        pv/model/search_table_model.h \
        pv/model/test_file_table_model.h \
        pv/model/vernier_list_model.h \
        pv/static/clipboard.h \
        pv/static/decode_service.h \
        pv/static/fpsitem.h \
        pv/static/log_help.h \
        pv/static/menustyle.h \
        pv/static/shared_memory_helper.h \
        pv/static/shortcut_listener.h \
        pv/static/util.h \
        pv/static/data_service.h \
        pv/controller/session_controller.h \
        pv/data/session.h \
        pv/data/session_config.h \
        pv/data/session_show_config.h \
        pv/data/segment.h \
        pv/draw/frameless_window.h \
        pv/draw/draw_channel.h \
        pv/draw/draw_channel_header.h \
        pv/static/window_error.h \
        pv/static/zip_helper.h \
        pv/thread/connect.h \
        pv/thread/sharedthread.h \
        pv/thread/thread_download.h \
        pv/thread/thread_read.h \
        pv/thread/thread_work.h \
        pv/usb/usb_base.h \
        pv/usb/usb_control.h \
        pv/usb/usb_hotplug.h \
        pv/usb/usb_server.h \
        pv/utils/qtlockedfile/qtlockedfile.h

# atk_decoder.h 所在目录（lib/include，见 README 源码环境说明）
INCLUDEPATH += $$PWD/lib/include

win32 {
        QMAKE_CXXFLAGS += /utf-8
        QMAKE_POST_LINK += xcopy /s /y \"$$SRC_DIR_DLL\" \"$$DST_DIR_DLL\"
    }

# QBreakpad 崩溃转储只在 Windows/MSVC 下启用
win32-msvc* {
        DEFINES += ATK_USE_QBREAKPAD
    }

# ---------------------------------------------------------------------------
# Linux 构建（详见 linux/README.md，一键脚本 linux/build.sh）
#   依赖: Qt 5.15 (quick/widgets/gui-private), libusb-1.0, libX11, libatk_decoder.so
#   ATK_DECODER_DIR: libatk_decoder 安装前缀，内含 include/atk_decoder.h 与 lib/libatk_decoder.so
#   可通过 qmake ATK_DECODER_DIR=/path 或同名环境变量指定，默认 $$PWD/build/decoder-install
# ---------------------------------------------------------------------------
unix:!macx {
        isEmpty(ATK_DECODER_DIR): ATK_DECODER_DIR = $$(ATK_DECODER_DIR)
        isEmpty(ATK_DECODER_DIR): ATK_DECODER_DIR = $$PWD/build/decoder-install
        !exists($$ATK_DECODER_DIR/include/atk_decoder.h) {
            warning("未找到 $$ATK_DECODER_DIR/include/atk_decoder.h，请先运行 linux/build.sh 或设置 ATK_DECODER_DIR")
        }
        INCLUDEPATH += $$ATK_DECODER_DIR/include
        LIBS += -L$$ATK_DECODER_DIR/lib -latk_decoder

        QT += x11extras
        CONFIG += link_pkgconfig
        PKGCONFIG += libusb-1.0 x11

        # 运行时在程序目录的 lib/ 下查找 libatk_decoder.so
        QMAKE_LFLAGS += "-Wl,-rpath,\'\$$ORIGIN/lib\'"

        # 把解码器脚本、测试文件、手册以及 libatk_decoder.so 复制到程序目录
        DESTDIR = $$OUT_PWD
        QMAKE_POST_LINK += mkdir -p \"$$DESTDIR/lib\" && \
            cp -rf \"$$PWD/runtime/.\" \"$$DESTDIR/\" && \
            cp -af \"$$ATK_DECODER_DIR/lib/\"libatk_decoder.so* \"$$DESTDIR/lib/\"
    }

RESOURCES += qml.qrc \
    resource.qrc

TRANSLATIONS += \
    translations/zh_CN.ts \
    translations/en_US.ts


CONFIG += resources_big
CONFIG += lrelease
CONFIG += embed_translations
# Additional import path used to resolve QML modules in Qt Creator's code model
QML_IMPORT_PATH =

# Additional import path used to resolve QML modules just for Qt Quick Designer
QML_DESIGNER_IMPORT_PATH =

# Default rules for deployment.
qnx: target.path = /tmp/$${TARGET}/bin
else: unix:!android: target.path = /opt/$${TARGET}/bin
!isEmpty(target.path): INSTALLS += target

