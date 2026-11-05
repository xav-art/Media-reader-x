"""Scan the SBOM with grype and fail on High or Critical findings that have no valid exception.

Exceptions live in packaging/vuln-exceptions.json: one entry per CVE, package and exact version, with a
reason and an expiry date. An expired entry fails the build, so every exception is looked at again.
An entry stops matching when the package version changes, so an upgrade never inherits an old waiver.

Usage: python tools/vuln_scan.py <sbom.cdx.json> <work dir>
Grype: %LOCALAPPDATA%\\Programs\\grype\\grype.exe, or the GRYPE environment variable.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKING = {"High", "Critical"}
WARN_DAYS = 14


def _grype() -> Path:
    exe = Path(os.environ.get("GRYPE") or Path(os.environ["LOCALAPPDATA"]) / "Programs" / "grype" / "grype.exe")
    if not exe.is_file():
        raise SystemExit(f"grype not found at {exe}. Install it per user (see SECURITY.md) or set GRYPE.")
    return exe


def load_exceptions(today: dt.date) -> list[dict]:
    data = json.loads((ROOT / "packaging" / "vuln-exceptions.json").read_text(encoding="utf-8"))
    expired = []
    for entry in data["exceptions"]:
        expires = dt.date.fromisoformat(entry["expires"])
        if expires < today:
            expired.append(entry)
        elif (expires - today).days <= WARN_DAYS:
            print(f"warning: the exception for {entry['id']} ({entry['package']}) expires on {expires}")
    if expired:
        names = ", ".join(f"{e['id']} ({e['package']}, expired {e['expires']})" for e in expired)
        raise SystemExit(f"Expired vulnerability exceptions: {names}. Check for a fix, then renew or remove them.")
    return data["exceptions"]


def main(sbom: Path, work: Path) -> int:
    exceptions = load_exceptions(dt.date.today())
    work.mkdir(parents=True, exist_ok=True)
    report = work / "grype.json"
    proc = subprocess.run([str(_grype()), f"sbom:{sbom}", "-o", "json", "--file", str(report), "-q"], check=False)
    if proc.returncode != 0 or not report.is_file():
        raise SystemExit(f"grype failed (exit {proc.returncode}); it needs network access to update its database")
    matches = json.loads(report.read_text(encoding="utf-8"))["matches"]
    waived = {(e["id"], e["package"], e["version"]): e for e in exceptions}
    used, blocking, other = set(), [], []
    seen = set()
    for m in matches:
        vuln, art = m["vulnerability"], m["artifact"]
        key = (vuln["id"], art["name"], art["version"])
        if key in seen:
            continue
        seen.add(key)
        if key in waived:
            used.add(key)
        elif vuln["severity"] in BLOCKING:
            blocking.append((vuln, art))
        else:
            other.append((vuln, art))
    for vuln, art in sorted(other, key=lambda x: (x[1]["name"], x[0]["id"])):
        print(f"  {vuln['severity']:<10} {art['name']} {art['version']}  {vuln['id']}")
    print(f"grype: {len(seen)} findings, {len(used)} accepted by exceptions, {len(other)} below High")
    for key in sorted(set(waived) - used):
        print(f"note: exception {key[0]} ({key[1]} {key[2]}) no longer matches anything; remove it")
    if blocking:
        print("BLOCKING (High or Critical, no exception):")
        for vuln, art in blocking:
            fix = ", ".join(vuln.get("fix", {}).get("versions") or []) or "no fix listed"
            print(f"  {vuln['severity']:<8} {art['name']} {art['version']}  {vuln['id']}  (fix: {fix})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
