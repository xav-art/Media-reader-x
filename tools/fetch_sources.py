"""Download the source archives that the GPL and LGPL require us to offer, and copy them to a folder.

The binary distribution ships x265 (GPL-2.0-or-later) and LGPL libraries (libheif, libde265, LibRaw,
FFmpeg). Their licences ask for the corresponding source "from the same place" as the binaries, so
build.ps1 puts these archives in dist\\source next to Setup.exe and the portable zip.

packaging/sources.json lists each archive: a version-pinned URL and its SHA-256. Each archive is
downloaded once into a cache (%LOCALAPPDATA%\\MediaReaderX-build\\sources) and checked against that
hash on every run, so a changed or tampered upstream file stops the build. The cache makes later
builds work offline.

Usage:
    python tools/fetch_sources.py <output folder>     copy every archive (and SOURCES.txt) there
    python tools/fetch_sources.py --pin               fill in a missing sha256 after a version bump
                                                      (download, print, and write it to the manifest)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "sources.json"
CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MediaReaderX-build" / "sources"
CHUNK = 1 << 20


class SourceError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "MediaReaderX-build"})
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(request, timeout=120) as response:
            shutil.copyfileobj(response, out, CHUNK)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _cached(entry: dict) -> Path:
    """Return the cached archive for one manifest entry, downloading it if needed. Checks the hash."""
    path = CACHE / entry["file"]
    expected = entry.get("sha256", "")
    if path.exists() and expected and _sha256(path) == expected:
        return path
    print(f"  downloading {entry['url']}")
    try:
        _download(entry["url"], path)
    except OSError as exc:
        raise SourceError(f"cannot download {entry['file']} ({exc}). Check the network, or put the file "
                          f"in {CACHE} by hand.") from exc
    actual = _sha256(path)
    if not expected:
        return path
    if actual != expected:
        path.unlink()
        raise SourceError(f"{entry['file']}: SHA-256 is {actual}, packaging/sources.json expects {expected}. "
                          "The upstream file changed: check it before you update the manifest.")
    return path


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def pin() -> None:
    manifest = load_manifest()
    changed = False
    for entry in manifest["sources"]:
        if entry.get("sha256"):
            continue
        path = _cached(entry)
        entry["sha256"] = _sha256(path)
        changed = True
        print(f"  pinned {entry['file']}: {entry['sha256']}")
    if changed:
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("manifest is pinned")


def _readme(manifest: dict) -> str:
    lines = [
        "Media Reader x: source code for the GPL and LGPL components",
        "==========================================================",
        "",
        "These archives are the source code of the GPL and LGPL libraries inside the Media Reader x",
        "binary distribution, at the exact versions that ship. THIRD_PARTY_NOTICES.txt lists every",
        "component, its licence and where to get its source. Media Reader x's own source code is at",
        "https://github.com/xav-art/Media-reader-x",
        "",
    ]
    for entry in manifest["sources"]:
        lines += [
            f"{entry['file']}",
            f"    {entry['about']}",
            f"    Licence: {entry['license']}",
            f"    From:    {entry['url']}",
            f"    SHA-256: {entry['sha256']}",
            "",
        ]
    return "\n".join(lines)


def copy_to(out: Path) -> None:
    manifest = load_manifest()
    missing = [e["file"] for e in manifest["sources"] if not e.get("sha256")]
    if missing:
        raise SourceError(f"no sha256 in packaging/sources.json for {missing}: run fetch_sources.py --pin")
    total = 0
    for entry in manifest["sources"]:
        src = _cached(entry)
        dest = out / entry["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        total += dest.stat().st_size
    (out / "SOURCES.txt").write_text(_readme(manifest), encoding="utf-8")
    print(f"sources: {len(manifest['sources'])} files, {total / 1048576:.1f} MB in {out}")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--pin":
            pin()
        elif len(sys.argv) == 2:
            copy_to(Path(sys.argv[1]))
        else:
            print(__doc__)
            sys.exit(2)
    except SourceError as exc:
        print(f"Source error: {exc}", file=sys.stderr)
        sys.exit(1)
