"""[3] Chat acknowledge pure parts: ack tokens (data-model vectors), link codes, commands,
channel payload / Telegram markup."""

from __future__ import annotations

import pytest

from netmon.chatack import linking, service, tokens

KEY = bytes(range(1, 33))


def test_token_vector() -> None:
    tok = tokens.encode(KEY, 7, 991, 29_400_000)
    assert tok.startswith("a1.") and len(tok) == 41
    assert len(tok.encode()) <= 64  # Telegram callback_data
    assert tokens.decode(KEY, tok) == (7, 991, 29_400_000)
    assert tokens.decode(KEY, tok, now_min=29_400_000) == (7, 991, 29_400_000)


def test_token_every_flipped_character_is_invalid() -> None:
    tok = tokens.encode(KEY, 7, 991, 29_400_000)
    for i in range(3, len(tok)):
        c = tok[i]
        repl = "A" if c != "A" else "B"
        bad = tok[:i] + repl + tok[i + 1 :]
        with pytest.raises(tokens.AckTokenError) as ei:
            tokens.decode(KEY, bad)
        assert ei.value.reason == "invalid"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: "a2." + t[3:],
        lambda t: t + "A",
        lambda t: t[:-1],
        lambda t: "",
        lambda t: t[:20] + "+" + t[21:],
    ],
)
def test_token_invalid(mutate) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(tokens.AckTokenError) as ei:
        tokens.decode(KEY, mutate(tokens.encode(KEY, 7, 991, 29_400_000)))
    assert ei.value.reason == "invalid"


def test_token_expired_and_wrong_key() -> None:
    tok = tokens.encode(KEY, 7, 991, 100)
    with pytest.raises(tokens.AckTokenError) as ei:
        tokens.decode(KEY, tok, now_min=101)
    assert ei.value.reason == "expired"
    with pytest.raises(tokens.AckTokenError) as ei:
        tokens.decode(bytes(32), tok)
    assert ei.value.reason == "invalid"
    assert tokens.derive_key("abc") == tokens.derive_key("abc") != tokens.derive_key("abd")


def test_link_codes() -> None:
    code = linking.new_code()
    assert code.startswith("NM-") and len(code) == 11
    assert linking.normalize(code.lower()) == code[3:]
    assert linking.normalize(code[3:]) == code[3:]
    assert linking.code_hash(code) == linking.code_hash(code[3:].lower())
    assert linking.normalize("NM-1234ABCD") is None  # 1 is not base32
    assert linking.code_hash("too-long-code") is None


def test_parse_command() -> None:
    assert linking.parse_command("/link NM-7KQ2MZ4P", "Bot") == ("link", "NM-7KQ2MZ4P")
    assert linking.parse_command("/link@NetMonBot NM-7KQ2MZ4P", "netmonbot") == (
        "link",
        "NM-7KQ2MZ4P",
    )
    assert linking.parse_command("/link@OtherBot NM-7KQ2MZ4P", "NetMonBot") is None
    assert linking.parse_command("/unlink", "B") == ("unlink", None)
    assert linking.parse_command("hello", "B") is None
    assert linking.parse_command("/link", "B") is None
    assert linking.display_of({"id": 5, "username": "jdoe"}) == "@jdoe"
    assert linking.display_of({"id": 5, "first_name": "J", "last_name": "Doe"}) == "J Doe"


class _Feat:
    def __init__(self, on: bool) -> None:
        self.on = on

    def enabled(self, name: str) -> bool:
        return self.on and name == "chat_ack"


class _Clock:
    def now(self):  # type: ignore[no-untyped-def]
        from datetime import UTC, datetime

        return datetime(2026, 11, 8, 12, tzinfo=UTC)


class _Rt:
    def __init__(self, on: bool) -> None:
        self.features = _Feat(on)
        self.clock = _Clock()
        self.secret_key = "s" * 40


def _payload(stage: str = "problem") -> dict:
    return {"stage": stage, "event_id": 991, "sensor": {"id": 7}, "url": "u"}


def test_decorate_payload() -> None:
    cfg = {"public_base_url": "https://nm.example/", "chat_ack_ttl_h": 2}
    off = service.decorate_payload(_Rt(False), _payload(), cfg)
    assert off["ack_url"] is None and "ack_token" not in off
    for stage in ("recovery", None):
        p = service.decorate_payload(_Rt(True), _payload(stage), cfg)  # type: ignore[arg-type]
        assert p["ack_url"] is None
    rt = _Rt(True)
    p = service.decorate_payload(rt, _payload("reminder"), cfg)
    tok = p["ack_token"]
    assert p["ack_url"] == f"https://nm.example/ack#{tok}"
    sid, eid, exp = tokens.decode(service.key_of(rt), tok, service.now_min(rt))
    assert (sid, eid, exp - service.now_min(rt)) == (7, 991, 120)
    nobase = service.decorate_payload(rt, _payload(), {})
    assert nobase["ack_url"] is None and nobase["ack_token"]


def test_channel_payload_and_markup() -> None:
    p = {"stage": "problem", "ack_token": "a1.TOKEN", "ack_url": "https://x/ack#a1.TOKEN"}
    wh = service.channel_payload({"type": "webhook", "config": {}}, p)
    assert "ack_token" not in wh and wh["ack_url"] == p["ack_url"]
    assert service.channel_payload({"type": "email", "config": {}}, p)["ack_url"] is None
    em = service.channel_payload({"type": "email", "config": {"ack_link": True}}, p)
    assert em["ack_url"] == p["ack_url"]
    assert service.channel_payload({"type": "teams", "config": {}}, p)["ack_url"]
    tm = service.channel_payload({"type": "teams", "config": {"ack_link": False}}, p)
    assert tm["ack_url"] is None
    tg = service.channel_payload({"type": "telegram", "config": {}}, p)
    assert tg["ack_url"] is None
    btn = service.telegram_markup({"ack_buttons": True}, tg, "en")
    assert btn == {"inline_keyboard": [[{"text": "Acknowledge", "callback_data": "a1.TOKEN"}]]}
    link = service.telegram_markup({"ack_link": True}, tg, "he")
    assert link == {"inline_keyboard": [[{"text": "אישור טיפול", "url": p["ack_url"]}]]}
    assert service.telegram_markup({}, tg, "en") is None
    plain = service.channel_payload({"type": "telegram", "config": {}}, {"stage": "problem"})
    assert service.telegram_markup({"ack_buttons": True}, plain, "en") is None


def test_teams_card_ack_action() -> None:
    from netmon.alerts import teams

    payload = {
        "event": "sensor.status_changed",
        "stage": "problem",
        "new_status": "down",
        "device": {"name": "d"},
        "sensor": {"name": "s"},
        "url": "https://x/sensors/1",
        "ack_url": "https://x/ack#a1.T",
    }
    actions = teams.build_card(payload, "en")["attachments"][0]["content"]["actions"]
    assert actions[0] == {
        "type": "Action.OpenUrl",
        "title": "Acknowledge",
        "url": "https://x/ack#a1.T",
    }
    assert actions[1]["url"] == "https://x/sensors/1"


def test_email_ack_button() -> None:
    from netmon.alerts import templates

    payload = {
        "event": "sensor.status_changed",
        "stage": "problem",
        "new_status": "down",
        "old_status": "up",
        "message": "m",
        "time": None,
        "ack_url": "https://x/ack#a1.T",
    }
    assert "https://x/ack#a1.T" in templates.html_body(payload, "en")
    assert "Acknowledge: https://x/ack#a1.T" in templates.text_body(payload, "en")
    payload["ack_url"] = None
    assert "ack#" not in templates.html_body(payload, "en")
