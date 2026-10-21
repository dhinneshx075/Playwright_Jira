"""
Visibility into errors the runner deliberately ignores (probing for elements, closing tabs ...).

Most of those are normal - a locator that matches nothing, a tab that already closed - so they stay
quiet. When a test fails for an unclear reason, run with RUNNER_DEBUG=1 to print each one:

    set RUNNER_DEBUG=1
    python -m test_runner TWC5958-T1218
"""

import os

ENABLED = os.environ.get("RUNNER_DEBUG", "") not in ("", "0")


def swallowed(where: str, error: BaseException) -> None:
    """Record an ignored error (printed only with RUNNER_DEBUG=1)."""
    if ENABLED:
        first_line = (str(error).splitlines() or [""])[0][:200]
        print(f"        [debug] ignored in {where}: {type(error).__name__}: {first_line}", flush=True)
