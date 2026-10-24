#!/usr/bin/env bash
# 把 linux/build.sh 的构建结果安装到 /opt/ATK-Logic，并创建桌面图标与命令行入口
#   sudo linux/install.sh            # 安装
#   sudo linux/install.sh --remove   # 卸载
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/build/linux-release"
DEST=/opt/ATK-Logic
if [[ $EUID -ne 0 ]]; then exec sudo "$0" "$@"; fi

if [[ "${1:-}" == "--remove" ]]; then
    rm -rf "$DEST" /usr/local/bin/atk-logic-gui /usr/share/applications/atk-logic.desktop \
           /usr/share/pixmaps/atk-logic.png /etc/udev/rules.d/99-atk-logic.rules
    echo "已卸载"; exit 0
fi

[[ -x "$SRC/ATK-Logic" ]] || { echo "未找到 $SRC/ATK-Logic，请先运行 linux/build.sh" >&2; exit 1; }
mkdir -p "$DEST"
cp -a "$SRC/ATK-Logic" "$SRC/lib" "$SRC/decoders" "$SRC/test" "$DEST/"
cp -a "$SRC"/*.pdf "$DEST/" 2>/dev/null || true
ln -sf "$DEST/ATK-Logic" /usr/local/bin/atk-logic-gui
install -Dm0644 "$ROOT/resource/icon/icon.png" /usr/share/pixmaps/atk-logic.png
install -Dm0644 "$ROOT/linux/atk-logic.desktop" /usr/share/applications/atk-logic.desktop
"$ROOT/linux/install-udev-rules.sh"
echo "已安装到 $DEST，可在应用菜单中打开 ATK-Logic，或运行 atk-logic-gui"
