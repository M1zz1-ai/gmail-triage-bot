"""auth.run(): success triggers the live E2E; redirect_uri_mismatch prints the hint.

All OAuth + E2E surface is mocked — no real consent flow, no network, no creds.
"""

import gmail_bot.auth as auth
from gmail_bot.auth import REDIRECT_MISMATCH_HINT, RedirectUriMismatchError
from gmail_bot.config import Config
from gmail_bot.live_smoke import LiveSmokeResult

FRESH_RTOK = "fresh-rtok"  # fake refresh-token stub for tests (not a real credential)

FAKE_CONFIG = Config(
    telegram_bot_token="tok",
    telegram_chat_id=123456789,
    gmail_client_id="cid",
    gmail_client_secret="csecret",
    gmail_refresh_token=FRESH_RTOK,
    gmail_token_uri="https://oauth2.googleapis.com/token",
    gmail_scopes=["https://www.googleapis.com/auth/gmail.modify"],
    anthropic_api_key="test-key",
)


def test_success_path_triggers_live_smoke(monkeypatch, tmp_path, capsys):
    """A minted token must auto-run run_live_smoke with the loaded config."""
    calls = {}

    def fake_run_oauth(env_path):
        calls["oauth_env"] = env_path
        return "freshtoken123456"

    def fake_load_config(env_path):
        calls["loaded"] = env_path
        return FAKE_CONFIG

    def fake_run_live_smoke(config, *, report_path):
        calls["smoke_config"] = config
        calls["smoke_report"] = report_path
        return LiveSmokeResult()  # zero steps -> zero failures -> green

    monkeypatch.setattr(auth, "run_oauth", fake_run_oauth)
    monkeypatch.setattr(auth, "load_config", fake_load_config)
    monkeypatch.setattr(auth, "run_live_smoke", fake_run_live_smoke)

    rc = auth.run(tmp_path / ".env", report_path=tmp_path / "REPORT.html")

    assert rc == 0
    assert calls["smoke_config"] is FAKE_CONFIG  # E2E ran with the loaded config
    assert calls["smoke_report"] == tmp_path / "REPORT.html"
    out = capsys.readouterr().out
    assert "GMAIL_REFRESH_TOKEN written" in out
    assert "live E2E fully green" in out


def test_failing_smoke_returns_nonzero(monkeypatch, tmp_path):
    """If any E2E step fails, run() must report a non-zero exit code."""
    monkeypatch.setattr(auth, "run_oauth", lambda env_path: "tok")
    monkeypatch.setattr(auth, "load_config", lambda env_path: FAKE_CONFIG)

    def failing_smoke(config, *, report_path):
        r = LiveSmokeResult()
        r.record(2, "Gmail token refresh", False, "boom")  # one failed step
        return r

    monkeypatch.setattr(auth, "run_live_smoke", failing_smoke)

    rc = auth.run(tmp_path / ".env", report_path=None)
    assert rc == 1


def test_smoke_false_skips_e2e(monkeypatch, tmp_path):
    """smoke=False must NOT call the live E2E (e.g. for a token-only re-mint)."""
    monkeypatch.setattr(auth, "run_oauth", lambda env_path: "tok")

    def should_not_run(*a, **k):
        raise AssertionError("run_live_smoke must not be called when smoke=False")

    monkeypatch.setattr(auth, "run_live_smoke", should_not_run)

    rc = auth.run(tmp_path / ".env", smoke=False)
    assert rc == 0


def test_redirect_uri_mismatch_prints_hint(monkeypatch, tmp_path, capsys):
    """A redirect_uri_mismatch must print the precise Desktop-app hint and not run E2E."""
    def raise_mismatch(env_path):
        raise RedirectUriMismatchError("(redirect_uri_mismatch) Bad Request")

    def should_not_run(*a, **k):
        raise AssertionError("run_live_smoke must not run when auth fails")

    monkeypatch.setattr(auth, "run_oauth", raise_mismatch)
    monkeypatch.setattr(auth, "run_live_smoke", should_not_run)

    rc = auth.run(tmp_path / ".env")

    assert rc == 1
    err = capsys.readouterr().err
    assert "Desktop app" in err
    assert REDIRECT_MISMATCH_HINT in err


def test_run_oauth_reclassifies_redirect_mismatch(monkeypatch):
    """run_oauth must turn a flow redirect_uri_mismatch into RedirectUriMismatchError."""
    import gmail_bot.auth as a

    class FakeFlow:
        def run_local_server(self, port):
            raise RuntimeError("Error 400: redirect_uri_mismatch")

    monkeypatch.setattr(a, "dotenv_values", lambda p: {
        "GMAIL_CLIENT_ID": "cid",
        "GMAIL_CLIENT_SECRET": "csecret",
        "GMAIL_SCOPES": "https://www.googleapis.com/auth/gmail.modify",
        "GMAIL_TOKEN_URI": "https://oauth2.googleapis.com/token",
    })
    monkeypatch.setattr(
        a.InstalledAppFlow, "from_client_config", staticmethod(lambda *a, **k: FakeFlow())
    )

    import pytest

    with pytest.raises(RedirectUriMismatchError):
        a.run_oauth()
