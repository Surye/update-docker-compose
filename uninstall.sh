#!/usr/bin/env bash
set -euo pipefail

data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
bin_dir="${UPDATE_DOCKER_COMPOSE_BIN_DIR:-$HOME/.local/bin}"
rm -f "$data_dir/applications/update-docker-compose.desktop" "$config_dir/autostart/update-docker-compose.desktop"
for name in update-docker-compose update-docker-compose-tray; do
    if [ -L "$bin_dir/$name" ]; then
        rm "$bin_dir/$name"
    fi
done
for name in app_config.py compose_backend.py compose_cli.py update_docker_compose.py config.example.toml uninstall.sh; do
    rm -f "$data_dir/update-docker-compose/$name"
done
# Remove only bytecode belonging to these modules, leaving unrelated files alone.
for name in app_config compose_backend compose_cli update_docker_compose; do
    rm -f "$data_dir/update-docker-compose/__pycache__/$name".*.pyc
done
rmdir "$data_dir/update-docker-compose/__pycache__" "$data_dir/update-docker-compose" 2>/dev/null || true
printf 'Removed app and login startup entry. Quit the running tray app to finish.\nConfiguration and cache are retained.\n'
