"""Exercise confirmation and launch behavior without changing Docker services."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import compose_cli
from app_config import Settings, load_settings
import compose_backend as backend
from update_docker_compose import ComposeUpdater, QSystemTrayIcon


class InteractiveWorkflowTests(unittest.TestCase):
    def run_cli(self, answer, results, update_errors=()):
        with patch.object(compose_cli, "compose_projects", return_value=[backend.ComposeProject("example", (Path("/example/compose.yaml"),), Path("/example"))]), \
             patch.object(compose_cli, "image_updates", side_effect=results), \
             patch.object(compose_cli, "update_projects", return_value=update_errors) as update, \
             patch("builtins.input", return_value=answer) as prompt, \
             contextlib.redirect_stdout(io.StringIO()):
            code = compose_cli.main()
        return code, update, prompt

    def test_default_confirmation_never_recreates_containers(self):
        code, update, _ = self.run_cli("", [backend.CheckResult(("app / server",), ())])
        self.assertEqual(code, 0)
        update.assert_not_called()

    def test_confirmation_streams_updates_and_refreshes_status(self):
        code, update, _ = self.run_cli("y", [backend.CheckResult(("app / server",), ()), backend.CheckResult((), ())])
        self.assertEqual(code, 0)
        update.assert_called_once_with(live=True)

    def test_failed_update_returns_failure(self):
        code, _, _ = self.run_cli("y", [backend.CheckResult(("app / server",), ()), backend.CheckResult((), ())], ("app: update failed",))
        self.assertEqual(code, 1)

    def test_up_to_date_does_not_prompt_or_update(self):
        code, update, prompt = self.run_cli("", [backend.CheckResult((), ())])
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        update.assert_not_called()

    def test_incomplete_check_is_not_reported_as_success(self):
        code, update, _ = self.run_cli("", [backend.CheckResult((), ("app: image pull failed",))])
        self.assertEqual(code, 1)
        update.assert_not_called()


class TrayLaunchTests(unittest.TestCase):
    def test_left_click_opens_cli_and_context_click_does_not(self):
        updater = Mock()
        ComposeUpdater.activate_tray(updater, QSystemTrayIcon.ActivationReason.Context)
        updater.open_cli.assert_not_called()
        ComposeUpdater.activate_tray(updater, QSystemTrayIcon.ActivationReason.Trigger)
        updater.open_cli.assert_called_once()

    def test_launch_uses_desktop_entry_through_gio(self):
        updater = Mock(cli_process=None)
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/example/data"}), \
             patch("update_docker_compose.subprocess.Popen") as launch:
            ComposeUpdater.open_cli(updater)
        self.assertEqual(launch.call_args.args[0], ["gio", "launch", "/example/data/applications/update-docker-compose.desktop"])
        updater.cli_timer.start.assert_called_once()


class BackendTests(unittest.TestCase):
    def test_failed_pull_prevents_project_recreation(self):
        with patch.object(backend, "compose_projects", return_value=[backend.ComposeProject("example", (Path("/example/compose.yaml"),), Path("/example"))]), \
             patch.object(backend, "run", return_value=Mock(returncode=1)) as run:
            errors = backend._update_projects(live=True)
        self.assertEqual(len(errors), 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], "pull")

    def test_check_publishes_shared_status_for_tray(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), \
             patch.object(backend, "_image_updates", return_value=backend.CheckResult(("app / web",), ())):
            result = backend.image_updates()
            state = json.loads(backend.status_file().read_text())
        self.assertEqual(state["updates"], list(result.updates))


class DeployedDiscoveryTests(unittest.TestCase):
    def metadata(self, name, directory, files):
        return json.dumps({
            "com.docker.compose.project": name,
            "com.docker.compose.project.working_dir": str(directory),
            "com.docker.compose.project.config_files": ",".join(map(str, files)),
        })

    def test_backup_yaml_is_never_pulled_or_started(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployed = root / "media-stack" / "docker-compose.yml"
            backup = root / "media-stack" / "backups" / "before-update" / "docker-compose.yml"
            for file in (deployed, backup):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            labels = self.metadata("media-stack", deployed.parent, [deployed])
            with patch.object(backend, "run", side_effect=[
                Mock(returncode=0, stdout="container-id"),
                Mock(returncode=0, stdout=labels),
                Mock(returncode=0), Mock(returncode=0),
            ]) as run:
                self.assertEqual(backend._update_projects(root), ())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 4)
            self.assertFalse(any(str(backup) in command for command in commands))
            self.assertIn(str(deployed), commands[2])
            self.assertIn("--project-name", commands[3])
            self.assertIn("media-stack", commands[3])

    def test_project_name_working_directory_and_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, override = root / "compose.yaml", root / "override.yaml"
            base.touch(); override.touch()
            labels = self.metadata("custom-name", root, [base, override])
            with patch.object(backend, "run", side_effect=[Mock(returncode=0, stdout="id"), Mock(returncode=0, stdout=labels)]):
                projects = backend.compose_projects(root)
            self.assertEqual(projects[0].command("pull"), [
                "docker", "compose", "--project-name", "custom-name", "--project-directory", str(root),
                "-f", str(base), "-f", str(override), "pull",
            ])

    def test_project_outside_root_is_excluded(self):
        labels = self.metadata("other-project", Path("/elsewhere"), [Path("/elsewhere/compose.yaml")])
        with patch.object(backend, "run", side_effect=[Mock(returncode=0, stdout="id"), Mock(returncode=0, stdout=labels)]):
            self.assertEqual(backend.compose_projects(Path("/srv/docker")), [])

    def test_docker_failure_does_not_fall_back_to_filesystem_scan(self):
        with patch.object(backend, "run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "Could not list"):
                backend.compose_projects()

    def test_missing_deployed_file_stops_the_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            labels = self.metadata("missing", root, [root / "missing.yaml"])
            with patch.object(backend, "run", side_effect=[Mock(returncode=0, stdout="id"), Mock(returncode=0, stdout=labels)]):
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    backend.compose_projects(root)


class ConfigurationTests(unittest.TestCase):
    def test_missing_configuration_has_no_directory_restriction(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(Path(directory) / "missing.toml")
        self.assertEqual(settings.include_directories, ())
        self.assertEqual(settings.check_interval_ms, 6 * 60 * 60 * 1000)

    def test_filters_exclusions_and_interval_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('check_interval_hours = 2\ninclude_directories = ["/opt/apps"]\nexclude_projects = ["dev"]\n')
            settings = load_settings(config)
        self.assertEqual(settings.include_directories, (Path('/opt/apps'),))
        self.assertEqual(settings.exclude_projects, ('dev',))
        self.assertEqual(settings.check_interval_ms, 7200000)

    def test_invalid_settings_fail_clearly(self):
        for text in ('check_interval_hours = 0', 'check_interval_hours = nan',
                     'check_interval_hours = true', 'exclude_projects = "dev"',
                     'include_directories = ["relative"]', 'wrong_setting = 1',
                     'invalid TOML ['):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.toml"
                config.write_text(text)
                with self.assertRaises(RuntimeError):
                    load_settings(config)


class PortableDiscoveryTests(unittest.TestCase):
    def test_default_discovers_projects_in_different_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            for name in ('home-apps', 'opt-apps'):
                project = root / name
                project.mkdir()
                file = project / 'compose.yaml'
                file.touch()
                records.append(DeployedDiscoveryTests().metadata(name, project, [file]))
            with patch.object(backend, 'load_settings', return_value=Settings()), \
                 patch.object(backend, 'run', side_effect=[Mock(returncode=0, stdout='a b'), Mock(returncode=0, stdout='\n'.join(records))]):
                projects = backend.compose_projects(None)
        self.assertEqual([project.name for project in projects], ['home-apps', 'opt-apps'])

    def test_excluded_project_does_not_require_accessible_files(self):
        metadata = DeployedDiscoveryTests().metadata('dev', Path('/unavailable'), [Path('/unavailable/compose.yaml')])
        with patch.object(backend, 'load_settings', return_value=Settings(exclude_projects=('dev',))), \
             patch.object(backend, 'run', side_effect=[Mock(returncode=0, stdout='id'), Mock(returncode=0, stdout=metadata)]):
            self.assertEqual(backend.compose_projects(None), [])

    def test_configured_directory_filters_discovery(self):
        metadata = DeployedDiscoveryTests().metadata('other', Path('/outside'), [Path('/outside/compose.yaml')])
        with patch.object(backend, 'load_settings', return_value=Settings(include_directories=(Path('/included'),))), \
             patch.object(backend, 'run', side_effect=[Mock(returncode=0, stdout='id'), Mock(returncode=0, stdout=metadata)]):
            self.assertEqual(backend.compose_projects(None), [])


if __name__ == "__main__":
    unittest.main()
