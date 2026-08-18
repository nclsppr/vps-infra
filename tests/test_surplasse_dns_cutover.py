#!/usr/bin/env python3
"""Adversarial tests for the inactive Surplasse DNS cutover controller."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/surplasse-dns-cutover"


def load_controller():
    loader = SourceFileLoader("surplasse_dns_cutover", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


DNS = load_controller()


def baseline_records() -> list[dict[str, object]]:
    return [
        {"id": 1, "fieldType": "A", "subDomain": "", "target": DNS.BASELINE_IPV4, "ttl": DNS.BASELINE_TTL},
        {"id": 2, "fieldType": "A", "subDomain": "www", "target": DNS.BASELINE_IPV4, "ttl": DNS.BASELINE_TTL},
        {"id": 3, "fieldType": "MX", "subDomain": "", "target": "1 mx1.mail.ovh.net.", "ttl": 3600},
        {"id": 4, "fieldType": "TXT", "subDomain": "", "target": '"v=spf1 include:mx.ovh.com -all"', "ttl": 3600},
        {"id": 5, "fieldType": "CAA", "subDomain": "", "target": '0 issue "letsencrypt.org"', "ttl": 3600},
        {"id": 6, "fieldType": "NS", "subDomain": "", "target": "dns101.ovh.net.", "ttl": 86400},
        {"id": 7, "fieldType": "NS", "subDomain": "", "target": "ns101.ovh.net.", "ttl": 86400},
        {
            "id": 8,
            "fieldType": "SOA",
            "subDomain": "",
            "target": "dns101.ovh.net. tech.ovh.net. 2026081801 86400 3600 3600000 300",
            "ttl": 86400,
        },
        {"id": 9, "fieldType": "TXT", "subDomain": "_dmarc", "target": '"v=DMARC1; p=none"', "ttl": 3600},
        {
            "id": 10,
            "fieldType": "TXT",
            "subDomain": "selector._domainkey",
            "target": '"v=DKIM1; p=example"',
            "ttl": 3600,
        },
    ]


class Clock:
    def __init__(self, value: int = 2_000_000_000):
        self.value = value

    def __call__(self) -> float:
        return float(self.value)


class Identifiers:
    def __init__(self):
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:032x}"


class FakeApi:
    def __init__(self, records: list[dict[str, object]] | None = None):
        self.records = [dict(record) for record in (records or baseline_records())]
        self.calls: list[tuple[object, ...]] = []
        self.next_id = 100
        self.crash_after_write_number: int | None = None
        self.write_count = 0
        self.refresh_count = 0

    def export_zone(self) -> bytes:
        self.calls.append(("GET", "export"))
        return b"; complete API export\n" + DNS.canonical_bytes(
            DNS.canonical_records(self.records)
        )

    def list_records(self) -> list[dict[str, object]]:
        self.calls.append(("GET", "records"))
        return DNS.canonical_records(self.records)

    def _after_write(self) -> None:
        self.write_count += 1
        if self.crash_after_write_number == self.write_count:
            raise RuntimeError("simulated process loss after remote commit")

    def put_record(self, record_id: int, record: dict[str, object]) -> None:
        matches = [index for index, candidate in enumerate(self.records) if candidate["id"] == record_id]
        if len(matches) != 1:
            raise AssertionError("unexpected PUT identifier")
        candidate = dict(record)
        self.records[matches[0]] = candidate
        self.calls.append(("PUT", record_id, candidate["target"], candidate["ttl"]))
        self._after_write()

    def post_record(self, record: dict[str, object]) -> dict[str, object]:
        self.next_id += 1
        candidate = {**record, "id": self.next_id}
        self.records.append(candidate)
        self.calls.append(("POST", candidate["subDomain"], candidate["target"], candidate["ttl"]))
        self._after_write()
        return dict(candidate)

    def delete_record(self, record_id: int) -> None:
        matches = [candidate for candidate in self.records if candidate["id"] == record_id]
        if len(matches) != 1:
            raise AssertionError("unexpected DELETE identifier")
        self.records = [candidate for candidate in self.records if candidate["id"] != record_id]
        self.calls.append(("DELETE", record_id))
        self._after_write()

    def refresh(self) -> None:
        self.calls.append(("POST", "refresh"))
        self.refresh_count += 1
        for record in self.records:
            if record["fieldType"] == "SOA":
                fields = str(record["target"]).split()
                fields[2] = str(int(fields[2]) + 1)
                record["target"] = " ".join(fields)


class FakeDns:
    def __init__(self, api: FakeApi):
        self.api = api
        self.calls: list[tuple[str, str, str]] = []
        self.fail_recursive = False

    def query(self, server: str, name: str, field_type: str):
        self.calls.append((server, name, field_type))
        if field_type == "NS":
            targets = [
                str(record["target"])
                for record in self.api.records
                if record["fieldType"] == "NS" and record["subDomain"] == ""
            ] or ["dns101.ovh.net.", "ns101.ovh.net."]
            return [DNS.DnsAnswer(name, 86400, "NS", target) for target in targets]
        if field_type == "SOA":
            targets = [
                str(record["target"])
                for record in self.api.records
                if record["fieldType"] == "SOA" and record["subDomain"] == ""
            ] or [
                "dns101.ovh.net. tech.ovh.net. 2026081801 86400 3600 3600000 300"
            ]
            return [DNS.DnsAnswer(name, 86400, "SOA", targets[0])]
        if field_type != "A":
            return []
        if name == DNS.ZONE:
            subdomain = ""
        elif name == f"www.{DNS.ZONE}":
            subdomain = "www"
        else:
            subdomain = "*"
        matches = [
            record
            for record in self.api.records
            if record["fieldType"] == "A" and record["subDomain"] == subdomain
        ]
        if not matches:
            return []
        target = str(matches[0]["target"])
        if self.fail_recursive and server in DNS.RECURSIVE_RESOLVERS:
            target = "192.0.2.1"
        return [
            DNS.DnsAnswer(
                name,
                int(matches[0]["ttl"]),
                "A",
                target,
            )
        ]


class Fixture:
    def __init__(self, root: Path, records: list[dict[str, object]] | None = None):
        self.root = root
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        lock_parent = root / "run/lock"
        lock_parent.mkdir(parents=True)
        self.paths = DNS.RuntimePaths(
            policy=root / "policy.json",
            state_root=self.state,
            deployment_lock=lock_parent / "vps-static.lock",
            dig=root / "usr/bin/dig",
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )
        self.clock = Clock()
        self.ids = Identifiers()
        self.api = FakeApi(records)
        self.dns = FakeDns(self.api)
        self.policy = DNS.Policy(True, "ready", root / "credentials")
        self.controller = DNS.Controller(
            self.paths,
            self.policy,
            api=self.api,
            dns=self.dns,
            now=self.clock,
            identifier=self.ids,
        )

    def lower_ttl(self) -> tuple[dict[str, object], dict[str, object]]:
        plan = self.controller.plan()
        result = self.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))
        return plan, result

    def cutover(self) -> tuple[dict[str, object], dict[str, object]]:
        active = self.controller.store.read_json(self.paths.active, "active")
        self.clock.value = int(active["not_before"])
        plan = self.controller.plan()
        result = self.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))
        return plan, result


class CutoverTests(unittest.TestCase):
    def test_repository_policy_is_locked_and_adapter_stays_locked(self) -> None:
        policy = json.loads(
            (ROOT / "policies/surplasse-dns-cutover-v1.json").read_text(encoding="utf-8")
        )
        self.assertIs(policy["enabled"], False)
        self.assertEqual(policy["activation_policy"], "locked")
        adapter = json.loads(
            (ROOT / "applications/surplasse/adapter.json").read_text(encoding="utf-8")
        )
        self.assertEqual(adapter["activation_policy"], "locked")
        release = json.loads(
            (ROOT / "releases/application-production.json").read_text(encoding="utf-8")
        )
        self.assertFalse(release["applications"]["surplasse"]["enabled"])

        tasks = (ROOT / "ansible/roles/surplasse_dns_cutover/tasks/main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("owner: root", tasks)
        self.assertIn('mode: "0444"', tasks)
        self.assertIn('mode: "0700"', tasks)

    def test_locked_policy_refuses_before_api_or_state_access(self) -> None:
        class TrapApi:
            def __getattr__(self, name):
                raise AssertionError(f"API accessed while locked: {name}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = DNS.RuntimePaths(
                state_root=root / "absent",
                deployment_lock=root / "absent.lock",
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
            )
            controller = DNS.Controller(
                paths,
                DNS.Policy(False, "locked", root / "credentials"),
                api=TrapApi(),
            )
            self.assertFalse(controller.doctor()["mutations_available"])
            with self.assertRaisesRegex(DNS.CutoverError, "disabled"):
                controller.plan()
            self.assertFalse(paths.state_root.exists())

    def test_first_plan_captures_complete_export_and_requires_ttl_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            expected_export = fixture.api.export_zone()
            fixture.api.calls.clear()
            plan = fixture.controller.plan()
            self.assertEqual(plan["kind"], "lower_ttl")
            self.assertEqual([change["name"] for change in plan["changes"]], [DNS.ZONE, f"www.{DNS.ZONE}"])
            self.assertEqual([change["after"]["ttl"] for change in plan["changes"]], [300, 300])
            self.assertEqual(len(str(plan["plan_sha256"])), 64)
            self.assertEqual(
                int(DNS.dt.datetime.fromisoformat(str(plan["expires_at"]).replace("Z", "+00:00")).timestamp()),
                fixture.clock.value + DNS.PLAN_TTL_SECONDS,
            )
            transaction_id = str(plan["transaction_id"])
            raw_export = fixture.controller.store.export_path(
                f"{transaction_id}.snapshot.zone"
            )
            self.assertEqual(raw_export.read_bytes(), expected_export)
            self.assertEqual(raw_export.stat().st_mode & 0o777, 0o400)
            snapshot = fixture.controller._snapshot(transaction_id)
            self.assertEqual(snapshot["records"], DNS.canonical_records(baseline_records()))
            self.assertIn(("GET", "export"), fixture.api.calls)

    def test_provider_managed_ns_and_soa_need_not_appear_in_record_list(self) -> None:
        records = [
            record
            for record in baseline_records()
            if record["fieldType"] not in {"NS", "SOA"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), records)
            plan, result = fixture.lower_ttl()
            snapshot = fixture.controller._snapshot(str(plan["transaction_id"]))
            self.assertEqual(
                snapshot["authority"]["nameservers"],
                ["dns101.ovh.net", "ns101.ovh.net"],
            )
            self.assertIn("<provider-managed-serial>", snapshot["authority"]["soa"])
            self.assertEqual(result["status"], "verified")

    def test_baseline_rejects_wrong_or_ambiguous_owned_records(self) -> None:
        cases: list[list[dict[str, object]]] = []
        wrong_target = baseline_records()
        wrong_target[0]["target"] = "192.0.2.4"
        cases.append(wrong_target)
        wildcard = baseline_records()
        wildcard.append({"id": 20, "fieldType": "A", "subDomain": "*", "target": DNS.BASELINE_IPV4, "ttl": 3600})
        cases.append(wildcard)
        aaaa = baseline_records()
        aaaa.append({"id": 20, "fieldType": "AAAA", "subDomain": "www", "target": "2001:db8::1", "ttl": 3600})
        cases.append(aaaa)
        cname = baseline_records()
        cname.append({"id": 20, "fieldType": "CNAME", "subDomain": "*", "target": "example.net.", "ttl": 3600})
        cases.append(cname)
        duplicate = baseline_records()
        duplicate.append({"id": 20, "fieldType": "A", "subDomain": "", "target": DNS.BASELINE_IPV4, "ttl": 3600})
        cases.append(duplicate)
        for records in cases:
            with self.subTest(records=records[-1]):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = Fixture(Path(directory), records)
                    with self.assertRaises(DNS.CutoverError):
                        fixture.controller.plan()
                    self.assertFalse(any(call[0] in {"PUT", "POST", "DELETE"} for call in fixture.api.calls))

    def test_apply_lowers_ttl_refreshes_and_sets_full_old_ttl_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            plan, result = fixture.lower_ttl()
            self.assertEqual(result["status"], "verified")
            self.assertEqual(fixture.api.refresh_count, 1)
            self.assertEqual(
                [call[:2] for call in fixture.api.calls if call[0] == "PUT"],
                [("PUT", 1), ("PUT", 2)],
            )
            active = fixture.controller.store.read_json(fixture.paths.active, "active")
            self.assertEqual(active["phase"], "ttl_lowered")
            self.assertGreaterEqual(active["not_before"] - active["ttl_lowered_at"], 3600)
            self.assertEqual(result["api_readback"]["status"], "exact")
            queried_servers = {call[0] for call in fixture.dns.calls}
            self.assertEqual(
                queried_servers,
                {"dns101.ovh.net", "ns101.ovh.net", "1.1.1.1", "8.8.8.8"},
            )
            self.assertEqual(len(plan["changes"]), 2)

    def test_cutover_plan_waits_and_only_writes_three_ipv4_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.lower_ttl()
            with self.assertRaisesRegex(DNS.CutoverError, "previous TTL"):
                fixture.controller.plan()
            plan, result = fixture.cutover()
            self.assertEqual(plan["kind"], "cutover")
            self.assertEqual([change["method"] for change in plan["changes"]], ["POST", "PUT", "PUT"])
            self.assertTrue(all(change["type"] == "A" for change in plan["changes"]))
            self.assertEqual(result["status"], "verified")
            DNS.require_cutover(fixture.api.list_records())
            self.assertFalse(any(record["fieldType"] == "AAAA" for record in fixture.api.records))

    def test_expired_or_changed_plan_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            plan = fixture.controller.plan()
            fixture.clock.value += DNS.PLAN_TTL_SECONDS + 1
            with self.assertRaisesRegex(DNS.CutoverError, "expired"):
                fixture.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))
            self.assertFalse(any(call[0] in {"PUT", "DELETE"} for call in fixture.api.calls))

    def test_unchanged_expired_plan_can_be_replaced_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            first = fixture.controller.plan()
            fixture.clock.value += DNS.PLAN_TTL_SECONDS + 1
            replacement = fixture.controller.plan()
            self.assertNotEqual(first["plan_id"], replacement["plan_id"])
            self.assertEqual(first["transaction_id"], replacement["transaction_id"])
            self.assertEqual(first["changes"], replacement["changes"])
            self.assertFalse(any(call[0] in {"PUT", "DELETE"} for call in fixture.api.calls))

    def test_changed_plan_precondition_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            plan = fixture.controller.plan()
            for record in fixture.api.records:
                if record["fieldType"] == "MX":
                    record["target"] = "1 changed.invalid."
            with self.assertRaisesRegex(DNS.CutoverError, "changed"):
                fixture.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))
            self.assertFalse(any(call[0] in {"PUT", "DELETE"} for call in fixture.api.calls))

    def test_recovery_observes_committed_write_without_replaying_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            plan = fixture.controller.plan()
            fixture.api.crash_after_write_number = 1
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                fixture.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))
            fixture.api.crash_after_write_number = None
            result = fixture.controller.recover(str(plan["plan_id"]), str(plan["plan_sha256"]))
            self.assertEqual(result["status"], "verified")
            record_one_puts = [call for call in fixture.api.calls if call[:2] == ("PUT", 1)]
            self.assertEqual(len(record_one_puts), 1)
            observations = [item["status"] for item in result["api_mutations"]]
            self.assertEqual(observations[0], "observed_after_interruption")
            with self.assertRaisesRegex(DNS.CutoverError, "already"):
                fixture.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))

    def test_ambiguous_dns_requires_one_separate_verify_and_no_reapply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            plan = fixture.controller.plan()
            fixture.dns.fail_recursive = True
            result = fixture.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))
            self.assertEqual(result["status"], "applied_unverified")
            with self.assertRaisesRegex(DNS.CutoverError, "already"):
                fixture.controller.apply(str(plan["plan_id"]), str(plan["plan_sha256"]))
            fixture.dns.fail_recursive = False
            verified = fixture.controller.verify(str(plan["plan_id"]))
            self.assertEqual(verified["status"], "verified")
            with self.assertRaisesRegex(DNS.CutoverError, "not eligible"):
                fixture.controller.verify(str(plan["plan_id"]))

    def test_rollback_restores_canonical_snapshot_and_refuses_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            lower_plan, _ = fixture.lower_ttl()
            fixture.cutover()
            rollback = fixture.controller.plan_rollback(str(lower_plan["transaction_id"]))
            self.assertEqual([change["method"] for change in rollback["changes"]], ["DELETE", "PUT", "PUT"])
            result = fixture.controller.apply(
                str(rollback["plan_id"]), str(rollback["plan_sha256"])
            )
            self.assertEqual(result["status"], "verified")
            self.assertTrue(
                DNS.records_match(
                    fixture.api.list_records(), DNS.canonical_records(baseline_records())
                )
            )
            active = fixture.controller.store.read_json(fixture.paths.active, "active")
            self.assertEqual(active["phase"], "rolled_back")
            with self.assertRaises(DNS.CutoverError):
                fixture.controller.apply(
                    str(rollback["plan_id"]), str(rollback["plan_sha256"])
                )
            with self.assertRaises(DNS.CutoverError):
                fixture.controller.plan_rollback(str(lower_plan["transaction_id"]))

    def test_rollback_refuses_protected_record_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            lower_plan, _ = fixture.lower_ttl()
            fixture.cutover()
            for record in fixture.api.records:
                if record["fieldType"] == "TXT" and record["subDomain"] == "_dmarc":
                    record["target"] = '"v=DMARC1; p=reject"'
            with self.assertRaisesRegex(DNS.CutoverError, "protected"):
                fixture.controller.plan_rollback(str(lower_plan["transaction_id"]))
            self.assertFalse(any(call[0] == "DELETE" for call in fixture.api.calls))

    def test_tampered_plan_digest_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            summary = fixture.controller.plan()
            plan_path = fixture.controller.store.plan_path(str(summary["plan_id"]))
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            document["expires_at"] += 1
            plan_path.chmod(0o600)
            plan_path.write_text(json.dumps(document), encoding="utf-8")
            plan_path.chmod(0o400)
            with self.assertRaises(DNS.CutoverError):
                fixture.controller.apply(
                    str(summary["plan_id"]), str(summary["plan_sha256"])
                )
            self.assertFalse(any(call[0] in {"PUT", "DELETE"} for call in fixture.api.calls))

    def test_apply_cli_has_no_target_and_credentials_are_not_an_interface(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                DNS.parser().parse_args(
                    [
                        "apply",
                        "--plan-id",
                        "1" * 32,
                        "--plan-sha256",
                        "2" * 64,
                        "--target",
                        DNS.ATLAS_IPV4,
                        "--json",
                    ]
                )
        self.assertEqual(raised.exception.code, DNS.EX_USAGE)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--application-key", source)
        self.assertNotIn("--application-secret", source)
        self.assertNotIn("--consumer-key", source)
        self.assertNotIn("os.environ", source)
        self.assertIn('env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}', source)

    def test_policy_validation_accepts_only_locked_or_explicit_ready_pair(self) -> None:
        document = json.loads(
            (ROOT / "policies/surplasse-dns-cutover-v1.json").read_text(encoding="utf-8")
        )
        locked = DNS.validate_policy_document(document)
        self.assertFalse(locked.enabled)
        invalid = dict(document)
        invalid["enabled"] = True
        with self.assertRaises(DNS.CutoverError):
            DNS.validate_policy_document(invalid)
        ready = dict(invalid)
        ready["activation_policy"] = "ready"
        self.assertTrue(DNS.validate_policy_document(ready).enabled)
        wrong_zone = dict(document)
        wrong_zone["zone"] = "example.com"
        with self.assertRaises(DNS.CutoverError):
            DNS.validate_policy_document(wrong_zone)

    def test_stdlib_ovh_client_uses_only_the_fixed_zone_routes(self) -> None:
        class Response:
            def __init__(self, payload: bytes):
                self.payload = payload
                self.headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, limit: int) -> bytes:
                return self.payload[:limit]

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout: int):
                self.requests.append((request, timeout))
                payload = (
                    b"; complete export\n"
                    if request.full_url.endswith("/export")
                    else b""
                )
                return Response(payload)

        opener = Opener()
        credentials = DNS.OvhCredentials("A" * 16, "B" * 16, "C" * 16)
        api = DNS.OvhZoneApi(credentials, now=lambda: 2_000_000_000, opener=opener)
        payload = api.request(
            "GET", "/domain/zone/surplasse.com/export", raw=True
        )
        self.assertEqual(payload, b"; complete export\n")
        api.request(
            "PUT",
            "/domain/zone/surplasse.com/record/1",
            {"target": DNS.ATLAS_IPV4, "ttl": 300},
        )
        api.request("POST", "/domain/zone/surplasse.com/refresh")
        self.assertEqual(
            [(request.get_method(), request.full_url) for request, _ in opener.requests],
            [
                ("GET", "https://eu.api.ovh.com/1.0/domain/zone/surplasse.com/export"),
                ("PUT", "https://eu.api.ovh.com/1.0/domain/zone/surplasse.com/record/1"),
                ("POST", "https://eu.api.ovh.com/1.0/domain/zone/surplasse.com/refresh"),
            ],
        )
        for request, timeout in opener.requests:
            self.assertEqual(timeout, 15)
            self.assertNotIn(credentials.application_signing_value, request.full_url)
            self.assertNotIn(credentials.consumer_key, request.full_url)
            self.assertNotIn(credentials.application_signing_value.encode(), request.data or b"")
            self.assertNotIn(credentials.consumer_key.encode(), request.data or b"")
        self.assertIsNone(
            DNS.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.invalid")
        )
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+ovh\b")


if __name__ == "__main__":
    unittest.main()
