"""[profiles] Pure parts of connection profiles, exhaustively: `resolve` / `origin` for every
combination per level, the request-mode table of api.md, the discovery import decisions
(`snmp_choice`, `ssh_choice`) and the discovery credential order."""

from __future__ import annotations

import itertools
import secrets
from collections.abc import Iterator
from typing import Any

import pytest

from netmon.discovery import importer
from netmon.discovery.options import (
    build_credentials,
    build_ssh_candidates,
    merge_options,
    probe_view,
    public_options,
    scan_snapshot,
)
from netmon.services import conn_profiles as cp
from netmon.services import secretbox
from netmon.services.conn_profiles import ChainNode, ModeRequestError, Profile


@pytest.fixture
def key() -> Iterator[str]:
    saved = secretbox.state()
    k = secrets.token_urlsafe(32)
    secretbox.configure(k, plaintext_copy=True)
    yield k
    secretbox.restore(saved)


SNMP_P = Profile(5, "snmp", "Core v2", {"version": "v2c", "community": "prof"})
SSH_P = Profile(6, "ssh", "Backup", {"username": "u", "password_enc": "x"})
PROFILES = {5: SNMP_P, 6: SSH_P}
MANUAL = {"snmp": {"version": "v2c", "community": "own"}, "ssh": {"username": "m"}}
PID = {"snmp": 5, "ssh": 6}
LEVEL_MODES = ("manual", "profile", "inherit", "both")


def node(kind: str, type_: str, id_: int, mode: str, name: str | None = None) -> ChainNode:
    creds = MANUAL[kind] if mode in ("manual", "both") else None
    pid = PID[kind] if mode in ("profile", "both") else None
    return ChainNode(type_, id_, creds, pid, name)


# ------------------------------------------------------------------ resolve
@pytest.mark.parametrize("kind", ["snmp", "ssh"])
def test_resolve_every_combination(kind: str) -> None:
    """device x group x parent x grandparent, each manual / profile / inherit / both."""
    for modes in itertools.product(LEVEL_MODES, repeat=4):
        chain = [node(kind, "device", 1, modes[0])] + [
            node(kind, "group", 10 + i, m, f"g{i}") for i, m in enumerate(modes[1:])
        ]
        res = cp.resolve(kind, chain, PROFILES)
        first = next((i for i, m in enumerate(modes) if m != "inherit"), None)
        if first is None:
            assert res.mode is None and res.node is None, modes
            assert res.source == ("default" if kind == "snmp" else "none")
            assert res.group_id is None
            continue
        decider = chain[first]
        assert res.node == decider, modes
        assert res.source == ("device" if first == 0 else "group")
        assert res.group_id == (None if first == 0 else decider.id)
        if modes[first] in ("profile", "both"):  # Q2: the profile wins over dormant data
            assert res.mode == "profile" and res.profile_id == PID[kind]
            assert res.profile == PROFILES[PID[kind]] and res.stored == PROFILES[PID[kind]].data
        else:
            assert res.mode == "manual" and res.stored == MANUAL[kind]
            assert res.profile_id is None


def test_fallbacks_and_effective(key: str) -> None:
    res = cp.resolve("snmp", [ChainNode("device", 1)], {})
    assert res.source == "default"
    assert cp.effective(res)["community"] == "public" and cp.effective(res)["version"] == "v2c"
    res = cp.resolve("ssh", [ChainNode("device", 1)], {})
    assert res.source == "none" and cp.effective(res) is None
    # manual SNMP data is opened with the node owner; profile data with the profile owner
    sealed = secretbox.seal_snmp({"version": "v2c", "community": "dev-c"}, "device", 1)
    res = cp.resolve("snmp", [ChainNode("device", 1, sealed)], {})
    assert cp.effective(res)["community"] == "dev-c"
    pdata = secretbox.seal_conn_profile_snmp({"version": "v2c", "community": "prof-c"}, 7)
    assert "community" not in pdata and pdata["community_enc"]  # encrypted only
    p = Profile(7, "snmp", "p", pdata)
    res = cp.resolve("snmp", [ChainNode("device", 1, sealed, 7)], {7: p})
    assert cp.effective(res)["community"] == "prof-c"  # dormant "dev-c" unused
    # a missing / wrong-kind profile never falls back to the default community
    res = cp.resolve("snmp", [ChainNode("device", 1, None, 99)], {})
    assert res.mode == "profile" and cp.effective(res)["community"] is None
    res = cp.resolve("snmp", [ChainNode("device", 1, None, 6)], PROFILES)
    assert res.stored is None and cp.effective(res)["community"] is None
    # SSH: the stored [3] object of the decider
    res = cp.resolve("ssh", [ChainNode("device", 1), ChainNode("group", 2, None, 6)], PROFILES)
    assert cp.effective(res) == SSH_P.data


def test_resolve_cycle_and_depth_guard() -> None:
    loop = [ChainNode("group", 1), ChainNode("group", 2), ChainNode("group", 1, MANUAL["snmp"])]
    assert cp.resolve("snmp", loop, {}).source == "default"  # the repeat ends the walk
    deep = [ChainNode("group", i) for i in range(cp.MAX_GROUP_DEPTH + 1)]
    assert cp.resolve("snmp", [*deep, ChainNode("group", 999, MANUAL["snmp"])], {}).mode is None
    ok = [ChainNode("group", i) for i in range(cp.MAX_GROUP_DEPTH)]
    assert cp.resolve("snmp", [*ok, ChainNode("group", 999, MANUAL["snmp"])], {}).mode == "manual"
    with pytest.raises(KeyError):
        cp.resolve("wmi", [], {})


# ------------------------------------------------------------------ origin
def test_origin_devices_and_groups() -> None:
    dev = [node("snmp", "device", 1, "inherit"), node("snmp", "group", 2, "profile", "Core")]
    assert cp.origin("snmp", dev, PROFILES) == {
        "source": "group",
        "group_id": 2,
        "group_name": "Core",
        "mode": "profile",
        "profile_id": 5,
        "profile_name": "Core v2",
    }
    own = [node("snmp", "device", 1, "both"), node("snmp", "group", 2, "manual", "Core")]
    assert cp.origin("snmp", own, PROFILES)["source"] == "self"
    assert cp.origin("snmp", own, PROFILES)["profile_name"] == "Core v2"
    grp = [node("ssh", "group", 2, "manual", "Core"), node("ssh", "group", 1, "profile", "R")]
    assert cp.origin("ssh", grp, PROFILES) == {
        "source": "self",
        "group_id": None,
        "group_name": None,
        "mode": "manual",
        "profile_id": None,
        "profile_name": None,
    }
    sub = [node("ssh", "group", 3, "inherit", "Sub"), *grp]
    assert cp.origin("ssh", sub, PROFILES)["source"] == "group"
    assert cp.origin("ssh", sub, PROFILES)["group_name"] == "Core"
    none = [node("ssh", "group", 3, "inherit", "Sub")]
    assert cp.origin("ssh", none, PROFILES) == {
        "source": "none",
        "group_id": None,
        "group_name": None,
        "mode": None,
        "profile_id": None,
        "profile_name": None,
    }
    assert cp.origin("snmp", [node("snmp", "device", 1, "inherit")], {})["source"] == "default"
    # a deleted profile (cannot happen through the FK; defensive): name null
    assert cp.origin("snmp", [ChainNode("device", 1, None, 42)], {})["profile_name"] is None


def test_node_mode_and_saved_manual() -> None:
    assert cp.node_mode(None, None) == "inherit"
    assert cp.node_mode({"x": 1}, None) == "manual"
    assert cp.node_mode(None, 5) == "profile"
    assert cp.node_mode({"x": 1}, 5) == "profile"
    assert cp.has_saved_manual({"x": 1}, 5) is True
    assert not cp.has_saved_manual(None, 5) and not cp.has_saved_manual({"x": 1}, None)


# ------------------------------------------------------------------ request-mode table
OBJ = {"community": "x"}


@pytest.mark.parametrize("kind", ["snmp", "ssh"])
def test_request_mode_table(kind: str) -> None:
    c, p = f"{kind}_credentials", f"{kind}_profile_id"

    def d(fields: dict[str, Any], current: str, create: bool = False, saved: bool = False) -> str:
        return cp.decide_mode(fields, kind, fields.get(c), fields.get(p), current, create, saved)

    for cur in cp.MODES:
        # neither field: PATCH unchanged, POST inherit
        assert d({}, cur) == "unchanged"
        assert d({"other": 1}, cur) == "unchanged"
        # K_profile_id: <id> (no K_credentials): profile, dormant data kept
        assert d({p: 5}, cur) == "profile"
        # with K_credentials: null: profile, manual / dormant data discarded
        assert d({p: 5, c: None}, cur) == "profile_clear"
        # with a non-null K_credentials: contradiction
        with pytest.raises(ModeRequestError):
            d({p: 5, c: OBJ}, cur)
        # K_credentials: {...} (no K_profile_id or null): manual
        assert d({c: OBJ}, cur) == "manual"
        assert d({c: OBJ, p: None}, cur) == "manual"
        # K_credentials: null: inherit
        assert d({c: None}, cur) == "inherit"
        assert d({c: None, p: None}, cur) == "inherit"
    # K_profile_id: null only
    assert d({p: None}, "profile", saved=True) == "manual_restore"
    assert d({p: None}, "profile", saved=False) == "inherit"
    assert d({p: None}, "manual") == "unchanged"  # never deletes manual data
    assert d({p: None}, "inherit") == "unchanged"
    # POST
    assert d({}, "inherit", create=True) == "inherit"
    assert d({p: 5}, "inherit", create=True) == "profile"
    assert d({c: OBJ}, "inherit", create=True) == "manual"
    assert d({c: None}, "inherit", create=True) == "inherit"
    with pytest.raises(ModeRequestError):
        d({p: 5, c: OBJ}, "inherit", create=True)
    # the other kind's fields never decide this kind
    other = "ssh" if kind == "snmp" else "snmp"
    assert d({f"{other}_profile_id": 5, f"{other}_credentials": None}, "profile") == "unchanged"


# ------------------------------------------------------------------ discovery
def test_credential_order_and_skip() -> None:
    opts = merge_options(
        {"snmp_port": None, "extra_communities": ["e1"], "snmp_profile_ids": [7, 5, 8]}
    )
    group = {"version": "v2c", "community": "grp", "port": 161}
    profiles = [
        (7, {"version": "v2c", "community": "p7", "port": 1161}),
        (5, {"version": "v2c", "community": "p5"}),
        (8, {"version": "v3", "v3_username": "u", "community": None}),
    ]
    out = build_credentials(group, opts, 5, profiles)  # group creds come from profile 5
    assert [c["_origin"] for c in out] == ["group", "profile", "profile", "extra"]
    assert [c["_profile_id"] for c in out] == [5, 7, 8, None]  # 5 skipped: it supplied 0
    assert [c.get("community") for c in out] == ["grp", "p7", None, "e1"]
    assert [c["port"] for c in out] == [161, 1161, 161, 161]  # own ports
    assert all(c["retries"] == 0 and c["timeout_s"] == 1 for c in out)
    over = build_credentials(group, {**opts, "snmp_port": 2161}, None, profiles)
    assert {c["port"] for c in over} == {2161} and len(over) == 5
    assert probe_view(out[1]) == {k: v for k, v in out[1].items() if not k.startswith("_")}
    big = merge_options(
        {"extra_communities": [f"c{i}" for i in range(5)], "snmp_profile_ids": list(range(8))}
    )
    big["extra_v3"] = [{"v3_username": "u"}] * 3
    many = build_credentials(group, big, None, [(i, {"community": f"p{i}"}) for i in range(8)])
    assert len(many) == 17  # 1 + 8 + 5 + 3


def test_ssh_candidates_and_options_hide_secrets() -> None:
    typed = {"username": "t", "port": 22, "password_enc": "ENC"}
    cands = build_ssh_candidates([(3, {"username": "a"}), (4, {"username": "b"})], typed)
    assert [(c["_origin"], c["_profile_id"]) for c in cands] == [
        ("profile", 3),
        ("profile", 4),
        ("typed", None),
    ]
    assert build_ssh_candidates([], None) == []
    opts = merge_options({"ssh_profile_ids": [3], "ssh_credentials": typed})
    pub = public_options(opts)
    assert pub["ssh_credentials"] is None and pub["ssh_credentials_set"] is True
    assert pub["ssh_username"] == "t" and pub["ssh_profile_ids"] == [3]
    assert "ENC" not in str(pub)
    snap = scan_snapshot(opts)
    assert snap["ssh_credentials"] is None and "ENC" not in str(snap)
    assert public_options(snap)["ssh_credentials_set"] is True
    assert public_options(snap)["ssh_username"] == "t"
    empty = public_options(merge_options(None))
    assert empty["ssh_profile_ids"] == [] and empty["ssh_credentials_set"] is False
    assert empty["snmp_profile_ids"] == []


def test_snmp_choice_table(key: str) -> None:
    group = {"version": "v2c", "community": "grp", "port": 161}
    cred0 = {**group, "timeout_s": 1, "retries": 0, "_profile_id": None, "_origin": "group"}
    pdata = secretbox.seal_conn_profile_snmp({"version": "v2c", "community": "pc", "port": 161}, 5)
    prof = Profile(5, "snmp", "P", pdata)
    credp = {"version": "v2c", "community": "pc", "port": 161, "_profile_id": 5}
    # 1. credential 0 equal to the group's current effective credentials
    assert importer.snmp_choice(cred0, 0, group, None, None, 161) == ("inherit", None)
    assert importer.snmp_choice(cred0, 0, {**group, "port": 1161}, None, None, 161)[0] == "manual"
    # 2. the import group inherits that very profile
    assert importer.snmp_choice(credp, 1, group, 5, prof, 161) == ("inherit", None)
    # 3. linked to the profile that answered on its own port
    assert importer.snmp_choice(credp, 1, group, None, prof, 161) == ("profile", 5)
    assert importer.snmp_choice(credp, 1, group, 9, prof, 161) == ("profile", 5)
    # 4. port override, deleted profile, wrong kind, extra credential -> manual copy
    assert importer.snmp_choice(credp, 1, group, None, prof, 2161) == ("manual", None)
    assert importer.snmp_choice(credp, 1, group, None, None, 161) == ("manual", None)
    wrong = Profile(5, "ssh", "P", {})
    assert importer.snmp_choice(credp, 1, group, None, wrong, 161) == ("manual", None)
    extra = {"version": "v2c", "community": "e", "_profile_id": None}
    assert importer.snmp_choice(extra, 2, group, None, None, 161) == ("manual", None)
    # credential 0 that came from the group's profile but differs now -> profile still exists
    cred0p = {**credp, "community": "old"}
    assert importer.snmp_choice(cred0p, 0, group, None, prof, 161) == ("profile", 5)


def test_ssh_choice_table() -> None:
    prof = Profile(3, "ssh", "S", {})
    cp3 = {"username": "a", "_profile_id": 3, "_origin": "profile"}
    typed = {"username": "t", "_profile_id": None, "_origin": "typed"}
    assert importer.ssh_choice(True, True, cp3, None, prof) == ("profile", 3)
    assert importer.ssh_choice(True, True, cp3, 3, prof) == ("inherit", None)
    assert importer.ssh_choice(True, True, cp3, None, None) == ("manual", None)  # deleted
    assert importer.ssh_choice(True, True, typed, None, None) == ("manual", None)
    assert importer.ssh_choice(False, True, cp3, None, prof) == ("inherit", None)  # declined
    assert importer.ssh_choice(True, False, cp3, None, prof) == ("inherit", None)  # no login
    assert importer.ssh_choice(True, True, None, None, prof) == ("inherit", None)

    class Row:
        ssh_ok = True
        ssh_credential = "typed"
        ssh_profile_id = None

    class Scan:
        ssh_candidates = [cp3, typed]

    assert importer.ssh_candidate(Scan(), Row()) == typed
    Row.ssh_ok = False
    assert importer.ssh_candidate(Scan(), Row()) is None


def test_registry() -> None:
    assert set(cp.KINDS) == {"snmp", "ssh"}
    assert cp.KINDS["snmp"].column == "snmp_credentials" and cp.KINDS["snmp"].fallback == "default"
    assert cp.KINDS["ssh"].column == "ssh_credentials" and cp.KINDS["ssh"].fallback == "none"
    assert cp.discovery_profile_ids({"snmp_profile_ids": [1, "2"], "ssh_profile_ids": [3]}) == [
        1,
        2,
        3,
    ]
    assert cp.discovery_profile_ids(None) == [] and cp.discovery_profile_ids({"x": 1}) == []
