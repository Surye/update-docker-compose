# Docker Compose Updates

A Linux system tray app that shows when deployed Docker Compose containers have
newer images available. Click its icon to review updates and apply them in your
terminal. Inspired by CachyOS Update's tray and CLI workflow.

Projects can live anywhere. The app discovers them from Docker's deployment
metadata; it does not search directories for YAML files. Saved backups and
undeployed Compose files are never started.

## Requirements

- Linux desktop with a system tray / StatusNotifierItem host. KDE Plasma is
  supported; GNOME needs a tray extension. Other desktops have not been checked.
- Python 3.11 or newer and PySide6, available from your distribution packages.
- Docker CLI with the Compose plugin, and permission to access the Docker daemon
  as your normal user.
- GIO (`gio`, commonly packaged with GLib) and a terminal emulator.

On Arch Linux / CachyOS, the Qt dependency is `python-pyside6`.

## Install

From your downloaded or cloned checkout:

```sh
./install.sh
~/.local/bin/update-docker-compose-tray
```

Installation is per user and needs no sudo. It copies the app to
`~/.local/share/update-docker-compose`, adds CLI commands to `~/.local/bin`, and
registers login startup. You can move or delete the source checkout afterward.
Add `~/.local/bin` to your PATH to run these commands by name:

```sh
update-docker-compose        # Review and confirm updates in the terminal
update-docker-compose --check  # Check availability without applying updates
update-docker-compose-tray   # Start the tray app
```

Run the installer again to install a newer version, then quit and restart the
tray app. Existing preferences are preserved. Quit the previous instance before
starting another.

`XDG_DATA_HOME` and `XDG_CONFIG_HOME` control application and configuration
locations. `UPDATE_DOCKER_COMPOSE_BIN_DIR` optionally changes the command directory.

## How it works

- Queries containers on your current Docker connection and groups deployed
  Compose projects by their labels, regardless of filesystem location.
- Preserves each project's original name, working directory, and ordered Compose
  file set. Excludes one-off containers and projects with only stopped containers.
- Checks on startup and every six hours by default. Checks **pull images into
  Docker's cache**, consuming bandwidth and disk space. They do not recreate
  containers.
- Shows a green badge when current, red when updates are pending, and amber
  during checks or when a check has failed.
- Left-click opens the CLI through a `Terminal=true` desktop launcher, as
  CachyOS Update does. The right-click menu lists pending containers and provides
  **Check for updates** and **Run Docker Compose Updates** actions.
- The CLI lists discovered projects and pending updates, then asks
  **Proceed with updates? [y/N]**. Confirming pulls images and runs
  `docker compose up -d --remove-orphans` for the included deployed projects.
  Compose recreates changed services. Press Enter to close the terminal.
- Shares a process lock between checks and updates. The tray reads the CLI's
  refreshed status automatically.

Checks follow the image references in your Compose files. They do not change
version tags or digest pins. Locally built images without a pullable registry
image are not supported. Original Compose files must be accessible on this
machine; inaccessible deployment files produce an error. For a remote Docker
connection, this usually requires matching local paths. Custom environment files
or shell variables used for the original deployment must also be available when
running the updater.

## Configure

Preferences are optional. Copy the example to your configuration directory:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/update-docker-compose"
cp config.example.toml "${XDG_CONFIG_HOME:-$HOME/.config}/update-docker-compose/config.toml"
```

```toml
check_interval_hours = 6
exclude_projects = ["development", "experiments"]

# Optional absolute directory filters; an empty array includes all directories.
include_directories = []
```

Exclusions match exact Compose project names. Directory filters match a project's
base Compose file. Paths beginning with `~` are supported. Use the tray's
**Open configuration folder** action to find these preferences. Restart the tray
after changing the check interval. Discovery filters are read on every check.

The legacy `DOCKER_COMPOSE_ROOT` environment variable remains supported as a
single directory filter. It is optional; there is no built-in project directory.

## Uninstall

Quit the tray app, then run:

```sh
./uninstall.sh
```

If the checkout has been removed, run
`~/.local/share/update-docker-compose/uninstall.sh` instead.

Configuration and cache are retained. No containers, images, volumes, or Compose
files are removed by uninstalling.

## Development

```sh
python3 -m unittest -v test_workflow
```

Tests mock Docker operations: they cover confirmation, failures, discovery,
backup exclusion, configuration, tray activation, and shared status.

The tray interaction follows CachyOS's
[tray implementation](https://github.com/CachyOS/cachy-update/blob/main/src/tray/src/tray.rs)
and [desktop launch helper](https://github.com/CachyOS/cachy-update/blob/main/src/tray/src/tray_helpers.rs).
