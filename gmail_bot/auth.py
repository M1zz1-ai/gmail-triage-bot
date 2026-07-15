"""OAuth bootstrap: mint a fresh GMAIL_REFRESH_TOKEN, then self-verify.

Run once (when the refresh token is missing or revoked):

    python -m gmail_bot.auth

Uses GMAIL_CLIENT_ID/SECRET/SCOPES from the master env, runs the installed-app
consent flow in a local browser, and writes the new GMAIL_REFRESH_TOKEN back
into ~/.config/gmail-triage-bot/.env (in place, preserving the other keys).

On success it AUTOMATICALLY runs the guarded live Gmail E2E and appends the
result to the HTML report — so the owner's single OAuth "Allow" proves the bot
works end-to-end with zero further round-trips.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values, set_key
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import MASTER_ENV_PATH, ConfigError, load_config
from .live_smoke import DEFAULT_REPORT_PATH, run_live_smoke

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

REDIRECT_MISMATCH_HINT = (
    "redirect_uri_mismatch — the OAuth client must be type \"Desktop app\".\n"
    "  The n8n credentials are a \"Web application\" client (redirect goes to "
    "n8n), so the local consent flow is rejected.\n"
    "  Fix: Google Cloud Console -> APIs & Services -> Credentials -> "
    "Create OAuth client ID -> Application type: Desktop app.\n"
    "  Then copy the new client_id / client_secret into ~/.config/gmail-triage-bot/.env "
    "(GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET) and re-run."
)


class RedirectUriMismatchError(RuntimeError):
    """Raised when the consent flow fails with redirect_uri_mismatch."""


def _require(values: dict[str, str | None], key: str) -> str:
    value = values.get(key)
    if value is None or value.strip() == "":
        raise ConfigError(f"Missing required config key: {key}")
    return value.strip()


def run_oauth(env_path: Path = MASTER_ENV_PATH) -> str:
    """Run the consent flow and persist the new refresh token. Returns it.

    Raises:
        ConfigError: required client id/secret missing.
        RedirectUriMismatchError: the OAuth client is not a Desktop-app client.
        RuntimeError: the flow returned no refresh token.
    """
    values = dict(dotenv_values(env_path))
    client_id = _require(values, "GMAIL_CLIENT_ID")
    client_secret = _require(values, "GMAIL_CLIENT_SECRET")
    scopes = (values.get("GMAIL_SCOPES") or "").split() or DEFAULT_SCOPES
    token_uri = values.get("GMAIL_TOKEN_URI") or "https://oauth2.googleapis.com/token"

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": token_uri,
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    try:
        creds = flow.run_local_server(port=0)
    except Exception as exc:  # noqa: BLE001 — re-classify redirect_uri_mismatch, re-raise rest
        if "redirect_uri_mismatch" in str(exc).lower():
            raise RedirectUriMismatchError(str(exc)) from exc
        raise
    if not creds.refresh_token:
        raise RuntimeError(
            "No refresh_token returned. Revoke prior grant and retry with prompt=consent."
        )

    set_key(str(env_path), "GMAIL_REFRESH_TOKEN", creds.refresh_token)
    return creds.refresh_token


def run(
    env_path: Path = MASTER_ENV_PATH,
    *,
    smoke: bool = True,
    report_path: Path | None = DEFAULT_REPORT_PATH,
) -> int:
    """Mint a fresh token, then (on success) self-verify via the live E2E.

    Returns a process exit code: 0 == token minted AND (if ``smoke``) every
    E2E step passed; non-zero otherwise.

    Args:
        env_path: master env file to read client creds from and write the token to.
        smoke: when True, run the guarded live Gmail E2E right after the token
            is written, and append the result to ``report_path``.
        report_path: HTML report to append the live-run section to (or None).
    """
    try:
        token = run_oauth(env_path)
    except RedirectUriMismatchError:
        print(f"OAuth bootstrap failed: {REDIRECT_MISMATCH_HINT}", file=sys.stderr)
        return 1
    except (ConfigError, RuntimeError) as exc:
        print(f"OAuth bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"GMAIL_REFRESH_TOKEN written to {env_path} (...{token[-6:]}).",
        flush=True,
    )

    if not smoke:
        print("Skipping live E2E (smoke=False). Restart the bot.")
        return 0

    print("\nRunning guarded live Gmail E2E to prove the bot end-to-end...\n", flush=True)
    config = load_config(env_path)
    result = run_live_smoke(config, report_path=report_path)
    if result.failures:
        print(
            f"\nLive E2E finished with {result.failures} failing step(s) — see the table above.",
            file=sys.stderr,
        )
        return 1
    print("\n✅ Token minted and live E2E fully green. The bot is proven working.")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
