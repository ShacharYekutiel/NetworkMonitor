"""Self-test of the Bedrock mock (run: backend/.venv/bin/python -m pytest tests/sim/bedrock -q).

Proves the mock is a faithful counterpart before the product is tested against it: contract vectors, botocore
signature verification, X.509 Roles Anywhere sessions, scripted scenarios, modes, validation, cancellation.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "injection"))

import client_helpers as ch  # noqa: E402
import es_codec  # noqa: E402
import scripted_model as sm  # noqa: E402
from mock_bedrock import STATIC_KEYS, MockBedrock  # noqa: E402

AK, SK = next(iter(STATIC_KEYS.items()))
MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture(scope="module")
def mock(tmp_path_factory):
    with MockBedrock(pki_dir=str(tmp_path_factory.mktemp("pki"))) as m:
        yield m


@pytest.fixture(autouse=True)
def _reset(mock):
    mock.reset()
    yield


def stream(mock, body, model=MODEL, region="us-east-1", **kw):
    r = ch.call(mock.url, ch.model_path(model), body, access_key=kw.pop("ak", AK), secret_key=kw.pop("sk", SK),
                region=region, **kw)
    return r, (es_codec.decode_events(r.content) if r.status_code == 200 else None)


def text_of(events):
    return "".join(e["body"]["delta"].get("text", "") for e in events if e["type"] == "contentBlockDelta")


# ------------------------------------------------------------------------------------------------ vectors
def test_eventstream_vector_and_chunked_decode():
    frame = es_codec.vector_frame()
    assert frame.hex() == es_codec.VECTOR_HEX
    buf, msgs = b"", []
    for b in frame:
        buf += bytes([b])
        got, buf = es_codec.decode(buf)
        msgs += got
    assert len(msgs) == 1 and json.loads(msgs[0]["payload"])["delta"]["text"] == "Hi"
    bad = bytearray(frame); bad[2] ^= 1
    with pytest.raises(es_codec.DecodeError, match="bad_prelude_crc"):
        es_codec.decode(bytes(bad))
    bad = bytearray(frame); bad[-10] ^= 1
    with pytest.raises(es_codec.DecodeError, match="bad_message_crc"):
        es_codec.decode(bytes(bad))


# ------------------------------------------------------------------------------------------------ converse
def test_ask_two_rounds_with_tool_use_and_links(mock):
    body = ch.converse_body("Which devices are down right now and since when? (Answer in English.)",
                            tools=["list_problems", "search_devices"])
    r, ev = stream(mock, body)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/vnd.amazon.eventstream"
    types = [e["type"] for e in ev]
    assert types[0] == "messageStart" and "contentBlockStart" in types and types[-2:] == ["messageStop", "metadata"]
    start = next(e for e in ev if e["type"] == "contentBlockStart")["body"]["start"]["toolUse"]
    assert start["name"] == "list_problems"
    inp = "".join(e["body"]["delta"]["toolUse"]["input"] for e in ev if e["type"] == "contentBlockDelta")
    assert json.loads(inp) == {}
    assert ev[-2]["body"]["stopReason"] == "tool_use"
    usage = ev[-1]["body"]["usage"]
    assert usage["cacheWriteInputTokens"] > 0 and usage["cacheReadInputTokens"] == 0
    result = {"ok": True, "tool": "list_problems", "rows": 1, "truncated": False, "untrusted_fields": ["message"],
              "note": None, "data": {"items": [{"sensor_id": 7, "ref": "sensor/7", "sensor_name": "Ping",
                                                "device_id": 3, "device_ref": "device/3", "device_name": "core-sw",
                                                "status": "down", "message": "timeout", "since": "2026-11-09T11:58:00Z"}]}}
    body["messages"] += [
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": start["toolUseId"], "name": "list_problems", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": start["toolUseId"], "status": "success",
                                                     "content": [{"json": result}]}}]}]
    r, ev = stream(mock, body)
    assert r.status_code == 200, r.text
    t = text_of(ev)
    assert "[core-sw / Ping](nm:sensor/7) is **down** since 2026-11-09T11:58:00Z" in t
    assert ev[-2]["body"]["stopReason"] == "end_turn"
    assert ev[-1]["body"]["usage"]["cacheReadInputTokens"] > 0
    log = mock.requests("converse_stream")
    assert [e["turn_index"] for e in log] == [0, 1]
    assert all(e["sig"]["ok"] and e["purpose"] == "ask" and e["scenario"] == "problems_down" for e in log)
    assert log[0]["offered_tools"] == ["list_problems", "search_devices"] and log[0]["has_cache_point"]


def test_non_streaming_converse_and_forced_tool(mock):
    body = ch.converse_body("ping", tools=["ping"], tool_choice={"tool": {"name": "ping"}}, max_tokens=50)
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["stopReason"] == "tool_use"
    assert j["output"]["message"]["content"][0]["toolUse"]["name"] == "ping"
    assert mock.requests("converse")[0]["purpose"] == "test"


def test_purpose_marker_and_hebrew_variant(mock):
    body = ch.converse_body("{\"device\": {\"name\": \"core-sw\"}} Answer in Hebrew.", system="Purpose: alert_note")
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    assert r.status_code == 200
    assert "בדוק" in r.json()["output"]["message"]["content"][0]["text"]
    body = ch.converse_body("{\"device\": {\"name\": \"core-sw\"}}", system="Purpose: alert_note")
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    assert "upstream link of core-sw" in r.json()["output"]["message"]["content"][0]["text"]


def test_final_no_tool_call_and_unknown_question(mock):
    msgs = [{"role": "user", "content": [{"text": "[qa:loop] go"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "list_problems", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"json": {"ok": True, "rows": 3}}]}},
                                         {"text": "Do not call tools; answer with what you have."}]}]
    body = ch.converse_body("", tools=["list_problems"], messages=msgs)
    r, ev = stream(mock, body)
    assert r.status_code == 200, r.text
    assert "I stopped after 1 lookups" in text_of(ev)
    assert mock.requests()[0]["final_call"] is True


def test_signature_errors(mock):
    body = ch.converse_body("hi", tools=["list_problems"])
    r, _ = stream(mock, body, sk="wrong-secret")
    assert r.status_code == 403 and r.headers["x-amzn-ErrorType"].startswith("InvalidSignatureException")
    r, _ = stream(mock, body, ak="AKIAUNKNOWN000000000")
    assert r.headers["x-amzn-ErrorType"].startswith("UnrecognizedClientException")
    mock.mode("clock_skew")
    r, _ = stream(mock, body)
    assert r.status_code == 403 and "Signature expired" in r.json()["message"]


def test_model_ids_by_region(mock):
    body = ch.converse_body("[qa:notools]", tools=["list_problems"])
    assert stream(mock, body, model="global.anthropic.claude-sonnet-4-5-20250929-v1:0", region="il-central-1")[0].status_code == 200
    r, _ = stream(mock, body, model="us.anthropic.claude-sonnet-4-5-20250929-v1:0", region="il-central-1")
    assert r.status_code == 400 and r.json()["message"] == "The provided model identifier is invalid."
    r, _ = stream(mock, body, model="anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert r.status_code == 400 and "on-demand throughput isn’t supported" in r.json()["message"]
    log = mock.requests()
    assert log[0]["raw_path"].startswith("/model/global.anthropic.claude-sonnet-4-5-20250929-v1%3A0/") and log[0]["model_id_encoded"]


@pytest.mark.parametrize("mode,etype,status", [
    ("access_denied", "AccessDeniedException", 403), ("model_access", "AccessDeniedException", 403),
    ("model_invalid", "ValidationException", 400), ("on_demand_unsupported", "ValidationException", 400),
    ("quota", "ServiceQuotaExceededException", 400), ("unavailable", "ServiceUnavailableException", 503),
    ("expired_token", "ExpiredTokenException", 403), ("validation", "ValidationException", 400)])
def test_error_modes(mock, mode, etype, status):
    mock.mode(mode)
    r, _ = stream(mock, ch.converse_body("x", tools=["list_problems"]))
    assert r.status_code == status and r.headers["x-amzn-ErrorType"].split(":")[0] == etype


def test_throttle_then_success_and_in_stream_exception(mock):
    mock.mode("throttle:2")
    body = ch.converse_body("[qa:notools]", tools=["list_problems"])
    assert [stream(mock, body)[0].status_code for _ in range(3)] == [429, 429, 200]
    mock.mode("throttle_stream:1:2")
    r, ev = stream(mock, ch.converse_body("[qa:long]", tools=["list_problems"]))
    assert r.status_code == 200
    assert ev[-1]["message_type"] == "exception" and ev[-1]["type"] == "throttlingException"
    assert sum(1 for e in ev if e["type"] == "contentBlockDelta") == 2


def test_cache_unsupported_mode(mock):
    mock.mode("cache_unsupported")
    r, _ = stream(mock, ch.converse_body("[qa:notools]", tools=["list_problems"]))
    assert r.status_code == 400 and "cachePoint" in r.json()["message"]
    r, _ = stream(mock, ch.converse_body("[qa:notools]", tools=["list_problems"], cache=False))
    assert r.status_code == 200


def test_sampling_and_forced_tool_modes(mock):
    mock.mode("sampling_unsupported")
    r, _ = stream(mock, ch.converse_body("[qa:notools]", tools=["list_problems"]))
    assert r.status_code == 400 and "temperature" in r.json()["message"]
    body = ch.converse_body("[qa:notools]", tools=["list_problems"])
    del body["inferenceConfig"]["temperature"]
    assert stream(mock, body)[0].status_code == 200
    mock.reset()
    mock.mode("forced_tool_unsupported")
    body = ch.converse_body("ping", tools=["ping"], tool_choice={"tool": {"name": "ping"}}, system="[purpose: test]")
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    assert r.status_code == 400 and "tool_choice" in r.json()["message"]
    body["toolConfig"]["toolChoice"] = {"auto": {}}
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    assert r.json()["output"]["message"]["content"][0]["toolUse"]["name"] == "ping"


def test_converse_validation(mock):
    msgs = [{"role": "user", "content": [{"text": "q"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "a", "name": "list_problems", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "b", "content": [{"json": {}}]}}]}]
    r, _ = stream(mock, ch.converse_body("", tools=["list_problems"], messages=msgs))
    assert r.status_code == 400 and "Expected toolResult blocks" in r.json()["message"]
    r, _ = stream(mock, ch.converse_body("", tools=None, messages=msgs))
    assert "toolConfig field must be defined" in r.json()["message"]


def test_max_tokens_cut(mock):
    r, ev = stream(mock, ch.converse_body("[qa:maxtokens]", tools=["list_problems"]))
    assert ev[-2]["body"]["stopReason"] == "max_tokens"
    r, ev = stream(mock, ch.converse_body("[qa:long]", tools=["list_problems"], max_tokens=5))
    assert ev[-2]["body"]["stopReason"] == "max_tokens" and len(text_of(ev)) <= 20


def test_client_cancel_is_recorded(mock):
    body = ch.converse_body("[qa:long]", tools=["list_problems"])
    raw = json.dumps(body).encode()
    url = mock.url + ch.model_path(MODEL)
    h = ch.sigv4_headers("POST", url, raw, access_key=AK, secret_key=SK)
    with httpx.Client(timeout=30) as c, c.stream("POST", url, content=raw, headers=h) as r:
        it = r.iter_bytes()
        next(it)
        t_close = time.time()
    for _ in range(50):
        e = mock.requests("converse_stream")[0]
        if e.get("closed_by_client"):
            break
        time.sleep(0.1)
    assert e["closed_by_client"] and e["closed_at"] - t_close < 1.0 and not e.get("complete")


def test_slow_and_stall_modes(mock):
    mock.mode("slow:1.5")
    t = time.time()
    r, ev = stream(mock, ch.converse_body("[qa:notools]", tools=["list_problems"]))
    assert r.status_code == 200 and time.time() - t >= 1.5
    mock.mode("stall:1")
    with pytest.raises(httpx.ReadTimeout):
        ch.call(mock.url, ch.model_path(MODEL), ch.converse_body("[qa:long]", tools=["list_problems"]),
                access_key=AK, secret_key=SK, timeout=2)


def test_runtime_scenario_and_templates(mock):
    mock.add_scenario({"name": "qa_rt", "match": "runtime-q", "turns": [
        {"tool_use": [{"name": "get_device", "input": {"device_id": "{{ $question | re(\"dev (\\d+)\") | int }}"}}]},
        {"text": "{{ $tool.get_device.data.name }} / {{ $all | len }}"}]})
    body = ch.converse_body("runtime-q dev 42", tools=["get_device"])
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    tu = r.json()["output"]["message"]["content"][0]["toolUse"]
    assert tu["input"] == {"device_id": 42}
    body["messages"] += [{"role": "assistant", "content": [{"toolUse": tu}]},
                         {"role": "user", "content": [{"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": {"ok": True, "data": {"name": "sw42"}}}]}}]}]
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    assert r.json()["output"]["message"]["content"][0]["text"] == "sw42 / 1"


def test_template_filters():
    errs: list[str] = []
    ctx = {"last": {"data": {"items": [{"id": 1, "type": "email"}, {"id": 5, "type": "telegram", "name": "tg"}]}}, "question": "x"}
    assert sm.render("{{ $.data.items | where(\"type=telegram\") | first | get(\"id\") }}", ctx, errs) == 5
    assert sm.render("{{ $.data.items[*].id | join(\"+\") }}", ctx, errs) == "1+5"
    assert sm.render("a {{ $.data.items | each(\"[{name}]\") }}", ctx, errs) == "a [?]\n[tg]"
    assert sm.render("{{ $.nope | default(7) }}", ctx, errs) == 7
    assert not errs


# ------------------------------------------------------------------------------------------------ KB, control plane
def test_retrieve_and_modes(mock):
    body = {"retrievalQuery": {"text": "test"}, "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 2}}}
    r = ch.call(mock.url, "/knowledgebases/QAKBTEST01/retrieve", body, access_key=AK, secret_key=SK)
    assert r.status_code == 200 and len(r.json()["retrievalResults"]) == 2
    assert r.json()["retrievalResults"][0]["location"]["type"] == "S3"
    r = ch.call(mock.url, "/knowledgebases/NOPE000000/retrieve", body, access_key=AK, secret_key=SK)
    assert r.status_code == 404
    mock.mode("kb_empty")
    assert ch.call(mock.url, "/knowledgebases/QAKBTEST01/retrieve", body, access_key=AK, secret_key=SK).json() == {"retrievalResults": []}
    mock.mode("kb_error")
    assert ch.call(mock.url, "/knowledgebases/QAKBTEST01/retrieve", body, access_key=AK, secret_key=SK).status_code == 403


def test_list_models_by_region(mock):
    r = ch.call(mock.url, "/inference-profiles?typeEquals=SYSTEM_DEFINED", None, access_key=AK, secret_key=SK,
                method="GET", region="il-central-1")
    ids = [p["inferenceProfileId"] for p in r.json()["inferenceProfileSummaries"]]
    assert ids and all(i.startswith("global.") for i in ids if "anthropic" in i)
    r = ch.call(mock.url, "/inference-profiles", None, access_key=AK, secret_key=SK, method="GET", region="us-east-1")
    assert any(p["inferenceProfileId"].startswith("us.anthropic") for p in r.json()["inferenceProfileSummaries"])


# ------------------------------------------------------------------------------------------------ Roles Anywhere
RA_BODY = {"durationSeconds": 3600, "profileArn": MockBedrock.ra_arns["profile"], "roleArn": MockBedrock.ra_arns["role"],
           "trustAnchorArn": MockBedrock.ra_arns["trust_anchor"]}


def create_session(mock, leaf, chain=(), region="us-east-1", body=None, password=None, key=None):
    raw = json.dumps(body or RA_BODY).encode()
    lf = mock.pki["leaves"][leaf]
    h = ch.x509_headers("POST", mock.url + "/sessions", raw, cert_pem=Path(lf["cert"]).read_bytes(),
                        key_pem=Path(key or lf["key"]).read_bytes(), password=password,
                        chain_pems=[Path(p).read_bytes() for p in chain], region=region)
    return httpx.post(mock.url + "/sessions", content=raw, headers=h)


@pytest.mark.parametrize("leaf", ["leaf-rsa", "leaf-ec", "leaf-ec384"])
def test_roles_anywhere_session_then_bedrock(mock, leaf):
    r = create_session(mock, leaf)
    assert r.status_code == 201, r.text
    c = r.json()["credentialSet"][0]["credentials"]
    assert c["accessKeyId"].startswith("ASIA") and c["expiration"].endswith("Z")
    e = mock.requests("create_session")[0]
    assert e["sig"]["serial"] == mock.pki["leaves"][leaf]["serial_decimal"]
    rr, ev = stream(mock, ch.converse_body("[qa:notools]", tools=["list_problems"]), ak=c["accessKeyId"],
                    sk=c["secretAccessKey"], token=c["sessionToken"])
    assert rr.status_code == 200
    mock.state.config["clock_offset_s"] = 0
    requests_post = mock.requests("converse_stream")
    assert requests_post[0]["sig"]["access_key_id"] == c["accessKeyId"]
    # expiry -> ExpiredTokenException
    httpx.post(mock.url + "/_credentials/expire", json={"access_key_id": c["accessKeyId"]})
    rr, _ = stream(mock, ch.converse_body("[qa:notools]", tools=["list_problems"]), ak=c["accessKeyId"],
                   sk=c["secretAccessKey"], token=c["sessionToken"])
    assert rr.status_code == 403 and rr.headers["x-amzn-ErrorType"].startswith("ExpiredTokenException")


def test_roles_anywhere_rejections(mock):
    assert create_session(mock, "leaf-wrongca").json()["message"].startswith("Untrusted certificate")
    assert "expired" in create_session(mock, "leaf-expired").json()["message"]
    assert "CA:TRUE" in create_session(mock, "leaf-catrue").json()["message"]
    assert create_session(mock, "leaf-rsa", key=mock.pki["leaves"]["leaf-ec"]["key"]).status_code == 403
    # root as anchor: the leaf needs the issuing CA as the chain
    mock.config(anchor="root")
    assert create_session(mock, "leaf-rsa").status_code == 403
    assert create_session(mock, "leaf-rsa", chain=[mock.pki["files"]["issuing.pem"]]).status_code == 201
    assert mock.requests("create_session")[-1]["sig"]["chain"] is True
    mock.mode("unable_to_assume")
    assert "Unable to assume role" in create_session(mock, "leaf-rsa", chain=[mock.pki["files"]["issuing.pem"]]).json()["message"]


def test_roles_anywhere_encrypted_key_and_pfx_exist(mock):
    from cryptography.hazmat.primitives.serialization import pkcs12
    files = mock.pki["files"]
    k, c, extra = pkcs12.load_key_and_certificates(Path(files["leaf-rsa.pfx"]).read_bytes(), b"Pfx-pass-1")
    assert c.serial_number == 0x1234 and len(extra) == 1
    r = create_session(mock, "leaf-rsa", key=files["leaf-rsa.enc.key"], password=b"Key-pass-1")
    assert r.status_code == 201


def test_csr_flow_sign_and_session(mock, tmp_path):
    import pki
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    from cryptography.x509.oid import NameOID
    key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (_x509.CertificateSigningRequestBuilder().subject_name(_x509.Name([_x509.NameAttribute(NameOID.COMMON_NAME, "netmonitor-bedrock.mnidom.net")]))
           .sign(key, hashes.SHA256()))
    pem = pki.sign_csr(mock.pki["dir"], csr.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "c.pem").write_bytes(pem)
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    raw = json.dumps(RA_BODY).encode()
    h = ch.x509_headers("POST", mock.url + "/sessions", raw, cert_pem=pem, key_pem=(tmp_path / "k.pem").read_bytes())
    assert httpx.post(mock.url + "/sessions", content=raw, headers=h).status_code == 201


# ------------------------------------------------------------------------------------------------ IMDS / container
def test_imds_v2_and_container(mock):
    tok = httpx.put(mock.url + "/latest/api/token", headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"}).text
    assert httpx.get(mock.url + "/latest/meta-data/iam/security-credentials/").status_code == 401
    role = httpx.get(mock.url + "/latest/meta-data/iam/security-credentials/", headers={"X-aws-ec2-metadata-token": tok}).text
    j = httpx.get(mock.url + f"/latest/meta-data/iam/security-credentials/{role}", headers={"X-aws-ec2-metadata-token": tok}).json()
    assert j["Code"] == "Success"
    r, _ = stream(mock, ch.converse_body("[qa:notools]", tools=["list_problems"]), ak=j["AccessKeyId"],
                  sk=j["SecretAccessKey"], token=j["Token"])
    assert r.status_code == 200
    assert httpx.put(mock.url + "/latest/api/token", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60",
                                                               "X-Forwarded-For": "10.0.0.1"}).status_code == 403
    assert httpx.get(mock.url + "/container-credentials").status_code == 403
    cc = httpx.get(mock.url + "/container-credentials", headers={"Authorization": "mock-container-auth-token"}).json()
    assert cc["AccessKeyId"].startswith("ASIA")


def test_https_listener(tmp_path):
    with MockBedrock(https=True, pki_dir=str(tmp_path / "pki")) as m:
        r = httpx.get(m.url + "/_health", verify=m.pki["files"]["trust.pem"])
        assert r.json() == {"ok": True}


# ------------------------------------------------------------------------------------------------ injection corpus
def test_injection_checks_and_obey_scenario(mock):
    from injection_checks import answer_problems, html_problems, load_corpus, telegram_problems
    corpus = load_corpus()
    assert len(corpus["entries"]) >= 15
    msgs = [{"role": "user", "content": [{"text": "[qa:inj] summarise the logs, other device 9"}]}]
    body = ch.converse_body("", tools=["search_syslog", "config_diff"], messages=msgs)
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    tu = r.json()["output"]["message"]["content"][0]["toolUse"]
    assert tu["name"] == "search_syslog"
    syslog = {"ok": True, "rows": 1, "data": {"items": [{"ref": "syslog/5", "message": corpus["entries"][1]["text"]}]}}
    msgs += [{"role": "assistant", "content": [{"toolUse": tu}]},
             {"role": "user", "content": [{"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": syslog}]}}]}]
    r = ch.call(mock.url, ch.model_path(MODEL, "converse"), body, access_key=AK, secret_key=SK)
    blocks = r.json()["output"]["message"]["content"]
    names = [b["toolUse"]["name"] for b in blocks if "toolUse" in b]
    assert names == ["update_device", "config_diff"]
    assert next(b for b in blocks if "toolUse" in b and b["toolUse"]["name"] == "config_diff")["toolUse"]["input"] == {"device_id": 9}
    # the checkers flag what an unsafe product would render
    assert answer_problems("![x](https://exfil.attacker.test/a) [a](nm:device/9)", {"device/1"})
    assert answer_problems("[a](nm:device/1)", {"device/1"}) == []
    assert html_problems('<p><img src="https://exfil.attacker.test/x"><a href="https://exfil.attacker.test">a</a></p>')
    assert html_problems('<p><a href="/devices/1">core</a></p>', "http://127.0.0.1") == []
    assert telegram_problems("<b>ok</b> &lt;script&gt;", "https://nm.test") == []
    assert telegram_problems("<script>x</script> & y", "https://nm.test")


def test_concurrent_streams(mock):
    out = []

    def one(i):
        r, ev = stream(mock, ch.converse_body(f"[qa:notools] {i}", tools=["list_problems"]))
        out.append(r.status_code)
    ts = [threading.Thread(target=one, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == [200] * 6
