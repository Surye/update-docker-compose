"""User configuration; project directories are optional filters."""
from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    check_interval_hours: float = 6
    include_directories: tuple[Path, ...] = ()
    exclude_projects: tuple[str, ...] = ()

    @property
    def check_interval_ms(self) -> int:
        return int(self.check_interval_hours * 60 * 60 * 1000)


def config_file() -> Path:
    directory = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return directory / "update-docker-compose" / "config.toml"


def load_settings(path: Path | None = None) -> Settings:
    path = path if path is not None else config_file()
    if not path.exists():
        return Settings()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Could not read configuration: {path}") from error
    unknown = data.keys() - {"check_interval_hours", "include_directories", "exclude_projects"}
    if unknown:
        raise RuntimeError("Unknown configuration option(s): " + ", ".join(sorted(unknown)))
    interval = data.get("check_interval_hours", 6)
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or not 1 <= interval * 3600000 <= 2147483647):
        raise RuntimeError("check_interval_hours must produce an interval between 1 ms and 24 days")
    for key in ("include_directories", "exclude_projects"):
        values = data.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise RuntimeError(f"{key} must be an array of nonempty strings")
    directories = tuple(Path(value).expanduser() for value in data.get("include_directories", []))
    if any(not directory.is_absolute() for directory in directories):
        raise RuntimeError("include_directories must contain absolute paths (or paths starting with ~)")
    return Settings(float(interval), tuple(directory.resolve() for directory in directories), tuple(data.get("exclude_projects", [])))
