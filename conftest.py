"""Root conftest: make the repository root importable for the whole test session.

``infra/`` is a top-level package by design — it is deployment code, and it is
deliberately excluded from the shipped wheel so that the runtime artifact cannot
contain infrastructure. The consequence is that ``import infra.stacks`` only works
when the repository root is on ``sys.path``, which is true when the test runner is
started from the root and not guaranteed otherwise.

Doing it here, once, is what keeps test modules free of ``sys.path`` edits.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
