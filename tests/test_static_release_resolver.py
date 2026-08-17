#!/usr/bin/env python3

from __future__ import annotations

import email.message
import hashlib
import http.client
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import urllib.response
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RESOLVER = load_script_module(
    "static_release_resolver",
    ROOT / "scripts/resolve-static-releases",
)
REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "a" * 64


class FakeClient:
    def __init__(self, responses):
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


def api_response(runs):
    body = json.dumps({"total_count": len(runs), "check_runs": runs}).encode()
    return RESOLVER.HttpResponse(body=body, headers={})


def check_run(name, *, status="completed", conclusion="success"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": REVISION,
    }


def manifest_bytes(application, kind):
    if kind == "site":
        artifact_type = "application/vnd.vps-infra.static-site.v1"
        layer_type = "application/vnd.vps-infra.static-site.v1+tar+gzip"
        title = "site.tar.gz"
    else:
        artifact_type = "application/vnd.vps-infra.route-inventory.v1"
        layer_type = "application/vnd.vps-infra.route-inventory.v1+json"
        title = "routes.json"
    value = {
        "schemaVersion": 2,
        "mediaType": RESOLVER.OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": artifact_type,
        "config": RESOLVER.OCI_EMPTY_CONFIG,
        "layers": [
            {
                "mediaType": layer_type,
                "digest": DIGEST,
                "size": 123,
                "annotations": {RESOLVER.TITLE_ANNOTATION: title},
            }
        ],
        "annotations": {
            RESOLVER.CREATED_ANNOTATION: "2026-08-17T12:00:00Z",
            RESOLVER.SOURCE_ANNOTATION: (
                f"https://github.com/{application.source_repository}"
            ),
            RESOLVER.REVISION_ANNOTATION: REVISION,
        },
    }
    return json.dumps(value, separators=(",", ":")).encode()


class ContractTests(unittest.TestCase):
    def test_versioned_contract_is_exact(self):
        contract = RESOLVER.load_contract(ROOT / "releases/static-production.json")
        self.assertEqual(
            [application.name for application in contract.applications],
            ["personal", "papersempire", "parkventory"],
        )
        self.assertEqual(
            [(application.enabled, application.mode) for application in contract.applications],
            [
                (True, "static-site"),
                (True, "static-site"),
                (True, "temporary-static-demo"),
            ],
        )
        self.assertEqual(
            contract.applications[2].required_checks,
            ("Publish immutable VPS artifacts", "build", "verify"),
        )
        self.assertEqual(
            contract.integration_revision,
            RESOLVER.EXPECTED_INTEGRATION_REVISION,
        )
        self.assertEqual(contract.caddy_image, RESOLVER.EXPECTED_CADDY_IMAGE)

    def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(self):
        with self.assertRaisesRegex(RESOLVER.ResolverError, "duplicate key"):
            RESOLVER.strict_json_bytes(b'{"schema":1,"schema":1}', "test")
        with self.assertRaisesRegex(RESOLVER.ResolverError, "not permitted"):
            RESOLVER.strict_json_bytes(b'{"value":NaN}', "test")

    def test_contract_rejects_unknown_keys(self):
        value = json.loads((ROOT / "releases/static-production.json").read_text())
        value["unexpected"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RESOLVER.ResolverError, "unexpected"):
                RESOLVER.load_contract(path)

    def test_contract_requires_schema_two_boolean_enabled_and_exact_mode(self):
        original = json.loads((ROOT / "releases/static-production.json").read_text())
        cases = (
            ("schema", lambda value: value.__setitem__("schema", 1), "integer 2"),
            (
                "enabled",
                lambda value: value["applications"]["parkventory"].__setitem__(
                    "enabled", 1
                ),
                "enabled must be a Boolean",
            ),
            (
                "mode",
                lambda value: value["applications"]["parkventory"].__setitem__(
                    "mode", "static-site"
                ),
                "mode must be 'temporary-static-demo'",
            ),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                value = json.loads(json.dumps(original))
                mutate(value)
                path = Path(directory) / "contract.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(RESOLVER.ResolverError, message):
                    RESOLVER.load_contract(path)

    def test_cross_contract_rejects_double_enabled_application(self):
        application_contract = json.loads(
            (ROOT / "releases/production.yaml").read_text(encoding="utf-8")
        )
        application_contract["applications"]["parkventory"]["enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "production.yaml"
            path.write_text(json.dumps(application_contract), encoding="utf-8")
            with self.assertRaisesRegex(
                RESOLVER.ResolverError,
                "both enable: parkventory",
            ):
                RESOLVER.load_contract(
                    ROOT / "releases/static-production.json",
                    path,
                )

    def test_disabled_static_demo_allows_future_dynamic_owner(self):
        static_contract = json.loads(
            (ROOT / "releases/static-production.json").read_text(encoding="utf-8")
        )
        static_contract["applications"]["parkventory"]["enabled"] = False
        application_contract = json.loads(
            (ROOT / "releases/production.yaml").read_text(encoding="utf-8")
        )
        application_contract["applications"]["parkventory"]["enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            static_path = root / "static-production.json"
            application_path = root / "production.yaml"
            static_path.write_text(json.dumps(static_contract), encoding="utf-8")
            application_path.write_text(json.dumps(application_contract), encoding="utf-8")
            contract = RESOLVER.load_contract(static_path, application_path)
        self.assertFalse(contract.applications[2].enabled)

    def test_repository_contracts_have_no_double_enabled_application(self):
        contract = RESOLVER.load_contract(
            ROOT / "releases/static-production.json",
            ROOT / "releases/production.yaml",
        )
        self.assertTrue(all(application.enabled for application in contract.applications))


class CheckRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = RESOLVER.load_contract(ROOT / "releases/static-production.json")
        cls.personal = cls.contract.applications[0]
        cls.park = cls.contract.applications[2]

    def resolution(self, application, runs, manifests=None):
        client = FakeClient([api_response(runs)])
        manifest_calls = []

        def resolve_manifest(_client, candidate, revision, *, kind):
            manifest_calls.append((candidate.name, revision, kind))
            return f"{getattr(candidate, kind + '_repository')}@{DIGEST}"

        result = RESOLVER.resolve_application(
            self.contract,
            application,
            client=client,
            head_resolver=lambda _application: REVISION,
            manifest_resolver=resolve_manifest if manifests is None else manifests,
        )
        return result, manifest_calls, client

    def test_park_release_race_waits_for_verify(self):
        runs = [
            check_run("Publish immutable VPS artifacts"),
            check_run("build"),
            check_run("verify", status="in_progress", conclusion=None),
        ]
        result, manifest_calls, _ = self.resolution(self.park, runs)
        self.assertEqual((result.status, result.reason), ("pending", "check-runs-in-progress"))
        self.assertEqual(manifest_calls, [])

    def test_disabled_application_uses_no_network_and_never_enters_matrix(self):
        application = RESOLVER.dataclasses.replace(self.park, enabled=False)
        head_resolver = mock.Mock(side_effect=AssertionError("head resolver called"))
        manifest_resolver = mock.Mock(side_effect=AssertionError("manifest resolver called"))
        result = RESOLVER.resolve_application(
            self.contract,
            application,
            client=FakeClient([]),
            head_resolver=head_resolver,
            manifest_resolver=manifest_resolver,
        )
        self.assertEqual(
            (result.status, result.reason),
            ("disabled", "disabled-by-static-production-contract"),
        )
        head_resolver.assert_not_called()
        manifest_resolver.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            RESOLVER.write_github_output(output, [result])
            values = dict(
                line.split("=", maxsplit=1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
        self.assertEqual(json.loads(values["matrix"]), {"include": []})
        self.assertEqual(values["ready_count"], "0")
        self.assertEqual(values["pending_count"], "0")
        self.assertEqual(values["blocked_count"], "0")
        self.assertEqual(values["disabled_count"], "1")

    def test_missing_required_check_is_pending(self):
        result, manifest_calls, _ = self.resolution(
            self.personal,
            [check_run("Publish immutable VPS artifacts")],
        )
        self.assertEqual(result.status, "pending")
        self.assertIn("build", result.reason)
        self.assertEqual(manifest_calls, [])

    def test_red_check_is_blocked(self):
        result, manifest_calls, _ = self.resolution(
            self.personal,
            [
                check_run("Publish immutable VPS artifacts"),
                check_run("build", conclusion="failure"),
            ],
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn("check-run-not-green", result.reason)
        self.assertEqual(manifest_calls, [])

    def test_any_present_red_check_is_blocked(self):
        result, _, _ = self.resolution(
            self.personal,
            [
                check_run("Publish immutable VPS artifacts"),
                check_run("build"),
                check_run("unlisted-security-check", conclusion="cancelled"),
            ],
        )
        self.assertEqual(result.status, "blocked")

    def test_ready_candidate_uses_only_exact_head_and_seven_fields(self):
        result, manifest_calls, client = self.resolution(
            self.personal,
            [check_run("Publish immutable VPS artifacts"), check_run("build")],
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            manifest_calls,
            [("personal", REVISION, "site"), ("personal", REVISION, "routes")],
        )
        self.assertIn(f"/commits/{REVISION}/check-runs?", client.requests[0][0])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0][3], RESOLVER.CHECK_RUN_ATTEMPTS)
        self.assertEqual(RESOLVER.CHECK_RUN_ATTEMPTS, 2)
        self.assertEqual(
            set(result.matrix_entry()),
            {
                "application",
                "source_revision",
                "site_reference",
                "routes_reference",
                "integration_revision",
                "integration_reference",
                "caddy_image",
            },
        )

    def test_neutral_and_skipped_unlisted_checks_are_accepted(self):
        result, _, _ = self.resolution(
            self.personal,
            [
                check_run("Publish immutable VPS artifacts"),
                check_run("build"),
                check_run("optional-neutral", conclusion="neutral"),
                check_run("optional-skipped", conclusion="skipped"),
            ],
        )
        self.assertEqual(result.status, "ready")

    def test_required_checks_must_conclude_success(self):
        result, manifest_calls, _ = self.resolution(
            self.personal,
            [
                check_run("Publish immutable VPS artifacts", conclusion="neutral"),
                check_run("build"),
            ],
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn("required-checks-not-successful", result.reason)
        self.assertEqual(manifest_calls, [])

    def test_one_blocked_application_does_not_suppress_ready_applications(self):
        paper = self.contract.applications[1]
        client = FakeClient(
            [
                api_response(
                    [
                        check_run("Publish immutable VPS artifacts"),
                        check_run("build", conclusion="failure"),
                    ]
                ),
                api_response(
                    [check_run("Publish immutable VPS artifacts"), check_run("build")]
                ),
                api_response(
                    [
                        check_run("Publish immutable VPS artifacts"),
                        check_run("build"),
                        check_run("verify"),
                    ]
                ),
            ]
        )

        def resolved_manifest(_client, application, _revision, *, kind):
            return f"{getattr(application, kind + '_repository')}@{DIGEST}"

        results = RESOLVER.resolve_all(
            self.contract,
            client=client,
            head_resolver=lambda _application: REVISION,
            manifest_resolver=resolved_manifest,
        )
        self.assertEqual(
            [(result.application, result.status) for result in results],
            [("personal", "blocked"), (paper.name, "ready"), ("parkventory", "ready")],
        )

    def test_head_change_during_resolution_never_yields_an_old_candidate(self):
        observed_heads = iter((REVISION, "f" * 40))
        client = FakeClient(
            [
                api_response(
                    [check_run("Publish immutable VPS artifacts"), check_run("build")]
                )
            ]
        )

        def resolved_manifest(_client, application, _revision, *, kind):
            return f"{getattr(application, kind + '_repository')}@{DIGEST}"

        result = RESOLVER.resolve_application(
            self.contract,
            self.personal,
            client=client,
            head_resolver=lambda _application: next(observed_heads),
            manifest_resolver=resolved_manifest,
        )
        self.assertEqual(
            (result.status, result.reason),
            ("pending", "canonical-head-changed-during-resolution"),
        )
        self.assertIsNone(result.site_reference)


class CanonicalHeadTests(unittest.TestCase):
    def test_ls_remote_cannot_inherit_checkout_credentials_or_local_config(self):
        contract = RESOLVER.load_contract(ROOT / "releases/static-production.json")
        application = contract.applications[0]
        completed = RESOLVER.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{REVISION}\trefs/heads/main\n".encode(),
            stderr=b"",
        )
        with mock.patch.object(RESOLVER.subprocess, "run", return_value=completed) as run:
            self.assertEqual(RESOLVER.resolve_head(application), REVISION)
        arguments, keywords = run.call_args
        self.assertEqual(keywords["cwd"], "/")
        self.assertNotIn("GITHUB_TOKEN", keywords["env"])
        self.assertIn("credential.helper=", arguments[0])
        self.assertIn("http.https://github.com/.extraheader=", arguments[0])


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = RESOLVER.load_contract(ROOT / "releases/static-production.json")
        cls.personal = cls.contract.applications[0]

    def test_registry_404_is_pending_and_produces_no_candidate(self):
        client = FakeClient(
            [
                api_response(
                    [check_run("Publish immutable VPS artifacts"), check_run("build")]
                ),
                RESOLVER.HttpResponse(body=b'{"token":"public-token"}', headers={}),
                RESOLVER.HttpFailure(404, "not found"),
            ]
        )
        result = RESOLVER.resolve_application(
            self.contract,
            self.personal,
            client=client,
            head_resolver=lambda _application: REVISION,
        )
        self.assertEqual((result.status, result.reason), ("pending", "site-manifest-not-published"))
        self.assertIsNone(result.site_reference)
        self.assertEqual(len(client.requests), 3)

    def test_manifest_reference_uses_digest_of_exact_bounded_body(self):
        raw = manifest_bytes(self.personal, "site")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        client = FakeClient(
            [
                RESOLVER.HttpResponse(body=b'{"token":"public-token"}', headers={}),
                RESOLVER.HttpResponse(
                    body=raw,
                    headers={
                        "content-type": RESOLVER.OCI_MANIFEST_MEDIA_TYPE,
                        "docker-content-digest": digest,
                    },
                ),
            ]
        )
        reference = RESOLVER.resolve_manifest(
            client, self.personal, REVISION, kind="site"
        )
        self.assertEqual(reference, f"{self.personal.site_repository}@{digest}")
        manifest_request = client.requests[1]
        self.assertTrue(manifest_request[0].endswith(f"/manifests/sha-{REVISION}"))
        self.assertEqual(manifest_request[2], RESOLVER.MAX_MANIFEST_BYTES)

    def test_manifest_digest_header_mismatch_is_blocked(self):
        raw = manifest_bytes(self.personal, "routes")
        client = FakeClient(
            [
                RESOLVER.HttpResponse(body=b'{"token":"public-token"}', headers={}),
                RESOLVER.HttpResponse(
                    body=raw,
                    headers={
                        "content-type": RESOLVER.OCI_MANIFEST_MEDIA_TYPE,
                        "docker-content-digest": DIGEST,
                    },
                ),
            ]
        )
        with self.assertRaisesRegex(RESOLVER.BlockedEvidence, "does-not-match"):
            RESOLVER.resolve_manifest(client, self.personal, REVISION, kind="routes")


class FakeUrlResponse:
    def __init__(self, url, body):
        self.url = url
        self.body = body
        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        return False

    def geturl(self):
        return self.url

    def getcode(self):
        return 200

    def read(self, amount):
        return self.body[:amount]


class RetryOpener:
    def __init__(self):
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        if self.calls == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                504,
                "Gateway Timeout",
                {},
                io.BytesIO(b""),
            )
        return FakeUrlResponse(request.full_url, b"{}")


class DisconnectedOpener:
    def __init__(self):
        self.calls = 0

    def open(self, _request, _timeout=None, **_keywords):
        self.calls += 1
        raise http.client.RemoteDisconnected("peer closed the connection")


class RedirectingHttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__()
        self.opened_urls = []

    def https_open(self, request):
        self.opened_urls.append(request.full_url)
        headers = email.message.Message()
        headers["Location"] = "https://attacker.invalid/stolen"
        response = urllib.response.addinfourl(
            io.BytesIO(b""),
            headers,
            request.full_url,
            code=302,
        )
        response.msg = "Found"
        return response


class HttpClientTests(unittest.TestCase):
    def test_transient_failure_retries_with_bounded_backoff(self):
        opener = RetryOpener()
        sleeps = []
        client = RESOLVER.BoundedHttpClient(
            opener=opener,
            sleeper=sleeps.append,
            retry_delays=(0.25, 0.5),
        )
        response = client.get(
            "https://api.github.com/example",
            headers={"Accept": "application/json"},
            max_bytes=1024,
            attempts=3,
        )
        self.assertEqual(response.body, b"{}")
        self.assertEqual(opener.calls, 2)
        self.assertEqual(sleeps, [0.25])

    def test_http_protocol_exception_retries_then_fails_closed(self):
        opener = DisconnectedOpener()
        sleeps = []
        client = RESOLVER.BoundedHttpClient(
            opener=opener,
            sleeper=sleeps.append,
            retry_delays=(0.25, 0.5),
        )
        with self.assertRaisesRegex(RESOLVER.HttpFailure, "peer closed"):
            client.get(
                "https://api.github.com/example",
                headers={"Accept": "application/json"},
                max_bytes=1024,
                attempts=3,
            )
        self.assertEqual(opener.calls, 3)
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_authorized_request_refuses_redirect_before_second_endpoint(self):
        transport = RedirectingHttpsHandler()
        opener = urllib.request.build_opener(RESOLVER.RejectRedirects(), transport)
        client = RESOLVER.BoundedHttpClient(
            opener=opener,
            sleeper=lambda _delay: None,
        )
        with self.assertRaisesRegex(RESOLVER.HttpFailure, "redirects are not permitted"):
            client.get(
                "https://ghcr.io/v2/nclsppr/personal/site/manifests/sha-test",
                headers={"Authorization": "Bearer private-token"},
                max_bytes=1024,
                attempts=1,
            )
        self.assertEqual(
            transport.opened_urls,
            ["https://ghcr.io/v2/nclsppr/personal/site/manifests/sha-test"],
        )


class WorkflowTests(unittest.TestCase):
    def test_central_workflow_is_bounded_and_pinned(self):
        path = ROOT / ".github/workflows/deploy-static-releases.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.load(text, Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "*/10 * * * *"}])
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertNotIn("push", workflow["on"])
        self.assertEqual(workflow["concurrency"]["group"], "production-vps")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertIn("github.ref == 'refs/heads/main'", workflow["jobs"]["resolve"]["if"])

        deploy = workflow["jobs"]["deploy"]
        gate = load_script_module(
            "deploy_static_live_gate_workflow_contract",
            ROOT / "scripts/deploy-static-live-gate",
        )
        self.assertEqual(deploy["strategy"]["fail-fast"], "false")
        self.assertEqual(deploy["strategy"]["max-parallel"], "1")
        self.assertEqual(deploy["environment"]["name"], "static-production")
        deploy_timeout_seconds = int(deploy["timeout-minutes"]) * 60
        self.assertGreater(
            deploy_timeout_seconds,
            gate.SYSTEMD_RUN_WAIT_TIMEOUT_SECONDS
            + gate.RECOVERY_TIMEOUT_SECONDS
            + 120,
        )
        self.assertIn("VPS_STATIC_DEPLOY_ENABLED", text)
        self.assertIn("--application-contract releases/production.yaml", text)
        self.assertEqual(
            workflow["jobs"]["resolve"]["outputs"]["disabled_count"],
            "${{ steps.releases.outputs.disabled_count }}",
        )
        self.assertIn("deploy-static-live", text)
        self.assertIn("fail-fast: false", text)
        self.assertNotIn("secrets.GITHUB_TOKEN", text)
        self.assertIn("/usr/bin/timeout --signal=TERM --kill-after=5s 30s", text)
        self.assertIn("-o ConnectTimeout=15", text)
        self.assertIn("-o ServerAliveInterval=30", text)
        self.assertIn(
            'remote_command="deploy-static-live ${APPLICATION} ${SOURCE_REVISION} '
            '${SITE_REFERENCE} ${ROUTES_REFERENCE} ${INTEGRATION_REVISION} '
            '${INTEGRATION_REFERENCE} ${CADDY_IMAGE}"',
            text,
        )

        action_references = []
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    action_references.append(step["uses"])
        self.assertEqual(
            action_references,
            ["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"],
        )
        checkout = workflow["jobs"]["resolve"]["steps"][0]
        self.assertEqual(checkout["with"]["persist-credentials"], "false")


if __name__ == "__main__":
    unittest.main()
