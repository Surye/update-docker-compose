"""Exercise confirmation and launch behavior without changing Docker services."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

import compose_cli
from app_config import Settings, load_settings
import compose_backend as backend
from update_docker_compose import ComposeUpdater, QSystemTrayIcon


class InteractiveWorkflowTests(unittest.TestCase):
    def run_cli(self, answer, results, update_errors=()):
        project = backend.ComposeProject("example", (Path("/example/compose.yaml"),), Path("/example"))
        self.plan = backend.UpdatePlan((project,), ("reviewed-digest",))
        with patch.object(compose_cli, "prepare_update_plan", return_value=self.plan), \
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
        update.assert_called_once_with(self.plan, live=True)

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
    def test_completed_checks_finish_worker_cleanup_before_refreshing_menu(self):
        # Isolate Qt and bound the test so a GUI deadlock fails instead of
        # hanging the suite. Docker and the desktop tray host stay mocked out.
        script = textwrap.dedent('''
            import json
            import tempfile
            from unittest.mock import patch
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            from compose_backend import CheckResult
            from update_docker_compose import ComposeUpdater

            app = QApplication([])
            finished_before_refresh = []

            class ObservedUpdater(ComposeUpdater):
                def show_status(self):
                    if self.thread is not None:
                        finished_before_refresh.append(self.thread.isFinished())
                    super().show_status()

            with tempfile.TemporaryDirectory() as directory, \
                 patch.dict("os.environ", {"XDG_CACHE_HOME": directory,
                                           "XDG_CONFIG_HOME": directory}), \
                 patch("update_docker_compose.image_updates",
                       return_value=CheckResult(("example / web",), ())), \
                 patch("update_docker_compose.QSystemTrayIcon.show"), \
                 patch("update_docker_compose.QTimer.singleShot"):
                updater = ObservedUpdater(app)

                def check_completion():
                    if updater.thread is None and finished_before_refresh:
                        if len(finished_before_refresh) == 3:
                            app.quit()
                        else:
                            updater.start_job("check")

                timer = QTimer()
                timer.timeout.connect(check_completion)
                timer.start(10)
                updater.start_job("check")
                app.exec()
                print(json.dumps({"finished_before_refresh": finished_before_refresh,
                                  "containers": [a.text() for a in updater.pending_menu.actions()]}))
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).parent,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state["finished_before_refresh"], [True, True, True])
        self.assertEqual(state["containers"], ["example / web"])

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
        project = backend.ComposeProject("example", (Path("/example/compose.yaml"),), Path("/example"))
        plan = backend.UpdatePlan((project,), ("reviewed-digest",))
        with patch.object(backend, "run", return_value=Mock(returncode=1)) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            errors = backend._update_projects(plan, live=True)
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


class InspectionFailureTests(unittest.TestCase):
    def setUp(self):
        self.project = backend.ComposeProject("example", (Path("/example/compose.yaml"),), Path("/example"))
        self.container = {
            "State": {"Status": "running"},
            "Config": {"Labels": {"com.docker.compose.service": "web"}, "Image": "example:latest"},
            "Image": "sha256:old",
        }

    def check(self, inspected, latest=None):
        calls = [Mock(returncode=0), Mock(returncode=0, stdout="container-id"), inspected]
        if latest is not None:
            calls.append(latest)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), \
             patch.object(backend, "compose_projects", return_value=[self.project]), \
             patch.object(backend, "run", side_effect=calls):
            result = backend.image_updates()
            state = json.loads(backend.status_file().read_text())
        self.assertEqual(state["errors"], list(result.errors))
        return result

    def test_failed_container_inspection_marks_check_incomplete(self):
        result = self.check(Mock(returncode=1))
        self.assertTrue(result.errors)
        self.assertIn("example", result.errors[0])
        self.assertEqual(result.updates, ())

    def test_malformed_container_inspection_marks_check_incomplete(self):
        for response in ("not-json", "[]", "{}", "null", "[null]", "[{}]",
                         '[{"State":{"Status":"invalid-state"}}]',
                         '[{"State":{"Status":"running"},"Config":null}]',
                         '[{"State":{"Status":"running"},"Config":{"Image":null},"Image":"old"}]'):
            with self.subTest(response=response):
                result = self.check(Mock(returncode=0, stdout=response))
                self.assertTrue(result.errors)
                self.assertEqual(result.updates, ())

    def test_failed_or_empty_image_inspection_marks_check_incomplete(self):
        for latest in (Mock(returncode=1), Mock(returncode=0, stdout=" \n")):
            with self.subTest(latest=latest):
                result = self.check(Mock(returncode=0, stdout=json.dumps([self.container])), latest)
                self.assertTrue(result.errors)
                self.assertEqual(result.updates, ())

    def test_successful_image_comparison_reports_updates(self):
        for image_id, updates in (("sha256:old", ()), ("sha256:new", ("example / web",))):
            with self.subTest(image_id=image_id):
                result = self.check(Mock(returncode=0, stdout=json.dumps([self.container])),
                                    Mock(returncode=0, stdout=image_id + "\n"))
                self.assertEqual(result.updates, updates)
                self.assertEqual(result.errors, ())

    def test_stopped_container_is_skipped_without_an_error(self):
        self.container["State"]["Status"] = "exited"
        result = self.check(Mock(returncode=0, stdout=json.dumps([self.container])))
        self.assertEqual(result, backend.CheckResult((), ()))


class ConsentScopeTests(unittest.TestCase):
    """Run the real CLI/backend against a Docker command boundary double."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.names = ["reviewed"]
        self.configurations = {"reviewed": {"services": {"web": {"image": "example:latest"}}}}
        self.updated = []
        self.pulled = []
        self.after_pull = lambda: None
        self.config_failure = False
        for name in ("reviewed", "unreviewed"):
            (self.root / f"{name}.yaml").touch()
        self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root)}))
        self.enterContext(patch.object(backend, "load_settings", return_value=Settings()))
        self.enterContext(patch.object(backend, "run", side_effect=self.docker))

    def docker(self, command, cwd=None, *, live=False):
        if command[1] == "ps":
            return Mock(returncode=0, stdout=" ".join(self.names))
        if command[1] == "inspect":
            if "--format" in command:
                records = [DeployedDiscoveryTests().metadata(name, self.root, [self.root / f"{name}.yaml"])
                           for name in self.names]
                return Mock(returncode=0, stdout="\n".join(records))
            name = command[-1]
            container = {"State": {"Status": "running"},
                         "Config": {"Labels": {"com.docker.compose.service": "web"}, "Image": "example:latest"},
                         "Image": "sha256:new" if name in self.updated else "sha256:old"}
            return Mock(returncode=0, stdout=json.dumps([container]))
        if command[1:3] == ["image", "inspect"]:
            return Mock(returncode=0, stdout="sha256:new")
        if command[1] == "compose":
            name = command[3]
            if "config" in command:
                if self.config_failure:
                    return Mock(returncode=1, stdout="", stderr="sensitive environment values")
                return Mock(returncode=0, stdout=json.dumps(self.configurations[name]))
            if "pull" in command:
                self.pulled.append(name)
                self.after_pull()
                return Mock(returncode=0)
            if "ps" in command:
                return Mock(returncode=0, stdout=name)
            if "up" in command:
                self.updated.append(name)
                return Mock(returncode=0)
        self.fail(f"Unexpected Docker command: {command}")

    def cli(self, change=lambda: None):
        def confirm(_prompt):
            change()
            return "y"

        self.output = io.StringIO()
        self.errors = io.StringIO()
        with patch("builtins.input", side_effect=confirm), \
             contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
            return compose_cli.entrypoint()

    def test_new_project_after_review_requires_fresh_confirmation(self):
        def change():
            self.names.append("unreviewed")
            self.configurations["unreviewed"] = self.configurations["reviewed"]

        self.assertEqual(self.cli(change), 1)
        self.assertEqual(self.updated, [])

    def test_removed_project_after_review_requires_fresh_confirmation(self):
        self.assertEqual(self.cli(self.names.clear), 1)
        self.assertEqual(self.updated, [])

    def test_changed_effective_configuration_requires_fresh_confirmation(self):
        def change():
            self.configurations["reviewed"]["services"]["web"]["privileged"] = True

        self.assertEqual(self.cli(change), 1)
        self.assertEqual(self.updated, [])
        self.assertEqual(self.pulled, ["reviewed"])

    def test_configuration_failure_after_review_prevents_update(self):
        def change():
            self.config_failure = True

        self.assertEqual(self.cli(change), 1)
        self.assertEqual(self.updated, [])
        self.assertNotIn("sensitive environment values", self.errors.getvalue())

    def test_configuration_change_during_update_pull_prevents_recreation(self):
        def change():
            if len(self.pulled) == 2:
                self.configurations["reviewed"]["services"]["web"]["privileged"] = True

        self.after_pull = change
        self.assertEqual(self.cli(), 1)
        self.assertEqual(self.updated, [])

    def test_unchanged_review_updates_successfully(self):
        self.assertEqual(self.cli(), 0)
        self.assertEqual(self.updated, ["reviewed"])


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
            with patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), patch.object(backend, "run", side_effect=[
                Mock(returncode=0, stdout="container-id"),
                Mock(returncode=0, stdout=labels),
                Mock(returncode=0, stdout="{}"),
                Mock(returncode=0), Mock(returncode=0, stdout="{}"), Mock(returncode=0),
            ]) as run:
                plan = backend.prepare_update_plan(root)
                self.assertEqual(backend._update_projects(plan), ())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 6)
            self.assertFalse(any(str(backup) in command for command in commands))
            self.assertIn(str(deployed), commands[2])
            self.assertIn("--project-name", commands[-1])
            self.assertIn("media-stack", commands[-1])

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
