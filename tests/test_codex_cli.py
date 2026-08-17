#!/usr/bin/env python3

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python before 3.11
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/codex_cli"


class CodexCliContractTests(unittest.TestCase):
    def test_app_server_proxy_smoke_uses_the_ubuntu_runuser_path(self) -> None:
        tasks = yaml.safe_load(
            (ROLE / "tasks/verify_app_server.yml").read_text(encoding="utf-8")
        )
        smoke_block = next(
            task
            for task in tasks
            if task["name"] == "Prove the private Codex App Server WebSocket proxy"
        )
        smoke = next(
            task
            for task in smoke_block["block"]
            if task["name"]
            == "Open a bounded WebSocket upgrade through the Codex proxy"
        )
        command = smoke["ansible.builtin.command"]
        argv = command["argv"]
        self.assertEqual(
            argv[:3],
            [
                "/usr/bin/python3",
                "-I",
                "-c",
            ],
        )
        self.assertEqual(
            argv[4:],
            [
                "/usr/sbin/runuser",
                "--user",
                "{{ vps_codex_remote_user }}",
                "--",
                "/usr/bin/env",
                "-i",
                "LANG=C.UTF-8",
                "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                "SSH_ORIGINAL_COMMAND=codex app-server proxy",
                "{{ vps_codex_remote_gate_path }}",
                "--runtime-validation",
            ],
        )
        self.assertNotIn("/usr/bin/timeout", argv)
        self.assertNotIn("/usr/bin/runuser", argv)
        self.assertNotIn("stdin", command)
        self.assertNotIn("stdin_add_newline", command)
        watchdog_start = argv[3].index(
            "signal.setitimer(signal.ITIMER_REAL, WATCHDOG_TIMEOUT)"
        )
        watchdog_stop = argv[3].index(
            "signal.setitimer(signal.ITIMER_REAL, 0)"
        )
        self.assertLess(watchdog_start, argv[3].index("process = subprocess.Popen("))
        self.assertGreater(watchdog_stop, argv[3].index("finally:"))
        self.assertNotIn("128 + signal.SIGTERM", argv[3])
        self.assertNotIn("128 + signal.SIGKILL", argv[3])
        self.assertNotIn("os.waitid", argv[3])
        self.assertNotIn("process.poll()", argv[3])
        self.assertNotIn("leader_cleanup_signal_sent", argv[3])
        self.assertNotIn("sys.stdin", argv[3])
        self.assertIn("process.stdin.write(REQUEST)", argv[3])
        self.assertIn("elif process.returncode != 0:", argv[3])

        rendered_harness = str(
            Templar(loader=DataLoader()).template(trust_as_template(argv[3]))
        )
        self.assertEqual(rendered_harness, argv[3])
        harness_tree = ast.parse(rendered_harness)
        request_assignment = next(
            node
            for node in harness_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "REQUEST"
                for target in node.targets
            )
        )
        request = ast.literal_eval(request_assignment.value)
        self.assertEqual(
            request,
            b"GET /rpc HTTP/1.1\r\n"
            b"Host: codex-app-server\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n",
        )
        self.assertEqual(len(request), 158)
        child = """
import select
import sys

expected = %r
request = sys.stdin.buffer.read(len(expected))
ready, _, _ = select.select([sys.stdin.buffer], [], [], 0)
if request != expected or ready:
    raise SystemExit(71)
sys.stdout.buffer.write(
    b"HTTP/1.1 101 Switching Protocols\\r\\nConnection: Upgrade\\r\\n\\r\\n"
)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
""" % request
        harness = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                rendered_harness,
                sys.executable,
                "-I",
                "-c",
                child,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        harness_started = time.monotonic()
        self.assertEqual(harness.wait(timeout=10), 0)
        self.assertLess(time.monotonic() - harness_started, 3)
        assert harness.stdin is not None
        assert harness.stdout is not None
        assert harness.stderr is not None
        harness.stdin.close()
        harness_output = harness.stdout.read()
        harness_errors = harness.stderr.read()
        harness.stdout.close()
        harness.stderr.close()
        self.assertEqual(harness_errors, b"")
        self.assertIn(b"HTTP/1.1 101", harness_output)

        watchdog_harness = rendered_harness.replace(
            "READ_TIMEOUT = 5.0", "READ_TIMEOUT = 30.0"
        ).replace("WATCHDOG_TIMEOUT = 10.0", "WATCHDOG_TIMEOUT = 0.1")
        watchdog_child = """
import os
import sys
import time

sys.stdin.buffer.read(%d)
print(f"WATCHDOG_CHILD={os.getpid()}", file=sys.stderr, flush=True)
time.sleep(30)
""" % len(request)
        watchdog_started = time.monotonic()
        watchdog_expired = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                watchdog_harness,
                sys.executable,
                "-I",
                "-c",
                watchdog_child,
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
        self.assertLess(time.monotonic() - watchdog_started, 3)
        self.assertEqual(watchdog_expired.returncode, 124)
        self.assertIn(b"WebSocket smoke watchdog expired", watchdog_expired.stderr)
        watchdog_child_match = re.search(
            rb"WATCHDOG_CHILD=([0-9]+)", watchdog_expired.stderr
        )
        self.assertIsNotNone(watchdog_child_match)
        watchdog_child_pid = int(watchdog_child_match.group(1))
        with self.assertRaises(ProcessLookupError):
            os.kill(watchdog_child_pid, 0)

        runuser_proxy = """
import select
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_DFL)
request = sys.stdin.buffer.read(%d)
ready, _, _ = select.select([sys.stdin.buffer], [], [], 0)
if len(request) != %d or ready:
    raise SystemExit(71)
sys.stdout.buffer.write(
    b"HTTP/1.1 101 Switching Protocols\\r\\nConnection: Upgrade\\r\\n\\r\\n"
)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
time.sleep(30)
""" % (len(request), len(request))
        runuser_wrapper = """
import signal
import subprocess
import sys

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(sys.argv[1:])
returncode = child.wait()
raise SystemExit(returncode if returncode >= 0 else 128 - returncode)
"""
        mapped_sigterm = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                rendered_harness,
                sys.executable,
                "-I",
                "-c",
                runuser_wrapper,
                sys.executable,
                "-I",
                "-c",
                runuser_proxy,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(mapped_sigterm.returncode, 143)
        self.assertIn(b"HTTP/1.1 101", mapped_sigterm.stdout)
        self.assertIn(
            b"WebSocket proxy exited with status 143",
            mapped_sigterm.stderr,
        )

        failed_child = child + "\nraise SystemExit(72)\n"
        rejected = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                rendered_harness,
                sys.executable,
                "-I",
                "-c",
                failed_child,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"HTTP/1.1 101", rejected.stdout)
        self.assertIn(b"WebSocket proxy exited with status 72", rejected.stderr)

        delayed_failure_child = child.replace(
            "sys.stdin.buffer.read()\n",
            "sys.stdin.buffer.read()\n"
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(0.6)\n",
        ) + "\nraise SystemExit(74)\n"
        delayed_rejected = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                rendered_harness,
                sys.executable,
                "-I",
                "-c",
                delayed_failure_child,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(delayed_rejected.returncode, 74)
        self.assertIn(
            b"WebSocket proxy exited with status 74",
            delayed_rejected.stderr,
        )

        crashed_child = child + (
            "\nimport os, signal\n"
            "os.kill(os.getpid(), signal.SIGSEGV)\n"
        )
        crashed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                rendered_harness,
                sys.executable,
                "-I",
                "-c",
                crashed_child,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(crashed.returncode, 139)
        self.assertIn(b"WebSocket proxy exited with status -11", crashed.stderr)

        def descendant_exit_child(
            exit_status: int,
            term_behavior: str,
            leader_exit_delay: float = 0.0,
        ) -> str:
            if term_behavior == "ignore":
                term_setup = "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            elif term_behavior == "exit":
                term_setup = (
                    "signal.signal(signal.SIGTERM, lambda *_: "
                    "(os.write(2, b'DESCENDANT_TERM\\n'), sys.exit(0))); "
                )
            else:  # pragma: no cover - test fixture misuse
                raise AssertionError(f"unsupported TERM behavior: {term_behavior}")
            descendant_program = (
                "import os,signal,sys,time; "
                + term_setup
                + "os.write(int(sys.argv[1]), b'R'); "
                "os.close(int(sys.argv[1])); time.sleep(30)"
            )
            return """
import os
import select
import subprocess
import sys
import time

request = sys.stdin.buffer.read(%d)
ready, _, _ = select.select([sys.stdin.buffer], [], [], 0)
if len(request) != %d or ready:
    raise SystemExit(71)
ready_reader, ready_writer = os.pipe()
descendant = subprocess.Popen([
    sys.executable,
    "-I",
    "-c",
    %r,
    str(ready_writer),
], pass_fds=(ready_writer,))
os.close(ready_writer)
if os.read(ready_reader, 1) != b"R":
    raise SystemExit(75)
os.close(ready_reader)
print(f"DESCENDANT={descendant.pid}", file=sys.stderr, flush=True)
sys.stdout.buffer.write(
    b"HTTP/1.1 101 Switching Protocols\\r\\nConnection: Upgrade\\r\\n\\r\\n"
)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
time.sleep(%r)
raise SystemExit(%d)
""" % (
                len(request),
                len(request),
                descendant_program,
                leader_exit_delay,
                exit_status,
            )

        def run_descendant_exit_case(
            exit_status: int,
            term_behavior: str,
            *,
            harness_source: str = rendered_harness,
            leader_exit_delay: float = 0.0,
        ) -> subprocess.CompletedProcess[bytes]:
            started = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    harness_source,
                    sys.executable,
                    "-I",
                    "-c",
                    descendant_exit_child(
                        exit_status, term_behavior, leader_exit_delay
                    ),
                ],
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(result.returncode, exit_status, result.stderr.decode())
            if exit_status == 0:
                self.assertNotIn(b"atlas Codex proxy smoke:", result.stderr)
            else:
                self.assertIn(
                    f"WebSocket proxy exited with status {exit_status}".encode(),
                    result.stderr,
                )
            descendant_match = re.search(rb"DESCENDANT=([0-9]+)", result.stderr)
            self.assertIsNotNone(descendant_match)
            descendant_pid = int(descendant_match.group(1))
            descendant_deadline = time.monotonic() + 2
            while time.monotonic() < descendant_deadline:
                try:
                    os.kill(descendant_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail("proxy smoke descendant survived process-group cleanup")
            return result

        # Widen the former waitid-to-killpg race deterministically: the leader
        # exits rc=0 while the harness is paused immediately before TERM, and a
        # descendant keeps the anchored process group alive. Linux killpg then
        # succeeds on the zombie group without changing the final rc=0 truth.
        race_harness = rendered_harness.replace(
            "        signal_group(process, signal.SIGTERM)",
            "        time.sleep(0.4)\n"
            "        signal_group(process, signal.SIGTERM)",
            1,
        )
        self.assertNotEqual(race_harness, rendered_harness)
        natural_0 = run_descendant_exit_case(
            0,
            "ignore",
            harness_source=race_harness,
            leader_exit_delay=0.6,
        )
        self.assertIn(b"HTTP/1.1 101", natural_0.stdout)
        natural_143 = run_descendant_exit_case(143, "ignore")
        self.assertNotIn(b"DESCENDANT_TERM", natural_143.stderr)
        natural_137 = run_descendant_exit_case(137, "exit")
        self.assertIn(b"DESCENDANT_TERM", natural_137.stderr)

        overflow_child = """
import sys

sys.stdin.buffer.read(%d)
sys.stdout.buffer.write(b"x" * 20000)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
""" % len(request)
        overflow = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                rendered_harness,
                sys.executable,
                "-I",
                "-c",
                overflow_child,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(overflow.returncode, 70, overflow.stderr.decode())
        self.assertEqual(len(overflow.stdout), 16384)
        self.assertIn(b"output exceeded its bound", overflow.stderr)

        misleading_child = """
import sys

sys.stdin.buffer.read(%d)
sys.stdout.buffer.write(
    b"HTTP/1.1 500 Broken\\r\\nContent-Length: 91\\r\\n\\r\\n"
    b"HTTP/1.1 101 Switching Protocols\\r\\n"
    b"Connection: Upgrade\\r\\nUpgrade: websocket\\r\\n"
    b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\\r\\n\\r\\n"
)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
""" % len(request)
        misleading = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                rendered_harness,
                sys.executable,
                "-I",
                "-c",
                misleading_child,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(misleading.returncode, 0, misleading.stderr.decode())
        self.assertEqual(
            misleading.stdout,
            b"HTTP/1.1 500 Broken\r\nContent-Length: 91\r\n\r\n",
        )
        status_assertion = smoke_block["block"][2]["ansible.builtin.assert"]["that"][1]
        self.assertIn(
            "stdout_lines[0:1] == ['HTTP/1.1 101 Switching Protocols']",
            status_assertion,
        )
        converge_tasks = yaml.safe_load(
            (ROLE / "tasks/converge.yml").read_text(encoding="utf-8")
        )
        prerequisites = next(
            task
            for task in converge_tasks
            if task["name"]
            == "Install bounded storage and package verification dependencies"
        )
        self.assertIn("util-linux", prerequisites["ansible.builtin.apt"]["name"])

    def test_release_artifacts_and_executables_are_digest_pinned(self) -> None:
        defaults = yaml.safe_load(
            (ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertIs(defaults["vps_codex_remote_control_enabled"], False)
        self.assertRegex(defaults["vps_codex_version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(set(defaults["vps_codex_artifacts"]), {"x86_64", "aarch64"})
        self.assertEqual(
            set(defaults["vps_codex_bubblewrap_artifacts"]),
            {"x86_64", "aarch64"},
        )
        expected_files = {
            "bin/codex",
            "bin/codex-code-mode-host",
            "codex-path/rg",
            "codex-resources/bwrap",
            "codex-resources/zsh/bin/zsh",
        }
        digest_pattern = re.compile(r"^[0-9a-f]{64}$")
        self.assertEqual(
            defaults["vps_codex_release_base_url"],
            "https://releases.openai.com/codex/releases",
        )
        expected_entries = {
            "bin|d",
            "bin/codex|f",
            "bin/codex-code-mode-host|f",
            "codex-package.json|f",
            "codex-path|d",
            "codex-path/rg|f",
            "codex-resources|d",
            "codex-resources/bwrap|f",
            "codex-resources/zsh|d",
            "codex-resources/zsh/bin|d",
            "codex-resources/zsh/bin/zsh|f",
        }
        self.assertEqual(set(defaults["vps_codex_release_entries"]), expected_entries)
        for architecture, artifact in defaults["vps_codex_artifacts"].items():
            self.assertRegex(artifact["archive_sha256"], digest_pattern)
            self.assertEqual(set(artifact["file_sha256"]), expected_files)
            for digest in artifact["file_sha256"].values():
                self.assertRegex(digest, digest_pattern)
            self.assertTrue(artifact["target"].endswith("-unknown-linux-musl"))
            self.assertTrue(artifact["target"].startswith(architecture))
        for artifact in defaults["vps_codex_bubblewrap_artifacts"].values():
            self.assertRegex(artifact["package_sha256"], digest_pattern)
            self.assertRegex(artifact["executable_sha256"], digest_pattern)

    def test_app_server_lifecycle_supports_all_remote_mode_combinations(
        self,
    ) -> None:
        converge_tasks = yaml.safe_load(
            (ROLE / "tasks/converge.yml").read_text(encoding="utf-8")
        )
        converge_by_name = {task["name"]: task for task in converge_tasks}
        derived_expression = converge_by_name[
            "Select the pinned Codex artifact for this architecture"
        ]["ansible.builtin.set_fact"]["vps_codex_app_server_enabled"]

        activate_tasks = yaml.safe_load(
            (ROLE / "tasks/activate.yml").read_text(encoding="utf-8")
        )
        activate_by_name = {task["name"]: task for task in activate_tasks}
        for task_name in (
            "Install the private Codex App Server unit",
            "Enable the private Codex App Server transactionally",
        ):
            self.assertEqual(
                activate_by_name[task_name]["when"],
                "vps_codex_app_server_enabled | bool",
            )
        self.assertEqual(
            activate_by_name["Validate the private Codex App Server unit"]["when"],
            ["not ansible_check_mode", "vps_codex_app_server_enabled | bool"],
        )
        for task_name in (
            "Remove the private Codex App Server unit when disabled",
            "Remove private Codex App Server boot activation when disabled",
        ):
            self.assertEqual(
                activate_by_name[task_name]["when"],
                "not (vps_codex_app_server_enabled | bool)",
            )

        gateway_conditions = {
            "Install the bounded Codex remote SSH command gate": (
                "vps_codex_remote_enabled | bool"
            ),
            "Install restricted Codex remote public keys transactionally": (
                "vps_codex_remote_enabled | bool"
            ),
            "Remove Codex remote SSH authorization transactionally": (
                "not (vps_codex_remote_enabled | bool)"
            ),
            "Remove the Codex remote SSH command gate when disabled": (
                "not (vps_codex_remote_enabled | bool)"
            ),
            "Install the exact Codex remote sudo gateway": (
                "vps_codex_remote_enabled | bool"
            ),
            "Remove the Codex remote sudo gateway when disabled": (
                "not (vps_codex_remote_enabled | bool)"
            ),
        }
        for task_name, expected_condition in gateway_conditions.items():
            self.assertEqual(
                activate_by_name[task_name]["when"], expected_condition
            )

        activation = yaml.safe_load(
            (ROLE / "tasks/activation_transaction.yml").read_text(
                encoding="utf-8"
            )
        )[0]
        activation_by_name = {
            task["name"]: task for task in activation["block"]
        }
        verify = activation_by_name[
            "Verify the private Codex App Server before activation commit"
        ]
        self.assertEqual(verify["when"], "vps_codex_app_server_enabled | bool")
        self.assertEqual(
            verify["vars"]["vps_codex_verify_proxy_smoke"],
            "{{ vps_codex_remote_enabled | bool }}",
        )
        self.assertEqual(
            activation_by_name[
                "Verify private Codex App Server disablement before activation commit"
            ]["when"],
            "not (vps_codex_app_server_enabled | bool)",
        )

        cases = (
            (False, False, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        )
        for ssh_remote, direct_mobile, expected_app_server in cases:
            variables = {
                "vps_codex_remote_enabled": ssh_remote,
                "vps_codex_remote_control_enabled": direct_mobile,
            }
            templar = Templar(loader=DataLoader(), variables=variables)
            observed_app_server = templar.template(
                trust_as_template(derived_expression)
            )
            observed_proxy_smoke = templar.template(
                trust_as_template(
                    verify["vars"]["vps_codex_verify_proxy_smoke"]
                )
            )
            with self.subTest(
                ssh_remote=ssh_remote, direct_mobile=direct_mobile
            ):
                self.assertIs(observed_app_server, expected_app_server)
                self.assertIs(observed_proxy_smoke, ssh_remote)

        locked_tasks = yaml.safe_load(
            (ROLE / "tasks/locked_convergence.yml").read_text(encoding="utf-8")
        )[0]["block"]
        locked_by_name = {task["name"]: task for task in locked_tasks}
        for task_name in (
            "Stop transient Codex remote gateway units behind the interlock",
            "Read transient Codex remote gateway units behind the interlock",
        ):
            argv = locked_by_name[task_name]["ansible.builtin.command"]["argv"]
            self.assertIn("atlas-codex-pairing-*.service", argv)
            self.assertIn("atlas-codex-proxy-*.service", argv)
            self.assertIn("atlas-codex-version-*.service", argv)

    def test_managed_policy_excludes_privileged_and_extensible_modes(self) -> None:
        requirements_template = (
            ROLE / "templates/requirements.toml.j2"
        ).read_text(encoding="utf-8")
        policy_variables = {
            "vps_codex_remote_user": "codex-remote",
            "vps_codex_remote_control_enabled": False,
        }
        rendered_requirements = str(
            Templar(loader=DataLoader(), variables=policy_variables).template(
                trust_as_template(requirements_template)
            )
        )
        requirements = tomllib.loads(
            rendered_requirements
        )
        self.assertEqual(requirements["allowed_approval_policies"], ["never"])
        self.assertNotIn("allowed_approvals_reviewers", requirements)
        self.assertEqual(requirements["allowed_login_methods"], ["chatgpt"])
        self.assertFalse(requirements["allow_appshots"])
        self.assertFalse(requirements["allow_login_shell"])
        self.assertTrue(requirements["allow_managed_hooks_only"])
        self.assertFalse(requirements["allow_remote_control"])
        self.assertEqual(requirements["allowed_web_search_modes"], [])
        self.assertFalse(requirements["check_for_update_on_startup"])
        self.assertEqual(requirements["default_permissions"], "atlas_workspace")
        self.assertEqual(
            requirements["allowed_permission_profiles"],
            {":read-only": True, "atlas_workspace": True},
        )
        self.assertNotIn(":danger-full-access", requirements["allowed_permission_profiles"])
        profile = requirements["permissions"]["atlas_workspace"]
        self.assertEqual(profile["extends"], ":workspace")
        self.assertFalse(profile["network"]["enabled"])
        self.assertEqual(profile["filesystem"][":root"], "deny")
        self.assertEqual(profile["filesystem"][":minimal"], "read")
        self.assertEqual(profile["filesystem"]["glob_scan_max_depth"], 32)
        denied = profile["filesystem"][":workspace_roots"]
        self.assertTrue(denied)
        self.assertEqual(set(denied.values()), {"deny"})
        self.assertEqual(requirements["mcp_servers"], {})
        protected_paths = requirements["permissions"]["filesystem"]["deny_read"]
        for protected_path in (
            "/etc/vps",
            "/home/codex-remote",
            "/srv/codex/home/.codex",
            "/home/vpsadmin",
            "/root",
            "/run/docker.sock",
            "/srv/vps",
            "/var/lib/docker",
        ):
            self.assertIn(protected_path, protected_paths)
        for feature in (
            "apps",
            "browser_use",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "computer_use",
            "image_generation",
            "in_app_browser",
            "plugins",
            "recommended_plugins",
            "remote_plugin",
            "skill_mcp_dependency_install",
        ):
            self.assertFalse(requirements["features"][feature])
        self.assertFalse(requirements["features"]["remote_control"])

        policy_variables["vps_codex_remote_control_enabled"] = True
        enabled_requirements = tomllib.loads(
            str(
                Templar(loader=DataLoader(), variables=policy_variables).template(
                    trust_as_template(requirements_template)
                )
            )
        )
        self.assertTrue(enabled_requirements["allow_remote_control"])
        self.assertFalse(enabled_requirements["features"]["remote_control"])
        self.assertEqual(
            enabled_requirements["permissions"], requirements["permissions"]
        )

    def test_user_defaults_forbid_escalation_and_avoid_history(self) -> None:
        config = tomllib.loads(
            (ROLE / "templates/config.toml.j2").read_text(encoding="utf-8")
        )
        self.assertEqual(config["approval_policy"], "never")
        self.assertNotIn("approvals_reviewer", config)
        self.assertFalse(config["allow_login_shell"])
        self.assertFalse(config["check_for_update_on_startup"])
        self.assertEqual(config["cli_auth_credentials_store"], "file")
        self.assertEqual(config["default_permissions"], "atlas_workspace")
        self.assertEqual(config["web_search"], "disabled")
        self.assertEqual(config["history"]["persistence"], "none")

    def test_role_uses_a_dedicated_account_and_proves_os_boundaries(self) -> None:
        tasks = yaml.safe_load(
            (ROLE / "tasks/converge.yml").read_text(encoding="utf-8")
        )
        activation_tasks = yaml.safe_load(
            (ROLE / "tasks/activate.yml").read_text(encoding="utf-8")
        )
        by_name = {
            task["name"]: task for task in [*tasks, *activation_tasks]
        }
        user = by_name["Create the isolated Codex runtime account"][
            "ansible.builtin.user"
        ]
        self.assertEqual(user["name"], "{{ vps_codex_user }}")
        self.assertEqual(user["groups"], "")
        self.assertFalse(user["append"])
        self.assertTrue(user["system"])
        self.assertFalse(user["create_home"])
        self.assertTrue(user["password_lock"])
        self.assertNotIn("sudo", str(user))
        self.assertNotIn("docker", str(user))

        remote_user_task = by_name[
            "Create the unprivileged Codex remote gateway account"
        ]
        remote_user = remote_user_task["ansible.builtin.user"]
        self.assertEqual(remote_user["name"], "{{ vps_codex_remote_user }}")
        self.assertEqual(remote_user["groups"], "")
        self.assertFalse(remote_user["append"])
        self.assertFalse(remote_user["system"])
        self.assertFalse(remote_user["create_home"])
        self.assertTrue(remote_user["password_lock"])
        self.assertEqual(
            remote_user_task["when"], "vps_codex_remote_enabled | bool"
        )
        self.assertNotIn("sudo", str(remote_user))
        self.assertNotIn("docker", str(remote_user))
        remote_home = by_name[
            "Create the root-owned Codex remote home boundary"
        ]["ansible.builtin.file"]
        self.assertEqual(remote_home["owner"], "root")
        self.assertEqual(remote_home["group"], "root")
        self.assertEqual(remote_home["mode"], "0755")
        self.assertIs(remote_home["follow"], False)
        remote_ssh = by_name[
            "Create the private Codex remote SSH directory"
        ]["ansible.builtin.file"]
        self.assertEqual(remote_ssh["owner"], "root")
        self.assertEqual(remote_ssh["group"], "root")
        self.assertEqual(remote_ssh["mode"], "0755")
        self.assertIs(remote_ssh["follow"], False)
        remote_keys = by_name[
            "Install restricted Codex remote public keys transactionally"
        ]["ansible.builtin.template"]
        self.assertEqual(remote_keys["owner"], "root")
        self.assertEqual(remote_keys["group"], "root")
        self.assertEqual(remote_keys["mode"], "0644")
        remote_state = by_name[
            "Create the root-owned Codex remote state boundary"
        ]["ansible.builtin.file"]
        self.assertEqual(remote_state["owner"], "root")
        self.assertEqual(remote_state["group"], "root")
        self.assertEqual(remote_state["mode"], "0755")
        self.assertIs(remote_state["follow"], False)
        remote_control = by_name[
            "Create the writable Codex remote control directory"
        ]["ansible.builtin.file"]
        self.assertEqual(remote_control["owner"], "{{ vps_codex_remote_user }}")
        self.assertEqual(remote_control["group"], "{{ vps_codex_remote_group }}")
        self.assertEqual(remote_control["mode"], "0700")
        self.assertIs(remote_control["follow"], False)

        remote_boundary_probe = by_name[
            "Inspect existing Codex remote filesystem boundaries"
        ]
        self.assertIs(
            remote_boundary_probe["ansible.builtin.stat"]["follow"], False
        )
        self.assertEqual(
            {item["path"]: item for item in remote_boundary_probe["loop"]},
            {
                "/home/{{ vps_codex_remote_user }}": {
                    "path": "/home/{{ vps_codex_remote_user }}",
                    "type": "directory",
                    "owner": "root",
                    "group": "root",
                    "mode": "0755",
                },
                "/home/{{ vps_codex_remote_user }}/.ssh": {
                    "path": "/home/{{ vps_codex_remote_user }}/.ssh",
                    "type": "directory",
                    "owner": "root",
                    "group": "root",
                    "mode": "0755",
                },
                "/home/{{ vps_codex_remote_user }}/.ssh/authorized_keys": {
                    "path": (
                        "/home/{{ vps_codex_remote_user }}/.ssh/authorized_keys"
                    ),
                    "type": "file",
                    "owner": "root",
                    "group": "root",
                    "mode": "0644",
                },
                "/home/{{ vps_codex_remote_user }}/.codex": {
                    "path": "/home/{{ vps_codex_remote_user }}/.codex",
                    "type": "directory",
                    "owner": "root",
                    "group": "root",
                    "mode": "0755",
                },
                "/home/{{ vps_codex_remote_user }}/.codex/app-server-control": {
                    "path": (
                        "/home/{{ vps_codex_remote_user }}/.codex/"
                        "app-server-control"
                    ),
                    "type": "directory",
                    "owner": "{{ vps_codex_remote_user }}",
                    "group": "{{ vps_codex_remote_group }}",
                    "mode": "0700",
                },
            },
        )
        remote_boundary_guard = " ".join(
            by_name[
                "Refuse unsafe existing Codex remote filesystem boundaries"
            ]["ansible.builtin.assert"]["that"]
        )
        for boundary in (
            "not item.stat.islnk",
            "item.stat.pw_name == item.item.owner",
            "item.stat.gr_name == item.item.group",
            "item.stat.mode == item.item.mode",
        ):
            self.assertIn(boundary, remote_boundary_guard)

        download = by_name["Download the exact official Codex release archive"][
            "ansible.builtin.get_url"
        ]
        self.assertEqual(
            download["url"],
            "{{ vps_codex_release_base_url }}/{{ vps_codex_version }}/"
            "{{ vps_codex_archive_name }}",
        )
        self.assertEqual(
            download["checksum"],
            "sha256:{{ vps_codex_artifact.archive_sha256 }}",
        )
        self.assertFalse(download["force"])

        dependencies = by_name[
            "Install bounded storage and package verification dependencies"
        ][
            "ansible.builtin.apt"
        ]["name"]
        self.assertIn("libcap2-bin", dependencies)
        bubblewrap_install = by_name["Install the exact Ubuntu bubblewrap package"][
            "ansible.builtin.apt"
        ]
        self.assertEqual(
            bubblewrap_install["name"],
            "bubblewrap={{ vps_codex_bubblewrap_version }}",
        )
        self.assertFalse(bubblewrap_install["allow_downgrade"])
        self.assertTrue(bubblewrap_install["allow_change_held_packages"])
        self.assertEqual(
            by_name["Install the exact Ubuntu bubblewrap package"]["register"],
            "vps_codex_bubblewrap_install",
        )
        bubblewrap_upgrade_guard = by_name[
            "Refuse a non-transactional bubblewrap change for an existing runtime"
        ]["ansible.builtin.assert"]
        self.assertIn(
            "'installed ' ~ vps_codex_bubblewrap_version",
            " ".join(bubblewrap_upgrade_guard["that"]),
        )
        self.assertIn(
            "vps_codex_surfaces_before_bubblewrap.results",
            " ".join(bubblewrap_upgrade_guard["that"]),
        )
        self.assertIn(
            "dedicated package migration",
            bubblewrap_upgrade_guard["fail_msg"],
        )
        self.assertEqual(
            by_name["Install the exact Ubuntu bubblewrap package"]["when"],
            "vps_codex_bubblewrap_bootstrap_required | bool",
        )
        self.assertEqual(
            by_name["Hold the reviewed Ubuntu bubblewrap package"][
                "ansible.builtin.dpkg_selections"
            ]["selection"],
            "hold",
        )
        bwrap_guard = by_name["Prove the distribution bubblewrap boundary"][
            "ansible.builtin.assert"
        ]["that"]
        self.assertIn("vps_codex_distribution_bwrap.stat.mode == '0755'", bwrap_guard)
        self.assertIn(
            "vps_codex_distribution_bwrap.stat.checksum == "
            "vps_codex_bubblewrap_artifact.executable_sha256",
            bwrap_guard,
        )
        self.assertIn(
            "vps_codex_distribution_bwrap_capabilities.stdout == ''",
            bwrap_guard,
        )
        for post_install_check in (
            "Read the installed bubblewrap version after reconciliation",
            "Inspect the distribution bubblewrap executable",
            "Inspect distribution bubblewrap file capabilities",
            "Prove the distribution bubblewrap boundary",
        ):
            self.assertIn(
                "vps_codex_bubblewrap_install.changed",
                by_name[post_install_check]["when"],
            )

        task_names = list(by_name)
        self.assertLess(
            task_names.index(
                "Refuse a non-transactional bubblewrap change for an existing runtime"
            ),
            task_names.index("Install the exact Ubuntu bubblewrap package"),
        )
        self.assertLess(
            task_names.index("Validate the published Codex release"),
            task_names.index("Atomically select the verified Codex release"),
        )
        release_transaction = yaml.safe_load(
            (ROLE / "tasks/release_transaction.yml").read_text(encoding="utf-8")
        )[0]
        stage = release_transaction["block"]
        stage_by_name = {task["name"]: task for task in stage}
        self.assertEqual(
            stage_by_name["Extract the Codex release into staging"][
                "ansible.builtin.unarchive"
            ]["extra_opts"],
            ["--no-same-owner"],
        )
        finish_names = [task["name"] for task in stage]
        self.assertLess(
            finish_names.index(
                "Validate Codex policy against the staged release"
            ),
            finish_names.index("Atomically publish the verified Codex release"),
        )
        self.assertEqual(
            release_transaction["always"][0]["ansible.builtin.file"]["state"],
            "absent",
        )

        boundary_probe = by_name["Probe prohibited Codex account capabilities"][
            "ansible.builtin.command"
        ]["argv"][-1]
        for forbidden_capability in (
            "/etc/vps/secrets",
            "/srv/vps/repository",
            "/var/run/docker.sock",
            "test -x {{ vps_codex_storage_root }}",
            "test -w {{ vps_codex_workspace_root }}",
            "/usr/bin/sudo -n",
        ):
            self.assertIn(forbidden_capability, boundary_probe)
        self.assertEqual(
            by_name["Probe prohibited Codex account capabilities"]["become_user"],
            "{{ vps_codex_user }}",
        )

        sandbox = by_name["Prove the bounded Codex session and managed sandbox"][
            "ansible.builtin.command"
        ]["argv"]
        self.assertEqual(sandbox, ["/usr/local/sbin/atlas-codex", "--verify"])
        self.assertEqual(
            by_name["Prove the bounded Codex session and managed sandbox"]["when"],
            "not ansible_check_mode",
        )

        launcher = (
            ROLE / "templates/atlas-codex.j2"
        ).read_text(encoding="utf-8")
        for boundary in (
            "--uid={{ vps_codex_user }}",
            "--gid={{ vps_codex_group }}",
            "--property=CPUQuota={{ vps_codex_cpu_quota }}",
            "--property=MemoryMax={{ vps_codex_memory_max }}",
            "--property=MemorySwapMax={{ vps_codex_memory_swap_max }}",
            "--property=TasksMax={{ vps_codex_tasks_max }}",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes",
            "InaccessiblePaths=-/etc/vps -/home/deploy "
            "-/home/{{ vps_codex_remote_user }} -/home/ubuntu "
            "-/home/vpsadmin -/root -/run/containerd/containerd.sock "
            "-/run/docker.sock -/srv/vps -/var/lib/docker "
            "-/var/lib/vps-controller -/var/run/docker.sock",
            "--property=ProtectProc=invisible",
            "--property=PrivateDevices=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
            "SocketBindAllow=udp",
            "SocketBindDeny=ipv4",
            "SocketBindDeny=ipv6",
            "TemporaryFileSystem=/tmp:rw,nosuid,nodev,noexec,size={{ vps_codex_tmpfs_size_mb }}M,mode=1777",
            "TemporaryFileSystem=/var/tmp:rw,nosuid,nodev,noexec,size={{ vps_codex_tmpfs_size_mb }}M,mode=1777",
            '--property=RuntimeMaxSec="$runtime_max"',
            "--slice=atlas-codex.slice",
        ):
            self.assertIn(boundary, launcher)
        self.assertIn("runtime_max=12h", launcher)
        self.assertIn("runtime_max=30s", launcher)
        self.assertIn("session_unit=atlas-codex-session", launcher)
        self.assertIn(
            "session_unit=atlas-codex-activation-verification", launcher
        )
        self.assertIn("session_unit=atlas-codex-proxy-verification", launcher)
        self.assertIn('--unit="$session_unit"', launcher)
        self.assertIn("{{ vps_codex_storage_image_path }}", launcher)
        self.assertIn(
            "PATH={{ vps_codex_release_path }}/codex-path:/usr/bin:",
            launcher,
        )
        self.assertNotIn("/codex-resources:", launcher)
        self.assertIn('test "$(command -v bwrap)" = /usr/bin/bwrap', launcher)
        self.assertIn(
            "{{ vps_codex_bubblewrap_artifact.executable_sha256 }}",
            launcher,
        )
        self.assertIn("sha256sum --check --status", launcher)
        self.assertIn("findmnt --noheadings --output FSTYPE -T /tmp", launcher)
        self.assertIn(
            "systemctl show atlas-codex-activation-verification.service "
            "--property=ProtectProc --value",
            launcher,
        )
        self.assertIn("test -r /proc/self/cmdline", launcher)
        self.assertIn("test ! -r /proc/1/cmdline", launcher)
        for inaccessible_path in (
            "/etc/vps",
            "/home/vpsadmin",
            "/srv/vps",
            "/var/lib/docker",
            "/var/lib/vps-controller",
            "/run/containerd/containerd.sock",
            "/run/docker.sock",
            "/var/run/docker.sock",
        ):
            self.assertIn(f"test ! -r {inaccessible_path}", launcher)
        self.assertIn("{{ vps_codex_home }}/.codex/config.toml", launcher)
        self.assertIn("--verify", launcher)
        self.assertIn("exit 64", launcher)

        entrypoint = (
            ROLE / "templates/codex-entrypoint.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("/atlas-codex.slice/", entrypoint)
        self.assertIn("/usr/bin/sudo -n /usr/local/sbin/atlas-codex", entrypoint)
        self.assertIn("exit 77", entrypoint)
        self.assertIn("--remote-version", entrypoint)
        self.assertIn("--remote-app-server", entrypoint)
        self.assertIn("--remote-proxy", entrypoint)
        self.assertNotIn("--validate-remote-proxy", entrypoint)
        self.assertNotIn('exec /usr/bin/sudo -n "$@"', entrypoint)

        policy_transaction = yaml.safe_load(
            (ROLE / "tasks/policy_transaction.yml").read_text(encoding="utf-8")
        )[0]
        always_names = {task["name"] for task in policy_transaction["always"]}
        self.assertEqual(
            always_names,
            {
                "Remove the disposable Codex policy probe home",
                "Remove remote Codex policy staging",
                "Remove the disposable staged-policy sandbox fixture",
                "Remove neutral Codex bind destinations after validation",
            },
        )

    def test_codex_remote_gateway_is_dedicated_and_bounded(self) -> None:
        ssh_template = (
            ROOT / "ansible/roles/ssh/templates/00-vps-infra.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            next(line for line in ssh_template.splitlines() if line.startswith("AllowUsers")),
            "AllowUsers {{ vps_admin_user }} {{ vps_deploy_user }}"
            "{% if vps_codex_remote_enabled | bool %} "
            "{{ vps_codex_remote_user }}{% endif %}",
        )
        self.assertIn("Match User {{ vps_codex_remote_user }}", ssh_template)
        self.assertIn(
            "ForceCommand {{ vps_codex_remote_gate_path }}", ssh_template
        )
        self.assertEqual(ssh_template.count("PermitUserEnvironment no"), 1)
        self.assertLess(
            ssh_template.index("PermitUserEnvironment no"),
            ssh_template.index("Match User {{ vps_deploy_user }}"),
        )
        for boundary in (
            "DisableForwarding yes",
            "PermitTTY no",
            "PermitUserRC no",
            "AllowAgentForwarding no",
            "AllowTcpForwarding no",
            "PermitTunnel no",
        ):
            self.assertIn(boundary, ssh_template)

        role_text = "\n".join(
            path.read_text(encoding="utf-8") for path in ROLE.rglob("*") if path.is_file()
        )
        self.assertNotIn("app-server daemon bootstrap", role_text)

        service = (
            ROLE / "templates/atlas-codex-app-server.service.j2"
        ).read_text(encoding="utf-8")
        for boundary in (
            "User={{ vps_codex_user }}",
            "Group={{ vps_codex_group }}",
            "Slice=atlas-codex.slice",
            " --listen unix://",
            "Restart=always",
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=",
            "ProtectHome=yes",
            "ProtectSystem=strict",
            "SocketBindAllow=udp",
            "SocketBindDeny=ipv4",
            "SocketBindDeny=ipv6",
            "WantedBy=multi-user.target",
        ):
            self.assertIn(boundary, service)
        self.assertLess(
            service.index("SocketBindAllow=udp"),
            service.index("SocketBindDeny=ipv4"),
        )
        self.assertIn(
            "ExecCondition=+/usr/local/sbin/atlas-codex --service-admission",
            service,
        )
        self.assertIn(
            "ExecCondition=+/usr/local/sbin/atlas-codex --service-preflight",
            service,
        )
        self.assertNotIn("RestartPreventExitStatus", service)
        self.assertIn("vps_codex_remote_control_enabled", service)
        self.assertIn(" --remote-control{% endif %} --listen unix://", service)
        self.assertNotIn("RuntimeMaxSec", service)

        launcher = (ROLE / "templates/atlas-codex.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn("--remote-control-pair)", launcher)
        self.assertIn("require_admin_caller", launcher)
        self.assertIn("codex remote-control pair", launcher)

        verification = (
            ROLE / "tasks/verify_app_server.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("--property=SocketBindAllow", verification)
        self.assertIn("(?m)^SocketBindAllow=.*udp", verification)

        authorized_keys = (
            ROLE / "templates/codex-remote-authorized-keys.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("restrict {{ key | trim }}", authorized_keys)

        sudoers = (
            ROLE / "templates/codex-remote.sudoers.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("!setenv", sudoers)
        self.assertIn("NOPASSWD:NOSETENV", sudoers)
        self.assertEqual(sudoers.count("NOPASSWD:NOSETENV"), 4)
        for command in (
            "--remote-version",
            "--remote-app-server",
            "--remote-proxy",
            "--validate-remote-proxy",
        ):
            self.assertIn(
                "/usr/local/sbin/atlas-codex " + command,
                sudoers,
            )
        self.assertNotIn("--remote-control-pair", sudoers)
        self.assertNotIn("ALL=(ALL", sudoers)

        activation_tasks = yaml.safe_load(
            (ROLE / "tasks/activate.yml").read_text(encoding="utf-8")
        )
        activation_by_name = {
            task["name"]: task for task in activation_tasks
        }
        gate_self_test = activation_by_name[
            "Validate current Codex desktop SSH command envelopes"
        ]
        self.assertEqual(
            gate_self_test["ansible.builtin.command"]["argv"],
            ["{{ vps_codex_remote_gate_path }}", "--self-test"],
        )
        self.assertIs(gate_self_test["changed_when"], False)
        self.assertEqual(
            gate_self_test["when"],
            ["not ansible_check_mode", "vps_codex_remote_enabled | bool"],
        )

        gate_template_path = (
            ROLE / "templates/atlas-codex-remote-gate.py.j2"
        )
        gate_source = gate_template_path.read_text(encoding="utf-8")
        rendered_gate = gate_source.replace(
            "{{ vps_codex_remote_user }}", "codex-remote"
        )
        self.assertNotIn("{{", rendered_gate)
        gate_namespace: dict[str, object] = {
            "__name__": "atlas_codex_remote_gate_contract_test"
        }
        exec(
            compile(rendered_gate, str(gate_template_path), "exec"),
            gate_namespace,
        )
        gate_namespace["_self_test"]()
        self.assertIn(
            'option = "--validate-remote-proxy"', rendered_gate
        )
        self.assertIn('"\\\\777" * 8', rendered_gate)
        self.assertIn("invalid Codex desktop marker", rendered_gate)
        self.assertIn("_APP_SERVER_BOOTSTRAP_0148", rendered_gate)
        self.assertIn("_MARKED_PROXY_0148", rendered_gate)
        self.assertIn("forwarded-ssh-agent.sock", rendered_gate)
        self.assertIn("pkill -9 -U", rendered_gate)
        self.assertIn("[d]esktop-ssh-websocket-v0.sock", rendered_gate)
        self.assertNotIn("command.startswith(", rendered_gate)

        site = yaml.safe_load(
            (ROOT / "ansible/playbooks/site.yml").read_text(encoding="utf-8")
        )
        self.assertIn({"role": "codex_cli"}, site[0]["roles"])

    def test_release_validation_forces_only_read_only_checks_in_check_mode(
        self,
    ) -> None:
        tasks = yaml.safe_load(
            (ROLE / "tasks/validate_release.yml").read_text(encoding="utf-8")
        )
        forced = {
            task["name"]: task
            for task in tasks
            if task.get("check_mode") is False
        }
        self.assertEqual(
            set(forced),
            {
                "Read the complete Codex release candidate tree",
            },
        )
        for task in forced.values():
            self.assertFalse(task["changed_when"])
            self.assertEqual(
                {key for key in task if key.startswith("ansible.builtin.")},
                {"ansible.builtin.command"},
            )
        by_name = {task["name"]: task for task in tasks}
        for runtime_check in (
            "Execute the Codex release candidate as the runtime account",
            "Prove the Codex release candidate version",
        ):
            self.assertEqual(by_name[runtime_check]["when"], "not ansible_check_mode")

        sandbox_block = by_name[
            "Prove the Codex release candidate loads the managed sandbox"
        ]
        self.assertEqual(
            sandbox_block["when"],
            [
                "not ansible_check_mode",
                "vps_codex_validate_sandbox | default(true) | bool",
            ],
        )
        sandbox_tasks = {task["name"]: task for task in sandbox_block["block"]}
        candidate_sandbox = sandbox_tasks[
            "Execute the managed Codex sandbox boundary probe"
        ]["ansible.builtin.command"]["argv"]
        self.assertIn("--include-managed-config", candidate_sandbox)
        self.assertIn("atlas_workspace", candidate_sandbox)
        self.assertIn(".codex/config.toml", candidate_sandbox[-1])
        self.assertIn("a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/.env", candidate_sandbox[-1])
        candidate_environment = sandbox_tasks[
            "Execute the managed Codex sandbox boundary probe"
        ]["environment"]
        self.assertTrue(candidate_environment["PATH"].startswith(
            "{{ vps_codex_candidate_path }}/codex-path:/usr/bin:"
        ))
        self.assertNotIn("/codex-resources:", candidate_environment["PATH"])
        self.assertEqual(
            sandbox_block["always"][0]["ansible.builtin.file"]["state"],
            "absent",
        )

        policy_transaction = yaml.safe_load(
            (ROLE / "tasks/policy_transaction.yml").read_text(encoding="utf-8")
        )[0]
        policy_block = {task["name"]: task for task in policy_transaction["block"]}
        for task_name in (
            "Parse and enforce the candidate Codex policy before publication",
            "Prove the staged policy denies host and deep workspace secrets",
        ):
            policy_argv = policy_block[task_name]["ansible.builtin.command"]["argv"]
            runtime_path = next(
                item
                for item in policy_argv
                if isinstance(item, str) and item.startswith("--setenv=PATH=")
            )
            self.assertIn("/codex-path:/usr/bin:", runtime_path)
            self.assertNotIn("/codex-resources:", runtime_path)

    def test_predictive_check_requires_a_complete_normal_install(self) -> None:
        tasks = yaml.safe_load(
            (ROLE / "tasks/converge.yml").read_text(encoding="utf-8")
        )
        by_name = {task["name"]: task for task in tasks}
        predictive_surfaces = str(
            by_name[
                "Inspect installed Codex boundaries before a predictive check"
            ]["loop"]
        )
        for remote_surface in (
            "/etc/systemd/system/atlas-codex-app-server.service",
            (
                "/etc/systemd/system/multi-user.target.wants/"
                "atlas-codex-app-server.service"
            ),
            "/etc/sudoers.d/92-codex-remote-codex",
            ".ssh/authorized_keys",
            "/usr/local/sbin/atlas-codex-remote-gate",
        ):
            self.assertIn(remote_surface, predictive_surfaces)
        self.assertIn(
            "if (vps_codex_app_server_enabled | bool) else []",
            predictive_surfaces,
        )
        self.assertIn(
            "if (vps_codex_remote_enabled | bool) else []",
            predictive_surfaces,
        )
        self.assertLess(
            predictive_surfaces.index("vps_codex_app_server_enabled"),
            predictive_surfaces.index("vps_codex_remote_enabled"),
        )
        preflight = by_name[
            "Refuse a predictive check before normal Codex convergence"
        ]
        self.assertEqual(preflight["when"], "ansible_check_mode")
        self.assertIn(
            "successful normal convergence",
            preflight["ansible.builtin.assert"]["fail_msg"],
        )

    def test_codex_persistent_state_is_bounded_by_a_private_mount(self) -> None:
        defaults = yaml.safe_load(
            (ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(defaults["vps_codex_storage_root"], "/srv/codex")
        self.assertEqual(
            defaults["vps_codex_storage_image_path"],
            "/var/lib/vps-infra/codex-storage.ext4",
        )
        self.assertEqual(defaults["vps_codex_storage_size_mb"], 6144)
        self.assertEqual(defaults["vps_codex_host_reserve_mb"], 10240)
        self.assertEqual(defaults["vps_codex_tmpfs_size_mb"], 512)
        self.assertEqual(defaults["vps_codex_home"], "/srv/codex/home")

        tasks = yaml.safe_load(
            (ROLE / "tasks/converge.yml").read_text(encoding="utf-8")
        )
        by_name = {task["name"]: task for task in tasks}
        allocation = by_name["Allocate a bounded Codex storage candidate"][
            "ansible.builtin.command"
        ]["argv"]
        self.assertEqual(allocation[-1], "{{ vps_codex_storage_image_path }}.next")
        formatter = by_name["Format the bounded Codex storage candidate"]
        self.assertIn("not ansible_check_mode", formatter["when"])
        publish_task = by_name[
            "Atomically publish the bounded Codex storage image"
        ]
        self.assertEqual(
            publish_task["args"]["creates"],
            "{{ vps_codex_storage_image_path }}",
        )
        self.assertIn(
            "Refuse divergent or duplicate Codex fstab entries", by_name
        )
        fstab_guard = by_name["Refuse divergent or duplicate Codex fstab entries"]
        self.assertIn(
            "duplicate fstab entry",
            fstab_guard["ansible.builtin.assert"]["fail_msg"],
        )
        fstab = by_name["Persist the bounded Codex storage mount"][
            "ansible.builtin.lineinfile"
        ]["line"]
        for option in ("loop", "nofail", "nodev", "nosuid", "noatime"):
            self.assertIn(option, fstab)

        task_names = list(by_name)
        self.assertLess(
            task_names.index("Prove the activated Codex storage contract"),
            task_names.index("Inspect the protected Codex state directory"),
        )

        state_directory = by_name[
            "Create the missing protected Codex state directory"
        ]
        self.assertIs(
            state_directory["ansible.builtin.file"]["follow"], False
        )
        self.assertEqual(
            state_directory["when"],
            "not vps_codex_state_directory.stat.exists",
        )
        state_guard = by_name["Refuse an unsafe existing Codex state directory"]
        state_guard_contract = " ".join(
            state_guard["ansible.builtin.assert"]["that"]
        )
        for boundary in (
            "stat.isdir",
            "not vps_codex_state_directory.stat.islnk",
            "stat.pw_name == vps_codex_user",
            "stat.gr_name == vps_codex_group",
            "stat.mode == '0700'",
        ):
            self.assertIn(boundary, state_guard_contract)

    def test_codex_resource_limits_are_aggregate_and_per_session(self) -> None:
        defaults = yaml.safe_load(
            (ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        slice_template = (
            ROLE / "templates/atlas-codex.slice.j2"
        ).read_text(encoding="utf-8")
        for setting in (
            "CPUQuota={{ vps_codex_cpu_quota }}",
            "MemoryHigh={{ vps_codex_memory_high }}",
            "MemoryMax={{ vps_codex_memory_max }}",
            "MemorySwapMax={{ vps_codex_memory_swap_max }}",
            "TasksMax={{ vps_codex_tasks_max }}",
        ):
            self.assertIn(setting, slice_template)
        self.assertEqual(defaults["vps_codex_memory_max"], "3G")
        self.assertEqual(defaults["vps_codex_memory_swap_max"], 0)
        self.assertEqual(defaults["vps_codex_tasks_max"], 256)

    def test_activation_is_serialized_and_durably_rolled_back(self) -> None:
        defaults = yaml.safe_load(
            (ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            defaults["vps_codex_activation_rollback_root"],
            "/var/lib/vps-infra/codex-activation-rollback",
        )
        self.assertEqual(
            {item["path"] for item in defaults["vps_codex_activation_surfaces"]},
            {
                "/srv/codex/home/.codex/config.toml",
                "/etc/codex/requirements.toml",
                "/etc/systemd/system/atlas-codex.slice",
                "/etc/systemd/system/atlas-codex-app-server.service",
                (
                    "/etc/systemd/system/multi-user.target.wants/"
                    "atlas-codex-app-server.service"
                ),
                "/etc/sudoers.d/92-codex-remote-codex",
                "/home/{{ vps_codex_remote_user }}/.ssh/authorized_keys",
                "/usr/local/sbin/atlas-codex-remote-gate",
                "/usr/local/sbin/atlas-codex",
                "/usr/local/bin/codex",
                "/usr/local/bin/codex-code-mode-host",
                "/opt/codex/current",
            },
        )
        activation_surfaces = {
            item["path"]: item
            for item in defaults["vps_codex_activation_surfaces"]
        }
        self.assertEqual(
            activation_surfaces[
                "/etc/systemd/system/multi-user.target.wants/"
                "atlas-codex-app-server.service"
            ]["type"],
            "link",
        )
        self.assertEqual(
            activation_surfaces["/usr/local/sbin/atlas-codex-remote-gate"][
                "type"
            ],
            "file",
        )
        self.assertEqual(
            activation_surfaces[
                "/home/{{ vps_codex_remote_user }}/.ssh/authorized_keys"
            ]["type"],
            "file",
        )

        main_tasks = yaml.safe_load(
            (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
        )
        transaction = yaml.safe_load(
            (ROLE / "tasks/locked_convergence.yml").read_text(encoding="utf-8")
        )[0]
        self.assertEqual(
            transaction["name"],
            "Converge Codex behind durable session and controller interlocks",
        )
        block_by_name = {task["name"]: task for task in transaction["block"]}
        mask = block_by_name[
            "Persistently prevent a new fixed Codex session"
        ]["ansible.builtin.command"]["argv"]
        self.assertEqual(
            mask,
            [
                "/usr/bin/systemctl",
                "mask",
                "atlas-codex-session.service",
            ],
        )
        self.assertNotIn("--runtime", mask)
        stop_app_server = block_by_name[
            "Stop the private Codex App Server behind the interlock"
        ]["ansible.builtin.command"]["argv"]
        self.assertEqual(
            stop_app_server,
            ["/usr/bin/systemctl", "stop", "{{ vps_codex_app_server_unit }}"],
        )
        strict = block_by_name[
            "Prove no Codex session survived interlock acquisition"
        ]["ansible.builtin.assert"]["that"]
        self.assertIn("vps_codex_interlocked_session_state.rc == 3", strict)
        always_by_name = {
            task["name"]: task for task in transaction["always"]
        }
        always_names = list(always_by_name)
        self.assertLess(
            always_names.index(
                "Prove final private Codex App Server lifecycle surfaces are coherent"
            ),
            always_names.index("Release the persistent Codex session mask"),
        )
        self.assertLess(
            always_names.index("Remove the Codex convergence maintenance marker"),
            always_names.index(
                "Reverify the committed private Codex App Server after "
                "interlock release"
            ),
        )
        self.assertLess(
            always_names.index("Remove the Codex convergence maintenance marker"),
            always_names.index(
                "Reverify the restored private Codex App Server after "
                "interlock release"
            ),
        )
        self.assertIn(
            "not vps_codex_final_rollback_state.stat.exists",
            always_by_name["Release the persistent Codex session mask"]["when"],
        )
        self.assertEqual(
            always_by_name[
                "Reverify the committed private Codex App Server after "
                "interlock release"
            ]["ansible.builtin.include_tasks"],
            "verify_app_server.yml",
        )
        self.assertIs(
            always_by_name[
                "Reverify the committed private Codex App Server after "
                "interlock release"
            ]["vars"]["vps_codex_verify_proxy_smoke"],
            False,
        )
        self.assertIn(
            "vps_codex_activation_committed | default(false) | bool",
            always_by_name[
                "Reverify the committed private Codex App Server after "
                "interlock release"
            ]["when"],
        )
        self.assertEqual(
            always_by_name[
                "Reverify the restored private Codex App Server after "
                "interlock release"
            ]["ansible.builtin.include_tasks"],
            "verify_restored_app_server.yml",
        )
        self.assertIn(
            "not (vps_codex_activation_committed | default(false) | bool)",
            always_by_name[
                "Reverify the restored private Codex App Server after "
                "interlock release"
            ]["when"],
        )
        self.assertEqual(
            always_by_name[
                "Reverify private Codex App Server disablement after interlock release"
            ]["ansible.builtin.include_tasks"],
            "verify_app_server_disabled.yml",
        )

        main_by_name = {task["name"]: task for task in main_tasks}
        acquisition_index = next(
            index
            for index, task in enumerate(main_tasks)
            if task["name"] == "Acquire the atomic host Codex convergence lease"
        )
        for task in main_tasks[:acquisition_index]:
            module_keys = {
                key for key in task if key.startswith("ansible.builtin.")
            }
            self.assertTrue(
                module_keys
                <= {
                    "ansible.builtin.assert",
                    "ansible.builtin.command",
                    "ansible.builtin.set_fact",
                },
                task["name"],
            )
            if "ansible.builtin.command" in task:
                self.assertIs(task["changed_when"], False, task["name"])

        owner = main_by_name[
            "Identify the persistent SSH controller process"
        ]["ansible.builtin.command"]["argv"]
        self.assertEqual(owner[:2], ["/bin/sh", "-c"])
        self.assertIn("pid=$PPID", owner[2])
        self.assertIn('"/proc/$pid/stat"', owner[2])
        self.assertIn("print $20", owner[2])

        lease = main_by_name[
            "Acquire the atomic host Codex convergence lease"
        ]["ansible.builtin.command"]["argv"]
        self.assertIn("/usr/bin/systemd-run", lease)
        self.assertIn("--collect", lease)
        self.assertIn("--property=RuntimeMaxSec=24h", lease)
        self.assertIn(
            "--property=RuntimeDirectory=atlas-codex-convergence", lease
        )
        self.assertIn("--property=RuntimeDirectoryMode=0700", lease)
        shell_index = lease.index("/bin/sh")
        self.assertEqual(lease[shell_index : shell_index + 2], ["/bin/sh", "-c"])
        self.assertIn("owner_pid=$1", lease[shell_index + 2])
        self.assertIn('"/proc/$owner_pid/stat"', lease[shell_index + 2])
        self.assertIn("print $20", lease[shell_index + 2])
        self.assertIn('[ ! -e "$release_path" ]', lease[shell_index + 2])
        self.assertIn('[ ! -L "$release_path" ]', lease[shell_index + 2])
        self.assertEqual(
            lease[shell_index + 3], "atlas-codex-convergence-lease"
        )
        self.assertIn("{{ vps_codex_controller_owner_pid }}", lease)
        self.assertIn(
            "--unit={{ vps_codex_convergence_unit | regex_replace('\\.service$', '') }}",
            lease,
        )
        self.assertIn(
            "--description={{ vps_codex_convergence_description }}", lease
        )
        self.assertFalse(
            (ROLE / "templates/codex-convergence-owner.j2").exists()
        )
        self.assertFalse(
            (ROLE / "templates/codex-convergence-lease.j2").exists()
        )
        lifecycle = main_by_name[
            "Own the complete Codex convergence lease lifecycle"
        ]
        self.assertEqual(
            lifecycle["always"][0]["name"],
            "Signal owner-aware Codex convergence lease release",
        )
        release = lifecycle["always"][0]["ansible.builtin.command"]["argv"]
        release_script = release[2]
        self.assertIn('set -C', release_script)
        self.assertIn(': > "$release_path"', release_script)
        self.assertNotIn("systemctl stop", release_script)
        self.assertEqual(
            lifecycle["always"][1]["name"],
            "Confirm owner-aware Codex convergence lease release",
        )

        activation = yaml.safe_load(
            (ROLE / "tasks/activation_transaction.yml").read_text(
                encoding="utf-8"
            )
        )
        activation_transaction = activation[0]
        commit = activation[1]
        cleanup = activation[2]
        committed_fact = activation[3]
        activation_names = [
            task["name"] for task in activation_transaction["block"]
        ]
        self.assertLess(
            activation_names.index("Record the complete Codex activation snapshot"),
            activation_names.index("Record the Codex activation publication boundary"),
        )
        self.assertLess(
            activation_names.index("Publish and verify all active Codex surfaces"),
            activation_names.index("Enter Codex runtime validation before commit"),
        )
        self.assertLess(
            activation_names.index("Enter Codex runtime validation before commit"),
            activation_names.index(
                "Verify the private Codex App Server before activation commit"
            ),
        )
        activation_block_by_name = {
            task["name"]: task for task in activation_transaction["block"]
        }
        runtime_validation = activation_block_by_name[
            "Enter Codex runtime validation before commit"
        ]["ansible.builtin.command"]["argv"]
        self.assertEqual(
            runtime_validation,
            [
                "/usr/bin/mv",
                "--no-target-directory",
                (
                    "{{ vps_codex_activation_rollback_root }}/"
                    "activation-started"
                ),
                (
                    "{{ vps_codex_activation_rollback_root }}/"
                    "runtime-validation-started"
                ),
            ],
        )
        self.assertEqual(
            activation_block_by_name[
                "Verify the private Codex App Server before activation commit"
            ]["ansible.builtin.include_tasks"],
            "verify_app_server.yml",
        )
        self.assertEqual(
            activation_block_by_name[
                "Verify the private Codex App Server before activation commit"
            ]["vars"]["vps_codex_verify_proxy_smoke"],
            "{{ vps_codex_remote_enabled | bool }}",
        )
        self.assertEqual(
            activation_block_by_name[
                "Verify the private Codex App Server before activation commit"
            ]["when"],
            "vps_codex_app_server_enabled | bool",
        )
        self.assertEqual(
            activation_block_by_name[
                "Verify private Codex App Server disablement before activation commit"
            ]["ansible.builtin.include_tasks"],
            "verify_app_server_disabled.yml",
        )
        self.assertEqual(
            activation_block_by_name[
                "Verify private Codex App Server disablement before activation commit"
            ]["when"],
            "not (vps_codex_app_server_enabled | bool)",
        )
        self.assertEqual(
            commit["name"], "Record the verified Codex activation commit"
        )
        self.assertEqual(commit["when"], "not ansible_check_mode")
        self.assertEqual(
            cleanup["name"], "Remove committed Codex activation recovery state"
        )
        self.assertEqual(
            committed_fact["name"],
            "Mark the current Codex activation as committed",
        )
        self.assertIs(
            committed_fact["ansible.builtin.set_fact"][
                "vps_codex_activation_committed"
            ],
            True,
        )
        rescue_by_name = {
            task["name"]: task for task in activation_transaction["rescue"]
        }
        self.assertIn(
            "Restore the prior Codex activation after publication failure",
            rescue_by_name,
        )
        failed_markers = rescue_by_name[
            "Inspect failed Codex activation publication markers"
        ]["loop"]
        self.assertEqual(
            failed_markers,
            [
                (
                    "{{ vps_codex_activation_rollback_root }}/"
                    "activation-started"
                ),
                (
                    "{{ vps_codex_activation_rollback_root }}/"
                    "runtime-validation-started"
                ),
            ],
        )
        self.assertIn(
            "vps_codex_failed_runtime_validation_started.exists",
            rescue_by_name[
                "Restore the prior Codex activation after publication failure"
            ]["when"],
        )
        self.assertNotIn(
            "Remove committed Codex activation recovery state",
            activation_names,
        )

        recovery_tasks = yaml.safe_load(
            (ROLE / "tasks/recover_activation.yml").read_text(
                encoding="utf-8"
            )
        )
        recovery_by_name = {task["name"]: task for task in recovery_tasks}
        recovery_markers = recovery_by_name[
            "Inspect durable Codex activation recovery state"
        ]["loop"]
        self.assertEqual(
            recovery_markers[3],
            (
                "{{ vps_codex_activation_rollback_root }}/"
                "runtime-validation-started"
            ),
        )
        recovery_selection = recovery_by_name[
            "Select durable Codex activation recovery markers"
        ]["ansible.builtin.set_fact"]
        self.assertEqual(
            recovery_selection["vps_codex_recovery_runtime_state"],
            "{{ vps_codex_recovery_state.results[3].stat }}",
        )
        recovery_guard = " ".join(
            recovery_by_name[
                "Refuse unsafe Codex activation recovery state"
            ]["ansible.builtin.assert"]["that"]
        )
        recovery_guard = " ".join(recovery_guard.split())
        self.assertIn(
            "not (vps_codex_recovery_started_state.exists and "
            "vps_codex_recovery_runtime_state.exists)",
            recovery_guard,
        )
        interrupted_rollback = " ".join(
            recovery_by_name[
                "Roll back an interrupted Codex activation"
            ]["when"]
        )
        self.assertIn(
            "vps_codex_recovery_runtime_state.exists",
            interrupted_rollback,
        )

        activation_tasks = yaml.safe_load(
            (ROLE / "tasks/activate.yml").read_text(encoding="utf-8")
        )
        selector_block = next(
            task
            for task in activation_tasks
            if task["name"] == "Atomically select the verified Codex release"
        )["block"]
        selector_candidate = next(
            task
            for task in selector_block
            if task["name"] == "Create the Codex release selector candidate"
        )["ansible.builtin.file"]
        self.assertIs(selector_candidate["follow"], False)
        activation_task_by_name = {
            task["name"]: task for task in activation_tasks
        }
        app_server_link = activation_task_by_name[
            "Enable the private Codex App Server transactionally"
        ]["ansible.builtin.file"]
        self.assertEqual(app_server_link["state"], "link")
        self.assertIs(app_server_link["follow"], False)

        restored_verification = (
            ROLE / "tasks/verify_restored_app_server.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("(?: --remote-control)? --listen unix://", restored_verification)

    def test_runtime_probes_use_unique_disposable_directories(self) -> None:
        policy = (
            ROLE / "tasks/policy_transaction.yml"
        ).read_text(encoding="utf-8")
        validation = (
            ROLE / "tasks/validate_release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("ansible.builtin.tempfile", policy)
        self.assertIn("prefix: .policy-sandbox-probe-", policy)
        self.assertIn("prefix: .policy-probe-", policy)
        self.assertNotIn(
            'path: "{{ vps_codex_workspace_root }}/.policy-sandbox-probe"',
            policy,
        )
        self.assertIn("ansible.builtin.tempfile", validation)
        self.assertIn("prefix: .sandbox-probe-", validation)
        self.assertNotIn(
            'path: "{{ vps_codex_workspace_root }}/.sandbox-probe"',
            validation,
        )

    def test_transaction_bookkeeping_does_not_report_durable_drift(self) -> None:
        def tasks_by_name(path: Path) -> dict[str, dict[str, object]]:
            observed: dict[str, dict[str, object]] = {}

            def visit(tasks: list[dict[str, object]]) -> None:
                for task in tasks:
                    observed[str(task["name"])] = task
                    for section in ("block", "rescue", "always"):
                        nested = task.get(section)
                        if isinstance(nested, list):
                            visit(nested)

            visit(yaml.safe_load(path.read_text(encoding="utf-8")))
            return observed

        expected_ephemeral_tasks = {
            "tasks/main.yml": {
                "Acquire the atomic host Codex convergence lease",
                "Signal owner-aware Codex convergence lease release",
            },
            "tasks/locked_convergence.yml": {
                "Record ownership of the Codex session mask",
                "Publish the Codex convergence maintenance marker",
                "Persistently prevent a new fixed Codex session",
                "Release the persistent Codex session mask",
                "Remove the Codex convergence maintenance marker",
                "Remove the released Codex session mask ownership record",
                "Stop the private Codex App Server behind the interlock",
                "Stop transient Codex remote gateway units behind the interlock",
            },
            "tasks/activation_transaction.yml": {
                "Create the private Codex activation snapshot",
                "Copy existing Codex activation surfaces into the snapshot",
                "Record originally absent Codex activation surfaces",
                "Record the complete Codex activation snapshot",
                "Record the Codex activation publication boundary",
                "Enter Codex runtime validation before commit",
                "Stop the private Codex App Server after activation failure",
                "Read private Codex App Server state after activation failure",
                "Record the verified Codex activation commit",
                "Remove committed Codex activation recovery state",
            },
            "tasks/verify_app_server.yml": {
                "Start the private Codex App Server for verified lifecycle",
                "Read effective private Codex App Server properties",
                "Stop a stale Codex proxy verification unit",
                "Open a bounded WebSocket upgrade through the Codex proxy",
                "Stop the bounded Codex proxy verification unit",
                "Read the bounded Codex proxy verification unit state",
            },
            "tasks/verify_app_server_disabled.yml": {
                "Read disabled private Codex App Server state",
                "Read disabled private Codex App Server boot state",
            },
            "tasks/verify_restored_app_server.yml": {
                "Start the restored private Codex App Server",
                "Read effective restored private Codex App Server properties",
            },
            "tasks/policy_transaction.yml": {
                "Create a private remote Codex policy staging directory",
                "Back up existing live Codex policy files",
                "Copy candidate Codex policy files to remote staging",
                "Create neutral validation bind destinations",
                "Allocate a disposable unit-visible Codex policy probe home",
                "Delegate the disposable Codex policy probe home",
                "Allocate a staged-policy sandbox probe directory",
                "Delegate the staged-policy sandbox probe directory",
                "Create a deeply nested staged-policy sandbox probe directory",
                "Create a disposable deeply nested staged-policy secret fixture",
                "Remove the disposable Codex policy probe home",
                "Remove remote Codex policy staging",
                "Remove the disposable staged-policy sandbox fixture",
                "Remove neutral Codex bind destinations after validation",
            },
            "tasks/validate_release.yml": {
                "Allocate a Codex sandbox probe directory",
                "Delegate the Codex sandbox probe directory",
                "Create a deeply nested Codex sandbox probe directory",
                "Create a disposable deeply nested Codex secret fixture",
                "Remove the disposable Codex sandbox probe fixture",
            },
            "tasks/release_transaction.yml": {
                "Create a private Codex release staging directory",
                "Make the Codex release staging directory traversable",
                "Extract the Codex release into staging",
                "Remove an unpublished Codex release staging directory",
            },
        }

        for relative_path, expected_names in expected_ephemeral_tasks.items():
            observed = tasks_by_name(ROLE / relative_path)
            for task_name in expected_names:
                self.assertIn(task_name, observed, relative_path)
                self.assertIs(
                    observed[task_name].get("changed_when"),
                    False,
                    f"{relative_path}: {task_name}",
                )


if __name__ == "__main__":
    unittest.main()
