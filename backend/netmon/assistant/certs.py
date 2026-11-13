"""[4] Moved to `netmon.aws.certs` (PR 4-0a). Re-export shim for one release: this
module IS `netmon.aws.certs` (the same object), so every name, private helper and
monkeypatch keeps working."""

import sys

from netmon.aws import certs as _moved

sys.modules[__name__] = _moved
