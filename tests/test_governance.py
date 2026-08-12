#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_governance_module():
    path = ROOT / "scripts/check-governance"
    loader = SourceFileLoader("governance_policy", str(path))
    spec = importlib.util.spec_from_loader("governance_policy", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class GovernanceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_governance_module()

    def test_repository_actions_are_pinned(self) -> None:
        self.assertEqual(self.module.check_action_pins(), [])

    def test_foundation_provenance_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            manifest = json.loads(
                (ROOT / "documentation.json").read_text(encoding="utf-8")
            )
            manifest["foundationEvaluation"]["commit"] = "0" * 40
            path = root / "documentation.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.module.MANIFEST = path
            with self.assertRaisesRegex(
                self.module.GovernanceError,
                "correspondre exactement",
            ):
                self.module.load_manifest()

    def test_unpinned_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "unsafe.yml").write_text(
                "jobs:\n  check:\n    steps:\n      - uses: actions/checkout@v7\n",
                encoding="utf-8",
            )
            self.module.ROOT = root
            failures = self.module.check_action_pins()
            self.assertEqual(len(failures), 1)
            self.assertIn("Action non épinglée par SHA complet", failures[0])

    def test_quoted_flow_style_uses_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "unsafe.yml").write_text(
                'jobs: {check: {steps: [{"uses" : actions/checkout@v7}]}}\n',
                encoding="utf-8",
            )
            self.module.ROOT = root
            failures = self.module.check_action_pins()
            self.assertEqual(len(failures), 1)
            self.assertIn("Action non épinglée par SHA complet", failures[0])

    def test_malformed_docker_action_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "unsafe.yml").write_text(
                "jobs:\n  check:\n    steps:\n      - uses: docker://tool@sha256:abc\n",
                encoding="utf-8",
            )
            self.module.ROOT = root
            failures = self.module.check_action_pins()
            self.assertEqual(len(failures), 1)
            self.assertIn("image d'Action non épinglée par digest", failures[0])

    def test_tracked_markdown_cannot_use_a_local_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            skill = root / ".agents/skills/local/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# Local\n", encoding="utf-8")
            readme = root / "README.md"
            readme.write_text("# Dépôt\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", root], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "add",
                    "README.md",
                    ".agents/skills/local/SKILL.md",
                ],
                check=True,
            )
            self.module.ROOT = root
            manifest = {
                "collections": [
                    {
                        "id": "orientation",
                        "title": "Orientation",
                        "visibility": "public",
                        "include": ["README.md"],
                    }
                ],
                "ignored": [
                    {
                        "pattern": ".agents/**/*.md",
                        "reason": "compétence locale non versionnée",
                    }
                ],
            }
            with self.assertRaisesRegex(
                self.module.GovernanceError,
                "suivi par Git ne peut pas être ignoré",
            ):
                self.module.classify(manifest)

    def test_markdown_symlink_is_rejected_without_path_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            readme = root / "README.md"
            readme.write_text("# Dépôt\n", encoding="utf-8")
            alias = root / "alias.md"
            alias.symlink_to("README.md")
            subprocess.run(["git", "init", "--quiet", root], check=True)
            subprocess.run(
                ["git", "-C", root, "add", "README.md", "alias.md"],
                check=True,
            )
            self.module.ROOT = root
            manifest = {
                "collections": [
                    {
                        "id": "orientation",
                        "title": "Orientation",
                        "visibility": "public",
                        "include": ["*.md"],
                    }
                ],
                "ignored": [],
            }
            with self.assertRaisesRegex(
                self.module.GovernanceError,
                "lien symbolique",
            ):
                self.module.classify(manifest)

    def test_validate_workflow_calls_canonical_check(self) -> None:
        self.assertEqual(self.module.check_canonical_ci(), [])

    def test_expanded_ci_command_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "validate.yml").write_text(
                "jobs:\n  check:\n    steps:\n      - run: make check-fast check-platform\n",
                encoding="utf-8",
            )
            self.module.ROOT = root
            failures = self.module.check_canonical_ci()
            self.assertEqual(len(failures), 1)
            self.assertIn("commande canonique", failures[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
