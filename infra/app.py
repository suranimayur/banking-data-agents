"""CDK application entry point.

``cdk.json`` points at this module, and ``bda infra synth`` calls the same builder
in-process. Running it directly works too::

    python -m infra.app -c stage=prod

The ``sys.path`` insertion at the top is the one concession to being a top-level
directory rather than a package under ``src/``: it makes ``python infra/app.py``
behave the same as ``python -m infra.app``. The alternative — moving the CDK code
inside the shipped wheel — would put deployment code in the runtime artifact.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.synth import build_app  # noqa: E402  (import after the path fix, by design)


def main() -> int:
    """Build and synthesise the application for the requested stage."""
    app = build_app()
    app.synth()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
