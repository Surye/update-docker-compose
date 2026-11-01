#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
app_dir="$data_dir/update-docker-compose"
bin_dir="${UPDATE_DOCKER_COMPOSE_BIN_DIR:-$HOME/.local/bin}"

/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 11); from PySide6.QtWidgets import QApplication' || {
    echo 'Install Python 3.11+ and PySide6 using your distribution package manager.' >&2
    exit 1
}
command -v docker >/dev/null || { echo 'Docker CLI is required.' >&2; exit 1; }
docker compose version >/dev/null
command -v gio >/dev/null || { echo 'GIO (glib2) is required to launch the terminal.' >&2; exit 1; }

mkdir -p "$app_dir" "$bin_dir" "$data_dir/applications" "$config_dir/autostart"
for filename in app_config.py compose_backend.py compose_cli.py update_docker_compose.py config.example.toml uninstall.sh; do
    install -m 644 "$project_dir/$filename" "$app_dir/$filename"
done
chmod +x "$app_dir/compose_cli.py" "$app_dir/update_docker_compose.py" "$app_dir/uninstall.sh"
ln -sfn "$app_dir/compose_cli.py" "$bin_dir/update-docker-compose"
ln -sfn "$app_dir/update_docker_compose.py" "$bin_dir/update-docker-compose-tray"
/usr/bin/python3 - "$app_dir" "$data_dir/applications" "$config_dir/autostart" <<'PY'
from pathlib import Path
import sys
app, applications, autostart = map(Path, sys.argv[1:])

def desktop_argument(path):
    # Desktop Exec fields have their own quoting rules, not shell syntax.
    value = str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    return '"' + value.replace('\\', '\\\\') + '"'

for directory, script, terminal, comment in (
    (applications, 'compose_cli.py', 'true', 'Review and apply Docker Compose updates'),
    (autostart, 'update_docker_compose.py', 'false', 'Check for Docker Compose updates in the system tray'),
):
    (directory / 'update-docker-compose.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Docker Compose Updates\n'
        f'Comment={comment}\nExec=/usr/bin/python3 {desktop_argument(app / script)}\n'
        f'Icon=system-software-update\nTerminal={terminal}\nCategories=System;\n'
        + ('X-GNOME-Autostart-enabled=true\n' if directory == autostart else '')
    )
PY
printf 'Installed to %s\nLogin startup enabled.\nStart tray now: %s/update-docker-compose-tray\nRun CLI: %s/update-docker-compose\n' "$app_dir" "$bin_dir" "$bin_dir"
