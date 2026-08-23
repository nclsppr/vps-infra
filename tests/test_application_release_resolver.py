#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def load_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RESOLVER = load_module(
    "application_release_resolver",
    ROOT / "scripts/resolve-application-releases",
)


class FakeClient:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, *, headers, max_bytes, attempts=3):
        self.requests.append((url, headers, max_bytes, attempts))
        if not self.responses:
            raise AssertionError(f"unexpected HTTP request: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(body, headers=None):
    return RESOLVER.HttpResponse(body=body, headers=headers or {})


def check_run(name, *, status="completed", conclusion="success", revision=REVISION):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": revision,
    }


def check_response(runs):
    return response(
        json.dumps({"total_count": len(runs), "check_runs": runs}).encode()
    )


def release_bytes(policy):
    integration = f"{policy.integration_repository}@{DIGEST_B}"
    value = {
        "schema": 1,
        "contract": RESOLVER.APPLICATION_RELEASE_CONTRACT,
        "application": policy.name,
        "source": {
            "repository": policy.source_repository,
            "branch": policy.source_branch,
            "revision": REVISION,
        },
        "components": {
            name: {
                "source_revision": REVISION,
                "image": f"{repository}@{DIGEST_A}",
            }
            for name, repository in policy.component_repositories.items()
        },
        "integration": {
            "source_revision": REVISION,
            "artifact": integration,
        },
        "migrations": {
            "strategy": policy.migration_strategy,
            "runtime_auto_migrate": False,
            "inventory_artifact": integration,
            "inventory_sha256": DIGEST_C,
        },
        "probes": {
            "inventory_artifact": integration,
            "inventory_sha256": DIGEST_A,
        },
    }
    return RESOLVER.canonical_json(value)


def manifest_bytes(policy, descriptor, *, created="2026-08-17T12:00:00Z"):
    value = {
        "schemaVersion": 2,
        "mediaType": RESOLVER.OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": RESOLVER.APPLICATION_RELEASE_ARTIFACT_TYPE,
        "config": RESOLVER.OCI_EMPTY_CONFIG,
        "layers": [
            {
                "mediaType": RESOLVER.APPLICATION_RELEASE_LAYER_MEDIA_TYPE,
                "digest": RESOLVER.content_digest(descriptor),
                "size": len(descriptor),
                "annotations": {
                    RESOLVER.TITLE_ANNOTATION: RESOLVER.APPLICATION_RELEASE_TITLE
                },
            }
        ],
        "annotations": {
            RESOLVER.CREATED_ANNOTATION: created,
            RESOLVER.SOURCE_ANNOTATION: (
                f"https://github.com/{policy.source_repository}"
            ),
            RESOLVER.REVISION_ANNOTATION: REVISION,
        },
    }
    return json.dumps(value, separators=(",", ":")).encode()


def manifest_response(raw):
    return response(
        raw,
        {
            "content-type": RESOLVER.OCI_MANIFEST_MEDIA_TYPE,
            "docker-content-digest": RESOLVER.content_digest(raw),
        },
    )


class AdmissionResolverTests(unittest.TestCase):
    def test_main_can_scope_resolution_to_parkventory(self):
        contract = RESOLVER.load_production_contract(
            ROOT / "releases/application-production.json",
            ROOT / "releases/static-production.json",
        )
        with (
            mock.patch.object(
                RESOLVER,
                "load_production_contract",
                return_value=contract,
            ),
            mock.patch.object(RESOLVER, "BoundedHttpClient", return_value=object()),
            mock.patch.object(RESOLVER, "resolve_all", return_value=[]) as resolve,
        ):
            self.assertEqual(RESOLVER.main(["--application", "parkventory"]), 0)
        scoped_contract = resolve.call_args.args[0]
        self.assertEqual(
            tuple(item.name for item in scoped_contract.applications),
            ("parkventory",),
        )

    @classmethod
    def setUpClass(cls):
        cls.contract = RESOLVER.load_production_contract(
            ROOT / "releases/application-production.json"
        )
        cls.surplasse = cls.contract.applications[0]
        cls.parkventory = cls.contract.applications[1]

    def enabled(self, application):
        return RESOLVER.dataclasses.replace(application, enabled=True)

    def release_responses(self, application, second_manifest=None):
        descriptor = release_bytes(application)
        first_manifest = manifest_bytes(application, descriptor)
        return [
            response(b'{"token":"registry-token"}'),
            manifest_response(first_manifest),
            response(descriptor),
            manifest_response(second_manifest or first_manifest),
        ]

    def test_ghcr_blob_redirect_is_one_hop_and_drops_authorization(self):
        descriptor = release_bytes(self.surplasse)
        digest = RESOLVER.content_digest(descriptor)
        target = (
            "https://pkg-containers.githubusercontent.com/ghcrblobs12/blobs/"
            f"{digest}?se=2026-08-18T12%3A00%3A00Z&sig=signed-value"
        )
        client = FakeClient(
            [
                RESOLVER.HttpsRedirect(307, target),
                response(descriptor),
            ]
        )

        result = RESOLVER._registry_blob_response(
            client,
            "nclsppr/surplasse/application-release",
            digest,
            "registry-token",
            max_bytes=RESOLVER.MAX_RELEASE_BYTES,
        )

        self.assertEqual(result.body, descriptor)
        self.assertEqual(len(client.requests), 2)
        first_url, first_headers, _, first_attempts = client.requests[0]
        second_url, second_headers, _, second_attempts = client.requests[1]
        self.assertEqual(
            first_url,
            f"https://ghcr.io/v2/nclsppr/surplasse/application-release/blobs/{digest}",
        )
        self.assertEqual(first_headers["Authorization"], "Bearer registry-token")
        self.assertEqual(second_url, target)
        self.assertNotIn("Authorization", second_headers)
        self.assertEqual(second_headers, {"Accept": "application/octet-stream"})
        self.assertEqual((first_attempts, second_attempts), (3, 3))

    def test_ghcr_blob_direct_response_remains_accepted(self):
        descriptor = release_bytes(self.surplasse)
        digest = RESOLVER.content_digest(descriptor)
        client = FakeClient([response(descriptor)])

        result = RESOLVER._registry_blob_response(
            client,
            "nclsppr/surplasse/application-release",
            digest,
            "registry-token",
            max_bytes=RESOLVER.MAX_RELEASE_BYTES,
        )

        self.assertEqual(result.body, descriptor)
        self.assertEqual(len(client.requests), 1)

    def test_ghcr_blob_redirect_rejects_every_target_boundary_bypass(self):
        digest = DIGEST_A
        valid = (
            "https://pkg-containers.githubusercontent.com/ghcrblobs12/blobs/"
            f"{digest}?se=2026-08-18T12%3A00%3A00Z&sig=do-not-log-this"
        )
        cases = (
            ("status", 302, valid, "status-is-invalid"),
            ("scheme", 307, valid.replace("https://", "http://"), "target-is-invalid"),
            (
                "host",
                307,
                valid.replace(
                    "pkg-containers.githubusercontent.com",
                    "pkg-containers.githubusercontent.com.example.invalid",
                ),
                "target-is-invalid",
            ),
            (
                "port",
                307,
                valid.replace(
                    "pkg-containers.githubusercontent.com",
                    "pkg-containers.githubusercontent.com:443",
                ),
                "target-is-invalid",
            ),
            (
                "userinfo",
                307,
                valid.replace(
                    "pkg-containers.githubusercontent.com",
                    "user@pkg-containers.githubusercontent.com",
                ),
                "target-is-invalid",
            ),
            ("fragment", 307, valid + "#fragment", "target-is-invalid"),
            (
                "path-prefix",
                307,
                valid.replace("/ghcrblobs12/", "/ghcr1/"),
                "target-is-invalid",
            ),
            (
                "wrong-digest",
                307,
                valid.replace(digest, DIGEST_B),
                "target-is-invalid",
            ),
            ("missing-query", 307, valid.split("?", maxsplit=1)[0], "target-is-invalid"),
            ("non-ascii", 307, valid + "&label=caf\u00e9", "location-is-not-ascii"),
            (
                "control-character",
                307,
                valid + "\n",
                "location-characters-are-invalid",
            ),
            (
                "oversized",
                307,
                valid + "&padding=" + ("a" * RESOLVER.MAX_REDIRECT_URL_BYTES),
                "location-size-is-invalid",
            ),
            ("missing-location", 307, None, "location-is-missing"),
        )
        for name, status, location, reason in cases:
            with self.subTest(name=name):
                client = FakeClient([RESOLVER.HttpsRedirect(status, location)])
                with self.assertRaisesRegex(RESOLVER.BlockedEvidence, reason) as raised:
                    RESOLVER._registry_blob_response(
                        client,
                        "nclsppr/surplasse/application-release",
                        digest,
                        "registry-token",
                        max_bytes=RESOLVER.MAX_RELEASE_BYTES,
                    )
                self.assertNotIn("do-not-log-this", str(raised.exception))
                self.assertEqual(len(client.requests), 1)

    def test_ghcr_blob_redirect_chain_is_rejected_without_leaking_target(self):
        digest = DIGEST_A
        first = (
            "https://pkg-containers.githubusercontent.com/ghcrblobs12/blobs/"
            f"{digest}?sig=first-secret"
        )
        second = (
            "https://pkg-containers.githubusercontent.com/ghcrblobs12/blobs/"
            f"{digest}?sig=second-secret"
        )
        client = FakeClient(
            [
                RESOLVER.HttpsRedirect(307, first),
                RESOLVER.HttpsRedirect(307, second),
            ]
        )

        with self.assertRaisesRegex(
            RESOLVER.BlockedEvidence,
            "redirect-chain-is-not-permitted",
        ) as raised:
            RESOLVER._registry_blob_response(
                client,
                "nclsppr/surplasse/application-release",
                digest,
                "registry-token",
                max_bytes=RESOLVER.MAX_RELEASE_BYTES,
            )
        self.assertNotIn("first-secret", str(raised.exception))
        self.assertNotIn("second-secret", str(raised.exception))
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("Authorization", client.requests[1][1])

    def test_redirect_handler_captures_target_without_logging_it(self):
        target = (
            "https://pkg-containers.githubusercontent.com/ghcrblobs12/blobs/"
            f"{DIGEST_A}?sig=handler-secret"
        )
        with self.assertRaises(RESOLVER.HttpsRedirect) as raised:
            RESOLVER.RejectRedirects().redirect_request(
                object(),
                object(),
                307,
                "Temporary Redirect",
                {"Location": target},
                target,
            )
        self.assertEqual(raised.exception.status, 307)
        self.assertEqual(raised.exception.location, target)
        self.assertNotIn(target, str(raised.exception))
        self.assertNotIn("handler-secret", str(raised.exception))

    def test_disabled_application_performs_zero_network_or_head_resolution(self):
        client = FakeClient()
        head = mock.Mock(side_effect=AssertionError("head resolver called"))
        release = mock.Mock(side_effect=AssertionError("release resolver called"))
        result = RESOLVER.resolve_application(
            self.parkventory,
            client=client,
            head_resolver=head,
            release_resolver=release,
        )
        self.assertEqual(
            (result.status, result.reason),
            ("disabled", "disabled-by-application-production-contract"),
        )
        self.assertEqual(client.requests, [])
        head.assert_not_called()
        release.assert_not_called()

    def test_all_observed_checks_must_be_complete_green_and_exact_sha(self):
        application = self.enabled(self.parkventory)
        cases = (
            (
                "red-unrelated",
                [
                    check_run("Publish immutable application release"),
                    check_run("verify"),
                    check_run("lint", conclusion="failure"),
                ],
                RESOLVER.BlockedEvidence,
                "check-run-not-green",
            ),
            (
                "in-progress",
                [
                    check_run("Publish immutable application release"),
                    check_run("verify", status="in_progress", conclusion=None),
                ],
                RESOLVER.PendingEvidence,
                "check-runs-in-progress",
            ),
            (
                "wrong-sha",
                [
                    check_run(
                        "Publish immutable application release",
                        revision="f" * 40,
                    ),
                    check_run("verify"),
                ],
                RESOLVER.BlockedEvidence,
                "head-sha-does-not-match",
            ),
            (
                "missing-required",
                [check_run("Publish immutable application release")],
                RESOLVER.PendingEvidence,
                "required-checks-missing:verify",
            ),
        )
        for name, runs, error, message in cases:
            with self.subTest(name=name):
                client = FakeClient([check_response(runs)])
                with self.assertRaisesRegex(error, message):
                    RESOLVER.check_candidate(client, application, REVISION)

    def test_tag_is_read_twice_around_descriptor_validation(self):
        application = self.enabled(self.surplasse)
        descriptor_bytes = release_bytes(application)
        manifest = manifest_bytes(application, descriptor_bytes)
        client = FakeClient(self.release_responses(application))
        reference, descriptor = RESOLVER.resolve_release(
            client, application, REVISION
        )
        self.assertEqual(
            reference,
            f"{application.release_repository}@{RESOLVER.content_digest(manifest)}",
        )
        self.assertEqual(descriptor.source_revision, REVISION)
        requested = [item[0] for item in client.requests]
        manifest_requests = [url for url in requested if "/manifests/" in url]
        self.assertEqual(len(manifest_requests), 2)
        self.assertEqual(manifest_requests[0], manifest_requests[1])
        self.assertTrue(manifest_requests[0].endswith(f"/sha-{REVISION}"))
        self.assertEqual(client.responses, [])

    def test_moving_tag_is_blocked_even_when_both_manifests_are_well_formed(self):
        application = self.enabled(self.surplasse)
        descriptor = release_bytes(application)
        second = manifest_bytes(
            application, descriptor, created="2026-08-17T12:00:01Z"
        )
        client = FakeClient(self.release_responses(application, second))
        with self.assertRaisesRegex(
            RESOLVER.BlockedEvidence, "tag-changed-during-resolution"
        ):
            RESOLVER.resolve_release(client, application, REVISION)

    def test_manifest_requires_matching_content_digest_header(self):
        application = self.enabled(self.surplasse)
        descriptor = release_bytes(application)
        manifest = manifest_bytes(application, descriptor)
        client = FakeClient(
            [
                response(b'{"token":"registry-token"}'),
                response(
                    manifest,
                    {
                        "content-type": RESOLVER.OCI_MANIFEST_MEDIA_TYPE,
                        "docker-content-digest": DIGEST_A,
                    },
                ),
            ]
        )
        with self.assertRaisesRegex(
            RESOLVER.BlockedEvidence, "digest-header-does-not-match"
        ):
            RESOLVER.resolve_release(client, application, REVISION)

    def test_ready_requires_checks_release_and_unchanged_exact_head(self):
        application = self.enabled(self.parkventory)
        runs = [
            check_run("Publish immutable application release"),
            check_run("verify"),
            check_run("lint", conclusion="neutral"),
        ]
        client = FakeClient(
            [check_response(runs), *self.release_responses(application)]
        )
        head = mock.Mock(side_effect=[REVISION, REVISION])
        result = RESOLVER.resolve_application(
            application, client=client, head_resolver=head
        )
        self.assertEqual((result.status, result.reason), ("ready", "candidate-admitted"))
        self.assertEqual(result.source_revision, REVISION)
        self.assertIsNotNone(result.release_reference)
        self.assertEqual(
            set(result.descriptor.component_references), {"backend", "frontend"}
        )
        self.assertEqual(head.call_count, 2)
        self.assertEqual(client.responses, [])

    def test_changed_head_after_release_keeps_current_deployment_unchanged(self):
        application = self.enabled(self.surplasse)
        runs = [check_run("Publish immutable application release")]
        client = FakeClient(
            [check_response(runs), *self.release_responses(application)]
        )
        head = mock.Mock(side_effect=[REVISION, "f" * 40])
        result = RESOLVER.resolve_application(
            application, client=client, head_resolver=head
        )
        self.assertEqual(
            (result.status, result.reason),
            ("pending", "canonical-head-changed-during-resolution"),
        )
        self.assertIsNone(result.release_reference)


if __name__ == "__main__":
    unittest.main()
