#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-/etc/game-dashboard.env}"
APP_ROOT="${APP_ROOT:-/opt/game-dashboard}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 运行：sudo bash set-password.sh" >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" || ! -x "${APP_ROOT}/.venv/bin/python" ]]; then
  echo "未找到已安装的 GameDeck，请先运行 install.sh。" >&2
  exit 1
fi

read -r -s -p "输入新的 Dashboard 密码（至少 12 位）：" new_password
echo
read -r -s -p "再次输入新密码：" confirmation
echo

if [[ "${new_password}" != "${confirmation}" ]]; then
  echo "两次输入的密码不一致。" >&2
  exit 1
fi
if (( ${#new_password} < 12 )); then
  echo "密码至少需要 12 位。" >&2
  exit 1
fi

password_hash="$("${APP_ROOT}/.venv/bin/python" -c \
  'import sys; from werkzeug.security import generate_password_hash; print(generate_password_hash(sys.argv[1]))' \
  "${new_password}")"
temporary="$(mktemp)"
trap 'rm -f "${temporary}"' EXIT

awk -v password_hash="${password_hash}" '
  BEGIN { replaced=0 }
  /^DASHBOARD_PASSWORD_HASH=/ {
    print "DASHBOARD_PASSWORD_HASH=" password_hash
    replaced=1
    next
  }
  { print }
  END {
    if (!replaced) print "DASHBOARD_PASSWORD_HASH=" password_hash
  }
' "${ENV_FILE}" > "${temporary}"

owner_group="$(stat -c '%U:%G' "${ENV_FILE}")"
install -o "${owner_group%:*}" -g "${owner_group#*:}" -m 0640 "${temporary}" "${ENV_FILE}"
systemctl restart game-dashboard.service
echo "Dashboard 密码已更新，请打开登录页面使用新密码登录。"
