#!/usr/bin/env python3

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python before 3.11
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/codex_cli"


class CodexCliContractTests(unittest.TestCase):
    def test_release_artifacts_and_executables_are_digest_pinned(self) -> None:
        defaults = yaml.safe_load(
            (ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
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

    def test_managed_policy_excludes_privileged_and_extensible_modes(self) -> None:
        requirements = tomllib.loads(
            (ROLE / "templates/requirements.toml.j2").read_text(encoding="utf-8")
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
        self.assertNotIn("glob_scan_max_depth", profile["filesystem"])
        denied = profile["filesystem"][":workspace_roots"]
        self.assertTrue(denied)
        self.assertEqual(set(denied.values()), {"deny"})
        self.assertEqual(requirements["mcp_servers"], {})
        protected_paths = requirements["permissions"]["filesystem"]["deny_read"]
        for protected_path in (
            "/etc/vps",
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
            "remote_control",
            "remote_plugin",
            "skill_mcp_dependency_install",
        ):
            self.assertFalse(requirements["features"][feature])

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
            "InaccessiblePaths=-/etc/vps -/home/deploy -/home/ubuntu "
            "-/home/vpsadmin -/root -/run/containerd/containerd.sock "
            "-/run/docker.sock -/srv/vps -/var/lib/docker "
            "-/var/lib/vps-controller -/var/run/docker.sock",
            "--property=ProtectProc=invisible",
            "--property=PrivateDevices=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
            "DeviceAllow=char-netlink rw",
            "TemporaryFileSystem=/tmp:rw,nosuid,nodev,noexec,size={{ vps_codex_tmpfs_size_mb }}M,mode=1777",
            "TemporaryFileSystem=/var/tmp:rw,nosuid,nodev,noexec,size={{ vps_codex_tmpfs_size_mb }}M,mode=1777",
            "--property=RuntimeMaxSec=12h",
            "--slice=atlas-codex.slice",
        ):
            self.assertIn(boundary, launcher)
        self.assertIn("session_unit=atlas-codex-session", launcher)
        self.assertIn(
            "session_unit=atlas-codex-activation-verification", launcher
        )
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
        self.assertIn("sudo atlas-codex", entrypoint)
        self.assertIn("exit 77", entrypoint)

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

    def test_codex_is_not_an_ssh_or_system_service_identity(self) -> None:
        ssh_template = (
            ROOT / "ansible/roles/ssh/templates/00-vps-infra.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            next(line for line in ssh_template.splitlines() if line.startswith("AllowUsers")),
            "AllowUsers {{ vps_admin_user }} {{ vps_deploy_user }}",
        )
        role_text = "\n".join(
            path.read_text(encoding="utf-8") for path in ROLE.rglob("*") if path.is_file()
        )
        self.assertNotIn("codex.service", role_text)
        self.assertNotIn("authorized_keys", role_text)

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
                "/usr/local/sbin/atlas-codex",
                "/usr/local/bin/codex",
                "/usr/local/bin/codex-code-mode-host",
                "/opt/codex/current",
            },
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
        strict = block_by_name[
            "Prove no Codex session survived interlock acquisition"
        ]["ansible.builtin.assert"]["that"]
        self.assertIn("vps_codex_interlocked_session_state.rc == 3", strict)
        always_by_name = {
            task["name"]: task for task in transaction["always"]
        }
        self.assertIn(
            "not vps_codex_final_rollback_state.stat.exists",
            always_by_name["Release the persistent Codex session mask"]["when"],
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
        activation_names = [
            task["name"] for task in activation_transaction["block"]
        ]
        self.assertLess(
            activation_names.index("Record the complete Codex activation snapshot"),
            activation_names.index("Record the Codex activation publication boundary"),
        )
        self.assertEqual(
            activation_names[-1],
            "Publish and verify all active Codex surfaces",
        )
        self.assertEqual(
            commit["name"], "Record the verified Codex activation commit"
        )
        self.assertEqual(commit["when"], "not ansible_check_mode")
        self.assertEqual(
            cleanup["name"], "Remove committed Codex activation recovery state"
        )
        rescue_names = {
            task["name"] for task in activation_transaction["rescue"]
        }
        self.assertIn(
            "Restore the prior Codex activation after publication failure",
            rescue_names,
        )
        self.assertNotIn(
            "Remove committed Codex activation recovery state",
            activation_names,
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
            },
            "tasks/activation_transaction.yml": {
                "Create the private Codex activation snapshot",
                "Copy existing Codex activation surfaces into the snapshot",
                "Record originally absent Codex activation surfaces",
                "Record the complete Codex activation snapshot",
                "Record the Codex activation publication boundary",
                "Record the verified Codex activation commit",
                "Remove committed Codex activation recovery state",
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
