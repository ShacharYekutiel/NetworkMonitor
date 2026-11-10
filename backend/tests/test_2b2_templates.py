"""[2b-2] Template library: schema validation (every error class), the built-in library
(zero errors, 19 templates, identical behaviour of the nine 2b-1 templates), user overrides,
reload with errors, apply of a user template."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from netmon.templates import registry
from netmon.templates.builtin import TEMPLATES, get_template, suggest
from netmon.templates.library import VENDOR_PREFIX, load_dir, strip_2b2, validate_template
from tests.conftest import make_device, requires_db

FIXTURE = Path(__file__).parent / "fixtures" / "templates_2b1.json"
LIB = registry.BUILTIN_DIR

MINIMAL = {
    "schema": 1,
    "id": "acme_ups",
    "name": {"en": "ACME UPS"},
    "priority": 30,
    "match": [{"requires_snmp": True, "enterprises": [99999]}],
    "entries": [{"kind": "sensor", "type": "ping", "name": {"en": "Ping"}}],
}


def _err(data: Any, stem: str = "acme_ups") -> tuple[str, str]:
    from netmon.templates.library import TemplateError

    with pytest.raises(TemplateError) as e:
        validate_template(data, stem)
    return e.value.path, e.value.message


def _with(**kw: Any) -> dict[str, Any]:
    return {**json.loads(json.dumps(MINIMAL)), **kw}


def _entry(**kw: Any) -> dict[str, Any]:
    return _with(entries=[kw])


# ------------------------------------------------------------------ built-in library
def test_builtin_library_loads_without_errors() -> None:
    templates, errors = load_dir(LIB, "builtin")
    assert errors == []
    assert len(templates) == 19
    for t in templates:
        assert t["source"] == "builtin" and t["overrides_builtin"] is False
        assert t["file"] == f"{Path(t['file']).parent.as_posix()}/{t['id']}.yaml"
        assert t["name"]["en"] and t["name"]["he"]
        assert t["description"]["en"] and t["description"]["he"]
        for e in t["entries"]:
            oids = []
            if e["kind"] == "sensor":
                oids = [e["config"].get("oid"), e["config"].get("percent_of_oid")]
            elif e["kind"] == "snmp_table":
                oids = [e["walk_oid"]]
            if any(isinstance(o, str) and o.startswith(VENDOR_PREFIX) for o in oids):
                assert e["confidence"] in ("high", "medium", "low"), (t["id"], e)
    new = {t["id"] for t in templates} - {t["id"] for t in json.loads(FIXTURE.read_text())}
    assert new == {
        "aruba_cx",
        "aruba_wifi",
        "checkpoint_gaia",
        "fortinet_fortigate",
        "cisco_ios",
        "paloalto_panos",
        "mikrotik_routeros",
        "apc_ups",
        "hpe_ilo",
        "dell_idrac",
    }
    assert all(not t["verified"] for t in templates if t["id"] in new)
    verified = {t["id"] for t in templates if t["verified"]}
    assert verified == {"generic_ping", "http_server", "generic_snmp"}


def test_converted_templates_identical_to_2b1() -> None:
    """Identity proof: the nine 2b-1 templates without the 2b-2 keys equal the fixture that was
    generated from the 2b-1 builtin.TEMPLATES before its data was removed (order included)."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # deliberate change since 2b-1 (owner's FF 5945 test): Comware reports 65535 for a CPU /
    # memory / temperature column an entity does not have; those rows are filtered out
    comware = next(t for t in fixture if t["id"] == "hpe_comware")
    for e in comware["entries"][2:5]:
        e["filters"] = [{"oid": e["walk_oid"], "in": None, "not_in": [65535]}]
    ids = [t["id"] for t in fixture]
    loaded = [strip_2b2(t) for t in TEMPLATES if t["id"] in ids]
    assert [t["id"] for t in loaded] == ids
    assert json.loads(json.dumps(loaded)) == fixture


def test_library_files_style() -> None:
    """One template per file, OIDs quoted (a YAML string), stem == id, folder in the list."""
    folders = {p.name for p in LIB.iterdir() if p.is_dir()}
    assert folders == {
        "generic",
        "ubiquiti",
        "opnsense",
        "hpe",
        "aruba",
        "checkpoint",
        "fortinet",
        "cisco",
        "paloalto",
        "mikrotik",
        "apc",
        "dell",
    }
    for path in LIB.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "\t" not in text
        data = yaml.safe_load(text)
        assert data["id"] == path.stem


# ------------------------------------------------------------------ validation errors
def test_minimal_template_defaults() -> None:
    t = validate_template(MINIMAL, "acme_ups")
    assert t["name"] == {"en": "ACME UPS", "he": "ACME UPS"}
    assert t["description"] == {"en": "", "he": ""}
    assert (t["version"], t["verified"], t["verified_note"], t["vendor"]) == (1, False, "", None)
    assert t["requires_snmp"] is False
    assert t["match"][0] == {
        "device_classes": None,
        "vendors": None,
        "requires_snmp": True,
        "any_open_ports": None,
        "enterprises": [99999],
        "sys_object_id_prefixes": None,
        "sys_descr_regex": None,
    }
    assert t["entries"][0] == {
        "kind": "sensor",
        "type": "ping",
        "name": {"en": "Ping", "he": "Ping"},
        "config": {},
        "channels": [],
        "interval_s": None,
        "probe": False,
        "when": [],
        "confidence": None,
        "notes": "",
    }


def test_interfaces_and_table_defaults() -> None:
    from netmon.templates.library import IF_EXCLUDE

    t = validate_template(
        _with(
            entries=[
                {"kind": "snmp_interfaces", "max": 4},
                {"kind": "snmp_interfaces", "max": 4, "exclude_name_regex": None},
                {
                    "kind": "snmp_table",
                    "walk_oid": ".1.3.6.1.2.1.99.1.1.1.4",
                    "name": {"en": "{label}"},
                    "config": {
                        "value_type": "enum",
                        "value_map": {1: {"status": "up"}, "2": {"status": "down", "text": "x"}},
                    },
                },
            ]
        ),
        "acme_ups",
    )
    i0, i1, tab = t["entries"]
    assert i0["exclude_name_regex"] == IF_EXCLUDE and i1["exclude_name_regex"] is None
    assert (i0["oper_up_only"], i0["exclude_if_types"], i0["channels"]) == (True, [24], [])
    assert tab["walk_oid"] == "1.3.6.1.2.1.99.1.1.1.4"
    assert (tab["max"], tab["skip_zero"], tab["filters"], tab["label_oid"]) == (8, False, [], None)
    assert list(tab["config"]["value_map"]) == ["1", "2"]  # YAML int keys -> decimal strings


@pytest.mark.parametrize(
    ("data", "path", "message"),
    [
        ([1, 2], "", "not a mapping"),
        ({"id": "acme_ups"}, "", "missing key: schema"),
        (_with(schema=2), "schema", "unsupported schema version: 2"),
        (_with(prio=1), "", "unknown key: prio"),
        ({k: v for k, v in MINIMAL.items() if k != "priority"}, "", "missing key: priority"),
        (_with(id="other_id"), "id", 'id must match the file name (expected "acme_ups")'),
        (_with(id="Bad-Id"), "id", "invalid value: id must match ^[a-z][a-z0-9_]{1,31}$"),
        (_with(priority=1001), "priority", "invalid value: must be 0-1000"),
        (_with(priority="50"), "priority", "invalid value: must be an integer"),
        (_with(verified="yes"), "verified", "invalid value: must be true or false"),
        (_with(name={"he": "x"}), "name", "missing key: en"),
        (_with(match=[]), "match", "invalid value: must be a list of 1-20 items"),
        (
            _with(match=[{"device_classes": ["firewall"]}]),
            "match[0].device_classes[0]",
            "invalid value: unknown class firewall",
        ),
        (_with(match=[{"sys_descr_regex": "("}]), "match[0].sys_descr_regex", None),
        (
            _with(match=[{"sys_object_id_prefixes": [1.10]}]),
            "match[0].sys_object_id_prefixes[0]",
            "invalid OID: 1.1",
        ),
        (_with(match=[{"colour": 1}]), "match[0]", "unknown key: colour"),
        (
            _entry(kind="sensor", type="foo", name={"en": "x"}),
            "entries[0].type",
            "unknown sensor type: foo",
        ),
        (_entry(kind="gadget"), "entries[0].kind", None),
        (
            _entry(kind="snmp_table", walk_oid="abc", name={"en": "x"}, config={}),
            "entries[0].walk_oid",
            "invalid OID: abc",
        ),
        (
            _entry(kind="snmp_table", walk_oid=1.10, name={"en": "x"}, config={}),
            "entries[0].walk_oid",
            "invalid OID: 1.1",
        ),
        (_entry(kind="snmp_interfaces"), "entries[0]", "missing key: max"),
        (
            _entry(kind="snmp_interfaces", max=1, include_name_regex="(["),
            "entries[0].include_name_regex",
            None,
        ),
        (
            _entry(kind="sensor", type="ping", name={"en": "x"}, config={"count": 99}),
            "entries[0].config",
            "invalid config: count: must be <= 20",
        ),
        (
            _entry(kind="sensor", type="ping", name={"en": "x"}, channels=[{"key": "nope"}]),
            "entries[0].channels",
            "invalid channels: 0.key: unknown channel 'nope'",
        ),
        (
            _entry(
                kind="sensor",
                type="snmp_custom",
                name={"en": "x"},
                config={"oid": "1.3.6.1.4.1.9.1.0"},
            ),
            "entries[0]",
            "confidence required for vendor OIDs",
        ),
        (
            _entry(kind="snmp_table", walk_oid="1.3.6.1.4.1.9.9.1", name={"en": "x"}, config={}),
            "entries[0]",
            "confidence required for vendor OIDs",
        ),
        (
            _entry(
                kind="sensor",
                type="snmp_custom",
                name={"en": "x"},
                config={
                    "oid": "1.3.6.1.2.1.1.3.0",
                    "value_type": "enum",
                    "value_map": {"up": {"status": "up"}},
                },
            ),
            "entries[0].config.value_map",
            "value_map keys must be integers",
        ),
        (
            _entry(kind="sensor", type="snmp_custom", name={"en": "x"}, config={"oid": 1.3}),
            "entries[0].config.oid",
            "invalid OID: 1.3",
        ),
        (
            _entry(
                kind="sensor", type="ping", name={"en": "x"}, when=[{"oid": "1.3.6.1", "in": "1"}]
            ),
            "entries[0].when[0].in",
            "invalid value: must be a list of integers",
        ),
        (
            _entry(
                kind="snmp_table",
                walk_oid="1.3.6.1.2.1.1",
                name={"en": "x"},
                config={"oid": "1.3.6"},
            ),
            "entries[0].config.oid",
            "invalid config: oid is set by the table",
        ),
        (
            _entry(
                kind="snmp_table",
                walk_oid="1.3.6.1.2.1.1",
                name={"en": "x"},
                config={"percent_base": "sum"},
            ),
            "entries[0].config",
            'invalid config: percent_base: "sum" requires percent_of_oid',
        ),
        (
            _entry(kind="sensor", type="ping", name={"en": "x"}, confidence="certain"),
            "entries[0].confidence",
            None,
        ),
        (
            _entry(kind="sensor", type="ping", name={"en": "x"}, interval_s=5),
            "entries[0].interval_s",
            "invalid value: must be 10-86400",
        ),
        (
            _entry(kind="sensor", type="ping", name={"en": "x"}, notes="x" * 501),
            "entries[0].notes",
            None,
        ),
    ],
)
def test_validation_errors(data: Any, path: str, message: str | None) -> None:
    got_path, got_message = _err(data)
    assert got_path == path
    if message is not None:
        assert got_message == message
    else:
        assert got_message.startswith(("invalid value", "invalid regex")), got_message


def test_table_percent_base_sum_ok() -> None:
    t = validate_template(
        _entry(
            kind="snmp_table",
            walk_oid="1.3.6.1.4.1.9.9.48.1.1.1.5",
            percent_of_column="1.3.6.1.4.1.9.9.48.1.1.1.6",
            name={"en": "x"},
            config={"value_type": "gauge", "unit": "%", "percent_base": "sum"},
            confidence="high",
        ),
        "acme_ups",
    )
    assert t["entries"][0]["config"]["percent_base"] == "sum"


def _write(root: Path, rel: str, data: Any) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    elif rel.endswith(".json"):
        p.write_text(json.dumps(data), encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_load_dir_file_errors(tmp_path: Path) -> None:
    _write(tmp_path, "a/acme_ups.yaml", MINIMAL)
    _write(tmp_path, "b/acme_ups.yml", MINIMAL)  # duplicate
    _write(tmp_path, "c/broken.yaml", "schema: 1\nid: [unclosed\n")
    _write(tmp_path, "c/broken_json.json", "{nope")
    _write(tmp_path, "c/big.yaml", "# " + "x" * (256 * 1024))
    _write(tmp_path, "c/ignored.txt", "not a template")
    _write(tmp_path, "d/upper.YAML", {**MINIMAL, "id": "upper"})
    templates, errors = load_dir(tmp_path, "user")
    assert [t["id"] for t in templates] == ["acme_ups", "upper"]
    assert templates[0]["file"] == "a/acme_ups.yaml" and templates[0]["source"] == "user"
    by_file = {e["file"]: e for e in errors}
    assert (
        by_file["b/acme_ups.yml"]["message"] == 'duplicate id "acme_ups" (also in a/acme_ups.yaml)'
    )
    assert by_file["b/acme_ups.yml"]["template_id"] == "acme_ups"
    assert by_file["c/broken.yaml"]["message"].startswith("invalid YAML: ")
    assert (
        by_file["c/broken.yaml"]["template_id"] is None and by_file["c/broken.yaml"]["path"] == ""
    )
    assert by_file["c/broken_json.json"]["message"].startswith("invalid JSON: ")
    assert by_file["c/big.yaml"]["message"] == "file too large (max 256 KiB)"
    assert set(by_file) == {"b/acme_ups.yml", "c/broken.yaml", "c/broken_json.json", "c/big.yaml"}
    assert all(e["source"] == "user" for e in errors)
    assert load_dir(tmp_path / "missing", "user") == ([], [])


def test_load_dir_too_many_files(tmp_path: Path) -> None:
    for i in range(203):
        _write(tmp_path, f"t/t{i:03d}.yaml", {**MINIMAL, "id": f"t{i:03d}"})
    templates, errors = load_dir(tmp_path, "user")
    assert len(templates) == 200
    assert [e["message"] for e in errors] == ["too many files"]


# ------------------------------------------------------------------ API: override and reload
@requires_db
async def test_user_override_and_reload(
    rt, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:  # type: ignore[no-untyped-def]
    udir = registry.user_dir(rt)
    comware = yaml.safe_load((LIB / "hpe" / "hpe_comware.yaml").read_text(encoding="utf-8"))
    generic = yaml.safe_load((LIB / "generic" / "generic_snmp.yaml").read_text(encoding="utf-8"))
    generic["entries"][0]["type"] = "foo"
    noconf = _with(
        id="noconf",
        entries=[
            {"kind": "sensor", "type": "ping", "name": {"en": "a"}},
            {"kind": "sensor", "type": "ping", "name": {"en": "b"}},
            {
                "kind": "sensor",
                "type": "snmp_custom",
                "name": {"en": "c"},
                "config": {"oid": "1.3.6.1.4.1.1.1.0"},
            },
        ],
    )
    files = {
        "override/hpe_comware.yaml": {**comware, "priority": 55},
        "acme/acme_ups.yaml": MINIMAL,
        "json/acme_json.json": {**MINIMAL, "id": "acme_json"},
        "dup/acme_ups.yml": MINIMAL,
        "bad/broken.yaml": "entries: [\n",
        "bad/unknown_key.yaml": {**MINIMAL, "id": "unknown_key", "prio": 1},
        "bad/wrong_name.yaml": {**MINIMAL, "id": "other_id"},
        "bad/badoid.yaml": _with(
            id="badoid",
            entries=[{"kind": "snmp_table", "walk_oid": "abc", "name": {"en": "x"}, "config": {}}],
        ),
        "bad/noconf.yaml": noconf,
        "override/generic_snmp.yaml": generic,
    }
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    try:
        for rel, data in files.items():
            _write(udir, rel, data)
        r = await admin.post("/api/device-templates/reload")
        assert r.status_code == 200, r.text
        lib = r.json()
        assert (lib["builtin_count"], lib["user_count"], lib["overridden"]) == (
            19,
            3,
            ["hpe_comware"],
        )
        assert lib["user_dir"] == str(udir.resolve())
        errs = {e["file"]: e for e in lib["errors"]}
        assert len(errs) == 7
        assert (
            errs["dup/acme_ups.yml"]["message"]
            == 'duplicate id "acme_ups" (also in acme/acme_ups.yaml)'
        )
        assert errs["bad/broken.yaml"]["message"].startswith("invalid YAML:")
        assert errs["bad/broken.yaml"]["template_id"] is None
        assert (errs["bad/unknown_key.yaml"]["message"], errs["bad/unknown_key.yaml"]["path"]) == (
            "unknown key: prio",
            "",
        )
        assert (
            errs["bad/wrong_name.yaml"]["message"]
            == 'id must match the file name (expected "wrong_name")'
        )
        assert (errs["bad/badoid.yaml"]["message"], errs["bad/badoid.yaml"]["path"]) == (
            "invalid OID: abc",
            "entries[0].walk_oid",
        )
        assert (errs["bad/noconf.yaml"]["message"], errs["bad/noconf.yaml"]["path"]) == (
            "confidence required for vendor OIDs",
            "entries[2]",
        )
        assert errs["override/generic_snmp.yaml"]["message"] == "unknown sensor type: foo"
        reloaded = [
            m
            for m in [q.get_nowait() for _ in range(q.qsize())]
            if m["type"] == "templates.reloaded"
        ]
        assert len(reloaded) == 1
        assert reloaded[0]["data"]["count"] == 21 and reloaded[0]["data"]["error_count"] == 7

        body = (await admin.get("/api/device-templates")).json()
        items = {t["id"]: t for t in body["items"]}
        assert (items["hpe_comware"]["source"], items["hpe_comware"]["overrides_builtin"]) == (
            "user",
            True,
        )
        assert items["hpe_comware"]["priority"] == 55
        assert items["hpe_comware"]["file"] == "override/hpe_comware.yaml"
        assert items["generic_snmp"]["source"] == "builtin"
        assert items["acme_ups"]["source"] == "user" and "acme_json" in items
        assert [t["id"] for t in body["items"]].index("hpe_comware") < [
            t["id"] for t in body["items"]
        ].index("apc_ups")
        assert len(body["library"]["errors"]) == 7
        vbody = (await viewer.get("/api/device-templates")).json()
        assert vbody["library"]["errors"] == [] and vbody["library"]["user_dir"] == ""
        assert (await viewer.post("/api/device-templates/reload")).status_code == 403
        assert (await admin.get("/api/device-templates/acme_ups")).json()["source"] == "user"

        # apply a user template; discovery import validates against the current registry
        dev = await make_device(admin, name="ups", host="127.0.0.1")
        r = await admin.post(
            f"/api/devices/{dev['id']}/apply-template", json={"template_id": "acme_ups"}
        )
        assert r.status_code == 200, r.text
        assert [c["name"] for c in r.json()["created"]] == ["Ping"]

        # remove the override -> back to built-in; suggestion still hpe_comware
        (udir / "override" / "hpe_comware.yaml").unlink()
        lib = (await admin.post("/api/device-templates/reload")).json()
        assert lib["overridden"] == []
        t = (await admin.get("/api/device-templates/hpe_comware")).json()
        assert (t["source"], t["priority"]) == ("builtin", 50)
        assert (
            suggest(
                device_class="switch",
                vendor="h3c",
                snmp_ok=True,
                open_ports=[],
                sys_object_id="1.3.6.1.4.1.25506.11.1.177",
            )
            == "hpe_comware"
        )
    finally:
        rt.hub.listeners.remove(q)
        shutil.rmtree(udir, ignore_errors=True)
        await registry.reload(rt, broadcast=False)
    assert get_template("acme_ups") is None and len(TEMPLATES) == 19


def test_yaml_pitfalls(tmp_path: Path) -> None:
    """Unquoted 1.10 is a YAML float -> invalid OID; unquoted `on` is a boolean."""
    base = yaml.safe_dump(MINIMAL)
    _write(tmp_path, "a/acme_ups.yaml", base)
    float_oid = base.replace("acme_ups", "float_oid").replace(
        "entries:\n",
        "entries:\n- {kind: snmp_table, walk_oid: 1.10, name: {en: x}, config: {}}\n",
        1,
    )
    _write(tmp_path, "b/float_oid.yaml", float_oid)
    bool_name = base.replace("acme_ups", "bool_name").replace("en: ACME UPS", "en: on")
    _write(tmp_path, "c/bool_name.yaml", bool_name)
    templates, errors = load_dir(tmp_path, "user")
    assert [t["id"] for t in templates] == ["acme_ups"]
    by = {e["file"]: e for e in errors}
    assert (by["b/float_oid.yaml"]["message"], by["b/float_oid.yaml"]["path"]) == (
        "invalid OID: 1.1",
        "entries[0].walk_oid",
    )
    assert by["c/bool_name.yaml"]["path"] == "name.en"


@pytest.mark.parametrize(
    ("scale", "precision", "factor"),
    [
        (9, 0, 1.0),  # units
        (9, 3, 0.001),  # Aruba CX: 23000 -> 23.000
        (8, 0, 0.001),  # milli
        (8, 1, 0.0001),
        (10, 0, 1000.0),  # kilo
        (9, -2, 100.0),
        (None, None, 1.0),  # columns not readable
        (99, 42, 1.0),  # out of range
    ],
)
def test_entity_sensor_multiplier(scale: Any, precision: Any, factor: float) -> None:
    from netmon.templates.apply import entity_sensor_multiplier

    assert entity_sensor_multiplier(scale, precision) == pytest.approx(factor)
