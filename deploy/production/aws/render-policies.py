#!/usr/bin/env python3
"""Fill the IAM policy templates in this folder for the AWS console / CLI.

    python3 render-policies.py --account 111122223333 --region us-east-1 \\
        --profile us.anthropic.<model> [--light-profile us.anthropic.<light-model>] \\
        [--ra-region us-east-1 --trust-anchor-id <uuid | ARN>] [--cert-cn netmonitor.mnidom.net] \\
        [--kb-id ABCDEFGHIJ] [--role-name netmon-bedrock] [--out ./out]

Writes one filled JSON file per template into --out (default ./out). The underlying
foundation-model id is derived from the inference profile id by removing its geography
prefix (us. / eu. / apac. / global. ...). Entries for the light model are dropped when
--light-profile is not given; templates whose placeholders are not all given are skipped
with a message (e.g. the trust policy without --trust-anchor-id). Standard library only,
so it also runs on a Windows admin PC. Nothing is sent anywhere.

--trust-anchor-id takes the bare id (the uuid) or the full trust-anchor ARN as the console
shows it; with an ARN, its region and account must match --ra-region (or --region) and
--account. --cert-cn is the subject CN of the certificate issued for the server: the trust
policy only matches a certificate whose subject CN is exactly that value. Check it with
    openssl x509 -in netmon.cer -noout -subject          (Linux)
    certutil -dump netmon.cer                              (Windows, "Subject:")
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLACEHOLDER = re.compile(r"<([A-Z_]+)>")
GEO_PREFIX = re.compile(r"^(?:[a-z]{2,6})\.(?=[a-z0-9-]+\.)")


def model_of(profile: str) -> str:
    """us.anthropic.claude-x-v1:0 -> anthropic.claude-x-v1:0 (ARNs are left alone)."""
    if profile.startswith("arn:"):
        raise SystemExit("pass the inference profile ID (e.g. us.anthropic....), not an ARN")
    return GEO_PREFIX.sub("", profile, count=1)


TRUST_ANCHOR_ARN = re.compile(
    r"^arn:(?P<partition>aws[a-z-]*):rolesanywhere:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
    r":trust-anchor/(?P<id>[A-Za-z0-9-]+)$"
)
TRUST_ANCHOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


def trust_anchor_id(value: str, account: str, ra_region: str) -> str:
    """The bare trust anchor id from `value` (a bare id or the full ARN).

    A full ARN used to be pasted into the template as-is, giving a doubled ARN
    (...:trust-anchor/arn:aws:rolesanywhere:...:trust-anchor/<id>) that never matches."""
    value = value.strip()
    m = TRUST_ANCHOR_ARN.match(value)
    if m:
        if m.group("account") != account:
            raise SystemExit(
                f"--trust-anchor-id: the ARN is in account {m.group('account')}, "
                f"but --account is {account}"
            )
        if m.group("region") != ra_region:
            raise SystemExit(
                f"--trust-anchor-id: the ARN is in region {m.group('region')}, but the Roles "
                f"Anywhere region is {ra_region} (set --ra-region {m.group('region')} if the "
                "trust anchor is really there)"
            )
        return m.group("id")
    if value.startswith("arn:") or ":" in value or "/" in value:
        raise SystemExit(
            "--trust-anchor-id: pass the trust anchor id (uuid) or its full ARN "
            "arn:aws:rolesanywhere:<region>:<account>:trust-anchor/<id>; got " + repr(value)
        )
    if not TRUST_ANCHOR_ID.match(value):
        raise SystemExit(f"--trust-anchor-id: not a trust anchor id: {value!r}")
    return value


def cert_cn(value: str) -> str:
    """The certificate subject CN the trust policy expects: the CN *value* only."""
    cn = value.strip()
    if not cn:
        raise SystemExit("--cert-cn must not be empty")
    if re.match(r"(?i)^cn\s*=", cn):
        raise SystemExit(f"--cert-cn: pass only the CN value (without 'CN='): {cn[cn.index('=') + 1:].strip()!r}")
    if cn != value or re.search(r"[\s,=/\"\\*]", cn) or len(cn) > 64:
        raise SystemExit(
            f"--cert-cn: {value!r} is not a plain subject CN (one name such as "
            "netmonitor.example.com: no spaces, commas, '=', '/', '*' or quotes; at most 64 "
            "characters). It must equal the subject CN of the issued certificate exactly."
        )
    return cn


def fill(node, values: dict[str, str]):
    """Substitute placeholders; list entries with an unknown LIGHT_* placeholder are dropped."""
    if isinstance(node, dict):
        return {k: fill(v, values) for k, v in node.items()}
    if isinstance(node, list):
        out = []
        for item in node:
            if isinstance(item, str) and any(
                m.startswith("LIGHT_") and m not in values for m in PLACEHOLDER.findall(item)
            ):
                continue
            out.append(fill(item, values))
        return out
    if isinstance(node, str):
        return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), node)
    return node


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True, help="12-digit AWS account id")
    ap.add_argument("--region", required=True, help="Bedrock region the app calls (us-east-1 / il-central-1)")
    ap.add_argument("--profile", required=True, help="inference profile id, e.g. us.anthropic.<model>")
    ap.add_argument("--light-profile", help="optional cheaper model's inference profile id")
    ap.add_argument("--ra-region", help="IAM Roles Anywhere region (default: --region)")
    ap.add_argument("--trust-anchor-id", help="Roles Anywhere trust anchor: the id (uuid) or the full ARN")
    ap.add_argument("--cert-cn", default="netmonitor.mnidom.net", help="subject CN of the issued certificate (must match it exactly)")
    ap.add_argument("--kb-id", help="Bedrock Knowledge Base id (only with the Knowledge Base)")
    ap.add_argument("--role-name", default="netmon-bedrock", help="IAM role name (endpoint policy)")
    ap.add_argument("--out", default="out", help="output folder (default ./out)")
    a = ap.parse_args(argv)

    if not re.fullmatch(r"\d{12}", a.account):
        raise SystemExit("--account must be 12 digits")
    if not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-\d", a.region):
        raise SystemExit(f"not an AWS region: {a.region}")
    values = {
        "ACCOUNT_ID": a.account,
        "REGION": a.region,
        "PROFILE_ID": a.profile,
        "MODEL_ID": model_of(a.profile),
        "RA_REGION": a.ra_region or a.region,
        "CERT_CN": cert_cn(a.cert_cn),
        "ROLE_NAME": a.role_name,
    }
    if a.light_profile:
        values["LIGHT_PROFILE_ID"] = a.light_profile
        values["LIGHT_MODEL_ID"] = model_of(a.light_profile)
    if a.ra_region and not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-\d", a.ra_region):
        raise SystemExit(f"not an AWS region: {a.ra_region}")
    if a.trust_anchor_id:
        values["TRUST_ANCHOR_ID"] = trust_anchor_id(
            a.trust_anchor_id, a.account, values["RA_REGION"]
        )
    if a.kb_id:
        values["KB_ID"] = a.kb_id

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for tpl in sorted(HERE.glob("*.json")):
        doc = fill(json.loads(tpl.read_text(encoding="utf-8")), values)
        text = json.dumps(doc, indent=2) + "\n"
        missing = sorted(set(PLACEHOLDER.findall(text)))
        if missing:
            print(f"skipped {tpl.name}: needs {', '.join('<' + m + '>' for m in missing)}")
            continue
        (out / tpl.name).write_text(text, encoding="utf-8")
        print(f"wrote {out / tpl.name}")
        written += 1
        if "TRUST_ANCHOR_ID" in tpl.read_text(encoding="utf-8"):
            print(
                f"  trust anchor: arn:aws:rolesanywhere:{values['RA_REGION']}:"
                f"{values['ACCOUNT_ID']}:trust-anchor/{values['TRUST_ANCHOR_ID']}\n"
                f"  expects certificate subject CN = {values['CERT_CN']}\n"
                "  (must equal the subject CN of the issued certificate exactly; check with\n"
                "   'openssl x509 -in <cert> -noout -subject' or 'certutil -dump <cert>')"
            )
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
