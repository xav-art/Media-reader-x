"""The dependency pins must agree between requirements.txt, requirements-dev.txt and pyproject.toml.

Dependabot bumps them in the same pull request; if one file is missed, this test fails the PR build
instead of the release shipping a version that the SBOM and the notices do not describe.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([^\s;#]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(lines: list[str]) -> dict[str, str]:
    pins = {}
    for line in lines:
        m = PIN.match(line)
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def _file_pins(name: str) -> dict[str, str]:
    return _pins((ROOT / name).read_text(encoding="utf-8").splitlines())


def test_every_requirement_is_pinned_exactly() -> None:
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-r "):
                assert PIN.match(line), f"{name}: not an exact pin: {line}"


def test_pyproject_matches_requirements() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = _pins(project["dependencies"])
    dev = _pins(project["optional-dependencies"]["dev"])
    req = _file_pins("requirements.txt")
    req_dev = _file_pins("requirements-dev.txt")
    assert runtime and dev
    for entry in project["dependencies"] + project["optional-dependencies"]["dev"]:
        assert PIN.match(entry) and "[" not in entry, f"pyproject.toml: not an exact pin: {entry}"
    for pkg, version in runtime.items():
        assert req.get(pkg) == version, f"{pkg}: pyproject.toml has {version}, requirements.txt {req.get(pkg)}"
    for pkg, version in dev.items():
        assert req_dev.get(pkg) == version, f"{pkg}: pyproject.toml has {version}, requirements-dev.txt {req_dev.get(pkg)}"
    missing = set(req_dev) - set(dev)
    assert not missing, f"requirements-dev.txt pins {sorted(missing)}, which pyproject.toml [dev] does not list"


def test_qt_packages_move_together() -> None:
    req = _file_pins("requirements.txt")
    qt = {req[p] for p in ("pyside6-essentials", "pyside6-addons", "shiboken6")}
    assert len(qt) == 1, f"PySide6-Essentials, PySide6-Addons and shiboken6 differ: {qt}"
