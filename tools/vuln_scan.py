"""Scan the SBOM with grype and fail on High or Critical findings that have no valid exception.

Exceptions live in packaging/vuln-exceptions.json: one entry per CVE, package and exact version, with a
reason and an expiry date. An expired entry fails the build, so every exception is looked at again.
An entry stops matching when the package version changes, so an upgrade never inherits an old waiver.

--strict (the fortnightly scan on GitHub): an exception that expires within STRICT_DAYS also fails, so the
owner hears about it before the build starts failing. Scans run on the 1st and 15th, so each expiry is
seen at least once in time.

In GitHub Actions (public logs), the scan prints counts only, never CVE ids or package names of findings
that have no fix yet. Run it locally for the details. The cause of a failure goes to the step summary,
which is also what the failure email links to.

Usage: python tools/vuln_scan.py [--strict] <sbom.cdx.json> <work dir>
Grype: %LOCALAPPDATA%\\Programs\\grype\\grype.exe, or the GRYPE environment variable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKING = {"High", "Critical"}
WARN_DAYS = 14
STRICT_DAYS = 30
PUBLIC_LOG = os.environ.get("GITHUB_ACTIONS") == "true"
LOCAL_COMMAND = "python tools/vuln_scan.py --strict packaging/shipped/<sbom file> build/security"


def summary(line: str) -> None:
    """Print a line and add it to the GitHub step summary when there is one."""
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n\n")


def _grype() -> Path:
    exe = Path(os.environ.get("GRYPE") or Path(os.environ["LOCALAPPDATA"]) / "Programs" / "grype" / "grype.exe")
    if not exe.is_file():
        raise SystemExit(f"grype not found at {exe}. Install it per user (see SECURITY.md) or set GRYPE.")
    return exe


def check_exceptions(exceptions: list[dict], today: dt.date, strict: bool) -> list[str]:
    """Return the failure causes for expired (or, with strict, soon expiring) exceptions."""
    expired, expiring = [], []
    for entry in exceptions:
        expires = dt.date.fromisoformat(entry["expires"])
        days = (expires - today).days
        if days < 0:
            expired.append(entry)
        elif strict and days <= STRICT_DAYS:
            expiring.append(entry)
        elif days <= WARN_DAYS:
            print(f"warning: the exception for {entry['id']} ({entry['package']}) expires on {expires}")
    causes = []
    if expired:
        names = ", ".join(f"{e['id']} ({e['package']}, expired {e['expires']})" for e in expired)
        causes.append(f"EXPIRED EXCEPTIONS: {names}. Check for a fix, then renew or remove them.")
    if expiring:
        first = min(e["expires"] for e in expiring)
        causes.append(f"EXCEPTIONS EXPIRE SOON: {len(expiring)} expire within {STRICT_DAYS} days (first on "
                      f"{first}). Check packaging/vuln-exceptions.json for fixes, then renew or remove them.")
    return causes


def classify(matches: list[dict], exceptions: list[dict]) -> tuple[list, list, set, set]:
    """Split grype matches into blocking, other, and the waived keys used; also return unused waivers."""
    waived = {(e["id"], e["package"], e["version"]) for e in exceptions}
    used, blocking, other, seen = set(), [], [], set()
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
    return blocking, other, used, waived - used


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true", help=f"fail on exceptions expiring within {STRICT_DAYS} days")
    parser.add_argument("sbom", type=Path)
    parser.add_argument("work", type=Path)
    args = parser.parse_args(argv)

    data = json.loads((ROOT / "packaging" / "vuln-exceptions.json").read_text(encoding="utf-8"))
    exceptions = data["exceptions"]
    causes = check_exceptions(exceptions, dt.date.today(), args.strict)

    args.work.mkdir(parents=True, exist_ok=True)
    report = args.work / "grype.json"
    report.unlink(missing_ok=True)  # never read an old report as if it were this run's
    proc = subprocess.run([str(_grype()), f"sbom:{args.sbom}", "-o", "json", "--file", str(report), "-q"],
                          check=False)
    if proc.returncode != 0 or not report.is_file():
        for cause in causes:
            summary(cause)
        summary(f"SCANNER ERROR: grype failed (exit {proc.returncode}). It needs network access to update its "
                "database. If this repeats, grype may be too old for its database: update the pinned version "
                "in .github/actions/install-grype/action.yml and in the local install (SECURITY.md).")
        return 1

    blocking, other, used, unused = classify(json.loads(report.read_text(encoding="utf-8"))["matches"], exceptions)
    if not PUBLIC_LOG:
        for vuln, art in sorted(other, key=lambda x: (x[1]["name"], x[0]["id"])):
            print(f"  {vuln['severity']:<10} {art['name']} {art['version']}  {vuln['id']}")
    total = len(blocking) + len(other) + len(used)
    print(f"grype: {total} findings, {len(used)} accepted by exceptions, {len(other)} below High")
    for key in sorted(unused):  # the exception file is public, so its ids may be printed
        print(f"note: exception {key[0]} ({key[1]} {key[2]}) no longer matches anything; remove it")
    if blocking:
        if PUBLIC_LOG:
            fixable = sum(1 for vuln, _ in blocking if vuln.get("fix", {}).get("versions"))
            causes.append(f"NEW FINDINGS: {len(blocking)} High or Critical without an exception ({fixable} with a "
                          f"fixed version listed). Details are not printed in public logs: run `{LOCAL_COMMAND}`.")
        else:
            print("BLOCKING (High or Critical, no exception):")
            for vuln, art in blocking:
                fix = ", ".join(vuln.get("fix", {}).get("versions") or []) or "no fix listed"
                print(f"  {vuln['severity']:<8} {art['name']} {art['version']}  {vuln['id']}  (fix: {fix})")
            causes.append(f"NEW FINDINGS: {len(blocking)} High or Critical without an exception (listed above).")
    for cause in causes:
        summary(cause)
    if not causes:
        summary(f"OK: no High or Critical finding without an exception ({total} findings, {len(used)} accepted).")
    return 1 if causes else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
