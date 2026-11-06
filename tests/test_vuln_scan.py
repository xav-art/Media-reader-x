"""tools/vuln_scan.py: failure causes, strict expiry, and no finding details in public (GitHub) logs."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("vuln_scan", ROOT / "tools" / "vuln_scan.py")
vuln_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vuln_scan)

TODAY = dt.date(2026, 12, 15)


def _exc(expires: str, cve: str = "CVE-1", pkg: str = "zlib", ver: str = "1.0") -> dict:
    return {"id": cve, "package": pkg, "version": ver, "expires": expires, "reason": "test"}


def _match(cve: str, pkg: str, ver: str, severity: str, fix: list[str] | None = None) -> dict:
    return {"vulnerability": {"id": cve, "severity": severity, "fix": {"versions": fix or []}},
            "artifact": {"name": pkg, "version": ver}}


def test_expired_exception_fails_always() -> None:
    causes = vuln_scan.check_exceptions([_exc("2026-12-14")], TODAY, strict=False)
    assert len(causes) == 1 and causes[0].startswith("EXPIRED EXCEPTIONS")


def test_strict_fails_within_30_days_only() -> None:
    assert vuln_scan.check_exceptions([_exc("2027-01-03")], TODAY, strict=True)[0].startswith("EXCEPTIONS EXPIRE SOON")
    assert vuln_scan.check_exceptions([_exc("2027-01-03")], TODAY, strict=False) == []
    assert vuln_scan.check_exceptions([_exc("2027-01-15")], TODAY, strict=True) == []


def test_classify_waives_exact_version_only() -> None:
    matches = [_match("CVE-1", "zlib", "1.0", "High"), _match("CVE-1", "zlib", "1.1", "High"),
               _match("CVE-2", "ffmpeg", "9", "Medium"), _match("CVE-1", "zlib", "1.0", "High")]
    blocking, other, used, unused = vuln_scan.classify(matches, [_exc("2027-06-01"), _exc("2027-06-01", cve="CVE-9")])
    assert [(v["id"], a["version"]) for v, a in blocking] == [("CVE-1", "1.1")]
    assert len(other) == 1 and used == {("CVE-1", "zlib", "1.0")} and unused == {("CVE-9", "zlib", "1.0")}


def _run(monkeypatch, tmp_path, matches: list[dict], public: bool, returncode: int = 0):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setattr(vuln_scan, "PUBLIC_LOG", public)
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging" / "vuln-exceptions.json").write_text('{"exceptions": []}', encoding="utf-8")
    monkeypatch.setattr(vuln_scan, "ROOT", tmp_path)  # independent of the real exceptions and their expiry
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("GRYPE", str(ROOT / "pyproject.toml"))  # any existing file; subprocess is faked

    def fake_run(cmd, check):
        if returncode == 0:
            Path(cmd[cmd.index("--file") + 1]).write_text(json.dumps({"matches": matches}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode)

    monkeypatch.setattr(vuln_scan.subprocess, "run", fake_run)
    code = vuln_scan.main(["sbom.json", str(tmp_path / "work")])
    return code, summary_file.read_text(encoding="utf-8")


def test_public_log_hides_new_finding_details(monkeypatch, tmp_path, capsys) -> None:
    matches = [_match("CVE-2099-777", "libsecret", "1.0", "Critical", ["1.1"]),
               _match("CVE-2099-888", "libmedium", "2.0", "Medium")]
    code, summary = _run(monkeypatch, tmp_path, matches, public=True)
    captured = capsys.readouterr()
    logs = captured.out + captured.err + summary
    assert code == 1
    assert "NEW FINDINGS: 1 High or Critical" in summary and "1 with a fixed version" in summary
    for secret in ("CVE-2099-777", "libsecret", "CVE-2099-888", "libmedium"):
        assert secret not in logs


def test_local_log_lists_new_finding(monkeypatch, tmp_path, capsys) -> None:
    code, _ = _run(monkeypatch, tmp_path, [_match("CVE-2099-777", "libsecret", "1.0", "High")], public=False)
    assert code == 1 and "CVE-2099-777" in capsys.readouterr().out


def test_scanner_error_names_the_pin(monkeypatch, tmp_path) -> None:
    code, summary = _run(monkeypatch, tmp_path, [], public=True, returncode=2)
    assert code == 1 and "SCANNER ERROR" in summary and "install-grype" in summary


def test_clean_scan_passes(monkeypatch, tmp_path) -> None:
    code, summary = _run(monkeypatch, tmp_path, [_match("CVE-5", "ffmpeg", "9", "Low")], public=True)
    assert code == 0 and summary.startswith("OK:")

