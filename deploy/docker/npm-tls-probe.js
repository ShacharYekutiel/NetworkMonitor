// Fail fast, with a clear message, when HTTPS to the npm registry is re-signed by a CA
// this build does not trust (TLS-inspecting corporate firewall). Without this check
// `npm ci` retries for minutes and may only print "Exit handler never called!".
//
//   node npm-tls-probe.js https://registry.npmjs.org/
//
// Exit 1 only on a certificate error; any other outcome (OK, timeout, DNS, proxy
// required, ...) exits 0 and lets npm itself try and report.
"use strict";
const https = require("https");

const url = process.argv[2] || "https://registry.npmjs.org/";
const CERT_ERRORS = new Set([
  "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
  "UNABLE_TO_GET_ISSUER_CERT",
  "SELF_SIGNED_CERT_IN_CHAIN",
  "DEPTH_ZERO_SELF_SIGNED_CERT",
  "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
  "CERT_UNTRUSTED",
  "CERT_SIGNATURE_FAILURE",
]);

const req = https.get(url, { timeout: 20000 }, (res) => {
  res.resume();
  process.exit(0);
});
req.on("timeout", () => req.destroy(new Error("timeout")));
req.on("error", (err) => {
  if (!CERT_ERRORS.has(err.code)) process.exit(0);
  console.error(
    [
      "",
      `ERROR: TLS certificate check failed for ${url}: ${err.code}`,
      "",
      "  Your network inspects HTTPS and re-signs it with a corporate CA that this",
      "  build does not trust. Export that root CA (Base-64 .cer / .crt / .pem) into",
      "  deploy/certs/ and run the build again:",
      "",
      "    docker compose -f deploy/docker-compose.yml up -d --build",
      "",
      "  How to export it on Windows: deploy/certs/README.md",
      "",
    ].join("\n"),
  );
  process.exit(1);
});
