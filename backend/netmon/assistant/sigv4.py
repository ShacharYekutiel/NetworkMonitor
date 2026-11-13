"""[4] Moved to `netmon.aws.sigv4` (PR 4-0a). Re-export shim for one release: this
module IS `netmon.aws.sigv4` (the same object), so every name, private helper and
monkeypatch keeps working."""

import sys

from netmon.aws import sigv4 as _moved

sys.modules[__name__] = _moved
