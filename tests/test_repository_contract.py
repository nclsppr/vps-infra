#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


APPLICATION_STATE = load_script_module(
    "application_state_policy",
    SCRIPTS / "validate-application-state",
)


class SupplyChainContractTests(unittest.TestCase):
    def test_engine_pin_is_fixed_and_held_packages_can_change(self) -> None:
        variables = yaml.safe_load(
            (ROOT / "ansible/inventories/production/group_vars/all.yml").read_text(
                encoding="utf-8"
            )
        )
        packages = {item["name"]: item["version"] for item in variables["vps_docker_packages"]}
        self.assertEqual(packages["docker-ce-cli"], packages["docker-ce"])
        engine_match = re.fullmatch(
            r"5:(\d+)\.(\d+)\.(\d+)-1~ubuntu\.26\.04~resolute",
            packages["docker-ce"],
        )
        self.assertIsNotNone(engine_match)
        assert engine_match is not None
        engine_version = tuple(int(part) for part in engine_match.groups())
        self.assertGreaterEqual(engine_version, (29, 7, 2))

        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/docker/tasks/main.yml").read_text(encoding="utf-8")
        )
        install = next(
            task
            for task in tasks
            if task["name"] == "Install exact Docker Engine and Compose versions"
        )
        self.assertIs(install["ansible.builtin.apt"]["allow_change_held_packages"], True)
        self.assertIs(install["ansible.builtin.apt"]["allow_downgrade"], False)

    def test_builder_runtime_images_are_immutable(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/caddy-image.yml").read_text(encoding="utf-8")
        )
        image_pattern = re.compile(
            r"^[a-z0-9./_-]+:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}$"
        )
        buildkit_image = workflow["env"]["BUILDKIT_IMAGE"]
        binfmt_image = workflow["env"]["BINFMT_IMAGE"]
        self.assertRegex(buildkit_image, image_pattern)
        self.assertRegex(binfmt_image, image_pattern)
        self.assertNotIn(":latest@", binfmt_image)
        self.assertNotIn(":buildx-stable-1@", buildkit_image)

        steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
        buildx_steps = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/setup-buildx-action@")
        ]
        qemu_steps = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/setup-qemu-action@")
        ]
        self.assertGreaterEqual(len(buildx_steps), 2)
        self.assertEqual(len(qemu_steps), 1)
        for step in buildx_steps:
            self.assertEqual(
                step["with"]["driver-opts"].strip(),
                "image=${{ env.BUILDKIT_IMAGE }}",
            )
        self.assertEqual(qemu_steps[0]["with"]["image"], "${{ env.BINFMT_IMAGE }}")
        self.assertEqual(qemu_steps[0]["with"]["platforms"], "arm64")


class SecurityBoundaryContractTests(unittest.TestCase):
    def test_assert_conditions_are_yaml_strings(self) -> None:
        def inspect_assertions(node: object, path: Path) -> None:
            if isinstance(node, dict):
                assertion = node.get("ansible.builtin.assert")
                if isinstance(assertion, dict):
                    conditions = assertion.get("that", [])
                    if isinstance(conditions, str):
                        conditions = [conditions]
                    for condition in conditions:
                        self.assertIsInstance(
                            condition,
                            str,
                            f"{path}: assert condition parsed as {type(condition).__name__}",
                        )
                for value in node.values():
                    inspect_assertions(value, path)
            elif isinstance(node, list):
                for value in node:
                    inspect_assertions(value, path)

        for path in (ROOT / "ansible").rglob("*.yml"):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            inspect_assertions(document, path.relative_to(ROOT))

    def test_ufw_validation_accepts_rendered_ipv4_and_ipv6_rules(self) -> None:
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/firewall/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/firewall/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        extraction = next(
            task
            for task in tasks
            if task["name"] == "Extract rules outside the exact managed ingress allowlist"
        )["ansible.builtin.set_fact"]
        assertions = next(
            task
            for task in tasks
            if task["name"]
            == "Assert UFW is active with the exact managed ingress allowlist"
        )["ansible.builtin.assert"]["that"]
        output_lines = [
            "Status: active",
            "",
            "     To                         Action      From",
            "     --                         ------      ----",
            "[ 1] 22/tcp                     ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 2] 80/tcp                     ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 3] 443/tcp                    ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 4] 443/udp                    ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 5] 22/tcp (v6)                ALLOW IN    Anywhere (v6)              # vps-infra managed",
            "[ 6] 80/tcp (v6)                ALLOW IN    Anywhere (v6)              # vps-infra managed",
            "[ 7] 443/tcp (v6)               ALLOW IN    Anywhere (v6)              # vps-infra managed",
            "[ 8] 443/udp (v6)               ALLOW IN    Anywhere (v6)              # vps-infra managed",
        ]
        rendered_rules = output_lines[4:]
        templar = Templar(
            loader=DataLoader(),
            variables={
                "vps_ufw_numbered_status": {"stdout_lines": output_lines},
                "vps_firewall_managed_rule_pattern": defaults[
                    "vps_firewall_managed_rule_pattern"
                ],
            }
        )

        rule_lines = templar.template(
            trust_as_template(extraction["vps_ufw_rule_lines"])
        )
        unexpected_rule_lines = templar.template(
            trust_as_template(extraction["vps_ufw_unexpected_rule_lines"])
        )
        self.assertEqual(rule_lines, rendered_rules)
        self.assertEqual(unexpected_rule_lines, [])

        for condition, ipv4_rule, ipv6_rule in zip(
            assertions[3:],
            rendered_rules[:4],
            rendered_rules[4:],
            strict=True,
        ):
            for rule in (ipv4_rule, ipv6_rule):
                result = Templar(
                    loader=DataLoader(),
                    variables={"vps_ufw_rule_lines": [rule]}
                ).template(trust_as_template("{{ " + condition + " }}"))
                self.assertIs(result, True)

    def test_firewall_matches_original_published_ports(self) -> None:
        firewall = (
            ROOT / "ansible/roles/firewall/templates/docker-ingress-firewall.sh.j2"
        ).read_text(encoding="utf-8")
        normalized = firewall.replace("\\\n", "")
        rules = [
            'ipt -A "$managed_chain" -m conntrack '
            "--ctstate RELATED,ESTABLISHED -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -p tcp   '
            "-m conntrack --ctstate NEW   "
            "-m conntrack --ctstate DNAT --ctdir ORIGINAL "
            "--ctorigdstport 80 -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -p tcp   '
            "-m conntrack --ctstate NEW   "
            "-m conntrack --ctstate DNAT --ctdir ORIGINAL "
            "--ctorigdstport 443 -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -p udp   '
            "-m conntrack --ctstate NEW   "
            "-m conntrack --ctstate DNAT --ctdir ORIGINAL "
            "--ctorigdstport 443 -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -j DROP',
            'ipt -A "$managed_chain" -j RETURN',
        ]
        positions = [normalized.index(rule) for rule in rules]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("--dports", normalized)
        self.assertNotIn(" --dport ", normalized)
        self.assertNotIn("--ctstate NEW,DNAT", normalized)
        self.assertEqual(normalized.count("--ctstate NEW"), 3)
        self.assertEqual(normalized.count("--ctstate DNAT"), 3)
        self.assertEqual(normalized.count("--ctdir ORIGINAL"), 3)
        self.assertEqual(
            normalized.count('ipt -I DOCKER-USER 1 -j "$managed_chain"'),
            1,
        )
        self.assertEqual(
            normalized.count(
                'ipt -I DOCKER-USER 2 -i "$public_interface" -j DROP'
            ),
            1,
        )

    def test_docker_apt_refresh_runs_only_after_key_repository_or_pin_change(
        self,
    ) -> None:
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/docker/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        handlers = yaml.safe_load(
            (ROOT / "ansible/roles/docker/handlers/main.yml").read_text(
                encoding="utf-8"
            )
        )
        key_index, key = next(
            (index, task)
            for index, task in enumerate(tasks)
            if task["name"]
            == "Download the Docker signing key with a pinned checksum"
        )
        repository_index, repository = next(
            (index, task)
            for index, task in enumerate(tasks)
            if task["name"] == "Configure the official Docker APT repository"
        )
        pin_index, pin = next(
            (index, task)
            for index, task in enumerate(tasks)
            if task["name"] == "Pin the selected Docker package versions"
        )
        refresh = next(
            handler
            for handler in handlers
            if handler["name"] == "Refresh Docker APT metadata"
        )
        flush_index = next(
            index
            for index, task in enumerate(tasks)
            if task["name"]
            == "Apply Docker repository handlers before package installation"
        )
        install_index = next(
            index
            for index, task in enumerate(tasks)
            if task["name"] == "Install exact Docker Engine and Compose versions"
        )

        self.assertEqual(key["notify"], "Refresh Docker APT metadata")
        self.assertEqual(repository["notify"], "Refresh Docker APT metadata")
        self.assertEqual(pin["notify"], "Refresh Docker APT metadata")
        self.assertIs(refresh["ansible.builtin.apt"]["update_cache"], True)
        self.assertFalse(
            any(
                task.get("ansible.builtin.apt", {}).get("update_cache") is True
                for task in tasks
            )
        )
        self.assertLess(key_index, repository_index)
        self.assertLess(repository_index, pin_index)
        self.assertLess(pin_index, flush_index)
        self.assertLess(flush_index, install_index)

    def test_normal_convergence_never_retires_an_administrator_key(self) -> None:
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/ssh/tasks/main.yml").read_text(encoding="utf-8")
        )
        key_mutations = [
            task for task in tasks if "ansible.builtin.lineinfile" in task
        ]
        self.assertEqual(len(key_mutations), 1)
        key_mutation = key_mutations[0]["ansible.builtin.lineinfile"]
        self.assertIn("/.ssh/authorized_keys", key_mutation["path"])
        self.assertNotIn("state", key_mutation)

        drift_check = next(
            task
            for task in tasks
            if task["name"] == "Refuse undeclared administrator key drift"
        )
        drift_contract = json.dumps(drift_check, sort_keys=True)
        self.assertIn("difference", drift_contract)
        self.assertIn("separate explicit operation", drift_contract)

    def test_root_test_mode_stops_before_controller_paths_are_selected(self) -> None:
        controller = (SCRIPTS / "deploy").read_text(encoding="utf-8")
        guard = controller.index("root cannot use controller test mode")
        production_path = controller.index("readonly state_dir=/var/lib/vps-controller")
        first_mutation = controller.index("install -d -m 0700")
        self.assertLess(guard, production_path)
        self.assertLess(guard, first_mutation)
        self.assertIn("GIT_CONFIG_PARAMETERS", controller)

    def test_convergence_uses_an_isolated_origin_main_archive(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        converge = (SCRIPTS / "converge").read_text(encoding="utf-8")
        self.assertIn("./scripts/converge", makefile)
        self.assertIn("./scripts/converge --check --diff", makefile)
        self.assertIn("+refs/heads/main:refs/remotes/origin/main", converge)
        self.assertIn("archive --format=tar \"$revision\"", converge)
        self.assertIn("cd \"$checkout\"", converge)
        self.assertIn('--extra-vars "vps_infra_revision=$revision"', converge)
        self.assertIn('"${ansible_playbook_options[@]}"', converge)

    def test_tasks_forced_outside_check_mode_are_read_only(self) -> None:
        expected = {
            (
                "base",
                "Detect whether the swap file has a swap signature",
            ): [
                "/usr/sbin/blkid",
                "-o",
                "value",
                "-s",
                "TYPE",
                "{{ vps_swap_path }}",
            ],
            ("base", "Read active swap devices"): [
                "/usr/bin/awk",
                "NR>1 {print $1}",
                "/proc/swaps",
            ],
            ("deploy", "Read the infrastructure mirror origin"): [
                "/usr/bin/git",
                "-C",
                "{{ vps_deploy_repository_dir }}",
                "remote",
                "get-url",
                "origin",
            ],
            ("deploy", "Read deployment account groups"): [
                "/usr/bin/id",
                "-nG",
                "{{ vps_deploy_user }}",
            ],
            ("docker", "Read the Docker key fingerprints"): [
                "/usr/bin/gpg",
                "--batch",
                "--show-keys",
                "--with-colons",
                "/etc/apt/keyrings/docker.asc",
            ],
            ("docker", "Read installed Docker package versions"): [
                "/usr/bin/dpkg-query",
                "-W",
                "-f=${Version}",
                "{{ item.name }}",
            ],
            ("firewall", "Read effective UFW policy"): [
                "/usr/sbin/ufw",
                "status",
                "verbose",
            ],
            ("firewall", "Read every numbered UFW rule"): [
                "/usr/sbin/ufw",
                "status",
                "numbered",
            ],
            ("layout", "Inspect managed external Docker networks"): [
                "/usr/bin/docker",
                "network",
                "inspect",
                "{{ item.name }}",
            ],
            ("ssh", "Read effective deploy SSH policy"): [
                "/usr/sbin/sshd",
                "-T",
                "-C",
                "user={{ vps_deploy_user }},host=localhost,addr=127.0.0.1",
            ],
            ("ssh", "Read effective administrator SSH policy"): [
                "/usr/sbin/sshd",
                "-T",
                "-C",
                "user={{ vps_admin_user }},host=localhost,addr=127.0.0.1",
            ],
        }
        observed: dict[tuple[str, str], list[str]] = {}
        for task_file in sorted((ROOT / "ansible/roles").glob("*/tasks/main.yml")):
            role = task_file.parent.parent.name
            for task in yaml.safe_load(task_file.read_text(encoding="utf-8")):
                if task.get("check_mode") is not False:
                    continue
                module_keys = {
                    key for key in task if key.startswith("ansible.builtin.")
                }
                self.assertEqual(module_keys, {"ansible.builtin.command"})
                self.assertIs(task.get("changed_when"), False)
                observed[(role, task["name"])] = task["ansible.builtin.command"][
                    "argv"
                ]

        self.assertEqual(observed, expected)

    def test_convergence_keeps_the_ssh_control_path_short(self) -> None:
        converge = (SCRIPTS / "converge").read_text(encoding="utf-8")
        self.assertIn("umask 077", converge)
        self.assertIn(
            'temporary_root=$("$mktemp_executable" -d '
            "/tmp/vps-c.XXXXXXXX)",
            converge,
        )
        representative_control_path = (
            Path("/tmp").resolve()
            / "vps-c.12345678/cp"
            / (
                # OpenSSH expands %C to a 40-character SHA-1 and briefly
                # appends a dot plus 16 random characters before rename.
                "0123456789abcdef0123456789abcdef01234567"
                ".ABCDEFGHIJKLMNOP"
            )
        )
        self.assertLess(len(os.fsencode(representative_control_path)), 104)
        self.assertIn(
            '"ANSIBLE_SSH_CONTROL_PATH_DIR=$control_path_dir"',
            converge,
        )
        self.assertEqual(converge.count("ANSIBLE_SSH_CONTROL_PATH_DIR"), 1)
        self.assertIn(
            'mkdir -m 0700 -- "$checkout" "$isolated_home" "$control_path_dir"',
            converge,
        )

    def test_convergence_explicitly_trusts_the_captured_mise_config(self) -> None:
        converge = (SCRIPTS / "converge").read_text(encoding="utf-8")
        trust = 'clean_command "$mise_executable" trust "$checkout/mise.toml"'
        install = 'clean_command "$mise_executable" install --locked'
        self.assertIn(trust, converge)
        self.assertLess(converge.index(trust), converge.index(install))

    def test_convergence_executes_the_captured_remote_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "work", root], check=True)

            (root / "ansible/collections").mkdir(parents=True)
            (root / "ansible/playbooks").mkdir(parents=True)
            (root / "ansible/ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
            (root / "ansible/collections/requirements.yml").write_text(
                "collections: []\n",
                encoding="utf-8",
            )
            (root / "ansible/playbooks/site.yml").write_text(
                "---\n- hosts: all\n  gather_facts: false\n",
                encoding="utf-8",
            )
            for name in ("mise.toml", "mise.lock", "pyproject.toml", "uv.lock"):
                (root / name).write_text(f"remote {name}\n", encoding="utf-8")
            (root / "marker.txt").write_text("remote-main\n", encoding="utf-8")
            subprocess.run(["git", "-C", root, "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "remote main",
                ],
                check=True,
            )
            remote_sha = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"],
                text=True,
            ).strip()

            (root / "marker.txt").write_text("divergent-worktree\n", encoding="utf-8")
            subprocess.run(["git", "-C", root, "add", "marker.txt"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "divergent worktree",
                ],
                check=True,
            )
            divergent_sha = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            (root / "ansible/roles/rogue/tasks").mkdir(parents=True)
            (root / "ansible/roles/rogue/tasks/main.yml").write_text(
                "---\n- debug: msg=rogue\n",
                encoding="utf-8",
            )

            expected_origin = "https://github.com/nclsppr/vps-infra.git"
            subprocess.run(
                ["git", "-C", root, "remote", "add", "origin", expected_origin],
                check=True,
            )
            self.assertNotEqual(remote_sha, divergent_sha)
            self.assertFalse(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        root,
                        "show-ref",
                        "--verify",
                        "--quiet",
                        "refs/remotes/origin/main",
                    ],
                    check=False,
                ).returncode
                == 0
            )

            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            converge = scripts_dir / "converge"
            shutil.copy2(SCRIPTS / "converge", converge)
            converge.chmod(0o755)

            support = Path(temporary) / "support"
            fake_bin = support / "bin"
            fake_bin.mkdir(parents=True)
            log = support / "execution.log"
            galaxy_template = support / "ansible-galaxy"
            playbook_template = support / "ansible-playbook"

            def write_executable(path: Path, content: str) -> None:
                path.write_text(content, encoding="utf-8")
                path.chmod(0o755)

            quoted_root = shlex.quote(str(root))
            quoted_log = shlex.quote(str(log))
            quoted_remote_sha = shlex.quote(remote_sha)
            quoted_divergent_sha = shlex.quote(divergent_sha)
            real_git = shlex.quote(shutil.which("git") or "/usr/bin/git")
            fetch_signature = (
                " --force --no-tags --no-recurse-submodules origin "
                "+refs/heads/main:refs/remotes/origin/main "
            )
            write_executable(
                fake_bin / "git",
                f"""#!/bin/sh
set -eu
[ -z "${{SSH_AUTH_SOCK+x}}" ]
[ -z "${{ANSIBLE_SSH_CONTROL_PATH_DIR+x}}" ]
case " $* " in
  *" fetch "*)
    printf '%s\\n' "$*" >>{quoted_log}
    case " $* " in
      *"{fetch_signature}"*) ;;
      *) exit 91 ;;
    esac
    [ ! -e {quoted_root}/fail-fetch ] || exit 42
    {real_git} -C {quoted_root} update-ref refs/remotes/origin/main {quoted_remote_sha}
    exit 0
    ;;
  *" rev-parse "*"refs/remotes/origin/main"*)
    revision=$({real_git} "$@")
    {real_git} -C {quoted_root} update-ref refs/remotes/origin/main {quoted_divergent_sha}
    printf '%s\\n' "$revision"
    exit 0
    ;;
esac
exec {real_git} "$@"
""",
            )
            write_executable(
                galaxy_template,
                f"""#!/bin/sh
set -eu
[ -z "${{SSH_AUTH_SOCK+x}}" ]
[ -z "${{ANSIBLE_SSH_CONTROL_PATH_DIR+x}}" ]
printf 'galaxy=%s\\n' "$PWD $*" >>{quoted_log}
""",
            )
            write_executable(
                playbook_template,
                f"""#!/bin/sh
set -eu
[ -z "${{ANSIBLE_LIBRARY+x}}" ]
[ -n "${{SSH_AUTH_SOCK+x}}" ]
[ -d "$ANSIBLE_SSH_CONTROL_PATH_DIR" ]
IFS= read -r marker < ../marker.txt
printf 'playbook_directory=%s\\n' "$PWD" >>{quoted_log}
printf 'marker=%s\\n' "$marker" >>{quoted_log}
printf 'control_path_dir=%s\\n' "$ANSIBLE_SSH_CONTROL_PATH_DIR" >>{quoted_log}
printf 'ssh_auth_sock=%s\\n' "$SSH_AUTH_SOCK" >>{quoted_log}
printf 'arguments=%s\\n' "$*" >>{quoted_log}
""",
            )
            write_executable(
                fake_bin / "mise",
                f"""#!/bin/sh
set -eu
[ -z "${{SSH_AUTH_SOCK+x}}" ]
[ -z "${{ANSIBLE_SSH_CONTROL_PATH_DIR+x}}" ]
printf 'mise=%s\\n' "$PWD $*" >>{quoted_log}
if [ "${{1:-}}" = exec ]; then
  mkdir -p .venv/bin
  cp {shlex.quote(str(galaxy_template))} .venv/bin/ansible-galaxy
  cp {shlex.quote(str(playbook_template))} .venv/bin/ansible-playbook
fi
""",
            )

            inventory = support / "hosts.yml"
            extra_vars = support / "keys.yml"
            inventory.write_text("all: {}\n", encoding="utf-8")
            extra_vars.write_text("vps_admin_authorized_keys: []\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "ANSIBLE_INVENTORY": str(inventory),
                    "ANSIBLE_EXTRA_VARS": str(extra_vars),
                    "ANSIBLE_LIBRARY": "/untrusted/ansible/library",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "protocol.file.allow",
                    "GIT_CONFIG_VALUE_0": "always",
                    "PYTHONPATH": "/untrusted/python",
                }
            )
            invalid_socket = support / "not-a-socket"
            invalid_socket.write_text("not a socket\n", encoding="utf-8")
            invalid_environment = environment.copy()
            invalid_environment["SSH_AUTH_SOCK"] = str(invalid_socket)
            invalid_agent = subprocess.run(
                [converge],
                cwd=root,
                env=invalid_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(invalid_agent.returncode, 78)
            self.assertIn("caller-owned Unix socket", invalid_agent.stderr)
            self.assertFalse(log.exists())

            agent_socket_path = support / "agent.sock"
            agent_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(agent_socket.close)
            agent_socket.bind(str(agent_socket_path))
            environment["SSH_AUTH_SOCK"] = str(agent_socket_path)
            result = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            execution = log.read_text(encoding="utf-8")
            self.assertIn("marker=remote-main", execution)
            self.assertNotIn("divergent-worktree", execution)
            self.assertIn(
                f"ssh_auth_sock={agent_socket_path.resolve()}",
                execution,
            )
            self.assertRegex(
                execution,
                r"(?m)^control_path_dir=/tmp/vps-c\.[A-Za-z0-9]+/cp$",
            )
            mise_calls = [
                line for line in execution.splitlines() if line.startswith("mise=")
            ]
            self.assertEqual(len(mise_calls), 3)
            self.assertIn(" trust ", mise_calls[0])
            self.assertTrue(mise_calls[0].endswith("/mise.toml"))
            self.assertIn(" install --locked", mise_calls[1])
            self.assertIn(" exec -- uv sync --locked", mise_calls[2])
            self.assertIn(f"vps_infra_revision={remote_sha}", execution)
            self.assertIn(
                "+refs/heads/main:refs/remotes/origin/main",
                execution,
            )

            log.unlink()
            check_result = subprocess.run(
                [converge, "--check", "--diff"],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(check_result.returncode, 0, check_result.stderr)
            check_execution = log.read_text(encoding="utf-8")
            self.assertRegex(
                check_execution,
                rf"(?m)^arguments=--check --diff .*vps_infra_revision={remote_sha} .*playbooks/site\.yml$",
            )

            log.unlink()
            for unsupported_arguments in (
                ["--check"],
                ["--diff", "--check"],
                ["--check", "--diff", "--limit", "atlas"],
            ):
                refused = subprocess.run(
                    [converge, *unsupported_arguments],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(refused.returncode, 64)
                self.assertIn(
                    "arguments must be empty or exactly: --check --diff",
                    refused.stderr,
                )
                self.assertFalse(log.exists())

            result = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log.unlink()
            (root / "fail-fetch").touch()
            failed_fetch = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(failed_fetch.returncode, 0)
            self.assertIn("bounded fetch of origin/main failed", failed_fetch.stderr)
            self.assertNotIn("mise=", log.read_text(encoding="utf-8"))


class ApplicationStateContractTests(unittest.TestCase):
    def test_repository_state_matches_locked_manifest(self) -> None:
        APPLICATION_STATE.validate_repository(ROOT)

    def test_locked_platform_rejects_an_enabled_application(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "releases", root / "releases")
            shutil.copytree(ROOT / "platform/caddy", root / "platform/caddy")
            shutil.copytree(
                ROOT / "platform/observability/prometheus",
                root / "platform/observability/prometheus",
            )
            manifest_path = root / "releases/production.yaml"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["platform"]["enabled"] = True
            manifest["applications"]["personal"]["enabled"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                APPLICATION_STATE.ApplicationStateError,
                "cannot be enabled by the locked platform baseline",
            ):
                APPLICATION_STATE.validate_repository(root)

    def test_disabled_application_rejects_an_active_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "releases", root / "releases")
            shutil.copytree(ROOT / "platform/caddy", root / "platform/caddy")
            shutil.copytree(
                ROOT / "platform/observability/prometheus",
                root / "platform/observability/prometheus",
            )
            disabled = root / "platform/caddy/routes/personal.caddy.disabled"
            disabled.rename(disabled.with_suffix(""))
            with self.assertRaisesRegex(
                APPLICATION_STATE.ApplicationStateError,
                "missing personal.caddy.disabled.*unexpected personal.caddy",
            ):
                APPLICATION_STATE.validate_repository(root)

    def test_unknown_route_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "releases", root / "releases")
            shutil.copytree(ROOT / "platform/caddy", root / "platform/caddy")
            shutil.copytree(
                ROOT / "platform/observability/prometheus",
                root / "platform/observability/prometheus",
            )
            (root / "platform/caddy/routes/unreviewed.caddy").write_text(
                ":443 { respond 200 }\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                APPLICATION_STATE.ApplicationStateError,
                "unexpected unreviewed.caddy",
            ):
                APPLICATION_STATE.validate_repository(root)

    def test_inactive_prometheus_target_has_valid_file_sd_structure(self) -> None:
        target_path = (
            ROOT
            / "platform/observability/prometheus/targets/surplasse.yml.disabled"
        )
        target_groups = yaml.safe_load(target_path.read_text(encoding="utf-8"))
        self.assertIsInstance(target_groups, list)
        self.assertGreater(len(target_groups), 0)
        for group in target_groups:
            self.assertIsInstance(group, dict)
            self.assertEqual(set(group), {"targets", "labels"})
            self.assertIsInstance(group["targets"], list)
            self.assertGreater(len(group["targets"]), 0)
            for target in group["targets"]:
                self.assertRegex(target, r"^[a-z0-9][a-z0-9.-]*:[1-9][0-9]{0,4}$")
            self.assertIsInstance(group["labels"], dict)
            self.assertTrue(
                all(
                    isinstance(name, str) and isinstance(value, str)
                    for name, value in group["labels"].items()
                )
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
