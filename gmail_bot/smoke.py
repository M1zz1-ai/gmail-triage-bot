"""On-demand entrypoint for the guarded live Gmail E2E.

Run when GMAIL_ACCESS_TOKEN (or a valid GMAIL_REFRESH_TOKEN) is in the master
env and you want to prove the bot against the real APIs without re-running the
OAuth bootstrap:

    uv run python -m gmail_bot.smoke

Same hard guardrails as the auth path: the only mail created/mutated is a
self-test sent FROM the account TO itself; third-party mail is never touched;
the continuous poll loop is never started; Telegram output goes only to the
owner chat. The PASS/FAIL table is printed and a dated section is appended to
the HTML report — identical to the post-OAuth run.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import MASTER_ENV_PATH, ConfigError, load_config
from .live_smoke import DEFAULT_REPORT_PATH, run_live_smoke


def run(
    env_path: Path = MASTER_ENV_PATH,
    *,
    report_path: Path | None = DEFAULT_REPORT_PATH,
) -> int:
    """Load config and run the guarded live E2E. Returns a process exit code.

    Returns 0 when every E2E step passes, non-zero otherwise (or 1 on a config
    error before the run can start).
    """
    try:
        config = load_config(env_path)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    print("Running guarded live Gmail E2E...\n", flush=True)
    result = run_live_smoke(config, report_path=report_path)
    if result.failures:
        print(
            f"\nLive E2E finished with {result.failures} failing step(s) — see the table above.",
            file=sys.stderr,
        )
        return 1
    print("\n✅ Live E2E fully green. The bot is proven working.")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
