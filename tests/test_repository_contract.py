#!/usr/bin/env python3

from __future__ import annotations

import base64
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


def test_public_key(algorithm: str = "ssh-ed25519") -> str:
    encoded_algorithm = algorithm.encode("ascii")
    blob = (
        len(encoded_algorithm).to_bytes(4, byteorder="big")
        + encoded_algorithm
        + (32).to_bytes(4, byteorder="big")
        + bytes(range(32))
    )
    return f"{algorithm} {base64.b64encode(blob).decode('ascii')} contract-test"


VALID_PUBLIC_KEY = test_public_key()


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


class OperatorInputValidationTests(unittest.TestCase):
    def valid_inventory(self, user: str = "vpsadmin") -> dict[str, object]:
        return {
            "all": {
                "children": {
                    "vps": {
                        "hosts": {
                            "atlas": {
                                "ansible_host": "203.0.113.10",
                                "ansible_port": 22,
                                "ansible_user": user,
                            }
                        }
                    }
                }
            }
        }

    def valid_extra_vars(self) -> dict[str, object]:
        return {
            "vps_admin_authorized_keys": [VALID_PUBLIC_KEY],
            "vps_deploy_authorized_keys": [],
        }

    def run_validator(
        self,
        *,
        mode: str = "converge",
        inventory: object | str | None = None,
        extra_vars: object | str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "inventory.yml"
            extra_vars_path = root / "extra-vars.yml"
            inventory_value = self.valid_inventory() if inventory is None else inventory
            extra_vars_value = self.valid_extra_vars() if extra_vars is None else extra_vars
            inventory_path.write_text(
                inventory_value
                if isinstance(inventory_value, str)
                else yaml.safe_dump(inventory_value, sort_keys=False),
                encoding="utf-8",
            )
            extra_vars_path.write_text(
                extra_vars_value
                if isinstance(extra_vars_value, str)
                else yaml.safe_dump(extra_vars_value, sort_keys=False),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            return subprocess.run(
                [
                    sys.executable,
                    SCRIPTS / "validate-ansible-inputs",
                    "--mode",
                    mode,
                    "--inventory",
                    inventory_path,
                    "--extra-vars",
                    extra_vars_path,
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def assert_refused(self, **arguments: object) -> str:
        result = self.run_validator(**arguments)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn("Validation des entrées Ansible refusée :", result.stderr)
        return result.stderr

    def test_accepts_only_one_explicit_remote_ssh_target(self) -> None:
        bootstrap = self.run_validator(
            mode="bootstrap",
            inventory=self.valid_inventory(user="root"),
        )
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)

        converge_inventory = self.valid_inventory()
        host_variables = converge_inventory["all"]["children"]["vps"]["hosts"][
            "atlas"
        ]
        host_variables["ansible_connection"] = "ssh"
        converge = self.run_validator(inventory=converge_inventory)
        self.assertEqual(converge.returncode, 0, converge.stderr)

    def test_rejects_inventory_scope_and_connection_bypasses(self) -> None:
        additional_host = self.valid_inventory()
        additional_host["all"]["children"]["vps"]["hosts"]["spare"] = {
            "ansible_host": "203.0.113.11",
            "ansible_port": 22,
            "ansible_user": "vpsadmin",
        }
        self.assertIn(
            "exactement un hôte",
            self.assert_refused(inventory=additional_host),
        )

        additional_group = self.valid_inventory()
        additional_group["all"]["children"]["unbounded"] = {"hosts": {}}
        self.assertIn(
            "clés inattendues : unbounded",
            self.assert_refused(inventory=additional_group),
        )

        for target in (
            "localhost",
            "node.localhost",
            "loopback",
            "127.0.0.1",
            "127.25.4.8",
            "127.1",
            "0x7f000001",
            "::1",
            "::ffff:127.0.0.1",
            "vps.example.invalid",
        ):
            with self.subTest(target=target):
                inventory = self.valid_inventory()
                inventory["all"]["children"]["vps"]["hosts"]["atlas"][
                    "ansible_host"
                ] = target
                self.assertIn(
                    "cible locale, de bouclage ou invalide interdite",
                    self.assert_refused(inventory=inventory),
                )

        for connection in ("local", "docker", "community.docker.docker"):
            with self.subTest(connection=connection):
                inventory = self.valid_inventory()
                inventory["all"]["children"]["vps"]["hosts"]["atlas"][
                    "ansible_connection"
                ] = connection
                self.assertIn(
                    "ansible_connection doit être ssh ou smart",
                    self.assert_refused(inventory=inventory),
                )

    def test_rejects_inventory_variables_that_override_execution_policy(self) -> None:
        for variable_name, value in (
            ("ansible_python_interpreter", "/tmp/operator-python"),
            ("ansible_ssh_common_args", "-o ProxyCommand=operator-command"),
            ("ansible_ssh_executable", "/tmp/operator-ssh"),
            ("vps_admin_user", "operator"),
            ("vps_ssh_port", 2022),
        ):
            with self.subTest(variable_name=variable_name):
                inventory = self.valid_inventory()
                inventory["all"]["children"]["vps"]["hosts"]["atlas"][
                    variable_name
                ] = value
                self.assertIn(
                    f"clés inattendues : {variable_name}",
                    self.assert_refused(inventory=inventory),
                )

    def test_rejects_extra_vars_that_override_versions_paths_or_policy(self) -> None:
        for variable_name, value in (
            ("vps_infra_revision", "0" * 40),
            ("vps_supported_ubuntu_releases", ["99.99"]),
            ("vps_docker_packages", []),
            ("vps_deploy_repository_dir", "/tmp/operator-repository"),
            ("ansible_connection", "local"),
        ):
            with self.subTest(variable_name=variable_name):
                extra_vars = self.valid_extra_vars()
                extra_vars[variable_name] = value
                self.assertIn(
                    f"clés inattendues : {variable_name}",
                    self.assert_refused(extra_vars=extra_vars),
                )

        self.assertIn(
            "doit contenir un objet YAML",
            self.assert_refused(extra_vars=[VALID_PUBLIC_KEY]),
        )
        self.assertIn(
            "clé de mapping dupliquée",
            self.assert_refused(
                extra_vars=(
                    "vps_admin_authorized_keys:\n"
                    f"  - {VALID_PUBLIC_KEY}\n"
                    "vps_admin_authorized_keys: []\n"
                    "vps_deploy_authorized_keys: []\n"
                )
            ),
        )
        self.assertIn(
            "clés de fusion YAML ne sont pas acceptées",
            self.assert_refused(
                extra_vars=(
                    "defaults: &operator\n"
                    "  vps_admin_authorized_keys: []\n"
                    "<<: *operator\n"
                    "vps_deploy_authorized_keys: []\n"
                )
            ),
        )

    def test_rejects_non_key_values_and_wrong_connection_phase(self) -> None:
        extra_vars = self.valid_extra_vars()
        extra_vars["vps_admin_authorized_keys"] = [
            "{{ lookup('pipe', 'operator-command') }}"
        ]
        self.assertIn(
            "syntaxe de clé publique OpenSSH invalide",
            self.assert_refused(extra_vars=extra_vars),
        )

        algorithm = b"ssh-ed25519"
        truncated_blob = len(algorithm).to_bytes(4, byteorder="big") + algorithm
        extra_vars["vps_admin_authorized_keys"] = [
            "ssh-ed25519 " + base64.b64encode(truncated_blob).decode("ascii")
        ]
        self.assertIn(
            "données de clé SSH tronquées",
            self.assert_refused(extra_vars=extra_vars),
        )

        rsa_algorithm = b"ssh-rsa"
        rsa_exponent = b"\x01\x00\x01"
        rsa_modulus = b"\x00\x80" + bytes(127)
        rsa_blob = b"".join(
            len(field).to_bytes(4, byteorder="big") + field
            for field in (rsa_algorithm, rsa_exponent, rsa_modulus)
        )
        extra_vars["vps_admin_authorized_keys"] = [
            "ssh-rsa " + base64.b64encode(rsa_blob).decode("ascii")
        ]
        self.assertIn(
            "module RSA inférieur à 2048 bits",
            self.assert_refused(extra_vars=extra_vars),
        )

        self.assertIn(
            "convergence exige ansible_user vpsadmin",
            self.assert_refused(inventory=self.valid_inventory(user="root")),
        )
        self.assertIn(
            "compte deploy ne peut pas exécuter l'amorçage",
            self.assert_refused(
                mode="bootstrap",
                inventory=self.valid_inventory(user="deploy"),
            ),
        )

    def test_rejects_one_key_reused_for_admin_and_deploy(self) -> None:
        extra_vars = self.valid_extra_vars()
        key_without_comment = VALID_PUBLIC_KEY.rsplit(" ", maxsplit=1)[0]
        extra_vars["vps_admin_authorized_keys"] = [
            f"{key_without_comment} administrator"
        ]
        extra_vars["vps_deploy_authorized_keys"] = [
            f"{key_without_comment} github-deploy"
        ]
        self.assertIn(
            "cryptographiquement disjointes",
            self.assert_refused(extra_vars=extra_vars),
        )


class SupplyChainContractTests(unittest.TestCase):
    def test_bootstrap_refuses_an_unsupported_os_before_mutation(self) -> None:
        playbook = yaml.safe_load(
            (ROOT / "ansible/playbooks/bootstrap.yml").read_text(encoding="utf-8")
        )[0]
        pre_tasks = playbook["pre_tasks"]
        names = [task["name"] for task in pre_tasks]
        probe_index = names.index("Read the operating system release before any mutation")
        refusal_index = names.index(
            "Refuse an unsupported operating system before any mutation"
        )
        python_index = names.index("Ensure Python is available on a minimal Ubuntu image")
        self.assertLess(probe_index, refusal_index)
        self.assertLess(refusal_index, python_index)

        probe = pre_tasks[probe_index]
        self.assertEqual(probe["ansible.builtin.raw"], "cat /etc/os-release")
        self.assertFalse(probe["changed_when"])
        refusal = json.dumps(pre_tasks[refusal_index], sort_keys=True)
        self.assertIn("bootstrap_os_release.stdout", refusal)
        self.assertIn("vps_supported_ubuntu_releases", refusal)

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
    def test_repository_check_anchors_platform_candidate_to_full_git_history(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        )
        checkout = next(
            step
            for step in workflow["jobs"]["check"]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(checkout["with"]["fetch-depth"], 0)

        repository_check = (SCRIPTS / "check").read_text(encoding="utf-8")
        self.assertIn("--print-platform-integration-revision", repository_check)
        self.assertIn("merge-base --is-ancestor", repository_check)
        self.assertIn('"$platform_integration_revision" HEAD', repository_check)

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

    def test_host_automation_uses_an_isolated_origin_main_archive(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        converge = (SCRIPTS / "converge").read_text(encoding="utf-8")
        bootstrap = (SCRIPTS / "bootstrap").read_text(encoding="utf-8")
        self.assertIn("./scripts/bootstrap", makefile)
        self.assertIn("./scripts/converge", makefile)
        self.assertIn("./scripts/converge --check --diff", makefile)
        for wrapper in (bootstrap, converge):
            self.assertIn("+refs/heads/main:refs/remotes/origin/main", wrapper)
            self.assertIn("archive --format=tar \"$revision\"", wrapper)
            self.assertIn("cd \"$checkout\"", wrapper)
            self.assertIn("validate-ansible-inputs", wrapper)
            self.assertLess(
                wrapper.index(
                    'validate_operator_inputs "$validator" "$operator_inventory"'
                ),
                wrapper.index("fetch \\\n"),
            )
            self.assertIn(
                'snapshot_validator="$checkout/scripts/validate-ansible-inputs"',
                wrapper,
            )
            self.assertLess(
                wrapper.index('archive --format=tar "$revision"'),
                wrapper.index(
                    'validate_operator_inputs "$snapshot_validator" "$inventory"'
                ),
            )
        self.assertIn("playbooks/bootstrap.yml", bootstrap)
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

    def test_host_wrappers_keep_the_ssh_control_path_short(self) -> None:
        for wrapper_name, prefix in (("bootstrap", "vps-b"), ("converge", "vps-c")):
            with self.subTest(wrapper=wrapper_name):
                wrapper = (SCRIPTS / wrapper_name).read_text(encoding="utf-8")
                self.assertIn("umask 077", wrapper)
                self.assertIn(
                    f'temporary_root=$("$mktemp_executable" -d /tmp/{prefix}.XXXXXXXX)',
                    wrapper,
                )
                representative_control_path = (
                    Path("/tmp").resolve()
                    / f"{prefix}.12345678/cp"
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
                    wrapper,
                )
                self.assertEqual(
                    wrapper.count("ANSIBLE_SSH_CONTROL_PATH_DIR"),
                    1,
                )
                self.assertIn('"$control_path_dir" "$input_directory"', wrapper)

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
            (root / "scripts").mkdir(parents=True)
            remote_validator = root / "scripts/validate-ansible-inputs"
            shutil.copy2(SCRIPTS / "validate-ansible-inputs", remote_validator)
            remote_validator.chmod(0o755)
            (root / "ansible/ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
            (root / "ansible/collections/requirements.yml").write_text(
                "collections: []\n",
                encoding="utf-8",
            )
            (root / "ansible/playbooks/site.yml").write_text(
                "---\n- hosts: all\n  gather_facts: false\n",
                encoding="utf-8",
            )
            (root / "ansible/playbooks/bootstrap.yml").write_text(
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
            scripts_dir.mkdir(exist_ok=True)
            converge = scripts_dir / "converge"
            bootstrap = scripts_dir / "bootstrap"
            validator = scripts_dir / "validate-ansible-inputs"
            shutil.copy2(SCRIPTS / "converge", converge)
            shutil.copy2(SCRIPTS / "bootstrap", bootstrap)
            shutil.copy2(SCRIPTS / "validate-ansible-inputs", validator)
            for executable in (bootstrap, converge, validator):
                executable.chmod(0o755)

            locked_bin = root / ".venv/bin"
            locked_bin.mkdir(parents=True)
            locked_python = locked_bin / "python"
            validator_log = shlex.quote(
                str(Path(temporary) / "support/execution.log")
            )
            locked_python.write_text(
                "#!/bin/sh\n"
                "case \"${1:-}\" in\n"
                "  /tmp/vps-b.*/checkout/scripts/validate-ansible-inputs|"
                "/tmp/vps-c.*/checkout/scripts/validate-ansible-inputs)\n"
                f"    printf 'snapshot_validator=%s\\n' \"$1\" >>{validator_log} ;;\n"
                "esac\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n",
                encoding="utf-8",
            )
            locked_python.chmod(0o755)

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
[ -z "${{PYTHONPATH+x}}" ]
[ -n "${{SSH_AUTH_SOCK+x}}" ]
[ -d "$ANSIBLE_SSH_CONTROL_PATH_DIR" ]
case "$ANSIBLE_CONFIG" in
  /tmp/vps-b.*/checkout/ansible/ansible.cfg|/tmp/vps-c.*/checkout/ansible/ansible.cfg) ;;
  *) exit 92 ;;
esac
case ":$PATH:" in
  *":{shlex.quote(str(fake_bin))}:"*) exit 93 ;;
esac
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
            inventory.write_text(
                yaml.safe_dump(
                    {
                        "all": {
                            "children": {
                                "vps": {
                                    "hosts": {
                                        "atlas": {
                                            "ansible_host": "203.0.113.10",
                                            "ansible_port": 22,
                                            "ansible_user": "vpsadmin",
                                        }
                                    }
                                }
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            extra_vars.write_text(
                yaml.safe_dump(
                    {
                        "vps_admin_authorized_keys": [VALID_PUBLIC_KEY],
                        "vps_deploy_authorized_keys": [],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
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
            self.assertIn("socket Unix appartenant à l'appelant", invalid_agent.stderr)
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
            snapshot_validations = [
                line
                for line in execution.splitlines()
                if line.startswith("snapshot_validator=")
            ]
            self.assertEqual(len(snapshot_validations), 1)
            self.assertRegex(
                snapshot_validations[0],
                r"^snapshot_validator=/tmp/vps-c\.[A-Za-z0-9]+/checkout/"
                r"scripts/validate-ansible-inputs$",
            )
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
            extra_vars.write_text(
                yaml.safe_dump(
                    {
                        "vps_admin_authorized_keys": [VALID_PUBLIC_KEY],
                        "vps_deploy_authorized_keys": [],
                        "vps_infra_revision": "0" * 40,
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            invalid_input = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(invalid_input.returncode, 78)
            self.assertIn("clés inattendues : vps_infra_revision", invalid_input.stderr)
            self.assertFalse(log.exists(), "invalid input reached Git fetch or setup")
            extra_vars.write_text(
                yaml.safe_dump(
                    {
                        "vps_admin_authorized_keys": [VALID_PUBLIC_KEY],
                        "vps_deploy_authorized_keys": [],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            inventory.write_text(
                yaml.safe_dump(
                    {
                        "all": {
                            "children": {
                                "vps": {
                                    "hosts": {
                                        "atlas": {
                                            "ansible_host": "203.0.113.10",
                                            "ansible_port": 22,
                                            "ansible_user": "root",
                                        }
                                    }
                                }
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            bootstrap_result = subprocess.run(
                [bootstrap],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(bootstrap_result.returncode, 0, bootstrap_result.stderr)
            bootstrap_execution = log.read_text(encoding="utf-8")
            self.assertIn("marker=remote-main", bootstrap_execution)
            self.assertRegex(
                bootstrap_execution,
                r"(?m)^snapshot_validator=/tmp/vps-b\.[A-Za-z0-9]+/checkout/"
                r"scripts/validate-ansible-inputs$",
            )
            self.assertRegex(
                bootstrap_execution,
                r"(?m)^control_path_dir=/tmp/vps-b\.[A-Za-z0-9]+/cp$",
            )
            self.assertRegex(
                bootstrap_execution,
                r"(?m)^arguments=--inventory .* --extra-vars @.* playbooks/bootstrap\.yml$",
            )
            log.unlink()
            unsupported_bootstrap_argument = subprocess.run(
                [bootstrap, "--check"],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(unsupported_bootstrap_argument.returncode, 64)
            self.assertIn(
                "arguments en ligne de commande ne sont pas acceptés",
                unsupported_bootstrap_argument.stderr,
            )
            self.assertFalse(log.exists())

            inventory.write_text(
                yaml.safe_dump(
                    {
                        "all": {
                            "children": {
                                "vps": {
                                    "hosts": {
                                        "atlas": {
                                            "ansible_host": "203.0.113.10",
                                            "ansible_port": 22,
                                            "ansible_user": "vpsadmin",
                                        }
                                    }
                                }
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
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
                    "arguments doivent être absents ou exactement : --check --diff",
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
            self.assertIn(
                "récupération bornée de origin/main a échoué",
                failed_fetch.stderr,
            )
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
