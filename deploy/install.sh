#!/usr/bin/env bash
set -Eeuo pipefail

# GameDeck production installer
# Supported: Ubuntu, Debian, CentOS Stream, Rocky Linux, AlmaLinux
# Run from the project directory: sudo bash deploy/install.sh

APP_USER="${APP_USER:-gameserver}"
APP_ROOT="${APP_ROOT:-/opt/game-dashboard}"
GAME_ROOT="${GAME_ROOT:-/srv/games}"
DATA_ROOT="${DATA_ROOT:-/var/lib/game-dashboard}"
ENV_FILE="${ENV_FILE:-/etc/game-dashboard.env}"
INTERNAL_PORT="${INTERNAL_PORT:-8000}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8443}"
DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-admin}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXISTING_PASSWORD_HASH=""
EXISTING_CSRF_TOKEN=""
GENERATED_PASSWORD=""

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 运行：sudo bash deploy/install.sh" >&2
  exit 1
fi

if [[ -f "${ENV_FILE}" ]]; then
  EXISTING_PASSWORD_HASH="$(sed -n 's/^DASHBOARD_PASSWORD_HASH=//p' "${ENV_FILE}" | head -n 1)"
  EXISTING_CSRF_TOKEN="$(sed -n 's/^DASHBOARD_CSRF_TOKEN=//p' "${ENV_FILE}" | head -n 1)"
  EXISTING_USERNAME="$(sed -n 's/^DASHBOARD_USERNAME=//p' "${ENV_FILE}" | head -n 1)"
  [[ -n "${EXISTING_USERNAME}" ]] && DASHBOARD_USERNAME="${EXISTING_USERNAME}"
fi

if [[ ! -f /etc/os-release ]]; then
  echo "无法识别 Linux 发行版。" >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
DISTRO_ID="${ID,,}"
DISTRO_LIKE="${ID_LIKE:-}"

detect_server_ip() {
  if [[ -n "${SERVER_IP:-}" ]]; then
    printf '%s' "${SERVER_IP}"
    return
  fi
  local public_ip
  public_ip="$(curl -4fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  if [[ "${public_ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    printf '%s' "${public_ip}"
    return
  fi
  ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
}

install_packages() {
  if [[ "${DISTRO_ID}" == "ubuntu" || "${DISTRO_ID}" == "debian" || "${DISTRO_LIKE}" == *"debian"* ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 python3-venv python3-pip nginx curl ca-certificates openssl rsync tar sudo \
      lib32gcc-s1 openjdk-21-jre-headless || \
      apt-get install -y python3 python3-venv python3-pip nginx curl ca-certificates openssl rsync tar sudo \
        lib32gcc-s1 default-jre-headless
    NGINX_USER="www-data"
  elif [[ "${DISTRO_ID}" =~ ^(centos|rhel|rocky|almalinux|fedora)$ || "${DISTRO_LIKE}" == *"rhel"* ]]; then
    local package_manager="dnf"
    command -v dnf >/dev/null 2>&1 || package_manager="yum"
    "${package_manager}" install -y python3 python3-pip nginx curl ca-certificates openssl rsync tar sudo \
      policycoreutils-python-utils glibc.i686 libstdc++.i686 java-21-openjdk-headless || \
      "${package_manager}" install -y python3 python3-pip nginx curl ca-certificates openssl rsync tar sudo \
        policycoreutils-python-utils glibc.i686 libstdc++.i686 java-17-openjdk-headless
    NGINX_USER="nginx"
  else
    echo "不支持的发行版：${PRETTY_NAME:-$DISTRO_ID}" >&2
    exit 1
  fi
}

read_credentials() {
  if [[ -n "${EXISTING_PASSWORD_HASH}" && -z "${DASHBOARD_PASSWORD:-}" ]]; then
    echo "检测到现有 Dashboard 凭据，将保留用户名和密码。"
    return
  fi
  if [[ -z "${DASHBOARD_PASSWORD:-}" ]]; then
    GENERATED_PASSWORD="$(openssl rand -hex 10)"
    DASHBOARD_PASSWORD="${GENERATED_PASSWORD}"
  fi
  if (( ${#DASHBOARD_PASSWORD} < 12 )); then
    echo "DASHBOARD_PASSWORD 至少需要 12 位。" >&2
    exit 1
  fi
}

create_user_and_directories() {
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/${APP_USER}" --shell /bin/bash "${APP_USER}"
  fi

  install -d -o root -g "${APP_USER}" -m 0750 "${APP_ROOT}"
  install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "${DATA_ROOT}"
  for game in palworld minecraft; do
    for folder in server data config backups; do
      install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "${GAME_ROOT}/${game}/${folder}"
    done
  done

  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
    "${SOURCE_DIR}/" "${APP_ROOT}/"
  chown -R root:"${APP_USER}" "${APP_ROOT}"
  find "${APP_ROOT}" -type d -exec chmod 0750 {} +
  find "${APP_ROOT}" -type f -exec chmod 0640 {} +
}

install_python_environment() {
  python3 -m venv "${APP_ROOT}/.venv"
  "${APP_ROOT}/.venv/bin/python" -m pip install --upgrade pip
  "${APP_ROOT}/.venv/bin/pip" install -r "${APP_ROOT}/requirements.txt"
  chown -R root:"${APP_USER}" "${APP_ROOT}/.venv"
  chmod -R g+rX,o-rwx "${APP_ROOT}/.venv"
}

install_steamcmd() {
  install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 /opt/steamcmd
  if [[ ! -x /opt/steamcmd/steamcmd.sh ]]; then
    curl --fail --location --retry 3 \
      https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
      -o /tmp/steamcmd_linux.tar.gz
    tar -xzf /tmp/steamcmd_linux.tar.gz -C /opt/steamcmd
    rm -f /tmp/steamcmd_linux.tar.gz
    chown -R "${APP_USER}:${APP_USER}" /opt/steamcmd
  fi
  if [[ -f /opt/steamcmd/linux64/steamclient.so ]]; then
    install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "/home/${APP_USER}/.steam/sdk64"
    install -o "${APP_USER}" -g "${APP_USER}" -m 0644 \
      /opt/steamcmd/linux64/steamclient.so "/home/${APP_USER}/.steam/sdk64/steamclient.so"
  fi
}

write_environment() {
  local password_hash csrf_token
  if [[ -n "${EXISTING_PASSWORD_HASH}" && -z "${DASHBOARD_PASSWORD:-}" ]]; then
    password_hash="${EXISTING_PASSWORD_HASH}"
  else
    password_hash="$("${APP_ROOT}/.venv/bin/python" -c \
      'import sys; from werkzeug.security import generate_password_hash; print(generate_password_hash(sys.argv[1]))' \
      "${DASHBOARD_PASSWORD}")"
  fi
  csrf_token="${EXISTING_CSRF_TOKEN:-$(openssl rand -hex 32)}"
  umask 077
  cat > "${ENV_FILE}" <<EOF
GAME_DASHBOARD_EXECUTE=1
GAME_DASHBOARD_DATA=${DATA_ROOT}
GAME_ROOT=${GAME_ROOT}
STEAMCMD_PATH=/opt/steamcmd/steamcmd.sh
DASHBOARD_USERNAME=${DASHBOARD_USERNAME}
DASHBOARD_PASSWORD_HASH=${password_hash}
DASHBOARD_CSRF_TOKEN=${csrf_token}
EOF
  chown root:"${APP_USER}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
}

write_dashboard_service() {
  cat > /etc/systemd/system/game-dashboard.service <<EOF
[Unit]
Description=GameDeck Flask Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_ROOT}/.venv/bin/python -m gunicorn --workers 1 --threads 8 --timeout 1800 --bind 127.0.0.1:${INTERNAL_PORT} app:app
Restart=on-failure
RestartSec=5
NoNewPrivileges=false
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${DATA_ROOT} ${GAME_ROOT} /opt/steamcmd /home/${APP_USER}

[Install]
WantedBy=multi-user.target
EOF
}

write_game_services() {
  cat > /etc/systemd/system/palworld.service <<EOF
[Unit]
Description=Palworld Dedicated Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
Environment=HOME=/home/${APP_USER}
Environment=SteamAppId=2394010
WorkingDirectory=${GAME_ROOT}/palworld/server
ExecStart=${GAME_ROOT}/palworld/server/PalServer.sh -useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS
Restart=on-failure
RestartSec=10
TimeoutStopSec=60
LimitNOFILE=100000
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${GAME_ROOT}/palworld /home/${APP_USER}

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/minecraft.service <<EOF
[Unit]
Description=Minecraft Java Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${GAME_ROOT}/minecraft/server
ExecStart=/usr/local/libexec/gamedash-minecraft-start
Restart=on-failure
RestartSec=10
TimeoutStopSec=60
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${GAME_ROOT}/minecraft

[Install]
WantedBy=multi-user.target
EOF
}

write_helpers_and_sudoers() {
  install -d -o root -g root -m 0755 /usr/local/libexec
  cat > /usr/local/libexec/gamedash-minecraft-start <<EOF
#!/bin/sh
set -eu
JAVA_XMS=2G
JAVA_XMX=6G
if [ -r "${GAME_ROOT}/minecraft/config/runtime.env" ]; then
  . "${GAME_ROOT}/minecraft/config/runtime.env"
fi
exec /usr/bin/java -Xms"\${JAVA_XMS}" -Xmx"\${JAVA_XMX}" -jar "${GAME_ROOT}/minecraft/server/server.jar" nogui
EOF
  chown root:root /usr/local/libexec/gamedash-minecraft-start
  chmod 0755 /usr/local/libexec/gamedash-minecraft-start

  cat > /etc/sudoers.d/game-dashboard <<EOF
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start palworld.service
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl stop palworld.service
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart palworld.service
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start minecraft.service
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl stop minecraft.service
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart minecraft.service
EOF
  chmod 0440 /etc/sudoers.d/game-dashboard
  visudo -cf /etc/sudoers.d/game-dashboard
}

write_tls_and_nginx() {
  local server_ip
  server_ip="$(detect_server_ip)"
  server_ip="${server_ip:-127.0.0.1}"
  install -d -m 0700 /etc/game-dashboard/tls
  local certificate_needs_update="false"
  if [[ ! -f /etc/game-dashboard/tls/dashboard.key || ! -f /etc/game-dashboard/tls/dashboard.crt ]]; then
    certificate_needs_update="true"
  elif ! openssl x509 -in /etc/game-dashboard/tls/dashboard.crt -noout -ext subjectAltName 2>/dev/null | grep -Fq "IP Address:${server_ip}"; then
    certificate_needs_update="true"
  fi
  if [[ "${certificate_needs_update}" == "true" ]]; then
    rm -f /etc/game-dashboard/tls/dashboard.key /etc/game-dashboard/tls/dashboard.crt
    openssl req -x509 -nodes -newkey rsa:3072 -days 825 \
      -keyout /etc/game-dashboard/tls/dashboard.key \
      -out /etc/game-dashboard/tls/dashboard.crt \
      -subj "/CN=${server_ip}" \
      -addext "subjectAltName=IP:${server_ip}"
    chmod 0600 /etc/game-dashboard/tls/dashboard.key
    chmod 0644 /etc/game-dashboard/tls/dashboard.crt
  fi

  cat > /etc/nginx/conf.d/game-dashboard.conf <<EOF
server {
    listen ${DASHBOARD_PORT} ssl;
    listen [::]:${DASHBOARD_PORT} ssl;
    server_name _;

    ssl_certificate /etc/game-dashboard/tls/dashboard.crt;
    ssl_certificate_key /etc/game-dashboard/tls/dashboard.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:${INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 1800;
    }
}
EOF

  rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
  if command -v getenforce >/dev/null 2>&1 && command -v setsebool >/dev/null 2>&1 && [[ "$(getenforce)" != "Disabled" ]]; then
    setsebool -P httpd_can_network_connect 1
  fi
  nginx -t
}

enable_services() {
  systemctl daemon-reload
  systemctl enable game-dashboard.service
  systemctl restart game-dashboard.service
  systemctl enable nginx
  systemctl restart nginx
}

print_summary() {
  local server_ip
  server_ip="$(detect_server_ip)"
  echo
  echo "GameDeck 安装完成"
  echo "访问地址：https://${server_ip:-服务器IP}:${DASHBOARD_PORT}"
  echo "用户名：${DASHBOARD_USERNAME}"
  if [[ -n "${GENERATED_PASSWORD}" ]]; then
    echo "首次登录密码：${GENERATED_PASSWORD}"
    echo "请立即保存此密码；安装脚本不会以明文保存。"
  else
    echo "登录密码：保留现有密码"
  fi
  echo
  echo "正式执行模式已启用：GAME_DASHBOARD_EXECUTE=1"
  echo "当前未安装任何游戏服务端。请进入 Dashboard 后按需点击“开始安装”。"
  echo "请在服务器防火墙和云安全组中开放 TCP ${DASHBOARD_PORT}。"
  echo "自签名证书首次访问会出现浏览器警告，这是预期行为。"
}

install_packages
read_credentials
create_user_and_directories
install_python_environment
install_steamcmd
write_environment
write_dashboard_service
write_game_services
write_helpers_and_sudoers
write_tls_and_nginx
enable_services
print_summary
