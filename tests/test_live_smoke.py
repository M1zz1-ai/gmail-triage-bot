"""live_smoke: report append inserts a styled RU section; summary prints a table.

No creds, no network — exercises only the pure result/report helpers.
"""

from gmail_bot.live_smoke import (
    LiveSmokeResult,
    append_report_section,
    print_summary,
)

REPORT_STUB = """\
<html><body>
<div class="wrap">
  <h2>existing</h2>
  <footer>logs</footer>
</div>
</body></html>
"""


def _mixed_result() -> LiveSmokeResult:
    r = LiveSmokeResult()
    r.record(1, "config --check", True, "all 8 keys")
    r.record(2, "Gmail token refresh", False, "invalid_grant")
    return r


def test_append_inserts_before_footer_with_classes(tmp_path):
    report = tmp_path / "REPORT.html"
    report.write_text(REPORT_STUB, encoding="utf-8")

    append_report_section(report, _mixed_result(), ts=1700000000)
    out = report.read_text(encoding="utf-8")

    # Section landed BEFORE the footer.
    assert out.index("Живой Gmail-E2E") < out.index("<footer>")
    # Reused existing styling classes.
    assert 'class="pill p-ok"' in out
    assert 'class="pill p-bad"' in out
    # Both step rows rendered with PASS/FAIL.
    assert "config --check" in out and ">PASS<" in out
    assert "Gmail token refresh" in out and ">FAIL<" in out
    # Footer preserved exactly once.
    assert out.count("<footer>") == 1


def test_append_summary_pill_reflects_failures(tmp_path):
    report = tmp_path / "R.html"
    report.write_text(REPORT_STUB, encoding="utf-8")

    green = LiveSmokeResult()
    green.record(1, "config --check", True, "ok")
    append_report_section(report, green, ts=1700000000)
    assert "все шаги PASS" in report.read_text(encoding="utf-8")


def test_append_falls_back_when_no_footer(tmp_path):
    report = tmp_path / "NF.html"
    report.write_text("<html><body>no footer here</body></html>", encoding="utf-8")
    r = LiveSmokeResult()
    r.record(1, "config --check", True, "ok")
    append_report_section(report, r, ts=1700000000)
    assert "Живой Gmail-E2E" in report.read_text(encoding="utf-8")


def test_detail_is_html_escaped(tmp_path):
    report = tmp_path / "ESC.html"
    report.write_text(REPORT_STUB, encoding="utf-8")
    r = LiveSmokeResult()
    r.record(2, "Gmail token refresh", False, "RefreshError: <script>&bad")
    append_report_section(report, r, ts=1700000000)
    out = report.read_text(encoding="utf-8")
    assert "&lt;script&gt;&amp;bad" in out
    assert "<script>" not in out


def test_print_summary_lists_each_step(capsys):
    r = LiveSmokeResult()
    r.record(1, "config --check", True, "ok")
    r.record(2, "Gmail token refresh", False, "boom")
    print_summary(r)
    out = capsys.readouterr().out
    assert "LIVE E2E SUMMARY" in out
    assert "step 1: PASS — config --check" in out
    assert "step 2: FAIL — Gmail token refresh" in out
    assert "1 failure(s)." in out


def test_failures_property_counts_only_failed():
    r = LiveSmokeResult()
    r.record(1, "a", True, "")
    r.record(2, "b", False, "")
    r.record(3, "c", False, "")
    assert r.failures == 2
