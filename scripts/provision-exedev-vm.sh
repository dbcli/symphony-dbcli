#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Provision symphony-dbcli on an exe.dev VM.

Default:
  scripts/provision-exedev-vm.sh

Common:
  scripts/provision-exedev-vm.sh \
    --github-app-id YOUR_GITHUB_APP_ID \
    --github-installation-id YOUR_GITHUB_INSTALLATION_ID \
    --github-private-key-file /path/to/private-key.pem \
    --share-email alice@example.com \
    --share-email bob@example.com

Options:
  --vm NAME                         exe.dev VM name. Default: symphony-dbcli
  --repo OWNER/REPO                 GitHub repo to deploy. Default: amjith/symphony-dbcli
  --git-ref REF                     Branch, tag, or commit to deploy. Default: main
  --remote-dir PATH                 Checkout path on the VM. Default: $HOME/<repo>
  --integration-name NAME           exe.dev GitHub integration name. Default: symphony-dbcli-repo
  --public-clone                    Clone from https://github.com instead of exe.dev GitHub integration
  --clone-url URL                   Clone URL override
  --no-github-integration           Skip exe.dev GitHub repo integration setup
  --github-env-file PATH            Read GitHub App values from an env file. Default: .symphony/github-app.env
  --no-github-env-file              Do not read .symphony/github-app.env
  --github-app-id ID                Symphony GitHub App id
  --github-installation-id ID       Symphony GitHub App installation id
  --github-private-key-file PATH    Local GitHub App private key to copy to the VM
  --share-email EMAIL               Grant exe.dev HTTPS access to an email. Repeatable; comma-separated OK
  --no-share                        Skip exe.dev share port/private/email setup
  --no-start                        Install the systemd unit but do not start/restart it
  --force-start                     Start even if GitHub App credentials are incomplete
  --skip-codex-config               Do not write ~/.codex/config.toml for exe.dev LLM Gateway
  --service-name NAME               systemd unit name. Default: symphony-dbcli
  --git-user-name NAME              git commit author name on the VM. Default: symphony-dbcli
  --git-user-email EMAIL            git commit author email on the VM
  -h, --help                        Show this help

The script is idempotent for normal reprovisioning. It will not overwrite tracked
local changes in an existing remote checkout.
EOF
}

log() {
  printf '\n==> %s\n' "$*" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

csv_to_array() {
  local value="$1"
  local item
  IFS=',' read -r -a _csv_items <<<"$value"
  for item in "${_csv_items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    if [[ -n "$item" ]]; then
      SHARE_EMAILS+=("$item")
    fi
  done
}

read_env_value() {
  local path="$1"
  local target_key="$2"
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
    if [[ "$key" == "$target_key" ]]; then
      printf '%s\n' "$value"
      return 0
    fi
  done <"$path"
  return 1
}

remote_env_assignment() {
  local key="$1"
  local value="$2"
  local quoted
  printf -v quoted "%q" "$value"
  printf "%s=%s" "$key" "$quoted"
}

load_github_env_file() {
  local path="$1"
  local value
  [[ -n "$path" && -f "$path" ]] || return
  log "Reading GitHub App settings from ${path}"
  if [[ -z "$GITHUB_APP_ID" ]] && value="$(read_env_value "$path" SYMPHONY_GITHUB_APP_ID)"; then
    GITHUB_APP_ID="$value"
  fi
  if [[ -z "$GITHUB_INSTALLATION_ID" ]] && value="$(read_env_value "$path" SYMPHONY_GITHUB_INSTALLATION_ID)"; then
    GITHUB_INSTALLATION_ID="$value"
  fi
  if [[ -z "$GITHUB_PRIVATE_KEY_FILE" ]] && value="$(read_env_value "$path" SYMPHONY_GITHUB_PRIVATE_KEY_PATH)"; then
    if [[ "$value" == /* ]]; then
      GITHUB_PRIVATE_KEY_FILE="$value"
    else
      GITHUB_PRIVATE_KEY_FILE="$(cd "$(dirname "$path")" && pwd)/$value"
    fi
  fi
}

VM_NAME="${VM_NAME:-symphony-dbcli}"
REPO="${REPO:-amjith/symphony-dbcli}"
GIT_REF="${GIT_REF:-main}"
REMOTE_DIR="${REMOTE_DIR:-}"
INTEGRATION_NAME="${INTEGRATION_NAME:-symphony-dbcli-repo}"
CLONE_URL="${CLONE_URL:-}"
GITHUB_ENV_FILE="${SYMPHONY_GITHUB_ENV_FILE:-.symphony/github-app.env}"
GITHUB_APP_ID="${SYMPHONY_GITHUB_APP_ID:-}"
GITHUB_INSTALLATION_ID="${SYMPHONY_GITHUB_INSTALLATION_ID:-}"
GITHUB_PRIVATE_KEY_FILE="${SYMPHONY_GITHUB_PRIVATE_KEY_FILE:-}"
CONFIGURE_SHARE=1
USE_PUBLIC_CLONE=0
CONFIGURE_GITHUB_INTEGRATION=1
START_MODE="auto"
CONFIGURE_CODEX=1
SERVICE_NAME="symphony-dbcli"
GIT_USER_NAME="symphony-dbcli"
GIT_USER_EMAIL="symphony-dbcli[bot]@users.noreply.github.com"
SHARE_EMAILS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm)
      VM_NAME="${2:?missing value for --vm}"
      shift 2
      ;;
    --repo)
      REPO="${2:?missing value for --repo}"
      shift 2
      ;;
    --git-ref)
      GIT_REF="${2:?missing value for --git-ref}"
      shift 2
      ;;
    --remote-dir)
      REMOTE_DIR="${2:?missing value for --remote-dir}"
      shift 2
      ;;
    --integration-name)
      INTEGRATION_NAME="${2:?missing value for --integration-name}"
      shift 2
      ;;
    --public-clone)
      USE_PUBLIC_CLONE=1
      CONFIGURE_GITHUB_INTEGRATION=0
      shift
      ;;
    --clone-url)
      CLONE_URL="${2:?missing value for --clone-url}"
      CONFIGURE_GITHUB_INTEGRATION=0
      shift 2
      ;;
    --no-github-integration)
      CONFIGURE_GITHUB_INTEGRATION=0
      shift
      ;;
    --github-env-file)
      GITHUB_ENV_FILE="${2:?missing value for --github-env-file}"
      shift 2
      ;;
    --no-github-env-file)
      GITHUB_ENV_FILE=""
      shift
      ;;
    --github-app-id)
      GITHUB_APP_ID="${2:?missing value for --github-app-id}"
      shift 2
      ;;
    --github-installation-id)
      GITHUB_INSTALLATION_ID="${2:?missing value for --github-installation-id}"
      shift 2
      ;;
    --github-private-key-file)
      GITHUB_PRIVATE_KEY_FILE="${2:?missing value for --github-private-key-file}"
      shift 2
      ;;
    --share-email)
      csv_to_array "${2:?missing value for --share-email}"
      shift 2
      ;;
    --no-share)
      CONFIGURE_SHARE=0
      shift
      ;;
    --no-start)
      START_MODE="never"
      shift
      ;;
    --force-start)
      START_MODE="always"
      shift
      ;;
    --skip-codex-config)
      CONFIGURE_CODEX=0
      shift
      ;;
    --service-name)
      SERVICE_NAME="${2:?missing value for --service-name}"
      shift 2
      ;;
    --git-user-name)
      GIT_USER_NAME="${2:?missing value for --git-user-name}"
      shift 2
      ;;
    --git-user-email)
      GIT_USER_EMAIL="${2:?missing value for --git-user-email}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

load_github_env_file "$GITHUB_ENV_FILE"

require_command ssh
require_command scp

VM_HOST="${VM_NAME}.exe.xyz"

if [[ -z "$CLONE_URL" ]]; then
  if [[ "$USE_PUBLIC_CLONE" -eq 1 ]]; then
    CLONE_URL="https://github.com/${REPO}.git"
  else
    CLONE_URL="https://${INTEGRATION_NAME}.int.exe.xyz/${REPO}.git"
  fi
fi

ensure_github_integration() {
  if [[ "$CONFIGURE_GITHUB_INTEGRATION" -eq 0 ]]; then
    log "Skipping exe.dev GitHub repo integration setup"
    return
  fi

  log "Verifying exe.dev GitHub account connection"
  local verify_output
  if ! verify_output="$(ssh exe.dev integrations setup github --verify 2>&1)"; then
    printf '%s\n' "$verify_output"
    log "Starting exe.dev GitHub setup"
    ssh exe.dev integrations setup github
    verify_output="$(ssh exe.dev integrations setup github --verify 2>&1)"
  elif printf '%s\n' "$verify_output" | grep -Eiq 'no github accounts connected|run: integrations setup github'; then
    printf '%s\n' "$verify_output"
    log "Starting exe.dev GitHub setup"
    ssh exe.dev integrations setup github
    verify_output="$(ssh exe.dev integrations setup github --verify 2>&1)"
  fi
  printf '%s\n' "$verify_output"
  if printf '%s\n' "$verify_output" | grep -Eiq 'no github accounts connected|run: integrations setup github'; then
    die "exe.dev GitHub setup did not complete; run 'ssh exe.dev integrations setup github' and retry"
  fi

  log "Creating or attaching repo integration ${INTEGRATION_NAME}"
  local output
  if output=$(ssh exe.dev integrations add github \
      --name "$INTEGRATION_NAME" \
      --repository "$REPO" \
      --attach "vm:${VM_NAME}" 2>&1); then
    printf '%s\n' "$output"
    return
  fi

  if printf '%s\n' "$output" | grep -Eiq 'already|exists|duplicate|name.*taken'; then
    printf '%s\n' "$output"
    local attach_output
    if attach_output="$(ssh exe.dev integrations attach "$INTEGRATION_NAME" "vm:${VM_NAME}" 2>&1)"; then
      printf '%s\n' "$attach_output"
      return
    fi
    if printf '%s\n' "$attach_output" | grep -Eiq 'already attached|already.*vm'; then
      printf '%s\n' "$attach_output"
      return
    fi
    printf '%s\n' "$attach_output" >&2
    exit 1
    return
  fi

  printf '%s\n' "$output" >&2
  exit 1
}

configure_exedev_share() {
  if [[ "$CONFIGURE_SHARE" -eq 0 ]]; then
    log "Skipping exe.dev share setup"
    return
  fi

  log "Configuring private exe.dev HTTPS proxy on port 8765"
  ssh exe.dev share port "$VM_NAME" 8765
  ssh exe.dev share set-private "$VM_NAME"

  local email output
  for email in "${SHARE_EMAILS[@]}"; do
    log "Sharing ${VM_NAME} with ${email}"
    if output=$(ssh exe.dev share add "$VM_NAME" "$email" 2>&1); then
      printf '%s\n' "$output"
      continue
    fi
    if printf '%s\n' "$output" | grep -Eiq 'already|exists'; then
      printf '%s\n' "$output"
      continue
    fi
    printf '%s\n' "$output" >&2
    exit 1
  done

  ssh exe.dev share show "$VM_NAME"
}

stage_private_key() {
  if [[ -z "$GITHUB_PRIVATE_KEY_FILE" ]]; then
    printf '\n'
    return
  fi
  [[ -f "$GITHUB_PRIVATE_KEY_FILE" ]] || die "GitHub private key file not found: $GITHUB_PRIVATE_KEY_FILE"
  local staged="/tmp/symphony-dbcli-github-app-key-${RANDOM}-$$.pem"
  log "Copying GitHub App private key to VM"
  scp "$GITHUB_PRIVATE_KEY_FILE" "${VM_HOST}:${staged}" >/dev/null
  printf '%s\n' "$staged"
}

bootstrap_remote() {
  local staged_key="$1"
  log "Bootstrapping ${VM_HOST}"
  ssh "$VM_HOST" \
    "$(remote_env_assignment SYMPHONY_REPO "$REPO")" \
    "$(remote_env_assignment SYMPHONY_GIT_REF "$GIT_REF")" \
    "$(remote_env_assignment SYMPHONY_CLONE_URL "$CLONE_URL")" \
    "$(remote_env_assignment SYMPHONY_REMOTE_DIR "$REMOTE_DIR")" \
    "$(remote_env_assignment SYMPHONY_GITHUB_APP_ID_ARG "$GITHUB_APP_ID")" \
    "$(remote_env_assignment SYMPHONY_GITHUB_INSTALLATION_ID_ARG "$GITHUB_INSTALLATION_ID")" \
    "$(remote_env_assignment SYMPHONY_STAGED_KEY "$staged_key")" \
    "$(remote_env_assignment SYMPHONY_START_MODE "$START_MODE")" \
    "$(remote_env_assignment SYMPHONY_SERVICE_NAME "$SERVICE_NAME")" \
    "$(remote_env_assignment SYMPHONY_GIT_USER_NAME "$GIT_USER_NAME")" \
    "$(remote_env_assignment SYMPHONY_GIT_USER_EMAIL "$GIT_USER_EMAIL")" \
    "$(remote_env_assignment SYMPHONY_CONFIGURE_CODEX "$CONFIGURE_CODEX")" \
    bash -s <<'REMOTE_SCRIPT'
set -euo pipefail

repo="${SYMPHONY_REPO:?missing SYMPHONY_REPO}"
git_ref="${SYMPHONY_GIT_REF:?missing SYMPHONY_GIT_REF}"
clone_url="${SYMPHONY_CLONE_URL:?missing SYMPHONY_CLONE_URL}"
remote_dir_arg="${SYMPHONY_REMOTE_DIR:-}"
github_app_id_arg="${SYMPHONY_GITHUB_APP_ID_ARG:-}"
github_installation_id_arg="${SYMPHONY_GITHUB_INSTALLATION_ID_ARG:-}"
staged_key="${SYMPHONY_STAGED_KEY:-}"
start_mode="${SYMPHONY_START_MODE:?missing SYMPHONY_START_MODE}"
configure_codex="${SYMPHONY_CONFIGURE_CODEX:?missing SYMPHONY_CONFIGURE_CODEX}"
service_name="${SYMPHONY_SERVICE_NAME:?missing SYMPHONY_SERVICE_NAME}"
git_user_name="${SYMPHONY_GIT_USER_NAME:?missing SYMPHONY_GIT_USER_NAME}"
git_user_email="${SYMPHONY_GIT_USER_EMAIL:?missing SYMPHONY_GIT_USER_EMAIL}"

log() {
  printf '\n==> %s\n' "$*"
}

remote_user="$(id -un)"
remote_group="$(id -gn)"
repo_basename="${repo##*/}"
remote_dir="$remote_dir_arg"
if [[ -z "$remote_dir" ]]; then
  remote_dir="${HOME}/${repo_basename}"
fi
if [[ -n "$staged_key" ]]; then
  find /tmp \
    -maxdepth 1 \
    -type f \
    -name "symphony-dbcli-github-app-key-*.pem" \
    ! -path "$staged_key" \
    -delete 2>/dev/null || true
fi

log "Installing OS packages and uv"
if command -v apt-get >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    ca-certificates \
    curl \
    git \
    jq
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="${HOME}/.local/bin:${PATH}"
uv_bin="$(command -v uv)"

log "Creating persistent Symphony directories"
sudo install -d -m 755 -o "$remote_user" -g "$remote_group" /srv/symphony
sudo install -d -m 755 -o "$remote_user" -g "$remote_group" /srv/symphony/repos
sudo install -d -m 755 -o "$remote_user" -g "$remote_group" /srv/symphony/worktrees
sudo install -d -m 700 -o "$remote_user" -g "$remote_group" /srv/symphony/secrets

if [[ "$configure_codex" == "1" ]]; then
  log "Configuring Codex to use exe.dev LLM Gateway"
  mkdir -p "${HOME}/.codex"
  if [[ -f "${HOME}/.codex/config.toml" ]]; then
    cp "${HOME}/.codex/config.toml" "${HOME}/.codex/config.toml.bak.$(date +%Y%m%d%H%M%S)"
  fi
  cat > "${HOME}/.codex/config.toml" <<'EOF'
model_provider = "exe-openai"

[model_providers.exe-openai]
name = "exe.dev LLM Gateway"
base_url = "http://169.254.169.254/gateway/llm/openai/v1"
requires_openai_auth = false
EOF
fi

log "Cloning or updating ${repo}"
if [[ -d "${remote_dir}/.git" ]]; then
  if [[ -n "$(git -C "$remote_dir" status --porcelain --untracked-files=no)" ]]; then
    echo "Remote checkout has tracked local changes: ${remote_dir}" >&2
    echo "Commit, stash, or remove them before reprovisioning." >&2
    exit 1
  fi
  git -C "$remote_dir" remote set-url origin "$clone_url"
  git -C "$remote_dir" fetch --prune origin
else
  mkdir -p "$(dirname "$remote_dir")"
  git clone "$clone_url" "$remote_dir"
fi

if git -C "$remote_dir" rev-parse --verify --quiet "origin/${git_ref}" >/dev/null; then
  git -C "$remote_dir" checkout -B "$git_ref" "origin/${git_ref}"
else
  git -C "$remote_dir" checkout "$git_ref"
fi

log "Installing Python dependencies"
cd "$remote_dir"
"$uv_bin" python install 3.12
"$uv_bin" sync

log "Configuring git author"
git config --global user.name "$git_user_name"
git config --global user.email "$git_user_email"

env_file="/srv/symphony/secrets/symphony-dbcli.env"
existing_app_id=""
existing_installation_id=""
existing_private_key_path="/srv/symphony/secrets/github-app.private-key.pem"
if [[ -f "$env_file" ]]; then
  # shellcheck disable=SC1090
  set +u
  . "$env_file"
  set -u
  existing_app_id="${SYMPHONY_GITHUB_APP_ID:-}"
  existing_installation_id="${SYMPHONY_GITHUB_INSTALLATION_ID:-}"
  existing_private_key_path="${SYMPHONY_GITHUB_PRIVATE_KEY_PATH:-$existing_private_key_path}"
fi

github_app_id="${github_app_id_arg:-$existing_app_id}"
github_installation_id="${github_installation_id_arg:-$existing_installation_id}"
github_private_key_path="$existing_private_key_path"

if [[ -n "$staged_key" && -f "$staged_key" ]]; then
  github_private_key_path="/srv/symphony/secrets/github-app.private-key.pem"
  sudo install -m 600 -o "$remote_user" -g "$remote_group" "$staged_key" "$github_private_key_path"
  rm -f "$staged_key"
fi

log "Writing Symphony environment file"
tmp_env="$(mktemp)"
cat > "$tmp_env" <<EOF
SYMPHONY_PROFILE=prod
SYMPHONY_GITHUB_APP_ID=${github_app_id}
SYMPHONY_GITHUB_INSTALLATION_ID=${github_installation_id}
SYMPHONY_GITHUB_PRIVATE_KEY_PATH=${github_private_key_path}
EOF
sudo install -m 600 -o "$remote_user" -g "$remote_group" "$tmp_env" "$env_file"
rm -f "$tmp_env"

export SYMPHONY_PROFILE=prod
export SYMPHONY_GITHUB_APP_ID="$github_app_id"
export SYMPHONY_GITHUB_INSTALLATION_ID="$github_installation_id"
export SYMPHONY_GITHUB_PRIVATE_KEY_PATH="$github_private_key_path"

log "Validating workflow and initializing SQLite"
"$uv_bin" run symphony-dbcli --profile prod workflow validate
"$uv_bin" run symphony-dbcli --profile prod init-db

log "Installing systemd unit"
unit_tmp="$(mktemp)"
cat > "$unit_tmp" <<EOF
[Unit]
Description=Symphony DBCLI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${remote_user}
WorkingDirectory=${remote_dir}
EnvironmentFile=${env_file}
ExecStart=${uv_bin} run symphony-dbcli --profile prod serve --no-reload
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$unit_tmp" "/etc/systemd/system/${service_name}.service"
rm -f "$unit_tmp"
sudo systemctl daemon-reload
sudo systemctl enable "${service_name}.service"

creds_complete=0
if [[ -n "$github_app_id" && -n "$github_installation_id" && -f "$github_private_key_path" ]]; then
  creds_complete=1
fi

should_start=0
case "$start_mode" in
  never)
    should_start=0
    ;;
  always)
    should_start=1
    ;;
  auto)
    should_start="$creds_complete"
    ;;
  *)
    echo "Invalid start mode: $start_mode" >&2
    exit 1
    ;;
esac

if [[ "$should_start" == "1" ]]; then
  log "Starting ${service_name}.service"
  sudo systemctl restart "${service_name}.service"
  sleep 2
  sudo systemctl --no-pager --full status "${service_name}.service"
  curl -fsS http://127.0.0.1:8765/api/health
  printf '\n'
else
  log "Service installed but not started"
  if [[ "$creds_complete" != "1" ]]; then
    cat <<EOF
GitHub App credentials are incomplete. Edit ${env_file}, ensure
${github_private_key_path} exists, then run:

  sudo systemctl restart ${service_name}.service
  curl -fsS http://127.0.0.1:8765/api/health
EOF
  fi
fi

log "Remote provisioning complete"
printf 'Checkout: %s\n' "$remote_dir"
printf 'Service:  %s.service\n' "$service_name"
printf 'Env:      %s\n' "$env_file"
REMOTE_SCRIPT
}

ensure_github_integration
staged_key="$(stage_private_key)"
bootstrap_remote "$staged_key"
configure_exedev_share

cat <<EOF

Provisioning complete.

Dashboard:
  https://${VM_NAME}.exe.xyz/

Useful checks:
  ssh ${VM_HOST} 'sudo systemctl status ${SERVICE_NAME}.service'
  ssh ${VM_HOST} 'sudo journalctl -u ${SERVICE_NAME}.service -f'
  ssh ${VM_HOST} 'curl -fsS http://127.0.0.1:8765/api/health'
EOF
