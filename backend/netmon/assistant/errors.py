"""[4] Moved to `netmon.aws.errors` (PR 4-0a). Re-export shim for one release: this
module IS `netmon.aws.errors` (the same object), so every name, private helper and
monkeypatch keeps working."""

import sys

from netmon.aws import errors as _moved

sys.modules[__name__] = _moved
