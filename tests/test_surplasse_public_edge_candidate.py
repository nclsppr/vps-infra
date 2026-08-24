#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import contextlib
import io
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_validator():
    path = ROOT / "scripts/validate-surplasse-public-edge-candidate"
    loader = SourceFileLoader("surplasse_public_edge_candidate", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


VALIDATOR = load_validator()


class SurplassePublicEdgeCandidateTests(unittest.TestCase):
    def test_candidate_files_keep_the_static_profile_unchanged(self) -> None:
        base = yaml.safe_load(
            (ROOT / "platform/public-static-edge/compose.yaml").read_text(
                encoding="utf-8"
            )
        )
        caddy = base["services"]["caddy"]
        self.assertEqual(base["name"], "vps-public-static-edge")
        self.assertEqual(
            set(base["networks"]),
            {"app_monflorian", "app_parkventory", "edge"},
        )
        self.assertNotIn("environment", caddy)
        self.assertNotIn("secrets", caddy)
        self.assertEqual(
            set(caddy["networks"]),
            {"app_monflorian", "app_parkventory", "edge"},
        )
        self.assertEqual(
            caddy["networks"]["app_monflorian"],
            {"ipv4_address": "172.30.40.254"},
        )
        self.assertEqual(
            caddy["networks"]["app_parkventory"],
            {"ipv4_address": "172.30.20.254"},
        )

    def test_override_is_exact_and_uses_the_separate_dns_directory(self) -> None:
        override = yaml.safe_load(
            (
                ROOT
                / "applications/surplasse/integration/public-edge.override.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(set(override["services"]), {"caddy"})
        caddy = override["services"]["caddy"]
        self.assertEqual(
            caddy["networks"],
            {
                "edge": {},
                "app_surplasse": {"ipv4_address": "172.30.10.254"},
            },
        )
        self.assertEqual(
            caddy["volumes"],
            [
                {
                    "type": "bind",
                    "source": (
                        "/srv/vps/runtime/public-static-edge/surplasse-tls.caddy"
                    ),
                    "target": "/etc/caddy/surplasse-tls.caddy",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            ],
        )
        for secret_name, file_name in VALIDATOR.DNS_CREDENTIAL_SOURCES.items():
            self.assertEqual(
                override["secrets"][secret_name]["file"],
                f"/etc/vps/secrets/dns/surplasse/{file_name}",
            )

    def test_route_delegates_dns_01_to_the_exact_atlas_snippet(self) -> None:
        route = ROOT / "platform/caddy/routes/surplasse.caddy.disabled"
        VALIDATOR.validate_route_bytes(route, None)
        route_text = route.read_text(encoding="utf-8")
        self.assertEqual(
            route_text.count("\timport /etc/caddy/surplasse-tls.caddy\n"), 1
        )
        self.assertNotIn("dns ovh", route_text)
        self.assertNotIn("OVH_", route_text)
        VALIDATOR.validate_tls_snippet()

    def test_route_byte_mismatch_and_embedded_provider_are_rejected(self) -> None:
        valid_route = (
            ROOT / "platform/caddy/routes/surplasse.caddy.disabled"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            attested = root / "attested.caddy"
            installed = root / "installed.caddy"
            attested.write_bytes(valid_route)
            installed.write_bytes(valid_route + b"\n")
            with self.assertRaisesRegex(VALIDATOR.CandidateError, "differs"):
                VALIDATOR.validate_route_bytes(attested, installed)

            embedded = root / "embedded.caddy"
            embedded.write_bytes(
                valid_route.replace(
                    b"\timport /etc/caddy/surplasse-tls.caddy\n",
                    b"\ttls { dns ovh }\n",
                )
            )
            with self.assertRaisesRegex(VALIDATOR.CandidateError, "import"):
                VALIDATOR.validate_route_content(embedded.read_bytes())

            extra_identity = root / "extra-identity.caddy"
            extra_identity.write_bytes(
                valid_route + b"\nhttps://unreviewed.example { respond 200 }\n"
            )
            with self.assertRaisesRegex(VALIDATOR.CandidateError, "approved"):
                VALIDATOR.validate_route_bytes(extra_identity, None)

    def test_live_cli_requires_the_exact_runtime_paths(self) -> None:
        base_arguments = [
            "validate-surplasse-public-edge-candidate",
            "candidate.json",
            "--approved-route",
            "approved.caddy",
            "--attested-route",
            "attested.caddy",
            "--installed-route",
            VALIDATOR.RUNTIME_ROUTE_PATH,
            "--installed-tls-snippet",
            VALIDATOR.TLS_SNIPPET_SOURCE,
        ]
        invalid_arguments = (
            base_arguments[:-4]
            + [
                "--installed-route",
                "/tmp/copied-route.caddy",
                "--installed-tls-snippet",
                VALIDATOR.TLS_SNIPPET_SOURCE,
            ],
            base_arguments[:-2]
            + ["--installed-tls-snippet", "/tmp/copied-tls.caddy"],
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(
                io.StringIO()
            ), mock.patch.object(VALIDATOR.sys, "argv", arguments):
                with self.assertRaises(SystemExit) as raised:
                    VALIDATOR.main()
                self.assertEqual(raised.exception.code, 2)

    def test_live_tls_snippet_must_equal_the_approved_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            installed = Path(temporary_directory) / "surplasse-tls.caddy"
            installed.write_bytes(VALIDATOR.EXPECTED_TLS_SNIPPET + b"\n")
            with self.assertRaisesRegex(VALIDATOR.CandidateError, "installed Atlas"):
                VALIDATOR.validate_tls_snippet(installed)

    def test_candidate_tools_are_executable(self) -> None:
        for name in (
            "validate-surplasse-public-edge-candidate",
            "verify-surplasse-public-edge-caddy",
        ):
            self.assertTrue(os.access(ROOT / "scripts" / name, os.X_OK), name)


if __name__ == "__main__":
    unittest.main()
