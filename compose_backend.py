"""Shared Docker operations for the tray app and interactive CLI."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app_config import load_settings

# Compatibility with existing installs. No directory filter is applied by default.
DOCKER_ROOT = Path(os.environ["DOCKER_COMPOSE_ROOT"]).expanduser() if os.environ.get("DOCKER_COMPOSE_ROOT") else None


@dataclass(frozen=True)
class CheckResult:
    updates: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ComposeProject:
    name: str
    files: tuple[Path, ...]
    directory: Path

    def command(self, *arguments: str) -> list[str]:
        command = ["docker", "compose", "--project-name", self.name,
                   "--project-directory", str(self.directory)]
        for file in self.files:
            command.extend(["-f", str(file)])
        return [*command, *arguments]


def compose_projects(root: Path | None = DOCKER_ROOT) -> list[ComposeProject]:
    """Use deployed container metadata; saved YAML files are never deployments."""
    settings = load_settings()
    directories = (root.resolve(),) if root is not None else settings.include_directories
    containers = run(["docker", "ps", "--filter", "label=com.docker.compose.project", "--format", "{{.ID}}"])
    if containers.returncode:
        raise RuntimeError("Could not list deployed Compose containers")
    identifiers = containers.stdout.split()
    if not identifiers:
        return []
    inspected = run(["docker", "inspect", "--format", "{{json .Config.Labels}}", *identifiers])
    if inspected.returncode:
        raise RuntimeError("Could not inspect deployed Compose projects")
    projects: dict[str, ComposeProject] = {}
    for line in inspected.stdout.splitlines():
        labels = json.loads(line) or {}
        if labels.get("com.docker.compose.oneoff", "false").lower() == "true":
            continue
        name = labels.get("com.docker.compose.project")
        config = labels.get("com.docker.compose.project.config_files", "")
        workdir = labels.get("com.docker.compose.project.working_dir", "")
        if not name or not config or not workdir:
            continue
        if name in settings.exclude_projects:
            continue
        directory = Path(workdir).resolve()
        files = tuple((directory / file.strip()).resolve() for file in config.split(","))
        if directories and not any(files[0].is_relative_to(directory) for directory in directories):
            continue
        if not all(file.is_file() for file in files):
            raise RuntimeError(f"{name}: a deployed Compose file is missing")
        project = ComposeProject(name, files, directory)
        if name in projects and projects[name] != project:
            raise RuntimeError(f"{name}: containers disagree on deployed Compose configuration")
        projects[name] = project
    return sorted(projects.values(), key=lambda project: project.name)


def run(command: list[str], cwd: Path | None = None, *, live: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=None if live else subprocess.PIPE,
        stderr=None if live else subprocess.PIPE,
        check=False,
    )


def _image_updates(root: Path | None = DOCKER_ROOT, report: Callable[[str], None] | None = None) -> CheckResult:
    """Pull project images, then compare them with images used by running containers."""
    updates: list[str] = []
    errors: list[str] = []

    for project in compose_projects(root):
        project_dir = project.directory
        if report:
            report(f"Checking {project.name}…")
        pull = run(project.command("pull", "--quiet"), project_dir)
        if pull.returncode:
            errors.append(f"{project_dir.name}: image pull failed")
            continue

        containers = run(
            project.command("ps", "--all", "-q"), project_dir
        )
        if containers.returncode:
            errors.append(f"{project_dir.name}: container query failed")
            continue

        for container_id in containers.stdout.split():
            inspected = run(["docker", "inspect", container_id])
            if inspected.returncode:
                continue
            try:
                container = json.loads(inspected.stdout)[0]
                if container.get("State", {}).get("Status") != "running":
                    continue
                service = container.get("Config", {}).get("Labels", {}).get(
                    "com.docker.compose.service", "container"
                )
                current_id = container.get("Image")
                image_ref = container.get("Config", {}).get("Image")
                latest = run(["docker", "image", "inspect", image_ref, "--format", "{{.Id}}"])
                if latest.returncode == 0 and latest.stdout.strip() != current_id:
                    updates.append(f"{project.name} / {service}")
            except (ValueError, IndexError, TypeError):
                continue

    return CheckResult(tuple(updates), tuple(errors))


def _update_projects(root: Path | None = DOCKER_ROOT, *, live: bool = False) -> tuple[str, ...]:
    errors: list[str] = []
    for project in compose_projects(root):
        project_dir = project.directory
        if live:
            print(f"\nUpdating {project.name}…", flush=True)
        pull = run(
            project.command("pull", *([] if live else ["--quiet"])),
            project_dir, live=live,
        )
        if pull.returncode:
            errors.append(f"{project_dir.name}: image pull failed")
            continue
        result = run(
            project.command("up", "-d", "--remove-orphans"),
            project_dir,
            live=live,
        )
        if result.returncode:
            errors.append(f"{project_dir.name}: update failed")
    return tuple(errors)


@contextmanager
def operation_lock() -> Iterator[None]:
    """Serialize checks and updates across the tray and terminal processes."""
    with (cache_directory() / "operations.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def cache_directory() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    directory = cache / "update-docker-compose"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def status_file() -> Path:
    return cache_directory() / "status.json"


def save_status(result: CheckResult) -> None:
    state = {"updates": result.updates, "errors": result.errors, "checked_at": time.time()}
    with tempfile.NamedTemporaryFile(mode="w", dir=cache_directory(), delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(state, handle)
    try:
        temporary.replace(status_file())
    finally:
        temporary.unlink(missing_ok=True)


def image_updates(root: Path | None = DOCKER_ROOT, report: Callable[[str], None] | None = None) -> CheckResult:
    with operation_lock():
        result = _image_updates(root, report)
        save_status(result)
        return result


def update_projects(root: Path | None = DOCKER_ROOT, *, live: bool = False) -> tuple[str, ...]:
    with operation_lock():
        return _update_projects(root, live=live)
