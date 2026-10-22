# Security policy

Media Reader x parses untrusted files with native decoders (libheif, libjxl, LibRaw, FFmpeg, Pillow). Most real-world risk comes from those libraries, so keeping dependencies current is the main control.

## Reporting

Open a private [security advisory](https://github.com/xav-art/Media-reader-x/security/advisories/new). Please don't use a public issue.

## Controls in place

- Pinned dependencies (`requirements.txt`) and a CycloneDX SBOM published with every build, so you can scan them with Dependabot, `pip-audit` or your SCA tool.
- File-size, pixel-count (decompression bomb) and animation memory caps before and during decoding.
- Decoding runs off the UI thread, and failures are caught and shown as messages, not crashes.
- No network access, no telemetry, no auto-update, no shell execution of file content.
- Per-user install. No services, drivers, scheduled tasks or elevation.

## Updating dependencies

1. Bump the versions in `requirements.txt`, `requirements-dev.txt` and `pyproject.toml`.
2. Run `build.ps1`. Tests plus the frozen `--self-test` must pass.
3. Bump `__version__` in `src/mediareaderx/__init__.py`, update `CHANGELOG.md`, and tag `vX.Y.Z`.
