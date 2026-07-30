#!/usr/bin/env sh
set -eu

action="${1:-}"
if [ "$#" -gt 0 ]; then
  shift
fi

source_dir=""
config_file="/etc/mimo-console-agent/agent.json"
install_root="/opt/mimo-console-agent"
service_name="mimo-console-agent"
python_bin="/usr/bin/python3"

usage() {
  cat <<'EOF'
Usage:
  manage-service.sh install|upgrade --source PATH [options]
  manage-service.sh uninstall [options]

Options:
  --config PATH         Agent JSON config (default: /etc/mimo-console-agent/agent.json)
  --install-root PATH   Runtime directory (default: /opt/mimo-console-agent)
  --service NAME        systemd service name (default: mimo-console-agent)
  --source PATH         Repository agent directory containing pyproject.toml and uv.lock
  --python PATH         System Python visible to systemd (default: /usr/bin/python3)

Uninstall removes the runtime and systemd unit, but preserves config, tokens and state.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source)
      source_dir="${2:-}"
      shift 2
      ;;
    --config)
      config_file="${2:-}"
      shift 2
      ;;
    --install-root)
      install_root="${2:-}"
      shift 2
      ;;
    --service)
      service_name="${2:-}"
      shift 2
      ;;
    --python)
      python_bin="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] || {
  echo "This command must run as root." >&2
  exit 77
}

case "$service_name" in
  *[!A-Za-z0-9_.@-]*|"")
    echo "Invalid service name: $service_name" >&2
    exit 64
    ;;
esac
command -v readlink >/dev/null 2>&1 || {
  echo "GNU readlink is required." >&2
  exit 69
}
install_root="$(readlink -m -- "$install_root")"
case "$install_root" in
  /opt/?*|/usr/local/lib/?*) ;;
  *)
    echo "install-root must be below /opt or /usr/local/lib." >&2
    exit 64
    ;;
esac

unit_file="/etc/systemd/system/${service_name}.service"

if [ "$action" = "uninstall" ]; then
  systemctl disable --now "$service_name" 2>/dev/null || true
  if [ -f "$unit_file" ]; then
    rm -f "$unit_file"
    systemctl daemon-reload
  fi
  if [ -d "$install_root" ]; then
    rm -rf "$install_root"
  fi
  echo "Removed $service_name; config and state were preserved."
  exit 0
fi

case "$action" in
  install|upgrade) ;;
  *)
    usage >&2
    exit 64
    ;;
esac

[ -n "$source_dir" ] || {
  echo "--source is required for $action." >&2
  exit 64
}
source_dir="$(cd "$source_dir" && pwd -P)"
[ -f "$source_dir/pyproject.toml" ] && [ -f "$source_dir/uv.lock" ] || {
  echo "Source must contain pyproject.toml and uv.lock." >&2
  exit 66
}
[ -f "$config_file" ] || {
  echo "Agent config does not exist: $config_file" >&2
  exit 66
}
command -v uv >/dev/null 2>&1 || {
  echo "uv is required." >&2
  exit 69
}
command -v systemctl >/dev/null 2>&1 || {
  echo "systemd is required." >&2
  exit 69
}
[ -x "$python_bin" ] || {
  echo "Python interpreter is not executable: $python_bin" >&2
  exit 69
}

read_write_paths="$(
  "$python_bin" - "$config_file" <<'PY'
import json
import pathlib
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
roots = {
    str(pathlib.Path(item["project_root"]).expanduser().resolve())
    for item in config.get("instances", {}).values()
}
for root in sorted(roots):
    if "\n" in root or "\r" in root or '"' in root:
        raise SystemExit(f"Unsafe project_root for systemd: {root!r}")
    print(root)
PY
)"

staging="${install_root}.new.$$"
previous="${install_root}.previous"
unit_previous="${unit_file}.previous"
had_previous_runtime=0
had_previous_unit=0
trap 'rm -rf "$staging"' EXIT INT TERM
install -d -m 0755 "$staging"
cp "$source_dir/pyproject.toml" "$source_dir/uv.lock" "$staging/"
cp -R "$source_dir/src" "$staging/src"
if [ -f "$source_dir/README.md" ]; then
  cp "$source_dir/README.md" "$staging/"
fi
uv sync --project "$staging" --python "$python_bin" --frozen --no-dev
"$staging/.venv/bin/mimo-console-agent" --config "$config_file" --check-config
rm -rf "$staging/.venv"

if [ -d "$install_root" ]; then
  had_previous_runtime=1
  rm -rf "$previous"
  mv "$install_root" "$previous"
else
  rm -rf "$previous"
fi
if [ -f "$unit_file" ]; then
  had_previous_unit=1
  cp -p "$unit_file" "$unit_previous"
else
  rm -f "$unit_previous"
fi
mv "$staging" "$install_root"
trap - EXIT INT TERM
if ! uv sync \
  --project "$install_root" \
  --python "$python_bin" \
  --frozen \
  --no-dev; then
  echo "Failed to create the runtime environment at its final path." >&2
  rm -rf "$install_root"
  if [ "$had_previous_runtime" -eq 1 ] && [ -d "$previous" ]; then
    mv "$previous" "$install_root"
  fi
  rm -f "$unit_previous"
  exit 70
fi

cat >"$unit_file" <<EOF
[Unit]
Description=Mimo Console Docker deployment agent
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$install_root
Environment=DOCKER_CONFIG=/var/lib/mimo-console-agent/docker-config
ExecStart="$install_root/.venv/bin/mimo-console-agent" --config "$config_file"
Restart=on-failure
RestartSec=3
RuntimeDirectory=mimo-agent
RuntimeDirectoryMode=0750
StateDirectory=mimo-console-agent
StateDirectoryMode=0750
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/mimo-console-agent
ReadWritePaths=/run/mimo-agent
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
EOF
printf '%s\n' "$read_write_paths" | while IFS= read -r project_root; do
  [ -n "$project_root" ] || continue
  printf 'ReadWritePaths="%s"\n' "$project_root" >>"$unit_file"
done
cat >>"$unit_file" <<'EOF'

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$service_name"
restart_status=0
systemctl restart "$service_name" || restart_status=$?
sleep 1
if [ "$restart_status" -ne 0 ] || ! systemctl is-active --quiet "$service_name"; then
  systemctl --no-pager --full status "$service_name" || true
  systemctl stop "$service_name" 2>/dev/null || true
  rm -rf "$install_root"
  if [ "$had_previous_runtime" -eq 1 ] && [ -d "$previous" ]; then
    mv "$previous" "$install_root"
  fi
  if [ "$had_previous_unit" -eq 1 ] && [ -f "$unit_previous" ]; then
    mv "$unit_previous" "$unit_file"
  else
    rm -f "$unit_file" "$unit_previous"
    systemctl disable "$service_name" 2>/dev/null || true
  fi
  systemctl daemon-reload
  if [ "$had_previous_runtime" -eq 1 ] && [ "$had_previous_unit" -eq 1 ]; then
    systemctl restart "$service_name" || true
  fi
  echo "Failed to start the new Agent; the previous runtime and unit were restored." >&2
  exit 70
fi
rm -rf "$previous"
rm -f "$unit_previous"
systemctl --no-pager --full status "$service_name"
echo "$action completed for $service_name."
