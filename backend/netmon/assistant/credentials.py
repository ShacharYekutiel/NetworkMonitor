"""[4] Moved to `netmon.aws.credentials` (PR 4-0a). Re-export shim for one release: this
module IS `netmon.aws.credentials` (the same object), so every name, private helper and
monkeypatch keeps working."""

import sys

from netmon.aws import credentials as _moved

sys.modules[__name__] = _moved
