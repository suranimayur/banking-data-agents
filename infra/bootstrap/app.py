"""CDK entry point for the bootstrap stack.

    python -m infra.bootstrap.app

Deploying it needs administrator access; synthesising it needs nothing. Run
``bda infra synth --env bootstrap`` to inspect the trust policies it would write
before letting it write them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.bootstrap.stack import build_app  # noqa: E402  (after the path fix, by design)


def main() -> int:
    app = build_app()
    app.synth()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
