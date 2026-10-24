#!/usr/bin/env bash
# ATK-Logic Linux 一键构建脚本
#
#   linux/build.sh                 # 构建到 build/linux-release
#   linux/build.sh --deps          # 先安装依赖（Debian/Ubuntu，需要 sudo）
#   ATK_SRD_DIR=/path/to/atk_libsigrokdecode linux/build.sh   # 使用本地已有的解码库源码
#
# 产物: build/linux-release/ATK-Logic （同目录下有 decoders/、lib/libatk_decoder.so 等）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
SRD_REPO="https://github.com/alientek-openedv/atk_libsigrokdecode.git"
# 已验证可用的上游提交；设为空则使用最新代码
SRD_REV="${SRD_REV:-783fec96047f5859c2122ac79123a6929a35f75f}"

if [[ "${1:-}" == "--deps" ]]; then
    sudo apt-get install -y \
        build-essential cmake pkg-config git \
        qtbase5-dev qtbase5-private-dev libqt5x11extras5-dev qtdeclarative5-dev qtquickcontrols2-5-dev qttools5-dev-tools \
        qml-module-qtquick-controls qml-module-qtquick-controls2 qml-module-qtquick-dialogs \
        qml-module-qtquick-layouts qml-module-qtquick-window2 qml-module-qt-labs-platform \
        qml-module-qt-labs-settings qml-module-qtgraphicaleffects qml-module-qtqml-models2 \
        libusb-1.0-0-dev libx11-dev libglib2.0-dev python3-dev
fi

# 1. 获取并编译 libatk_decoder.so
SRD_DIR="${ATK_SRD_DIR:-$BUILD/atk_libsigrokdecode}"
if [[ ! -f "$SRD_DIR/atk_decoder.c" ]]; then
    echo "==> 下载 atk_libsigrokdecode 到 $SRD_DIR"
    git clone "$SRD_REPO" "$SRD_DIR"
    if [[ -n "$SRD_REV" ]]; then git -C "$SRD_DIR" checkout -q "$SRD_REV"; fi
fi

echo "==> 编译 libatk_decoder.so"
cmake -S "$ROOT/linux/decoder" -B "$BUILD/decoder" \
      -DATK_SRD_DIR="$SRD_DIR" -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$BUILD/decoder-install"
cmake --build "$BUILD/decoder" -j "$JOBS"
cmake --install "$BUILD/decoder" >/dev/null

# 2. 编译 ATK-Logic
QMAKE="${QMAKE:-$(command -v qmake-qt5 || command -v qmake || true)}"
if [[ -z "$QMAKE" ]]; then
    echo "找不到 qmake（Qt5），请先运行: $0 --deps" >&2
    exit 1
fi
echo "==> 使用 $QMAKE ($("$QMAKE" -query QT_VERSION))"
mkdir -p "$BUILD/linux-release"
cd "$BUILD/linux-release"
"$QMAKE" "$ROOT/ATK-Logic.pro" CONFIG+=release CONFIG-=debug_and_release \
         ATK_DECODER_DIR="$BUILD/decoder-install"
make -j "$JOBS"

echo
echo "构建完成: $BUILD/linux-release/ATK-Logic"
echo "首次使用请安装 udev 规则以便普通用户访问设备:"
echo "    sudo linux/install-udev-rules.sh"
