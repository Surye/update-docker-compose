# Repository Guidelines

## Project Structure & Module Organization

This Linux desktop application uses a flat Python layout:

- `update_docker_compose.py`: PySide6 tray interface, background workers, and terminal launching; draws its icons in code.
- `compose_cli.py`: terminal review, confirmation, and update workflow.
- `compose_backend.py`: Docker discovery, image checks, updates, locking, and shared status cache.
- `app_config.py`: TOML configuration loading and validation.
- `test_workflow.py`: mocked workflow and regression tests.
- `install.sh` / `uninstall.sh`: per-user installation and desktop integration.
- `config.example.toml`: supported preferences; `README.md`: setup and usage.

## Build, Test, and Development Commands

Use Python 3.11+ with distribution-provided PySide6. Live operation also requires Docker with the Compose plugin, GIO, a terminal, and a desktop tray host.

- `python3 -m unittest -v test_workflow`: run the mocked test suite without changing Docker services.
- `python3 compose_cli.py --check`: check deployed projects; pulls images into Docker's cache.
- `python3 compose_cli.py`: review updates and request confirmation before recreating services.
- `python3 update_docker_compose.py`: run the tray locally; install first for its desktop launcher.
- `./install.sh`: copy the application into per-user directories and register login startup. Rerun after source changes to refresh the installed copy.

There is no separate build step or configured formatter/linter.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions and variables, and `PascalCase` classes. Follow existing type annotations, dataclasses, and `pathlib.Path` usage. Keep Docker logic in the backend and presentation in the CLI/tray modules. Retain Bash strict mode (`set -euo pipefail`) and quote path variables.

## Testing Guidelines

Use standard-library `unittest`, `unittest.mock.patch`, and temporary directories. Name tests `test_<behavior>` within descriptive `TestCase` classes. Cover confirmation defaults, discovery filters, failures, configuration, and shared status when changing those behaviors. Mock Docker commands rather than contacting a live daemon. No coverage threshold is configured.

## Commit & Pull Request Guidelines

Git metadata is absent from this checkout, so historical commit conventions cannot be verified. Use concise imperative subjects, such as `Fix discovery of Compose overrides`. PRs should describe behavior changes, link relevant issues, and report validation commands and results. Include screenshots for visible tray changes.

## Configuration & Operational Safety

Respect XDG directory overrides. Preserve discovery from deployed container labels, ordered Compose files, project names, and working directories. Never discover deployments by scanning backup YAML files. Keep explicit update confirmation and shared operation locking intact.
