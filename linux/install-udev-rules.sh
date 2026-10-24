#!/usr/bin/env bash
# 安装 udev 规则，使普通用户可以访问 ATK-Logic 逻辑分析仪（需要 sudo）
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $EUID -ne 0 ]]; then exec sudo "$0" "$@"; fi
install -m 0644 "$HERE/99-atk-logic.rules" /etc/udev/rules.d/99-atk-logic.rules
udevadm control --reload-rules || echo "提示: udev 服务未运行，规则将在下次启动后生效"
udevadm trigger --subsystem-match=usb --attr-match=idVendor=1a86 || true
echo "已安装 /etc/udev/rules.d/99-atk-logic.rules，请重新插拔逻辑分析仪。"
echo "若仍提示无权限，请把用户加入 plugdev 组: sudo usermod -aG plugdev \$USER （需重新登录）"
