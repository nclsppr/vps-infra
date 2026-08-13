#!/usr/bin/env python3

from __future__ import annotations

import copy
import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-surplasse-smtp-preflight"
RSA_SPKI_BASE64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0p1t2yokVcCP3YOOeBPj"
    "lY+D5XYcAdSfEfrSFcyBGJG6HOB+njrFJpDC6Po4lHfm8ZsqWxDFyhfaIxWkRCZQ"
    "ZeUBBkQC7ZN3iZFcEmpR8tDhaBS6mqkblrU2cEmQQpupg82tmTGF5OIh49NsrLZmk"
    "b4HoMNDD4u5c0K65sFFFZxoXfJzcWjw4SyqlNJH/bZKrZQ7+SbdbCirmlx3Z8+oKT"
    "p4WnW4qgTGndoJVO+rHVUHGTP5+5MUk+WVPm+H7Hp4pc8l5Hf8/GHy9tuZeRP/rN"
    "JraAr8k6RnDe3WOYbj64YJrWy91NabxstZXxEeve+z3qfmRIpYZAChGalot5NJVwID"
    "AQAB"
)
RSA_PKCS1_BASE64 = (
    "MIIBCgKCAQEA0p1t2yokVcCP3YOOeBPjlY+D5XYcAdSfEfrSFcyBGJG6HOB+njrF"
    "JpDC6Po4lHfm8ZsqWxDFyhfaIxWkRCZQZeUBBkQC7ZN3iZFcEmpR8tDhaBS6mqkb"
    "lrU2cEmQQpupg82tmTGF5OIh49NsrLZmkb4HoMNDD4u5c0K65sFFFZxoXfJzcWjw"
    "4SyqlNJH/bZKrZQ7+SbdbCirmlx3Z8+oKTp4WnW4qgTGndoJVO+rHVUHGTP5+5MUk"
    "+WVPm+H7Hp4pc8l5Hf8/GHy9tuZeRP/rNJraAr8k6RnDe3WOYbj64YJrWy91Nabx"
    "stZXxEeve+z3qfmRIpYZAChGalot5NJVwIDAQAB"
)


def load_script_module():
    loader = SourceFileLoader("surplasse_smtp_preflight", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


PREFLIGHT = load_script_module()


def valid_contract() -> dict:
    return {
        "schema": 1,
        "provider": "managed-relay",
        "domain": "surplasse.com",
        "from": "no-reply@surplasse.com",
        "smtp": {
            "host": "smtp.relay-provider.net",
            "port": 587,
            "security": "starttls-required",
        },
        "dns": {
            "mx": [
                {"priority": 1, "target": "mx1.mail.ovh.net."},
                {"priority": 5, "target": "mx2.mail.ovh.net."},
                {"priority": 100, "target": "mx3.mail.ovh.net."},
            ],
            "provider_spf_mechanism": "include:spf.relay-provider.net",
            "domain_verification": [],
            "spf": {
                "name": "surplasse.com.",
                "value": (
                    "v=spf1 include:mx.ovh.com "
                    "include:spf.relay-provider.net -all"
                ),
            },
            "dkim": [
                {
                    "name": "relay1._domainkey.surplasse.com.",
                    "type": "TXT",
                    "value": (
                        "v=DKIM1; k=rsa; "
                        f"p={RSA_SPKI_BASE64}"
                    ),
                }
            ],
            "dmarc": {
                "name": "_dmarc.surplasse.com.",
                "value": (
                    "v=DMARC1; p=none; "
                    "rua=mailto:dmarc-reports@surplasse.com"
                ),
            },
        },
    }


def dig_txt(value: str) -> str:
    return json.dumps(value)


class ContractTests(unittest.TestCase):
    def test_exact_contract_is_valid(self) -> None:
        self.assertEqual(PREFLIGHT.validate_contract(valid_contract())["schema"], 1)

    def test_rsa_pkcs1_dkim_key_is_valid(self) -> None:
        contract = valid_contract()
        contract["dns"]["dkim"][0]["value"] = (
            f"v=DKIM1; k=rsa; p={RSA_PKCS1_BASE64}"
        )
        self.assertEqual(PREFLIGHT.validate_contract(contract)["schema"], 1)

    def test_provider_without_spf_mechanism_is_valid(self) -> None:
        contract = valid_contract()
        contract["dns"]["provider_spf_mechanism"] = None
        contract["dns"]["spf"]["value"] = "v=spf1 include:mx.ovh.com -all"
        contract["dns"]["domain_verification"] = [
            {
                "name": "surplasse.com.",
                "type": "TXT",
                "value": "provider-code:reviewed-public-value",
            }
        ]
        self.assertEqual(PREFLIGHT.validate_contract(contract)["schema"], 1)

    def test_provider_spf_and_cname_allow_underscored_dns_owners(self) -> None:
        contract = valid_contract()
        contract["dns"]["provider_spf_mechanism"] = (
            "include:_spf.relay-provider.net"
        )
        contract["dns"]["spf"]["value"] = (
            "v=spf1 include:mx.ovh.com include:_spf.relay-provider.net -all"
        )
        contract["dns"]["domain_verification"] = [
            {
                "name": "relay-code._domainkey.surplasse.com.",
                "type": "CNAME",
                "value": "relay-code._domainkey.relay-provider.net.",
            }
        ]
        self.assertEqual(PREFLIGHT.validate_contract(contract)["schema"], 1)

    def test_transport_and_domain_bypasses_are_rejected(self) -> None:
        cases = (
            (
                "placeholder-host",
                lambda value: value["smtp"].update(host="smtp.example.invalid"),
                "public non réservé",
            ),
            (
                "single-label-host",
                lambda value: value["smtp"].update(host="relay"),
                "public non réservé",
            ),
            (
                "special-use-onion-host",
                lambda value: value["smtp"].update(host="smtp.provider.onion"),
                "public non réservé",
            ),
            (
                "special-use-home-arpa-host",
                lambda value: value["smtp"].update(host="relay.home.arpa"),
                "public non réservé",
            ),
            (
                "special-use-alt-host",
                lambda value: value["smtp"].update(host="relay.provider.alt"),
                "public non réservé",
            ),
            (
                "reverse-arpa-host",
                lambda value: value["smtp"].update(host="1.0.0.127.in-addr.arpa"),
                "public non réservé",
            ),
            (
                "ipv4-literal",
                lambda value: value["smtp"].update(host="127.0.0.1"),
                "public non réservé",
            ),
            (
                "numeric-terminal-label",
                lambda value: value["smtp"].update(host="relay.provider.999"),
                "public non réservé",
            ),
            (
                "implicit-tls-port",
                lambda value: value["smtp"].update(port=465),
                "587",
            ),
            (
                "optional-starttls",
                lambda value: value["smtp"].update(security="starttls-optional"),
                "starttls-required",
            ),
            (
                "changed-mx",
                lambda value: value["dns"]["mx"].pop(),
                "trois MX OVH",
            ),
            (
                "second-spf-shape",
                lambda value: value["dns"]["spf"].update(
                    value="v=spf1 include:spf.relay-provider.net -all"
                ),
                "conserver exactement OVH",
            ),
            (
                "reserved-provider-spf",
                lambda value: value["dns"].update(
                    provider_spf_mechanism="include:provider.example.invalid"
                ),
                "public non réservé",
            ),
            (
                "ip-provider-spf",
                lambda value: value["dns"].update(
                    provider_spf_mechanism="include:8.8.8.8"
                ),
                "public non réservé",
            ),
            (
                "numeric-provider-spf-tld",
                lambda value: value["dns"].update(
                    provider_spf_mechanism="include:spf.provider.999"
                ),
                "public non réservé",
            ),
            (
                "recursive-provider-spf",
                lambda value: value["dns"].update(
                    provider_spf_mechanism="include:surplasse.com"
                ),
                "FQDN public distinct",
            ),
            (
                "missing-dmarc-reports",
                lambda value: value["dns"]["dmarc"].update(
                    value="v=DMARC1; p=none"
                ),
                "rua",
            ),
            (
                "empty-dmarc-mailbox",
                lambda value: value["dns"]["dmarc"].update(
                    value="v=DMARC1; p=none; rua=mailto:"
                ),
                "rua",
            ),
            (
                "invalid-dmarc-dot-atom",
                lambda value: value["dns"]["dmarc"].update(
                    value=(
                        "v=DMARC1; p=none; "
                        "rua=mailto:a..b@surplasse.com"
                    )
                ),
                "rua",
            ),
            (
                "obsolete-dmarc-size-limit",
                lambda value: value["dns"]["dmarc"].update(
                    value=(
                        "v=DMARC1; p=none; "
                        "rua=mailto:reports@surplasse.com!10m"
                    )
                ),
                "rua",
            ),
            (
                "malformed-dkim",
                lambda value: value["dns"]["dkim"][0].update(
                    value="garbage; p=x"
                ),
                "tag invalide",
            ),
            (
                "dkim-without-email-service",
                lambda value: value["dns"]["dkim"][0].update(
                    value=f"v=DKIM1; k=rsa; s=other; p={RSA_SPKI_BASE64}"
                ),
                "service email",
            ),
            (
                "invalid-dmarc-percentage",
                lambda value: value["dns"]["dmarc"].update(
                    value=(
                        "v=DMARC1; p=reject; pct=0; "
                        "rua=mailto:reports@surplasse.com"
                    )
                ),
                "seulement les tags",
            ),
            (
                "unexpected-key",
                lambda value: value["smtp"].update(username="not-allowed"),
                "inattendues",
            ),
            (
                "apex-cname",
                lambda value: value["dns"].update(
                    domain_verification=[
                        {
                            "name": "surplasse.com.",
                            "type": "CNAME",
                            "value": "verification.relay-provider.net.",
                        }
                    ]
                ),
                "CNAME à l'apex",
            ),
        )
        for label, mutate, expected_message in cases:
            with self.subTest(label=label):
                changed = copy.deepcopy(valid_contract())
                mutate(changed)
                with self.assertRaisesRegex(PREFLIGHT.PreflightError, expected_message):
                    PREFLIGHT.validate_contract(changed)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            contract_path = Path(temporary_directory) / "contract.json"
            contract_path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
            with self.assertRaisesRegex(PREFLIGHT.PreflightError, "dupliquée"):
                PREFLIGHT.load_contract(contract_path)

    def test_pathological_json_is_reported_without_a_traceback(self) -> None:
        cases = (
            ("deeply-nested", "[" * 10_000 + "0" + "]" * 10_000),
            ("oversized-integer", '{"schema":' + "9" * 5_000 + "}"),
        )
        for label, raw_contract in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                contract_path = Path(directory) / "contract.json"
                contract_path.write_text(raw_contract, encoding="utf-8")
                result = subprocess.run(
                    [str(SCRIPT), "--contract", str(contract_path), "--validate-only"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("préflight SMTP Surplasse refusé", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_unexpected_json_key_cannot_inject_an_output_line(self) -> None:
        contract = valid_contract()
        contract["FORGED\nSTARTTLS SMTP valide"] = True
        with self.assertRaises(PREFLIGHT.PreflightError) as raised:
            PREFLIGHT.validate_contract(contract)
        self.assertNotIn("\n", str(raised.exception))

    def test_validate_only_cli_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            contract_path = Path(temporary_directory) / "contract.json"
            contract_path.write_text(
                json.dumps(valid_contract()),
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(SCRIPT), "--contract", str(contract_path), "--validate-only"],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "contrat SMTP Surplasse valide\n")

    def test_main_emits_no_partial_success_before_dns_failure(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(PREFLIGHT, "load_contract", return_value=valid_contract()),
            mock.patch.object(
                PREFLIGHT,
                "verify_dns",
                side_effect=PREFLIGHT.PreflightError("échec DNS attendu"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = PREFLIGHT.main(["--contract", "/unused/contract.json"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("échec DNS attendu", stderr.getvalue())

    def test_fake_base64_dkim_key_is_rejected(self) -> None:
        contract = valid_contract()
        contract["dns"]["dkim"][0]["value"] = (
            "v=DKIM1; k=rsa; p=QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB"
        )
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "RSA DER"):
            PREFLIGHT.validate_contract(contract)

    def test_cname_owner_must_be_unique_and_exclusive(self) -> None:
        cases = (
            (
                "two-cnames",
                [
                    {
                        "name": "provider.surplasse.com.",
                        "type": "CNAME",
                        "value": "first.relay-provider.net.",
                    },
                    {
                        "name": "provider.surplasse.com.",
                        "type": "CNAME",
                        "value": "second.relay-provider.net.",
                    },
                ],
                "unique et exclusif",
            ),
            (
                "cname-with-dkim-txt",
                [
                    {
                        "name": "relay1._domainkey.surplasse.com.",
                        "type": "CNAME",
                        "value": "relay1.relay-provider.net.",
                    }
                ],
                "unique et exclusif",
            ),
            (
                "cname-with-dmarc-txt",
                [
                    {
                        "name": "_dmarc.surplasse.com.",
                        "type": "CNAME",
                        "value": "dmarc.relay-provider.net.",
                    }
                ],
                "unique et exclusif",
            ),
            (
                "self-referencing-cname",
                [
                    {
                        "name": "provider.surplasse.com.",
                        "type": "CNAME",
                        "value": "provider.surplasse.com.",
                    }
                ],
                "cycle CNAME",
            ),
            (
                "two-node-cname-cycle",
                [
                    {
                        "name": "first.surplasse.com.",
                        "type": "CNAME",
                        "value": "second.surplasse.com.",
                    },
                    {
                        "name": "second.surplasse.com.",
                        "type": "CNAME",
                        "value": "first.surplasse.com.",
                    },
                ],
                "cycle CNAME",
            ),
        )
        for label, records, expected_message in cases:
            with self.subTest(label=label):
                contract = valid_contract()
                contract["dns"]["domain_verification"] = records
                with self.assertRaisesRegex(
                    PREFLIGHT.PreflightError,
                    expected_message,
                ):
                    PREFLIGHT.validate_contract(contract)

    def test_multilabel_dkim_selector_is_valid(self) -> None:
        contract = valid_contract()
        contract["dns"]["dkim"][0]["name"] = (
            "tenant.relay1._domainkey.surplasse.com."
        )
        self.assertEqual(PREFLIGHT.validate_contract(contract)["schema"], 1)

    def test_dkim_version_may_be_omitted_but_sha1_and_test_mode_are_rejected(
        self,
    ) -> None:
        contract = valid_contract()
        contract["dns"]["dkim"][0]["value"] = (
            f"k=rsa; h=sha256; p={RSA_SPKI_BASE64}"
        )
        self.assertEqual(PREFLIGHT.validate_contract(contract)["schema"], 1)

        cases = (
            (f"v=DKIM2; k=rsa; p={RSA_SPKI_BASE64}", "version DKIM"),
            (
                f"v=DKIM1; k=rsa; h=sha1:sha256; p={RSA_SPKI_BASE64}",
                "uniquement",
            ),
            (f"v=DKIM1; k=rsa; t=y; p={RSA_SPKI_BASE64}", "mode test"),
        )
        for value, expected_message in cases:
            with self.subTest(value=value[:24]):
                changed = valid_contract()
                changed["dns"]["dkim"][0]["value"] = value
                with self.assertRaisesRegex(
                    PREFLIGHT.PreflightError,
                    expected_message,
                ):
                    PREFLIGHT.validate_contract(changed)

    def test_dmarc_whitespace_is_valid_but_unknown_tags_are_rejected(self) -> None:
        contract = valid_contract()
        contract["dns"]["dmarc"]["value"] = (
            "v = DMARC1; p = none; "
            "rua = mailto:dmarc-reports@surplasse.com"
        )
        self.assertEqual(PREFLIGHT.validate_contract(contract)["schema"], 1)

        contract["dns"]["dmarc"]["value"] += "; pct=100"
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "seulement les tags"):
            PREFLIGHT.validate_contract(contract)


class DnsTests(unittest.TestCase):
    def query_for(self, contract: dict):
        dns = contract["dns"]
        records = {
            ("surplasse.com.", "MX"): [
                "1 mx1.mail.ovh.net.",
                "5 mx2.mail.ovh.net.",
                "100 mx3.mail.ovh.net.",
            ],
            ("surplasse.com.", "TXT"): [
                dig_txt("site-verification=preserved"),
                dig_txt(dns["spf"]["value"]),
            ],
            (dns["dkim"][0]["name"], "TXT"): [
                dig_txt(dns["dkim"][0]["value"]),
            ],
            (dns["dmarc"]["name"], "TXT"): [
                dig_txt(dns["dmarc"]["value"]),
            ],
            ("spf.relay-provider.net", "TXT"): [
                dig_txt("v=spf1 ip4:8.8.8.0/24 -all"),
            ],
            ("mx.ovh.com", "TXT"): [
                dig_txt("v=spf1 ip4:8.8.4.0/24 -all"),
            ],
        }

        def query(name: str, record_type: str) -> list[str]:
            return records.get((name, record_type), [])

        return query, records

    def test_exact_dns_proof_passes(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, _records = self.query_for(contract)
        PREFLIGHT.verify_dns(contract, query)

    def test_pathological_mx_priority_is_rejected_without_native_error(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, records = self.query_for(contract)
        records[("surplasse.com.", "MX")][0] = (
            "9" * 5_000 + " mx1.mail.ovh.net."
        )
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "MX"):
            PREFLIGHT.verify_dns(contract, query)

    def test_second_spf_record_is_rejected(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, records = self.query_for(contract)
        records[("surplasse.com.", "TXT")].append(
            dig_txt("v=spf1 include:unreviewed.invalid -all")
        )
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "unique SPF"):
            PREFLIGHT.verify_dns(contract, query)

    def test_missing_provider_spf_is_rejected(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, records = self.query_for(contract)
        records[("spf.relay-provider.net", "TXT")] = []
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "unique SPF"):
            PREFLIGHT.verify_dns(contract, query)

    def test_provider_spf_must_contain_a_positive_authorization_path(self) -> None:
        cases = (
            "v=spf1 -all",
            "v=spf1 ?all",
            "v=spf1 -ip4:8.8.8.0/24 -all",
            "v=spf1 ~mx -all",
        )
        for provider_spf in cases:
            with self.subTest(provider_spf=provider_spf):
                contract = PREFLIGHT.validate_contract(valid_contract())
                query, records = self.query_for(contract)
                records[("spf.relay-provider.net", "TXT")] = [
                    dig_txt(provider_spf)
                ]
                with self.assertRaisesRegex(
                    PREFLIGHT.PreflightError,
                    "aucun chemin",
                ):
                    PREFLIGHT.verify_dns(contract, query)

    def test_spf_cycle_and_lookup_budget_are_rejected(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, records = self.query_for(contract)
        records[("spf.relay-provider.net", "TXT")] = [
            dig_txt("v=spf1 include:loop.relay-provider.net -all")
        ]
        records[("loop.relay-provider.net", "TXT")] = [
            dig_txt("v=spf1 include:spf.relay-provider.net -all")
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "cycle"):
            PREFLIGHT.verify_dns(contract, query)

        records[("spf.relay-provider.net", "TXT")] = [
            dig_txt(
                "v=spf1 "
                + " ".join(
                    f"exists:mail{index}.relay-provider.net"
                    for index in range(10)
                )
                + " -all"
            )
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "dix recherches"):
            PREFLIGHT.verify_dns(contract, query)

    def test_spf_redirect_cannot_be_masked_by_all(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, records = self.query_for(contract)
        records[("spf.relay-provider.net", "TXT")] = [
            dig_txt("v=spf1 redirect=pass.relay-provider.net -all")
        ]
        records[("pass.relay-provider.net", "TXT")] = [
            dig_txt("v=spf1 ip4:8.8.8.0/24 -all")
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "masque"):
            PREFLIGHT.verify_dns(contract, query)

    def test_spf_exp_modifier_may_follow_final_all(self) -> None:
        query = lambda _name, _record_type: []
        self.assertTrue(
            PREFLIGHT.verify_spf_graph(
                "relay-provider.net",
                "v=spf1 ip4:8.8.8.0/24 -all exp=explain.relay-provider.net",
                query,
            )
        )

    def test_dkim_drift_is_rejected(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, records = self.query_for(contract)
        records[(contract["dns"]["dkim"][0]["name"], "TXT")] = [
            dig_txt("v=DKIM1; k=rsa; p=changed")
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "DKIM public"):
            PREFLIGHT.verify_dns(contract, query)

    def test_domain_verification_record_is_required_exactly_once(self) -> None:
        contract = valid_contract()
        contract["dns"]["domain_verification"] = [
            {
                "name": "surplasse.com.",
                "type": "TXT",
                "value": "provider-code:reviewed-public-value",
            }
        ]
        contract = PREFLIGHT.validate_contract(contract)
        query, records = self.query_for(contract)
        records[("surplasse.com.", "TXT")].append(
            dig_txt("provider-code:reviewed-public-value")
        )
        PREFLIGHT.verify_dns(contract, query)
        records[("surplasse.com.", "TXT")].append(
            dig_txt("provider-code:reviewed-public-value")
        )
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "exactement"):
            PREFLIGHT.verify_dns(contract, query)

    def test_domain_verification_cname_rejects_an_extra_answer(self) -> None:
        contract = valid_contract()
        contract["dns"]["domain_verification"] = [
            {
                "name": "verify.surplasse.com.",
                "type": "CNAME",
                "value": "verification.relay-provider.net.",
            }
        ]
        contract = PREFLIGHT.validate_contract(contract)
        query, records = self.query_for(contract)
        records[("verify.surplasse.com.", "CNAME")] = [
            "verification.relay-provider.net.",
            "unreviewed.relay-provider.net.",
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "exactement"):
            PREFLIGHT.verify_dns(contract, query)

    def test_dkim_cname_requires_a_terminal_key(self) -> None:
        contract = valid_contract()
        contract["dns"]["dkim"] = [
            {
                "name": "relay1._domainkey.surplasse.com.",
                "type": "CNAME",
                "value": "relay1.provider-dkim.net.",
            }
        ]
        contract = PREFLIGHT.validate_contract(contract)
        query, records = self.query_for(contract)
        records[("relay1._domainkey.surplasse.com.", "CNAME")] = [
            "relay1.provider-dkim.net."
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "unique TXT"):
            PREFLIGHT.verify_dns(contract, query)
        records[("relay1.provider-dkim.net.", "TXT")] = [
            dig_txt(f"v=DKIM1; k=rsa; p={RSA_SPKI_BASE64}")
        ]
        PREFLIGHT.verify_dns(contract, query)

    def test_external_dmarc_reports_require_dns_authorization(self) -> None:
        contract = valid_contract()
        contract["dns"]["dmarc"]["value"] = (
            "v=DMARC1; p=none; rua=mailto:reports@monitoring-provider.net"
        )
        contract = PREFLIGHT.validate_contract(contract)
        query, records = self.query_for(contract)
        records[(contract["dns"]["dmarc"]["name"], "TXT")] = [
            dig_txt(contract["dns"]["dmarc"]["value"])
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "n'autorise pas"):
            PREFLIGHT.verify_dns(contract, query)
        records[
            (
                "surplasse.com._report._dmarc.monitoring-provider.net.",
                "TXT",
            )
        ] = [dig_txt("v=DMARC1; rua=mailto:override@monitoring-provider.net")]
        PREFLIGHT.verify_dns(contract, query)

    def test_external_dmarc_authorization_handles_wsp_and_rejects_redirect(
        self,
    ) -> None:
        contract = valid_contract()
        contract["dns"]["dmarc"]["value"] = (
            "v=DMARC1; p=none; "
            "rua = mailto:reports@monitoring-provider.net"
        )
        contract = PREFLIGHT.validate_contract(contract)
        query, records = self.query_for(contract)
        records[(contract["dns"]["dmarc"]["name"], "TXT")] = [
            dig_txt(contract["dns"]["dmarc"]["value"])
        ]
        authorization_key = (
            "surplasse.com._report._dmarc.monitoring-provider.net.",
            "TXT",
        )
        records[authorization_key] = [
            dig_txt("unrelated=value"),
            dig_txt("v=DMARC1; rua=mailto:stolen@attacker-provider.org"),
        ]
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "n'autorise pas"):
            PREFLIGHT.verify_dns(contract, query)

        records[authorization_key].append(dig_txt("v = DMARC1"))
        PREFLIGHT.verify_dns(contract, query)

    def test_dmarc_version_boundary_ignores_unrelated_txt(self) -> None:
        contract = PREFLIGHT.validate_contract(valid_contract())
        query, records = self.query_for(contract)
        records[(contract["dns"]["dmarc"]["name"], "TXT")].append(
            dig_txt("v=DMARC10 vendor-token")
        )
        PREFLIGHT.verify_dns(contract, query)

    def test_subdomain_dmarc_report_destination_is_internal(self) -> None:
        contract = valid_contract()
        contract["dns"]["dmarc"]["value"] = (
            "v=DMARC1; p=none; rua=mailto:reports@ops.surplasse.com"
        )
        contract = PREFLIGHT.validate_contract(contract)
        query, records = self.query_for(contract)
        records[(contract["dns"]["dmarc"]["name"], "TXT")] = [
            dig_txt(contract["dns"]["dmarc"]["value"])
        ]
        PREFLIGHT.verify_dns(contract, query)

    def test_split_txt_chunks_are_joined(self) -> None:
        self.assertEqual(
            PREFLIGHT.parse_txt_lines(
                ['"v=spf1 include:mx.ovh.com " "-all"'],
                "TXT",
            ),
            ["v=spf1 include:mx.ovh.com -all"],
        )

    def test_cname_answer_cannot_satisfy_a_txt_contract(self) -> None:
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "TXT cité"):
            PREFLIGHT.parse_txt_lines(
                ["verification.relay-provider.net."],
                "réponse de vérification",
            )


class ScriptedConnection:
    def __init__(
        self,
        replies: bytes = b"",
        *,
        connect_error: OSError | None = None,
        tls_version: str = "TLSv1.3",
        cipher_name: str = "TLS_AES_256_GCM_SHA384",
        recv_chunk_size: int | None = None,
        bulk_reads: bool = False,
    ) -> None:
        self.replies = replies
        self.connect_error = connect_error
        self.tls_version = tls_version
        self.cipher_name = cipher_name
        self.recv_chunk_size = recv_chunk_size
        self.bulk_reads = bulk_reads
        self.recv_offset = 0
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.address = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def connect(self, address) -> None:
        self.address = address
        if self.connect_error is not None:
            raise self.connect_error

    def recv(self, maximum: int) -> bytes:
        if self.recv_offset >= len(self.replies):
            return b""
        line_end = self.replies.find(b"\n", self.recv_offset)
        natural_end = (
            len(self.replies)
            if self.bulk_reads or line_end < 0
            else line_end + 1
        )
        limit = maximum
        if self.recv_chunk_size is not None:
            limit = min(limit, self.recv_chunk_size)
        end = min(natural_end, self.recv_offset + limit)
        chunk = self.replies[self.recv_offset : end]
        self.recv_offset = end
        return chunk

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def close(self) -> None:
        self.closed = True

    def version(self) -> str:
        return self.tls_version

    def cipher(self) -> tuple[str, str, int]:
        return self.cipher_name, self.tls_version, 256


class ScriptedTlsContext:
    def __init__(self, outcome: ScriptedConnection | ssl.SSLError) -> None:
        self.outcome = outcome
        self.server_names: list[str] = []

    def wrap_socket(self, _connection, *, server_hostname: str):
        self.server_names.append(server_hostname)
        if isinstance(self.outcome, ssl.SSLError):
            raise self.outcome
        return self.outcome


class StartTlsTests(unittest.TestCase):
    @staticmethod
    def address(ip: str) -> tuple[int, int, int, tuple[str, int]]:
        return socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (ip, 587)

    @staticmethod
    def starttls_replies() -> bytes:
        return (
            b"220 relay ready\r\n"
            b"250-relay-provider\r\n"
            b"250 STARTTLS\r\n"
            b"220 begin tls\r\n"
        )

    @staticmethod
    def secured_replies(auth: bytes = b"AUTH PLAIN LOGIN") -> bytes:
        return (
            b"250-relay-provider\r\n"
            + b"250 "
            + auth
            + b"\r\n"
            + b"221 closing\r\n"
        )

    def probe_with(
        self,
        addresses,
        connections,
        contexts,
        *,
        timeout: float = 10.0,
    ):
        with (
            mock.patch.object(PREFLIGHT, "public_addresses", return_value=addresses),
            mock.patch.object(PREFLIGHT.socket, "socket", side_effect=connections),
            mock.patch.object(PREFLIGHT, "build_tls_context", side_effect=contexts),
        ):
            return PREFLIGHT.probe_starttls(valid_contract(), timeout)

    def test_multiline_smtp_reply_is_parsed(self) -> None:
        connection = ScriptedConnection(
            b"250-relay.example\r\n250-STARTTLS\r\n250 AUTH LOGIN\r\n"
        )
        code, lines = PREFLIGHT.read_smtp_reply(
            PREFLIGHT.SmtpLineReader(connection),
            PREFLIGHT.time.monotonic() + 10.0,
        )
        self.assertEqual(code, 250)
        self.assertEqual(lines, ["relay.example", "STARTTLS", "AUTH LOGIN"])

    def test_smtp_reply_requires_crlf_and_a_512_byte_line_limit(self) -> None:
        cases = (
            b"250 relay.example\n",
            b"250 relay\rexample\r\n",
            b"250 " + (b"x" * 507) + b"\r\n",
        )
        for response in cases:
            with self.subTest(length=len(response)):
                reader = PREFLIGHT.SmtpLineReader(ScriptedConnection(response))
                with self.assertRaises(PREFLIGHT.PreflightError):
                    PREFLIGHT.read_smtp_reply(
                        reader,
                        PREFLIGHT.time.monotonic() + 10.0,
                    )

    def test_empty_ehlo_greeting_is_rejected_without_native_error(self) -> None:
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "EHLO est vide"):
            PREFLIGHT.ehlo_capabilities([""])

    def test_smtp_reply_deadline_cannot_be_extended_by_a_slow_stream(self) -> None:
        connection = ScriptedConnection(
            b"250 relay.example\r\n",
            recv_chunk_size=1,
        )
        reader = PREFLIGHT.SmtpLineReader(connection)
        with mock.patch.object(
            PREFLIGHT.time,
            "monotonic",
            side_effect=[0.0, 2.0],
        ):
            with self.assertRaisesRegex(PREFLIGHT.PreflightError, "délai total"):
                PREFLIGHT.read_smtp_reply(reader, 1.0)

    def test_private_relay_address_is_rejected(self) -> None:
        def query(_name: str, record_type: str) -> list[str]:
            return ["127.0.0.1"] if record_type == "A" else []

        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "non publique"):
            PREFLIGHT.public_addresses("smtp.relay-provider.net", 587, query)

    def test_smtp_address_set_is_bounded(self) -> None:
        def query(_name: str, record_type: str) -> list[str]:
            if record_type != "A":
                return []
            return [f"8.8.8.{index}" for index in range(1, 10)]

        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "plus de huit"):
            PREFLIGHT.public_addresses("smtp.relay-provider.net", 587, query)

    def test_deadline_expiry_is_fail_closed(self) -> None:
        with mock.patch.object(PREFLIGHT.time, "monotonic", return_value=11.0):
            with self.assertRaisesRegex(PREFLIGHT.PreflightError, "délai total"):
                PREFLIGHT.remaining_timeout(10.0)

    def test_tls_context_rejects_environment_ca_overrides(self) -> None:
        with mock.patch.dict(
            PREFLIGHT.os.environ,
            {"SSL_CERT_FILE": "/tmp/unreviewed-ca.pem"},
        ):
            with self.assertRaisesRegex(PREFLIGHT.PreflightError, "interdits"):
                PREFLIGHT.build_tls_context()

    def test_probe_starttls_validates_transport_and_auth(self) -> None:
        plain = ScriptedConnection(self.starttls_replies())
        secured = ScriptedConnection(self.secured_replies())
        context = ScriptedTlsContext(secured)

        result = self.probe_with(
            [self.address("8.8.8.8")],
            [plain],
            [context],
        )

        self.assertEqual(result, ("8.8.8.8", "TLSv1.3", "TLS_AES_256_GCM_SHA384"))
        self.assertEqual(
            plain.sent,
            [b"EHLO surplasse.com\r\n", b"STARTTLS\r\n"],
        )
        self.assertEqual(
            secured.sent,
            [b"EHLO surplasse.com\r\n", b"QUIT\r\n"],
        )
        self.assertEqual(context.server_names, ["smtp.relay-provider.net"])

    def test_probe_starttls_rejects_missing_or_refused_upgrade(self) -> None:
        cases = (
            (
                "missing",
                b"220 relay ready\r\n250 relay-provider\r\n",
                "n'annonce pas STARTTLS",
            ),
            (
                "refused",
                (
                    b"220 relay ready\r\n"
                    b"250-relay-provider\r\n"
                    b"250 STARTTLS\r\n"
                    b"454 tls unavailable\r\n"
                ),
                "STARTTLS est refusé",
            ),
        )
        for label, replies, expected_message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(PREFLIGHT.PreflightError, expected_message):
                    self.probe_with(
                        [self.address("8.8.8.8")],
                        [ScriptedConnection(replies)],
                        [],
                    )

    def test_probe_starttls_falls_back_after_certificate_failure(self) -> None:
        first = ScriptedConnection(self.starttls_replies())
        second = ScriptedConnection(self.starttls_replies())
        secured = ScriptedConnection(self.secured_replies())

        result = self.probe_with(
            [self.address("8.8.8.8"), self.address("8.8.4.4")],
            [first, second],
            [
                ScriptedTlsContext(ssl.SSLCertVerificationError("hostname mismatch")),
                ScriptedTlsContext(secured),
            ],
        )

        self.assertEqual(result[0], "8.8.4.4")
        self.assertTrue(first.closed)

    def test_probe_starttls_rejects_post_tls_ehlo_or_auth_drift(self) -> None:
        cases = (
            ("ehlo-refused", b"550 denied\r\n", "EHLO après STARTTLS"),
            (
                "auth-absent",
                b"250-relay-provider\r\n250 SIZE 1000\r\n",
                "AUTH PLAIN ou LOGIN",
            ),
            (
                "auth-incompatible",
                self.secured_replies(b"AUTH XOAUTH2"),
                "AUTH PLAIN ou LOGIN",
            ),
            (
                "auth-empty",
                self.secured_replies(b"AUTH"),
                "AUTH PLAIN ou LOGIN",
            ),
        )
        for label, secured_replies, expected_message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(PREFLIGHT.PreflightError, expected_message):
                    self.probe_with(
                        [self.address("8.8.8.8")],
                        [ScriptedConnection(self.starttls_replies())],
                        [ScriptedTlsContext(ScriptedConnection(secured_replies))],
                    )

    def test_probe_rejects_unsolicited_bytes_after_secured_ehlo(self) -> None:
        secured = ScriptedConnection(
            self.secured_replies()[:-len(b"221 closing\r\n")]
            + b"535 unsolicited\r\n",
            bulk_reads=True,
        )
        with self.assertRaisesRegex(PREFLIGHT.PreflightError, "inattendus"):
            self.probe_with(
                [self.address("8.8.8.8")],
                [ScriptedConnection(self.starttls_replies())],
                [ScriptedTlsContext(secured)],
            )

    def test_probe_starttls_enforces_one_deadline_across_addresses(self) -> None:
        addresses = [self.address("8.8.8.8"), self.address("8.8.4.4")]
        connections = [
            ScriptedConnection(connect_error=OSError("first unavailable")),
            ScriptedConnection(),
        ]
        with (
            mock.patch.object(PREFLIGHT, "public_addresses", return_value=addresses),
            mock.patch.object(PREFLIGHT.socket, "socket", side_effect=connections),
            mock.patch.object(PREFLIGHT.time, "monotonic", side_effect=[0.0, 0.1, 2.0]),
        ):
            with self.assertRaisesRegex(PREFLIGHT.PreflightError, "délai total"):
                PREFLIGHT.probe_starttls(valid_contract(), 1.0)

    def test_probe_deadline_starts_after_address_resolution(self) -> None:
        connection = ScriptedConnection(self.starttls_replies())
        secured = ScriptedConnection(self.secured_replies())
        observed = []

        def addresses(_host: str, _port: int):
            observed.append("dns")
            return [self.address("8.8.8.8")]

        def monotonic() -> float:
            observed.append("clock")
            return 0.0

        with (
            mock.patch.object(PREFLIGHT, "public_addresses", side_effect=addresses),
            mock.patch.object(PREFLIGHT.socket, "socket", return_value=connection),
            mock.patch.object(
                PREFLIGHT,
                "build_tls_context",
                return_value=ScriptedTlsContext(secured),
            ),
            mock.patch.object(PREFLIGHT.time, "monotonic", side_effect=monotonic),
        ):
            PREFLIGHT.probe_starttls(valid_contract(), 10.0)
        self.assertEqual(observed[:2], ["dns", "clock"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
