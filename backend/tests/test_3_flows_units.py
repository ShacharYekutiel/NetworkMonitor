"""[3] Pure units of the flow collector: decoders (data-model "Decoder vectors" plus hostile
datagrams), template cache, sampling precedence, services map, aggregator vectors, budget."""

from __future__ import annotations

import random
import struct
import time

import pytest

from netmon.flows import limits
from netmon.flows.aggregate import MinuteAggregator
from netmon.flows.decode import decode_datagram, exporter_of
from netmon.flows.decode_ipfix import decode_ipfix
from netmon.flows.decode_netflow import decode_v5, decode_v9
from netmon.flows.decode_sflow import decode_sflow
from netmon.flows.records import FlowRecord, ip_text
from netmon.flows.services import ServiceMap, service_port
from netmon.flows.templates import Template, TemplateCache, TemplateError
from netmon.flows.worker import FlowWorker, RawItem, effective_rate
from tests import flow_packets as fp

EXP = "10.0.0.5"


def v9_flow(i: int = 0) -> dict:
    return fp.flow_values("10.0.0.1", "10.0.0.9", 51000 + i, 443, 6, 1000 + i, 10, 1, 2)


# ================================================================ NetFlow v5
def test_v5_vector_two_records_rate_100() -> None:
    recs = [
        {
            "src": "10.0.0.1",
            "dst": "10.0.0.9",
            "sport": 51000,
            "dport": 443,
            "bytes": 600,
            "packets": 6,
            "in_if": 1,
            "out_if": 2,
        },
        {
            "src": "10.0.0.2",
            "dst": "10.0.0.9",
            "sport": 52000,
            "dport": 53,
            "bytes": 100,
            "packets": 1,
            "in_if": 1,
            "out_if": 3,
            "proto": 17,
        },
    ]
    res = decode_v5(fp.v5(recs, sampling=0x4064), EXP)
    assert res.protocol == "netflow5" and not res.errors
    assert len(res.records) == 2
    r = res.records[0]
    assert (ip_text(r.src), ip_text(r.dst), r.sport, r.dport, r.proto) == (
        "10.0.0.1",
        "10.0.0.9",
        51000,
        443,
        6,
    )
    assert (r.nbytes, r.packets, r.in_if, r.out_if, r.rate) == (600, 6, 1, 2, 100)
    assert res.records[1].proto == 17 and res.records[1].rate == 100


def test_v5_count_31_parse_error() -> None:
    rec = {"src": "10.0.0.1", "dst": "10.0.0.2", "bytes": 1}
    res = decode_v5(fp.v5([rec] * 31), EXP)
    assert res.errors == {"parse_error": 1} and res.records == []


def test_v5_count_larger_than_data_and_short_header() -> None:
    rec = {"src": "10.0.0.1", "dst": "10.0.0.2", "bytes": 1}
    assert decode_v5(fp.v5([rec], count=3), EXP).errors == {"parse_error": 1}
    assert decode_v5(b"\x00\x05\x00", EXP).errors == {"parse_error": 1}


def test_v5_no_sampling_rate_none() -> None:
    rec = {"src": "10.0.0.1", "dst": "10.0.0.2", "bytes": 5}
    assert decode_v5(fp.v5([rec]), EXP).records[0].rate is None


# ================================================================ NetFlow v9
def test_v9_template_then_data() -> None:
    cache = TemplateCache()
    tmpl = fp.set_(0, fp.v9_template(256, fp.V9_FIELDS))
    data = fp.set_(256, fp.data_record(fp.V9_FIELDS, v9_flow()) * 2, pad=2)
    res = decode_v9(fp.v9([tmpl, data]), EXP, cache, 0.0)
    assert not res.errors, res.errors
    assert len(res.records) == 2 and res.templates == 1
    r = res.records[0]
    assert (
        ip_text(r.src),
        ip_text(r.dst),
        r.sport,
        r.dport,
        r.proto,
        r.nbytes,
        r.packets,
        r.in_if,
        r.out_if,
        r.rate,
    ) == ("10.0.0.1", "10.0.0.9", 51000, 443, 6, 1000, 10, 1, 2, None)


def test_v9_data_before_template_no_template_then_ok() -> None:
    cache = TemplateCache()
    data = fp.set_(256, fp.data_record(fp.V9_FIELDS, v9_flow()))
    res = decode_v9(fp.v9([data]), EXP, cache, 0.0)
    assert res.errors == {"no_template": 1} and not res.records
    decode_v9(fp.v9([fp.set_(0, fp.v9_template(256, fp.V9_FIELDS))]), EXP, cache, 1.0)
    res = decode_v9(fp.v9([data]), EXP, cache, 2.0)
    assert not res.errors and len(res.records) == 1


def test_v9_templates_are_per_exporter_and_source_id() -> None:
    cache = TemplateCache()
    decode_v9(fp.v9([fp.set_(0, fp.v9_template(256, fp.V9_FIELDS))], source_id=1), EXP, cache, 0)
    data = fp.set_(256, fp.data_record(fp.V9_FIELDS, v9_flow()))
    assert decode_v9(fp.v9([data], source_id=2), EXP, cache, 0).errors == {"no_template": 1}
    assert decode_v9(fp.v9([data], source_id=1), "10.9.9.9", cache, 0).errors == {"no_template": 1}
    assert len(decode_v9(fp.v9([data], source_id=1), EXP, cache, 0).records) == 1


def test_v9_options_sampling_exporter_wide_512() -> None:
    cache = TemplateCache()
    opt_t = fp.set_(1, fp.v9_options_template(257, [(1, 4)], [(34, 4)]), pad=2)
    opt_d = fp.set_(257, struct.pack(">II", 0, 512))
    tmpl = fp.set_(0, fp.v9_template(256, fp.V9_FIELDS))
    data = fp.set_(256, fp.data_record(fp.V9_FIELDS, v9_flow()))
    res = decode_v9(fp.v9([opt_t, opt_d, tmpl, data]), EXP, cache, 0.0)
    assert not res.errors, res.errors
    assert cache.exporter_rate[EXP] == 512
    assert res.records[0].rate == 512


def test_v9_sampler_id_match_beats_exporter_wide() -> None:
    cache = TemplateCache()
    # options: scope system, fields sampler id (48, 1 byte), mode (49), random interval (50)
    opt_t = fp.set_(1, fp.v9_options_template(257, [(1, 4)], [(48, 1), (49, 1), (50, 4)]))
    opt_d = fp.set_(257, struct.pack(">IBBI", 0, 7, 2, 1000), pad=2)
    wide_t = fp.set_(1, fp.v9_options_template(258, [(1, 4)], [(34, 4)]), pad=2)
    wide_d = fp.set_(258, struct.pack(">II", 0, 64))
    fields = [*fp.V9_FIELDS, (48, 1)]
    tmpl = fp.set_(0, fp.v9_template(256, fields))
    vals = v9_flow()
    vals[48] = 7
    other = v9_flow(1)
    other[48] = 9  # unknown sampler -> exporter-wide
    data = fp.set_(256, fp.data_record(fields, vals) + fp.data_record(fields, other))
    res = decode_v9(fp.v9([opt_t, opt_d, wide_t, wide_d, tmpl, data]), EXP, cache, 0.0)
    assert not res.errors, res.errors
    assert [r.rate for r in res.records] == [1000, 64]


def test_v9_short_data_record_parse_error_for_that_set_only() -> None:
    cache = TemplateCache()
    tmpl = fp.set_(0, fp.v9_template(256, fp.V9_FIELDS))
    good = fp.set_(256, fp.data_record(fp.V9_FIELDS, v9_flow()))
    short = fp.set_(256, fp.data_record(fp.V9_FIELDS, v9_flow())[:20])
    res = decode_v9(fp.v9([tmpl, short, good]), EXP, cache, 0.0)
    assert res.errors == {"parse_error": 1}
    assert len(res.records) == 1


def test_v9_bidirectional_out_bytes_reverse_record() -> None:
    cache = TemplateCache()
    fields = [*fp.V9_FIELDS, (23, 4), (24, 4)]
    vals = v9_flow()
    vals[23], vals[24] = 5000, 40
    tmpl = fp.set_(0, fp.v9_template(256, fields))
    data = fp.set_(256, fp.data_record(fields, vals))
    res = decode_v9(fp.v9([tmpl, data]), EXP, cache, 0.0)
    fwd, rev = res.records
    assert (fwd.nbytes, ip_text(fwd.src), fwd.in_if, fwd.out_if) == (1000, "10.0.0.1", 1, 2)
    assert (rev.nbytes, rev.packets, ip_text(rev.src), ip_text(rev.dst)) == (
        5000,
        40,
        "10.0.0.9",
        "10.0.0.1",
    )
    assert (rev.sport, rev.dport, rev.in_if, rev.out_if) == (443, 51000, 2, 1)


def test_v9_egress_direction_swaps_interfaces() -> None:
    cache = TemplateCache()
    fields = [*fp.V9_FIELDS, (61, 1)]
    vals = v9_flow()
    vals[61] = 1
    res = decode_v9(
        fp.v9(
            [fp.set_(0, fp.v9_template(256, fields)), fp.set_(256, fp.data_record(fields, vals))]
        ),
        EXP,
        cache,
        0.0,
    )
    r = res.records[0]
    assert (r.in_if, r.out_if, r.egress) == (2, 1, True)


def test_v9_total_counts_only_when_deltas_absent() -> None:
    cache = TemplateCache()
    fields = [(8, 4), (12, 4), (4, 1), (85, 8), (86, 8)]
    vals = {8: "10.0.0.1", 12: "10.0.0.2", 4: 17, 85: 1234, 86: 3}
    res = decode_v9(
        fp.v9(
            [fp.set_(0, fp.v9_template(256, fields)), fp.set_(256, fp.data_record(fields, vals))]
        ),
        EXP,
        cache,
        0.0,
    )
    assert (res.records[0].nbytes, res.records[0].packets) == (1234, 3)


def test_v9_ipv6_and_odd_length_fields() -> None:
    cache = TemplateCache()
    fields = [(27, 16), (28, 16), (7, 2), (11, 2), (4, 1), (1, 3), (2, 6), (10, 4), (14, 4)]
    vals = {
        27: "2001:db8::1",
        28: "2001:db8::2",
        7: 1000,
        11: 53,
        4: 17,
        1: 70000,
        2: 5,
        10: 100000,
        14: 3,
    }
    res = decode_v9(
        fp.v9(
            [fp.set_(0, fp.v9_template(256, fields)), fp.set_(256, fp.data_record(fields, vals))]
        ),
        EXP,
        cache,
        0.0,
    )
    r = res.records[0]
    assert (ip_text(r.src), ip_text(r.dst), r.nbytes, r.packets, r.in_if) == (
        "2001:db8::1",
        "2001:db8::2",
        70000,
        5,
        100000,
    )


@pytest.mark.parametrize(
    "tmpl_body",
    [
        fp.v9_template(100, fp.V9_FIELDS),  # template id < 256
        fp.v9_template(256, []),  # zero fields
        fp.v9_template(256, [(8, 0), (12, 0)]),  # zero-length record
        fp.v9_template(256, fp.V9_FIELDS)[:10],  # truncated field list
        fp.v9_template(256, [(8, 65535)]),  # variable length is IPFIX only
    ],
)
def test_v9_bad_templates_parse_error(tmpl_body: bytes) -> None:
    cache = TemplateCache()
    res = decode_v9(fp.v9([fp.set_(0, tmpl_body)]), EXP, cache, 0.0)
    assert res.errors.get("parse_error") == 1
    assert len(cache) == 0


def test_v9_hostile_set_lengths_and_count() -> None:
    cache = TemplateCache()
    # set length beyond the datagram, set length < 4, huge header count: never raise
    assert decode_v9(fp.v9([struct.pack(">HH", 256, 400)]), EXP, cache, 0).errors == {
        "parse_error": 1
    }
    assert decode_v9(fp.v9([struct.pack(">HH", 256, 2)]), EXP, cache, 0).errors == {
        "parse_error": 1
    }
    res = decode_v9(fp.v9([], count=65535), EXP, cache, 0)
    assert not res.errors and not res.records
    assert decode_v9(b"\x00\x09\x00", EXP, cache, 0).errors == {"parse_error": 1}


def test_v9_options_template_recursion_free_scopes() -> None:
    # scope length not a multiple of 4 / longer than the set -> parse_error, no loop
    cache = TemplateCache()
    bad = struct.pack(">HHH", 257, 6, 4) + b"\x00" * 10
    assert decode_v9(fp.v9([fp.set_(1, bad)]), EXP, cache, 0).errors == {"parse_error": 1}
    bad2 = struct.pack(">HHH", 257, 4000, 4) + b"\x00" * 8
    assert decode_v9(fp.v9([fp.set_(1, bad2)]), EXP, cache, 0).errors == {"parse_error": 1}


# ================================================================ IPFIX
def test_ipfix_varlen_and_enterprise_fields_skipped() -> None:
    cache = TemplateCache()
    fields = [
        (8, 4),
        (12, 4),
        (82, 65535),
        (7, 2),
        (11, 2),
        (4, 1),
        (1000, 4, 9),
        (1, 8),
        (2, 8),
        (10, 4),
        (14, 4),
    ]
    tmpl = fp.set_(2, fp.ipfix_template(300, fields))

    def rec(name: bytes, nbytes: int) -> bytes:
        out = fp.pack_value("10.0.0.1", 4) + fp.pack_value("10.0.0.9", 4)
        out += (
            (bytes([len(name)]) + name)
            if len(name) < 255
            else (b"\xff" + struct.pack(">H", len(name)) + name)
        )
        out += struct.pack(">HHB", 51000, 443, 6) + b"ENTP"
        out += struct.pack(">QQII", nbytes, 3, 1, 2)
        return out

    data = fp.set_(300, rec(b"eth0", 700) + rec(b"x" * 300, 800) + rec(b"", 900))
    res = decode_ipfix(fp.ipfix([tmpl, data]), EXP, cache, 0.0)
    assert res.protocol == "ipfix"
    assert not res.errors, res.errors
    assert [r.nbytes for r in res.records] == [700, 800, 900]
    assert all(r.sport == 51000 and r.dport == 443 and r.in_if == 1 for r in res.records)


def test_ipfix_withdrawal_then_no_template() -> None:
    cache = TemplateCache()
    fields = [(8, 4), (12, 4), (4, 1), (1, 4)]
    tmpl = fp.set_(2, fp.ipfix_template(300, fields))
    data = fp.set_(300, fp.data_record(fields, {8: "10.0.0.1", 12: "10.0.0.2", 4: 6, 1: 5}))
    assert len(decode_ipfix(fp.ipfix([tmpl, data]), EXP, cache, 0).records) == 1
    withdraw = fp.set_(2, struct.pack(">HH", 300, 0))
    res = decode_ipfix(fp.ipfix([withdraw, data]), EXP, cache, 1)
    assert res.errors == {"no_template": 1} and not res.records
    # "withdraw all" (template id 2)
    decode_ipfix(fp.ipfix([tmpl]), EXP, cache, 2)
    assert len(cache) == 1
    decode_ipfix(fp.ipfix([fp.set_(2, struct.pack(">HH", 2, 0))]), EXP, cache, 3)
    assert len(cache) == 0


def test_ipfix_selector_packet_interval_space_rate_100() -> None:
    cache = TemplateCache()
    opt = fp.set_(3, fp.ipfix_template(301, [(302, 4), (305, 4), (306, 4)], scope=1), pad=2)
    opt_data = fp.set_(301, struct.pack(">III", 5, 1, 99))
    fields = [(8, 4), (12, 4), (4, 1), (1, 4), (302, 4)]
    tmpl = fp.set_(2, fp.ipfix_template(300, fields))
    data = fp.set_(
        300, fp.data_record(fields, {8: "10.0.0.1", 12: "10.0.0.2", 4: 6, 1: 5, 302: 5}), pad=3
    )
    res = decode_ipfix(fp.ipfix([opt, opt_data, tmpl, data]), EXP, cache, 0)
    assert not res.errors, res.errors
    assert res.records[0].rate == 100


def test_ipfix_exporter_wide_selector_without_id() -> None:
    cache = TemplateCache()
    opt = fp.set_(3, fp.ipfix_template(301, [(149, 4), (305, 4), (306, 4)], scope=1), pad=2)
    opt_data = fp.set_(301, struct.pack(">III", 1, 1, 99))
    decode_ipfix(fp.ipfix([opt, opt_data]), EXP, cache, 0)
    assert cache.exporter_rate[EXP] == 100


def test_ipfix_hostile() -> None:
    cache = TemplateCache()
    # message length beyond the datagram / shorter than the header
    assert decode_ipfix(fp.ipfix([], length=400), EXP, cache, 0).errors == {"parse_error": 1}
    assert decode_ipfix(fp.ipfix([], length=8), EXP, cache, 0).errors == {"parse_error": 1}
    # options template with scope count 0
    bad = fp.set_(3, fp.ipfix_template(301, [(305, 4)], scope=0))
    assert decode_ipfix(fp.ipfix([bad]), EXP, cache, 0).errors == {"parse_error": 1}
    # enterprise bit with truncated PEN
    bad2 = fp.set_(2, struct.pack(">HHHH", 300, 1, 0x8001, 4) + b"\x00\x00")
    assert decode_ipfix(fp.ipfix([bad2]), EXP, cache, 0).errors == {"parse_error": 1}
    # variable-length record cut in the middle
    fields = [(8, 4), (82, 65535), (1, 4)]
    tmpl = fp.set_(2, fp.ipfix_template(302, fields))
    data = fp.set_(302, fp.pack_value("10.0.0.1", 4) + b"\x20abc")
    res = decode_ipfix(fp.ipfix([tmpl, data]), EXP, cache, 0)
    assert res.errors == {"parse_error": 1} and not res.records


def test_fuzz_never_raises() -> None:
    rnd = random.Random(42)
    cache = TemplateCache()
    seeds = [
        fp.v5([{"src": "10.0.0.1", "dst": "10.0.0.2", "bytes": 9}]),
        fp.v9(
            [
                fp.set_(0, fp.v9_template(256, fp.V9_FIELDS)),
                fp.set_(256, fp.data_record(fp.V9_FIELDS, v9_flow())),
            ]
        ),
        fp.ipfix([fp.set_(2, fp.ipfix_template(300, [(8, 4), (82, 65535), (1, 4)]))]),
        fp.sflow(
            "10.0.0.12",
            [
                fp.sflow_flow_sample(
                    4096,
                    3,
                    4,
                    [fp.sflow_raw_record(fp.eth_ipv4_tcp("1.1.1.1", "2.2.2.2", 1, 2), 64)],
                )
            ],
        ),
    ]
    for _ in range(3000):
        pkt = bytearray(rnd.choice(seeds))
        for _ in range(rnd.randint(1, 8)):
            pkt[rnd.randrange(len(pkt))] = rnd.randrange(256)
        if rnd.random() < 0.3:
            pkt = pkt[: rnd.randrange(len(pkt))]
        sflow = rnd.random() < 0.3
        res = decode_datagram(bytes(pkt), EXP, sflow, cache, 0.0)
        assert isinstance(res.records, list)


# ================================================================ sFlow
def test_sflow_vector_expanded_sample_vlan() -> None:
    hdr = fp.eth_ipv4_tcp("10.0.0.1", "10.0.0.9", 51000, 443, vlan=10)
    sample = fp.sflow_flow_sample(4096, 7, 9, [fp.sflow_raw_record(hdr, 1514)])
    data = fp.sflow("10.0.0.12", [fp.sflow_counter_sample(), sample])
    assert exporter_of(data, "172.17.0.1", True) == "10.0.0.12"
    res = decode_sflow(data, "172.17.0.1")
    assert res.exporter == "10.0.0.12" and res.protocol == "sflow5"
    assert not res.errors
    (r,) = res.records
    assert (ip_text(r.src), ip_text(r.dst), r.sport, r.dport, r.proto) == (
        "10.0.0.1",
        "10.0.0.9",
        51000,
        443,
        6,
    )
    assert (r.nbytes, r.packets, r.rate, r.in_if, r.out_if) == (1514, 1, 4096, 7, 9)
    assert effective_rate(r, None) == 4096
    assert r.nbytes * effective_rate(r, None) == 6201344


def test_sflow_compact_sample_ifindex_format_bits() -> None:
    hdr = fp.eth_ipv6_udp("2001:db8::1", "2001:db8::2", 5353, 53)
    sample = fp.sflow_flow_sample(
        512, 5, 0x80000002, [fp.sflow_raw_record(hdr, 90)], expanded=False
    )
    res = decode_sflow(fp.sflow("2001:db8::12", [sample]), "10.1.1.1")
    assert res.exporter == "2001:db8::12"
    (r,) = res.records
    assert (r.in_if, r.out_if, r.dport, r.proto, r.rate) == (5, 0, 53, 17, 512)


def test_sflow_hostile() -> None:
    sample = fp.sflow_flow_sample(1, 1, 1, [fp.sflow_raw_record(b"\x00" * 20, 64)])
    assert decode_sflow(fp.sflow("10.0.0.12", [sample], nsamples=5), "x").errors == {
        "parse_error": 1
    }
    assert decode_sflow(fp.sflow("10.0.0.12", [], nsamples=100000), "x").errors == {
        "parse_error": 1
    }
    assert decode_sflow(b"\x00\x00\x00\x04" + b"\x00" * 30, "x").errors == {"unsupported": 1}
    assert decode_sflow(b"\x00\x00\x00\x05\x00\x00\x00\x07" + b"\x00" * 30, "x").errors == {
        "parse_error": 1
    }
    # non-IP header -> no record, no error
    res = decode_sflow(fp.sflow("10.0.0.12", [sample]), "x")
    assert not res.errors and not res.records


def test_dispatch_versions() -> None:
    cache = TemplateCache()
    assert decode_datagram(b"\x00\x07" + b"\x00" * 30, EXP, False, cache, 0).errors == {
        "unsupported": 1
    }
    assert decode_datagram(b"\x00", EXP, False, cache, 0).errors == {"parse_error": 1}


# ================================================================ templates
def test_template_cache_limits_and_expiry() -> None:
    cache = TemplateCache(per_domain=3, total=5, idle_s=100)
    f = [(8, 4, 0), (1, 4, 0)]
    for tid in range(256, 260):
        cache.put("a", 0, Template(tid, f, False), 0)
    assert len(cache) == 3 and cache.get("a", 0, 256, 1) is None  # LRU of the domain
    for tid in range(256, 259):
        cache.put("b", 0, Template(tid, f, False), 0)
    assert len(cache) == 5
    assert cache.get("b", 0, 258, 50) is not None
    assert cache.get("b", 0, 258, 200) is None  # idle -> expired on access
    assert cache.expire(1000) == 4 and len(cache) == 0


def test_template_redefinition_replaces() -> None:
    cache = TemplateCache()
    cache.put("a", 0, Template(256, [(8, 4, 0)], False), 0)
    cache.put("a", 0, Template(256, [(8, 4, 0), (12, 4, 0)], False), 1)
    t = cache.get("a", 0, 256, 2)
    assert t is not None and t.record_len == 8 and len(cache) == 1


def test_template_errors() -> None:
    with pytest.raises(TemplateError):
        Template(255, [(8, 4, 0)], False)
    with pytest.raises(TemplateError):
        Template(256, [], False)


# ================================================================ sampling precedence
def test_sampling_precedence() -> None:
    r = FlowRecord(b"", b"", 0, 0, 6, 10, 1, 0, 0, rate=4096)
    assert effective_rate(r, 100) == 100  # admin override first
    assert effective_rate(r, None) == 4096  # then the datagram / template context
    r.rate = None
    assert effective_rate(r, None) == 1


# ================================================================ services
@pytest.mark.parametrize(
    "args,port",
    [
        ((6, 51000, 443), 443),
        ((6, 443, 51000), 443),
        ((17, 53000, 53), 53),
        ((6, 50000, 8443), 8443),
        ((6, 3000, 50000), 3000),
        ((17, 60000, 61000), 0),
        ((1, 0, 0), 0),
        ((6, 40000, 45000), 40000),
    ],
)
def test_service_port_vectors(args: tuple[int, int, int], port: int) -> None:
    assert service_port(*args) == port


def test_service_names_and_overrides() -> None:
    m = ServiceMap(
        overrides=[
            {"protocol": 6, "port": 443, "name": "web"},
            {"protocol": 6, "port": 55555, "name": "custom"},
        ]
    )
    assert m.label(6, 443) == "web (tcp/443)"
    assert m.service_port(6, 60000, 55555) == 55555
    assert m.label(1, 0) == "icmp"
    assert m.label(0, -1) == "other"
    assert ServiceMap().label(17, 53) == "dns (udp/53)"


# ================================================================ aggregation vectors
def _rec(src: str, sp: int, dst: str, dp: int, proto: int, nbytes: int, i: int, o: int):
    return FlowRecord(fp.ip(src), fp.ip(dst), sp, dp, proto, nbytes, 1, i, o)


def test_aggregation_vectors() -> None:
    agg = MinuteAggregator(top_apps=2, top_hosts=1, top_conversations=1)
    recs = [
        _rec("10.0.0.1", 51000, "10.0.0.9", 443, 6, 600, 1, 2),
        _rec("10.0.0.2", 52000, "10.0.0.9", 443, 6, 300, 1, 2),
        _rec("10.0.0.1", 53000, "8.8.8.8", 53, 17, 100, 1, 3),
    ]
    agg.add(EXP, 60, recs)
    (f,) = agg.flush(60)
    assert f.records == 3 and f.bytes == 1000
    assert {(r[0], r[1]): r[2] for r in f.if_rows} == {
        (1, "in"): 1000,
        (2, "out"): 900,
        (3, "out"): 100,
    }
    assert [(r[0], r[1], r[2]) for r in f.app_rows] == [(6, 443, 900), (17, 53, 100)]
    hosts = {(r[0], r[1], r[2]): r[3] for r in f.host_rows}
    assert hosts == {
        ("10.0.0.1", "src", False): 700,
        ("0.0.0.0", "src", True): 300,
        ("10.0.0.9", "dst", False): 900,
        ("0.0.0.0", "dst", True): 100,
    }
    assert [(r[:8], r[8]) for r in f.conv_rows] == [
        ((1, 2, "10.0.0.1", "10.0.0.9", 6, 0, 443, False), 600),
        ((0, 0, "0.0.0.0", "0.0.0.0", 0, 0, 0, True), 400),
    ]
    for rows, i in ((f.app_rows, 2), (f.conv_rows, 8)):
        assert sum(r[i] for r in rows) == 1000
    assert sum(r[3] for r in f.host_rows if r[1] == "src") == 1000
    assert agg.buckets == {} and agg.keys == {}


def test_aggregation_sampling_and_flows_and_ties() -> None:
    agg = MinuteAggregator(top_apps=1)
    a = _rec("10.0.0.1", 1000, "10.0.0.2", 22, 6, 10, 1, 0)
    b = _rec("10.0.0.1", 1000, "10.0.0.2", 80, 6, 10, 1, 0)
    b.rate = 3
    agg.add(EXP, 0, [a, b], override=None)
    (f,) = agg.flush()
    assert f.app_rows[0][:3] == (6, 80, 30)  # sampling corrected bytes rank first
    assert f.app_rows[1] == (0, -1, 10, 1, 1)
    agg.add(EXP, 0, [a, b], override=2)
    (f,) = agg.flush()
    assert f.app_rows[0][:5] == (6, 22, 20, 2, 1)  # tie by bytes and packets -> key order


def test_aggregation_overflow_merges_into_other() -> None:
    agg = MinuteAggregator(max_keys=6)
    recs = [_rec(f"10.0.1.{i}", 40000 + i, "10.0.0.9", 443, 6, 10, 0, 0) for i in range(5)]
    agg.add(EXP, 0, recs)
    assert agg.overflow > 0
    (f,) = agg.flush()
    assert sum(r[2] for r in f.app_rows) == 50
    assert sum(r[3] for r in f.host_rows if r[1] == "src") == 50
    assert sum(r[8] for r in f.conv_rows) == 50


def test_aggregation_flush_by_minute_and_discard() -> None:
    agg = MinuteAggregator()
    agg.add("a", 0, [_rec("10.0.0.1", 1, "10.0.0.2", 2, 6, 1, 1, 0)])
    agg.add("b", 60, [_rec("10.0.0.1", 1, "10.0.0.2", 2, 6, 1, 1, 0)])
    assert [f.exporter for f in agg.flush(0)] == ["a"]
    assert agg.minutes() == [60]
    assert agg.discard() == 1 and agg.minutes() == []


# ================================================================ budget
def test_record_budget() -> None:
    b = limits.RecordBudget(1000, now=0.0)
    assert b.take(5000, now=0.0) == 2000  # burst 2x
    assert b.take(10, now=0.0) == 0
    assert b.take(600, now=0.5) == 500
    assert limits.clamp_budget(999999) == 50000 and limits.clamp_budget("x") == 10000


# ================================================================ worker thread
def _worker(now: list[float], budget: int = 10000) -> tuple[FlowWorker, list]:
    got: list = []
    w = FlowWorker(
        lambda flushes, stats, drops: got.append((flushes, stats)),
        lambda: now[0],
        budget,
        MinuteAggregator(),
    )
    return w, got


def _v5_item(ts: float, n: int = 2, override: int | None = None) -> RawItem:
    recs = [
        {"src": "10.0.0.1", "dst": "10.0.0.2", "bytes": 10, "dport": 80, "sport": 50000, "in_if": 1}
    ] * n
    return RawItem(fp.v5(recs, sampling=0x4004), EXP, EXP, False, ts, override)


def test_worker_closes_minutes_by_clock_and_on_request() -> None:
    now = [120.0]
    w, got = _worker(now)
    w.start()
    try:
        w.raw.put(_v5_item(61.0))  # minute 60, closes at 60 + 60 + 5
        w.raw.put(_v5_item(130.0, override=7))  # minute 120
        assert w.request_flush(5)
        flushes = [f for fl, _ in got for f in fl]
        assert sorted((f.minute, f.bytes) for f in flushes) == [(60, 80), (120, 140)]
        stats = got[-1][1][EXP]
        assert stats.records == 4 and stats.protocols == {"netflow5"} and stats.rate == 7
        got.clear()
        w.raw.put(_v5_item(185.0))  # minute 180
        now[0] = 244.0
        time.sleep(0.6)
        assert got == []  # still inside the 5 s grace
        now[0] = 245.0
        deadline = time.monotonic() + 3
        while not got and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [f.minute for f in got[0][0]] == [180]
        rec, drops = w.counters()
        assert rec == 6 and drops == {}
    finally:
        w.stop(flush=False, timeout=2)
    assert not w.alive


def test_worker_budget_and_bad_datagrams_and_discard() -> None:
    now = [0.0]
    w, got = _worker(now, budget=1000)
    w.start()
    try:
        for _ in range(100):
            w.raw.put(_v5_item(1.0, n=30))  # 3 000 records, burst 2 000
        w.raw.put(RawItem(b"\x00\x05garbage", EXP, EXP, False, 1.0))
        w.raw.put(RawItem(b"\x00\x0a\x00\x10" + b"\x00" * 12, EXP, EXP, False, 1.0))
        assert w.request_flush(5)
        rec, drops = w.counters()
        assert 2000 <= rec <= 2100 and drops["over_budget"] == 3000 - rec
        assert drops["parse_error"] == 1
        assert w.records_per_s(window=60) >= 0
    finally:
        w.raw.put(_v5_item(2.0))
        w.stop(flush=False, timeout=2)
    assert w.agg.buckets == {}


def test_worker_stop_with_flush_delivers_open_minute() -> None:
    now = [0.0]
    w, got = _worker(now)
    w.start()
    w.raw.put(_v5_item(1.0))
    time.sleep(0.3)
    w.stop(flush=True, timeout=3)
    assert [f.bytes for fl, _ in got for f in fl] == [80]
