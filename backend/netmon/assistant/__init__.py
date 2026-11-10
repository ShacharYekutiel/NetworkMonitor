"""[3b] "Ask the network" assistant on Amazon Bedrock and the AI features (architecture § 14).

Layer map (developer A = connection layer):

- pure: `sigv4`, `eventstream`, `errors`, `pricing`, `profiles`, `config` (settings
  validation / connection hash), `gates` (feature dependencies);
- I/O: `credentials` (Roles Anywhere / access key / default chain), `bedrock` (Converse,
  ConverseStream, Retrieve, list models), `usage` (counters and caps), `certs` (upload / CSR),
  `testconn` (Test connection), `service` (`rt.assistant`, the runtime component).
"""
