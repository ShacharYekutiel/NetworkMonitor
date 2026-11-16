"""[4] Moved to `netmon.aws.transport` (PR 4-0a). Re-export shim for one release: this
module IS `netmon.aws.transport` (the same object), so every name, private helper and
monkeypatch keeps working."""

import sys

from netmon.aws import transport as _moved

sys.modules[__name__] = _moved
