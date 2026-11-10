# Bedrock mock (phase 3b QA)

A single-process stand-in for everything the assistant talks to in AWS (docs/architecture.md §14.11):
Bedrock Runtime `Converse` / `ConverseStream`, Knowledge Base `Retrieve`, IAM Roles Anywhere `CreateSession`,
the control plane (*List models*), EC2 IMDSv2 and container credentials. No AWS account, no network.

| File | What |
|---|---|
| `mock_bedrock.py` | Starlette app + CLI + `MockBedrock` (in-process, uvicorn thread) |
| `scripted_model.py` | the scripted "model": scenario selection, templates, Converse request validation |
| `scenarios/*.yaml` | default scenarios (`ask.yaml`, `features.yaml`, `injection.yaml`) |
| `es_codec.py` | own `application/vnd.amazon.eventstream` encoder + reference decoder (contract vector) |
| `sigv4_check.py` | SigV4 verification with **botocore** `SigV4Auth`; Roles Anywhere X.509 verification |
| `pki.py` | test PKI: root -> issuing CA -> leaves (RSA/EC, expired, wrong CA, CA:TRUE, ...), `.pfx`, TLS cert |
| `client_helpers.py` | reference SigV4 / X.509 signers and request builders (self-tests, debugging) |
| `pytest_fixture.py` | `bedrock_mock`, `assistant_settings` fixtures for backend tests |
| `injection/` | prompt-injection corpus (`corpus.yaml`) + mechanical checkers (`injection_checks.py`) |
| `test_mock_selftest.py` | self-test of the mock (`backend/.venv/bin/python -m pytest tests/sim/bedrock -q`) |

`botocore` is a **test-only** dependency (`backend/pyproject.toml` `[dev]`), never imported by `netmon`.

## Running

```bash
# process (E2E): prints READY {"port", "url", "https_port", "https_url", "pki"}
backend/.venv/bin/python tests/sim/bedrock/mock_bedrock.py --port 0 --https-port 0 --pki /tmp/bpki --log /tmp/bedrock.jsonl
```

```python
# in-process (pytest)
sys.path.insert(0, "<repo>/tests/sim/bedrock")
from pytest_fixture import bedrock_mock, assistant_settings   # fixtures
from mock_bedrock import MockBedrock
with MockBedrock() as m:          # m.url, m.mode(...), m.requests("converse_stream"), m.add_scenario({...}), m.reset()
    ...
```

Point the backend at it (`NETMON_TEST_MODE=1` allows `http://`):
`endpoint_url`, `control_endpoint_url`, `ra_endpoint_url`, `assistant_kb.endpoint_url` = the mock URL;
`AWS_EC2_METADATA_SERVICE_ENDPOINT=<url>` for IMDSv2; `AWS_CONTAINER_CREDENTIALS_FULL_URI=<url>/container-credentials`
with `AWS_CONTAINER_AUTHORIZATION_TOKEN=mock-container-auth-token`. For HTTPS append `<pki>/trust.pem` to
`NETMON_EXTRA_CA_FILE` (`os.pathsep`-separated list) and use `https://localhost:<https_port>`.

### Credentials the mock accepts
| Mode | Values |
|---|---|
| `access_key` | `AKIAMOCKSTATIC000001` / `mockStaticSecretKey/000000000000000000000` (and `...002` / `...0002`); add more with `POST /_credentials` |
| `roles_anywhere` | any leaf of the test PKI issued by the trust anchor (default anchor: issuing CA; `--anchor root` or `POST /_config {"anchor": "root"}` requires the chain). ARNs: `MockBedrock.ra_arns` (`arn:aws:rolesanywhere:us-east-1:111122223333:trust-anchor/7a1b2c3d-...0001`, `...:profile/...0002`, `arn:aws:iam::111122223333:role/netmon-bedrock`). `.pfx` password `Pfx-pass-1`, encrypted key password `Key-pass-1`. Issued session credentials (`ASIA…`) are valid for `durationSeconds` (override: `POST /_config {"session_seconds": 60}` or mode `ra_short:60`) and then fail with `ExpiredTokenException`. |
| `default_chain` | IMDSv2 (`PUT /latest/api/token` with TTL header, then role `netmon-bedrock-role`; a request with `X-Forwarded-For`/`Via` -> 403, i.e. "went through a proxy") or container credentials |

### Models and regions (owner decision: `us-east-1` + `us.` profile; `il-central-1` + `global.`)
`strict_models` (default on) mimics AWS: `us.`/`eu.`/`apac.`/`global.` + a known Claude id are valid only in their
geography's regions (`il-central-1` accepts only `global.`); bare `anthropic.claude-…` ids fail with the real
on-demand text (`"…with on-demand throughput isn’t supported…"` — note the **U+2019 apostrophe**, the backend must
not match a plain `'`), except `anthropic.claude-3-haiku-20240307-v1:0` (on-demand allowed). `anthropic.claude-test*`
ids are accepted for unit tests. Inference-profile/provisioned ARNs must name the signed region.
Known ids: `anthropic.claude-sonnet-4-5-20250929-v1:0`, `anthropic.claude-haiku-4-5-20251001-v1:0`,
`anthropic.claude-opus-4-1-20250805-v1:0`.

## Purpose detection

The scenario's `purpose` is matched against the request's purpose, detected in this order:
1. **Marker in the system prompt** (please emit one): a line `Purpose: <purpose>` (or `[purpose:<purpose>]`),
   `<purpose>` from data-model `assistant_purpose` (`ask`, `explain`, `incident`, `alert_note`, `diff_explain`,
   `digest`, `rule_draft`, `template_draft`, `chat_ask`, `report_summary`, `test`).
2. Forced tool: `ping` -> `test`, `propose_alert_rule`/`propose_event_rule` -> `rule_draft`, `propose_template` ->
   `template_draft`, `report_diff_explanation` -> `diff_explain`, `report_incident_summary` -> `incident`, ...
3. Keywords in the system prompt (alert note, incident, digest, ...); otherwise `ask`.
The detected purpose is in the request log (`purpose`).

## Scenario format (`scenarios/*.yaml`, or `POST /_scenarios`)

```yaml
scenarios:
  - name: problems_down          # unique
    purpose: [ask, chat_ask]     # or "*"
    match: '\b(down|problems?)\b'  # regex (case-insensitive) on the question = last user message with text and no toolResult
    priority: 0                  # higher wins; runtime scenarios default to 100 and win ties
    requires_tools: []           # only selected when these tools are offered
    lang: he                     # optional: only when the product asked for Hebrew ("Answer/Write in Hebrew" in the
                                 # question wrapper or system prompt, else >= 30 % Hebrew letters in the question)
    match_system: 'Write in'     # optional regex on the system prompt
    stream: {chunk_chars: 16, delay_ms: 10, first_delay_ms: 0}
    usage: {inputTokens: 1200, outputTokens: 80, cacheReadInputTokens: 0, cacheWriteInputTokens: 0}  # optional; else estimated
    turns:                       # turn i answers the i-th model call after the question (stateless: i = number of
                                 # assistant messages after the question)
      - text_before: 'Let me look.'                 # optional text block before the tool calls (a "round note")
        tool_use: [{name: list_problems, input: {}}]  # several entries = parallel tool calls; ids tooluse_mock<seq>_<k>
        if_not_offered: {text: 'Syslog is off.'}      # played instead when a named tool is not offered (else sent anyway)
      - text: '{{ $.rows }} problems: {{ $.data.items | each("[{device_name}](nm:{device_ref})") }}'
        stop_reason: end_turn    # or max_tokens
      # - {tool_use: [...], repeat: true}   last turn repeats forever (limit tests)
      # - {raw: [{"text": "..."}, {"toolUse": {...}}]}  raw content blocks
    final: 'I stopped after {{ $all | len }} lookups.'  # the no-tools final call (limit reached), and past the last turn
    forced: {propose_alert_rule: {...}}                 # input when toolChoice forces a tool no turn defines
```
Forced `toolChoice: {"tool": {"name": X}}`: the first turn (from the current index) that calls X is played; else
`forced[X]`; else a minimal valid input generated from X's `inputSchema`. The **final no-tools call** is detected
when the system prompt or the last user text contains "do not call tools" / "answer with what you have", or when
`toolConfig` is present without `toolChoice` while the history has tool blocks.
A request without `toolConfig` never gets a tool call (the scenario's `final` text, or a fallback, is used).

### Templates
`{{ path | filter | ... }}` in any string (a string that is exactly one expression keeps the native type, e.g. an
int id). Roots: `$` = JSON of the first toolResult of the last user message; `$results[i]`; `$all[i]` (every tool
result since the question, in order); `$tool.<name>` (latest result of that tool); `$question`; `$input` (JSON
object found in the question text — the AI features' gathered input); `$system`; `$offered`; `$purpose`; `$now`,
`$hour_ago`, `$day_ago`, `$week_ago`, `$month_ago` (ISO UTC). Paths: `.key`, `[0]`, `[-1]`, `[*]` (map).
Filters: `len`, `json`, `join(", ")`, `first`, `last`, `take(n)`, `each("fmt {field}")` (one line per item,
missing fields -> `?`), `where("type=telegram")`, `get("id")`, `re("regex with (group)")`, `default(1)` (JSON arg),
`int`, `upper`, `lower`. Missing values render as `?` and are listed in the log entry's `template_errors`.

### Built-in triggers (E2E / pytest)
| Question contains | Scenario |
|---|---|
| down / problems / failing | `problems_down` (list_problems -> linked list) |
| Hebrew (למטה, תקלות, בעיות) | `problems_down_he` (search_devices -> Hebrew answer with host IPs) |
| `what happened on <name>` | `device_history` (search_devices -> list_events) |
| syslog / logs | `syslog_search` (search_syslog, or "Syslog is turned off…" when not offered) |
| traps | `traps_search` |
| `config … diff/change` or `[qa:config-diff]` (+ `device 12`) | `config_diff` (or "not available" when not offered) |
| `[qa:config-search] … text "snmp"` | `config_search` |
| a MAC / `where is <ip or IP-n>` | `mac_search` / `ip_search` (find_mac_or_ip) |
| availability / top talkers / runbook | `availability` / `top_talkers` / `knowledge` |
| `[qa:3rounds]` `[qa:parallel]` `[qa:retry]` `[qa:notools]` | 3 tool rounds / 3 parallel tools / invalid args then retry / no tools |
| `[qa:loop]` `[qa:loop-parallel]` | tools forever (model-call / tool-call limits) |
| `[qa:maxtokens]` `[qa:long]` `[qa:slowtool]` | stop_reason max_tokens / ~60 s stream (Stop, re-attach) / 3 s first event |
| `[qa:inj]` (+ `other device N`) | prompt-injection "obeying" model (see below) |
| `[qa:inj-kb]` | echoes KB passages + exfil image |
| poem / joke / weather | off-topic refusal |
| anything else (ask) | `ask_default` |

## Error and behaviour modes (`POST /_mode {"mode": "...", "target": "...", "times": n}`)

Target defaults: `converse` (runtime), `sessions` for Roles Anywhere modes, `kb` for `kb_*`, `imds` for IMDS modes;
`"all"` = converse+kb+control+sessions. `times` limits a mode to n calls (default: sticky until `DELETE /_mode`
or `POST /_reset`; `throttle:N` lasts N calls).

| Mode | Effect (status, `x-amzn-ErrorType`) | Product code (api.md) |
|---|---|---|
| `throttle:N` | first N calls 429 `ThrottlingException` | `bedrock_throttled` (retried) |
| `throttle_stream:N:K` | N streams send K deltas then in-stream `throttlingException` | `bedrock_throttled` |
| `access_denied` | 403 `AccessDeniedException` "User: … is not authorized to perform: bedrock:InvokeModelWithResponseStream …" | `bedrock_access_denied` |
| `model_access` | 403 `AccessDeniedException` "You don't have access to the model with the specified model ID." (real AWS text) | `bedrock_model_access` |
| `model_invalid` | 400 `ValidationException` "The provided model identifier is invalid." | `bedrock_model_invalid` |
| `on_demand_unsupported` | 400 `ValidationException` "Invocation of model ID … with on-demand throughput isn’t supported…" | `bedrock_model_invalid` |
| `not_found` | 404 `ResourceNotFoundException` | `bedrock_model_invalid` |
| `quota` | 400 `ServiceQuotaExceededException` | `bedrock_quota` |
| `validation` | 400 `ValidationException` (other) | `bedrock_validation` |
| `unavailable` / `model_timeout` / `model_not_ready` / `model_error` / `internal` | 503 / 408 / 429 / 424 / 500 | `bedrock_unavailable` |
| `stream_error:K` `stream_internal:K` `stream_unavailable:K` `stream_validation:K` | in-stream exception after K deltas | `bedrock_unavailable` / `bedrock_validation` |
| `expired_token` / `invalid_signature` / `unrecognized_client` | 403 | `aws_auth_failed` |
| `clock_skew[:seconds]` | mock clock +900 s: every signature -> "Signature expired: …" | `aws_clock_skew` |
| `cache_unsupported` | any `cachePoint` -> 400 `ValidationException` naming cachePoint (sticky) | retry without cache |
| `sampling_unsupported` | `temperature`/`topP` in `inferenceConfig` -> 400 `ValidationException` naming them (sticky) | retry without sampling params |
| `forced_tool_unsupported` | `toolChoice` `tool`/`any` -> 400 `ValidationException` naming `tool_choice` (sticky) | retry with `auto` (scenarios still call the tool) |
| `slow:S` | first stream event (or the non-stream response) after S s | timeouts / `stream_buffered` |
| `buffered:S` | the whole stream in one write after S s (buffering proxy) | warning `stream_buffered` |
| `stall:K` | K deltas, then silence (connection open) | idle timeout -> `assistant_timeout` |
| `hang[:S]` | no response headers for S s (default 600) | `assistant_timeout` |
| `untrusted_cert` / `unable_to_assume` / `profile_disabled` (sessions) | 403 `AccessDeniedException` | `rolesanywhere_failed` |
| `ra_short:S` (sessions) | issued credentials expire after S s | refresh before expiry |
| `kb_empty` / `kb_error` / `kb_validation` (kb) | `{"retrievalResults": []}` / 403 / 400 | `kb_unavailable` |
| `imds_down` / `imds_no_role` / `container_down` (imds) | 503 / 404 / 503 | `aws_auth_failed` (no source) |
| `normal` | clear the mode of a target (or all) | |

Signature failures happen without any mode: a wrong secret -> `InvalidSignatureException`, an unknown key ->
`UnrecognizedClientException`, a wrong/missing session token -> `UnrecognizedClientException`, an expired session ->
`ExpiredTokenException`, signing service other than `bedrock` -> `InvalidSignatureException`.
Converse requests are also **validated like Bedrock** (alternating roles, toolResult ids match the previous toolUse
ids, `toolConfig` required with tool blocks, `toolChoice` tool defined, no blank text blocks, last message from
the user) — a 400 `ValidationException` here is almost always a product bug.

## Request log (`GET /_requests`)

One JSON object per AWS request (also appended to `--log FILE`): `seq`, `time`, `op` (`converse`,
`converse_stream`, `retrieve`, `create_session`, `list_profiles`, `list_models`, `imds_token`, `imds_role`,
`imds_creds`, `container_creds`), `path`, `raw_path`, `headers`, `body` (exactly as received, parsed JSON),
`sig` (`ok`, `error`, `access_key_id`, `region`, `service`, `signed_headers`, `canonical_request_sha256`; for
`/sessions`: `alg`, `serial`, `subject`, `chain`), `region`, `model_id`, `model_id_encoded`, `purpose`, `scenario`,
`turn_index`, `offered_tools`, `forced_tool`, `tool_choice`, `has_cache_point`, `question`, `final_call`,
`max_tokens`, `mode`, `status`, `error_type`, `error_message`, `stop_reason`, `usage`, `response_blocks`,
`template_errors`, streaming: `events_sent`, `deltas_sent`, `first_event_at`, `complete`, `closed_by_client`,
`closed_at` (cancel tests: the product must close the Bedrock stream within 1 s), `stream_exception`.
Filters: `?op=converse,converse_stream&purpose=ask&since=<seq>`. `GET /_usage` sums the usage the mock returned
(per purpose) for usage-page assertions.

## Other control endpoints
`POST /_scenarios {"scenario": {...}}`, `DELETE /_scenarios`, `POST /_force_scenario {"name": "injection_obey"}`
(every question of a matching purpose plays it), `GET|POST /_kb {"knowledge_base_ids": [...], "passages": [...],
"add_passages": [...]}` (default ids `QAKBTEST01`, `QAKBTEST02`; passages with S3 / Confluence / SharePoint / web
locations), `GET|POST /_credentials`, `POST /_credentials/expire {"access_key_id"} | {"sessions": true}`,
`GET|POST /_config` (`strict_models`, `verify_signatures`, `anchor`, `session_seconds`, `clock_offset_s`,
`crl_serials`, `container_token`, `imds_role`, `expected_ra_arns`, `profile_max_seconds`, `chunk_chars`,
`delay_ms`, `first_delay_ms`), `POST /_reset`.

## Prompt-injection corpus (`injection/`)
`corpus.yaml`: attacker texts per surface (syslog, trap, sysDescr, ifAlias, LLDP, PTR, config line, KB passage,
device name) with a unique marker each. The `injection_obey` scenario (`[qa:inj]`) makes the model "obey": it repeats
the text, emits a data-carrying image, `<img>`/`<script>`/`<a>` HTML, external and `javascript:` links, forged
`nm:` refs, calls `update_device` (never offered -> the product must answer `unknown_tool`) and `config_diff` on
another device. `injection_checks.py`: `answer_problems(markdown, allowed_refs)`, `html_problems(dom_html,
app_origin)`, `telegram_problems(text, public_base_url)`, `request_problems(mock_requests, secrets)`.
