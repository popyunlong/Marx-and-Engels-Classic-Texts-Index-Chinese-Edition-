#!/usr/bin/env bash

set -euo pipefail

APP_DIR="/opt/marx-search"
APP_USER="www-data"
APP_GROUP="www-data"
SERVICE_NAME="marx-search"
ENV_PATH="/etc/marx-search.env"
CADDYFILE_PATH="/etc/caddy/Caddyfile"
SYSTEMD_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
DOMAIN=""
PUBLIC_BASE_URL=""
BASIC_AUTH_USER="marxadmin"
BASIC_AUTH_HASH=""
ZAI_API_KEY=""
APP_AI_PROVIDER=""
APP_AI_MODEL=""
APP_AI_BASE_URL=""
APP_AI_API_KEY=""
APP_AI_SEARCH_PROVIDER=""
APP_AI_SEARCH_BASE_URL=""
APP_AI_SEARCH_API_KEY=""
ZPAY_PID=""
ZPAY_KEY=""
ZPAY_SUBMIT_URL="https://zpayz.cn/submit.php"
ZPAY_MAPI_URL="https://zpayz.cn/mapi.php"
ZPAY_API_URL="https://zpayz.cn/api.php"
ZPAY_TYPE="alipay"
ZPAY_CHANNEL_ID=""
ZPAY_NOTIFY_URL=""
ZPAY_RETURN_URL=""
CONFIGURE_UFW="0"

usage() {
  cat <<'EOF'
Usage:
  sudo bash deploy/bootstrap_ubuntu.sh \
    --domain search.example.com

Required arguments:
  --domain              Public domain for the site.

Optional arguments:
  --public-base-url     Defaults to https://<domain>.
  --basic-auth-user     Deprecated; kept for compatibility.
  --basic-auth-hash     Deprecated; kept for compatibility.
  --app-dir             Defaults to /opt/marx-search.
  --app-user            Defaults to www-data.
  --app-group           Defaults to www-data.
  --service-name        Defaults to marx-search.
  --zai-api-key         Leave blank to keep AI disabled.
  --ai-provider         AI model provider, e.g. deepseek or zai.
  --ai-model            AI model id, e.g. deepseek-v4-flash.
  --ai-base-url         AI chat API base URL.
  --ai-api-key          AI chat API key. Overrides --zai-api-key for chat.
  --ai-search-provider  Web search provider, e.g. zai or disabled.
  --ai-search-base-url  Web search API base URL.
  --ai-search-api-key   Web search API key.
  --zpay-pid            ZPay merchant pid. Leave blank to keep payment disabled.
  --zpay-key            ZPay merchant key.
  --zpay-submit-url     Defaults to https://zpayz.cn/submit.php.
  --zpay-type           Payment type: alipay or wxpay.
  --zpay-channel-id     Optional channel id.
  --zpay-notify-url     Optional explicit async notify URL.
  --zpay-return-url     Optional explicit browser return URL.
  --configure-ufw       Open 22/80/443 with ufw if available.
  --help                Show this help.
EOF
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run this script with sudo or as root." >&2
    exit 1
  fi
}

escape_sed() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain)
        DOMAIN="$2"
        shift 2
        ;;
      --public-base-url)
        PUBLIC_BASE_URL="$2"
        shift 2
        ;;
      --basic-auth-user)
        BASIC_AUTH_USER="$2"
        shift 2
        ;;
      --basic-auth-hash)
        BASIC_AUTH_HASH="$2"
        shift 2
        ;;
      --app-dir)
        APP_DIR="$2"
        shift 2
        ;;
      --app-user)
        APP_USER="$2"
        shift 2
        ;;
      --app-group)
        APP_GROUP="$2"
        shift 2
        ;;
      --service-name)
        SERVICE_NAME="$2"
        SYSTEMD_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
        shift 2
        ;;
      --zai-api-key)
        ZAI_API_KEY="$2"
        shift 2
        ;;
      --ai-provider)
        APP_AI_PROVIDER="$2"
        shift 2
        ;;
      --ai-model)
        APP_AI_MODEL="$2"
        shift 2
        ;;
      --ai-base-url)
        APP_AI_BASE_URL="$2"
        shift 2
        ;;
      --ai-api-key)
        APP_AI_API_KEY="$2"
        shift 2
        ;;
      --ai-search-provider)
        APP_AI_SEARCH_PROVIDER="$2"
        shift 2
        ;;
      --ai-search-base-url)
        APP_AI_SEARCH_BASE_URL="$2"
        shift 2
        ;;
      --ai-search-api-key)
        APP_AI_SEARCH_API_KEY="$2"
        shift 2
        ;;
      --zpay-pid)
        ZPAY_PID="$2"
        shift 2
        ;;
      --zpay-key)
        ZPAY_KEY="$2"
        shift 2
        ;;
      --zpay-submit-url)
        ZPAY_SUBMIT_URL="$2"
        shift 2
        ;;
      --zpay-type)
        ZPAY_TYPE="$2"
        shift 2
        ;;
      --zpay-channel-id)
        ZPAY_CHANNEL_ID="$2"
        shift 2
        ;;
      --zpay-notify-url)
        ZPAY_NOTIFY_URL="$2"
        shift 2
        ;;
      --zpay-return-url)
        ZPAY_RETURN_URL="$2"
        shift 2
        ;;
      --configure-ufw)
        CONFIGURE_UFW="1"
        shift
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage
        exit 1
        ;;
    esac
  done

  if [[ -z "${DOMAIN}" ]]; then
    echo "--domain is required." >&2
    exit 1
  fi
  if [[ -z "${PUBLIC_BASE_URL}" ]]; then
    PUBLIC_BASE_URL="https://${DOMAIN}"
  fi
}

read_existing_env() {
  local key="$1"
  local line=""
  if [[ -f "${ENV_PATH}" ]]; then
    line="$(grep -m1 "^${key}=" "${ENV_PATH}" 2>/dev/null || true)"
  fi
  printf '%s' "${line#*=}"
}

preserve_existing_payment_env() {
  local existing=""
  existing="$(read_existing_env ZPAY_PID)"
  if [[ -z "${ZPAY_PID}" && -n "${existing}" ]]; then
    ZPAY_PID="${existing}"
  fi
  existing="$(read_existing_env ZPAY_KEY)"
  if [[ -z "${ZPAY_KEY}" && -n "${existing}" ]]; then
    ZPAY_KEY="${existing}"
  fi
  existing="$(read_existing_env ZPAY_SUBMIT_URL)"
  if [[ "${ZPAY_SUBMIT_URL}" == "https://zpayz.cn/submit.php" && -n "${existing}" ]]; then
    ZPAY_SUBMIT_URL="${existing}"
  fi
  existing="$(read_existing_env ZPAY_MAPI_URL)"
  if [[ "${ZPAY_MAPI_URL}" == "https://zpayz.cn/mapi.php" && -n "${existing}" ]]; then
    ZPAY_MAPI_URL="${existing}"
  fi
  existing="$(read_existing_env ZPAY_API_URL)"
  if [[ "${ZPAY_API_URL}" == "https://zpayz.cn/api.php" && -n "${existing}" ]]; then
    ZPAY_API_URL="${existing}"
  fi
  existing="$(read_existing_env ZPAY_TYPE)"
  if [[ "${ZPAY_TYPE}" == "alipay" && -n "${existing}" ]]; then
    ZPAY_TYPE="${existing}"
  fi
  existing="$(read_existing_env ZPAY_CHANNEL_ID)"
  if [[ -z "${ZPAY_CHANNEL_ID}" && -n "${existing}" ]]; then
    ZPAY_CHANNEL_ID="${existing}"
  fi
  existing="$(read_existing_env ZPAY_NOTIFY_URL)"
  if [[ -z "${ZPAY_NOTIFY_URL}" && -n "${existing}" ]]; then
    ZPAY_NOTIFY_URL="${existing}"
  fi
  existing="$(read_existing_env ZPAY_RETURN_URL)"
  if [[ -z "${ZPAY_RETURN_URL}" && -n "${existing}" ]]; then
    ZPAY_RETURN_URL="${existing}"
  fi
}

check_repo_layout() {
  local required=(
    "app.py"
    "serve.py"
    "runtime_env.py"
    "admin_store.py"
    "desktop_sync.py"
    "journal_alerts.py"
    "zpay.py"
    "requirements.txt"
    "scripts/journal_alert_worker.py"
    "deploy/marx-search.service"
    "deploy/marx-search-journal-alerts.service"
    "deploy/marx-search-journal-alerts.timer"
    "deploy/Caddyfile.example"
    "deploy/marx-search.env.example"
    "config"
    "data"
    "pdfs"
    "static"
    "templates"
  )

  for path in "${required[@]}"; do
    if [[ ! -e "${APP_DIR}/${path}" ]]; then
      echo "Missing required path: ${APP_DIR}/${path}" >&2
      exit 1
    fi
  done
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv python3-pip caddy curl
  if [[ "${CONFIGURE_UFW}" == "1" ]]; then
    apt-get install -y ufw
  fi
}

prepare_app_dir() {
  mkdir -p "${APP_DIR}"
  mkdir -p "${APP_DIR}/logs"
  mkdir -p "${APP_DIR}/releases"
  mkdir -p "${APP_DIR}/config/keys"
  chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
}

setup_python() {
  if [[ ! -d "${APP_DIR}/.venv" ]]; then
    python3 -m venv "${APP_DIR}/.venv"
  fi
  "${APP_DIR}/.venv/bin/pip" install --upgrade pip
  "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
}

write_env_file() {
  cat > "${ENV_PATH}" <<EOF
APP_MODE=server
BIND_HOST=127.0.0.1
PORT=8000
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
ENABLE_BROWSER_AUTOSTART=0
ENABLE_IDLE_SHUTDOWN=0
ENABLE_REMOTE_QUIT=0
ZAI_API_KEY=${ZAI_API_KEY}
APP_AI_PROVIDER=${APP_AI_PROVIDER}
APP_AI_MODEL=${APP_AI_MODEL}
APP_AI_BASE_URL=${APP_AI_BASE_URL}
APP_AI_API_KEY=${APP_AI_API_KEY}
APP_AI_SEARCH_PROVIDER=${APP_AI_SEARCH_PROVIDER}
APP_AI_SEARCH_BASE_URL=${APP_AI_SEARCH_BASE_URL}
APP_AI_SEARCH_API_KEY=${APP_AI_SEARCH_API_KEY}
ZPAY_PID=${ZPAY_PID}
ZPAY_KEY=${ZPAY_KEY}
ZPAY_SUBMIT_URL=${ZPAY_SUBMIT_URL}
ZPAY_MAPI_URL=${ZPAY_MAPI_URL}
ZPAY_API_URL=${ZPAY_API_URL}
ZPAY_TYPE=${ZPAY_TYPE}
ZPAY_CHANNEL_ID=${ZPAY_CHANNEL_ID}
ZPAY_SIGN_TYPE=MD5
ZPAY_SUBJECT_PREFIX=马恩文献检索会员
ZPAY_DEVICE=pc
ZPAY_NOTIFY_URL=${ZPAY_NOTIFY_URL}
ZPAY_RETURN_URL=${ZPAY_RETURN_URL}
JOURNAL_ALERT_BASE_URL=${PUBLIC_BASE_URL}
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM_EMAIL=
SMTP_FROM_NAME=期刊新文提醒
SMTP_USE_TLS=1
EOF
  chmod 640 "${ENV_PATH}"
}

install_systemd_service() {
  local escaped_app_dir escaped_service_name
  escaped_app_dir="$(escape_sed "${APP_DIR}")"
  escaped_service_name="$(escape_sed "${SERVICE_NAME}")"

  sed \
    -e "s|/opt/marx-search|${escaped_app_dir}|g" \
    -e "s|Marx Search Flask Server|Marx Search Flask Server (${escaped_service_name})|g" \
    "${APP_DIR}/deploy/marx-search.service" > "${SYSTEMD_PATH}"
  chmod 644 "${SYSTEMD_PATH}"

  sed \
    -e "s|/opt/marx-search|${escaped_app_dir}|g" \
    "${APP_DIR}/deploy/marx-search-journal-alerts.service" > "/etc/systemd/system/${SERVICE_NAME}-journal-alerts.service"
  sed \
    -e "s|marx-search-journal-alerts.service|${SERVICE_NAME}-journal-alerts.service|g" \
    "${APP_DIR}/deploy/marx-search-journal-alerts.timer" > "/etc/systemd/system/${SERVICE_NAME}-journal-alerts.timer"
  chmod 644 "/etc/systemd/system/${SERVICE_NAME}-journal-alerts.service" "/etc/systemd/system/${SERVICE_NAME}-journal-alerts.timer"

  sed \
    -e "s|/opt/marx-search|${escaped_app_dir}|g" \
    "${APP_DIR}/deploy/marx-search-backup.service" > "/etc/systemd/system/${SERVICE_NAME}-backup.service"
  cp "${APP_DIR}/deploy/marx-search-backup.timer" "/etc/systemd/system/${SERVICE_NAME}-backup.timer"
  chmod 644 "/etc/systemd/system/${SERVICE_NAME}-backup.service" "/etc/systemd/system/${SERVICE_NAME}-backup.timer"
}

install_caddyfile() {
  local escaped_domain escaped_user escaped_hash
  escaped_domain="$(escape_sed "${DOMAIN}")"
  escaped_user="$(escape_sed "${BASIC_AUTH_USER}")"
  escaped_hash="$(escape_sed "${BASIC_AUTH_HASH}")"

  sed \
    -e "s|marx-search.example.com|${escaped_domain}|g" \
    -e "s|marxadmin|${escaped_user}|g" \
    -e "s|\$2a\$14\$REPLACE_WITH_BCRYPT_HASH|${escaped_hash}|g" \
    "${APP_DIR}/deploy/Caddyfile.example" > "${CADDYFILE_PATH}"
  chmod 644 "${CADDYFILE_PATH}"
}

configure_ufw() {
  if [[ "${CONFIGURE_UFW}" != "1" ]]; then
    return
  fi
  if ! command -v ufw >/dev/null 2>&1; then
    echo "ufw is not installed; skipping firewall setup." >&2
    return
  fi

  ufw allow 22/tcp
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
}

enable_services() {
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"
  if ! systemctl restart "${SERVICE_NAME}.service"; then
    systemctl start "${SERVICE_NAME}.service"
  fi
  systemctl enable --now "${SERVICE_NAME}-journal-alerts.timer"
  systemctl enable --now "${SERVICE_NAME}-backup.timer"

  systemctl enable caddy
  if ! systemctl reload caddy; then
    systemctl restart caddy
  fi
}

print_next_steps() {
  cat <<EOF

Bootstrap completed.

Recommended verification:
  systemctl status ${SERVICE_NAME}.service --no-pager
  systemctl status caddy --no-pager
  curl http://127.0.0.1:8000/api/runtime
  journalctl -u ${SERVICE_NAME}.service -n 50 --no-pager

If HTTPS is not ready yet, confirm that DNS for ${DOMAIN} already points to this server.
EOF
}

main() {
  require_root
  parse_args "$@"
  preserve_existing_payment_env
  check_repo_layout
  install_packages
  prepare_app_dir
  setup_python
  write_env_file
  install_systemd_service
  install_caddyfile
  configure_ufw
  enable_services
  print_next_steps
}

main "$@"
