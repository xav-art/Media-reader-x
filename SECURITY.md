# Security policy

Media Reader x parses untrusted files with native decoders (libheif, libde265, libjxl, dav1d, LibRaw, FFmpeg, Pillow's C libraries, Qt). A bug in one of them can be exploited by a file made for it. Three controls matter most: running the decoders in locked-down helper processes, limiting which decoders a file can reach, and keeping those decoders current.

## Reporting

Open a private [security advisory](https://github.com/xav-art/Media-reader-x/security/advisories/new). Please don't use a public issue.

## What a file can reach

- **Protected decoding (from 1.6).** Pictures, thumbnails and video checks are decoded in helper processes, each in its own Windows AppContainer, job and private desktop. A helper gets a read-only handle to one file. It cannot read other files, write outside its own folder, use the network, start programs, use the clipboard or capture the screen. Replies are checked field by field before they become pictures, and the app fails closed once the sandbox has worked on a PC. Video playback still runs in the app. Details, and what this does not cover: [docs/SANDBOX.md](docs/SANDBOX.md).
- **One decoder per file, chosen from its content.** The app reads the first bytes and opens the file with the one decoder for that format (`src/mediareaderx/content.py`). Pillow's other 29 openers, such as EPS (Ghostscript), WMF (Windows GDI), FITS and PCD, are never used. A mismatched file (WebP saved as `.jpg`) opens with the decoder for its real content, and a notice says so. Content the app does not support is refused.
- **Camera RAW** files are chosen by extension, not content: they go to LibRaw, and Pillow reads their EXIF with its TIFF plugin only.
- **Videos:** a file must start with a known container signature (MP4/MOV, MKV/WebM, AVI, WMV, MPEG-TS, MPEG-PS, FLV, Ogg), and FFmpeg's own probe must choose the demuxer for that same container, before the player gets the file. So no playlist (HLS, concat, SDP), picture or other demuxer is used. A fresh protected helper then lists the streams: every video, audio and subtitle stream must use one of about 40 common codecs (`src/mediareaderx/videoprobe.py`), out of about 500 in Qt's FFmpeg.
- **No network from video:** Qt's FFmpeg build has network support. The app sets `QT_FFMPEG_PROTOCOL_WHITELIST=file`, so FFmpeg can open local files only, never a URL.
- **SVG:** pictures may be embedded only as PNG or JPEG, checked by content. Links to files or web addresses, other embedded formats and DTD entity declarations are refused. `.svgz` inflates with a 64 MB cap.
- **Encoders:** the HEIF writer is unregistered, so the bundled x265 encoder cannot run. The AV1 encoder in Pillow's AVIF module is never called.
- **Limits:** a 1 GB file cap, a pixel-count limit checked before decoding (decompression bombs), animation frame and memory caps, and libheif's own security limits (never disabled).
- **Failures** are caught and shown as messages, not crashes. Text from files (metadata, names) is always shown as plain text, never Qt rich text, so it cannot load pictures or reach a network share.
- **Delete** goes to the Recycle Bin only. On drives without one (network folders, USB drives) the app refuses, because Windows would delete permanently.
- **Logs** hold no paths or file names: every line is redacted, also error messages and Qt warnings.
- No network access, no telemetry, no auto-update, and no shell execution of file content.
- Per-user install. No services, drivers, scheduled tasks or elevation.

## Supply chain

- Pinned dependencies (`requirements.txt`), built with Python 3.14.
- A CycloneDX SBOM with every build. It lists the Python packages and the native libraries inside them, each with its version (read from the build environment) and a CPE id where NVD has one, so Dependency-Track, Defender Vulnerability Management or grype can match CVEs. Libraries compiled into Qt show no version in the binaries; their versions come from Qt's attribution files (`packaging/sbom-declared.json`).
- `build.ps1` runs `pip-audit` on the pinned requirements and `grype` on the SBOM. Any High or Critical finding stops the build, unless `packaging/vuln-exceptions.json` has an entry for that CVE, package and exact version. Each entry gives the reason and an expiry date, after which the build fails again.

## Updating dependencies

1. Bump the versions in `requirements.txt`, `requirements-dev.txt` and `pyproject.toml`. After a PySide6 upgrade, update `packaging/sbom-declared.json` from Qt's `qt_attribution.json` files.
2. Run `build.ps1`. Tests, the vulnerability audit and the frozen `--self-test` must pass.
3. Remove exceptions that no longer match (the build lists them).
4. Bump `__version__` in `src/mediareaderx/__init__.py`, update `CHANGELOG.md`, and tag `vX.Y.Z`.

Grype is installed per user: download the Windows zip from https://github.com/anchore/grype/releases, check it against the release's checksums file, and extract `grype.exe` to `%LOCALAPPDATA%\Programs\grype\` (or set `GRYPE`).
