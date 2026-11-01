#!/usr/bin/python3
"""Interactive terminal workflow launched by the Docker Compose tray app."""
from __future__ import annotations

import fcntl
import argparse
import sys

from compose_backend import cache_directory, image_updates, prepare_update_plan, update_projects


def main(*, check_only: bool = False) -> int:
    print("Docker Compose Updates\n", flush=True)
    plan = prepare_update_plan()
    projects = plan.projects
    if not projects:
        print("No deployed Compose projects match your configuration.")
        return 0
    print("Deployed projects: " + ", ".join(project.name for project in projects), flush=True)
    print("Checking for container updates… (waiting if another check is running)", flush=True)
    result = image_updates(report=lambda message: print(message, flush=True), projects=projects)
    for error in result.errors:
        print(f"Warning: {error}")
    if not result.updates:
        print("\nCheck incomplete." if result.errors else "\nContainers are up to date.")
        return 1 if result.errors else 0
    print(f"\n{len(result.updates)} container update(s) available:\n")
    for update in result.updates:
        print(f"  {update}")
    if check_only:
        return 1 if result.errors else 0
    print(f"\nThis will pull images and recreate changed services in all {len(projects)} deployed Compose projects.")
    try:
        answer = input("Proceed with updates? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Update cancelled.")
        return 0
    errors = update_projects(plan, live=True)
    for error in errors:
        print(f"Error: {error}")
    print("\nRefreshing container status…", flush=True)
    final = image_updates(report=lambda message: print(message, flush=True))
    for error in final.errors:
        print(f"Warning: {error}")
    if errors or final.errors:
        print("\nSome operations failed; review the output above.")
        return 1
    if final.updates:
        print("\nUpdates remain available:")
        for update in final.updates:
            print(f"  {update}")
        return 1
    print("\nUpdates have been applied.")
    return 0


def entrypoint(*, check_only: bool = False) -> int:
    # GIO hands off to a terminal and returns immediately; prevent two interactive
    # sessions independently of the shorter lock around Docker operations.
    with (cache_directory() / "interactive.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Docker Compose Updates is already running in another terminal.")
            return 1
        try:
            return main(check_only=check_only)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Could not run Docker Compose: {error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check and update deployed Docker Compose projects.")
    parser.add_argument("--check", action="store_true", help="check availability without prompting or restarting containers (pulls images)")
    arguments = parser.parse_args()
    code = entrypoint(check_only=arguments.check)
    if sys.stdin.isatty() and not arguments.check:
        try:
            input("\nPress Enter to quit ")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(code)
