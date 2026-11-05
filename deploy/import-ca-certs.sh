#!/bin/sh
# Normalise the corporate CA certificates found in SRC_DIR (default: deploy/certs) into
# DEST_DIR as one PEM certificate per "netmon-*.crt" file, ready for the system trust
# store tool (update-ca-certificates / update-ca-trust).
#
#   import-ca-certs.sh SRC_DIR DEST_DIR
#
# Accepted input: *.crt, *.pem, *.cer, in PEM ("Base-64 encoded X.509") or DER
# ("DER encoded binary X.509") form; a PEM file may hold several certificates (a chain).
# Anything else in SRC_DIR (README.md, .gitignore, ...) is ignored, so an empty folder
# is fine. Previously imported netmon-*.crt files in DEST_DIR are replaced, so removing
# a file from SRC_DIR and re-running removes it from the store too.
#
# POSIX sh + awk; openssl is used when available (validation, DER conversion).
# Used by the Dockerfile and by deploy/linux/install.sh.
set -eu

SRC_DIR="${1:?usage: import-ca-certs.sh SRC_DIR DEST_DIR}"
DEST_DIR="${2:?usage: import-ca-certs.sh SRC_DIR DEST_DIR}"

mkdir -p "$DEST_DIR"
rm -f "$DEST_DIR"/netmon-*.crt
[ -d "$SRC_DIR" ] || { echo "corporate CA: no $SRC_DIR, nothing to import"; exit 0; }

HAVE_OPENSSL=0
command -v openssl >/dev/null 2>&1 && HAVE_OPENSSL=1

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

count=0
for f in "$SRC_DIR"/*.crt "$SRC_DIR"/*.pem "$SRC_DIR"/*.cer "$SRC_DIR"/*.CRT "$SRC_DIR"/*.PEM "$SRC_DIR"/*.CER; do
  [ -f "$f" ] || continue
  base="$(basename "$f")"
  stem="$(printf '%s' "$base" | tr -c 'A-Za-z0-9._-' '_')"
  rm -f "$TMP"/part-*
  if grep -q -- '-----BEGIN CERTIFICATE-----' "$f"; then
    # PEM (possibly CRLF, possibly several certificates): one file per certificate.
    tr -d '\r' <"$f" | awk -v dir="$TMP" '
      /-----BEGIN CERTIFICATE-----/ { n++; out = sprintf("%s/part-%03d", dir, n); inside = 1 }
      inside { print > out }
      /-----END CERTIFICATE-----/ { inside = 0; close(out) }
    '
  elif [ "$HAVE_OPENSSL" -eq 1 ] && openssl x509 -inform DER -in "$f" -out "$TMP/part-001" 2>/dev/null; then
    echo "corporate CA: converted DER file $base to PEM"
  else
    echo "corporate CA: WARNING: $base is not a PEM/DER certificate, skipped" >&2
    echo "  (export it from Windows as 'Base-64 encoded X.509 (.CER)'; see deploy/certs/README.md)" >&2
    continue
  fi
  for part in "$TMP"/part-*; do
    [ -f "$part" ] || continue
    count=$((count + 1))
    dest="$DEST_DIR/netmon-${stem}-$(basename "$part" | sed 's/^part-//').crt"
    if [ "$HAVE_OPENSSL" -eq 1 ]; then
      if ! openssl x509 -in "$part" -out "$dest" 2>/dev/null; then
        echo "corporate CA: WARNING: invalid certificate in $base, skipped" >&2
        count=$((count - 1))
        continue
      fi
      subject="$(openssl x509 -in "$dest" -noout -subject 2>/dev/null | sed 's/^subject= *//')"
      issuer="$(openssl x509 -in "$dest" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')"
      echo "corporate CA: $base -> $subject"
      if [ "$subject" != "$issuer" ]; then
        # Not self-signed: an intermediate. Node accepts it as a trust anchor, but
        # OpenSSL/Python (pip, the app) need the chain to end at the root.
        echo "corporate CA: NOTE: $base is an intermediate certificate (issued by: $issuer)." >&2
        echo "  Its root must be in deploy/certs too (or already be a public root CA)." >&2
      fi
    else
      cp "$part" "$dest"
      echo "corporate CA: $base"
    fi
    chmod 0644 "$dest"
  done
done

echo "corporate CA: $count certificate(s) imported from $SRC_DIR"
