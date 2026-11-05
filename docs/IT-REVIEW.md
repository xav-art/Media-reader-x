# Media Reader x: brief for IT, security and legal review

A one-page summary for a reviewer who decides whether Media Reader x may run on company PCs. The linked
documents have the details.

## What it is

- An offline Windows viewer for photos and videos that Windows cannot open on its own: HEIC, AVIF, JPEG XL,
  camera RAW, Photoshop PSD, and HEVC, VP9 and AV1 video. It can view, zoom, rotate the view, copy to the
  clipboard, save a copy as JPG or PNG, print, and move a file to the Recycle Bin.
- A personal open-source project by Xavier Walle (GitHub `xav-art`), under the MIT licence. It is not a
  product of the author's employer. Source: https://github.com/xav-art/Media-reader-x
- Windows 10 version 1809 or later, 64-bit.

## Network and data

- No network calls, no telemetry, no auto-update and no accounts. FFmpeg may open local files only, never a
  URL. The only web link is the project link in Help > About, which opens the browser when the user clicks it.
- The app never changes a user's media: rotate changes only the view, Save a copy refuses to overwrite the
  original, and delete always goes to the Recycle Bin.
- What it stores, per user, in `%LOCALAPPDATA%\MediaReaderX`:
  - `settings.ini`: window layout, theme, volume, slideshow and print options, the GPS option, the last folder
    opened and the 10 most recent files (full paths). File > Open recent > Clear list empties the list, and
    "Remember recent files and folders" turned off stores no paths at all.
  - "Save a copy" removes the location (GPS) from the copy by default.
  - `logs\mediareaderx.log`: rotating, at most 6 MB in total. File type and size only. Paths and file names are
    removed from every line, also from error messages, so a log can go into a support ticket.
  - `selftest.txt`: only after `MediaReaderX.exe --self-test`.
  - No thumbnail or picture cache on disk. Previews stay in memory.
- Protected decoding (from 1.6) adds AppContainer profiles for the helper processes (up to 16, under
  `%LOCALAPPDATA%\Packages\xav-art.mediareaderx.helper.N`) and one permission entry on the install folder
  (read and execute for the app's own capability). The uninstaller removes both. See [SANDBOX.md](SANDBOX.md).

## Install and footprint

- Per-user install, no admin rights: `Setup.exe` (Inno Setup, `PrivilegesRequired=lowest`) installs to
  `%LOCALAPPDATA%\Programs\Media Reader x`. A portable zip needs no install at all.
- Registry: HKCU only. "Open with" entries and `RegisteredApplications` for the supported file types. Windows
  default apps are never changed.
- No services, drivers, scheduled tasks, shell extensions or elevation.
- Silent install: `Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`. Health check:
  `MediaReaderX.exe --self-test` (exit code 0 when every codec and content check works).

## Untrusted files

- Media files are untrusted input for native decoders (libheif, libde265, libjxl, dav1d, LibRaw, FFmpeg,
  Pillow's libraries, Qt). Each file reaches only the one decoder for its content. Videos must be a known
  container. Size, pixel, frame and memory limits apply. See [SECURITY.md](../SECURITY.md).
- Protected decoding runs the decoders in locked-down helper processes. [SANDBOX.md](SANDBOX.md) says what
  this covers and what it does not (video playback still runs in the main app).

## Supply chain

- Every dependency is pinned to an exact version. Each build runs the tests, `pip-audit` on the pinned
  requirements and `grype` on the SBOM. A High or Critical finding stops the build, unless
  `packaging/vuln-exceptions.json` has a reviewed entry for that CVE, package and exact version, with a reason
  and an expiry date.
- Each release has a CycloneDX SBOM (`MediaReaderX-<version>-sbom.cdx.json`). It lists the Python packages and
  the native libraries inside them, with versions, CPE ids, SPDX licences and source links. You can import it
  into Dependency-Track or Defender Vulnerability Management.
- The frozen app runs its self-test during the build. `SHA256SUMS.txt` has a checksum for every file in the
  release folder. No UPX packing. The one-folder build unpacks nothing to `%TEMP%`.

## Code signing and allow-listing

- The binaries are **not code-signed**. SmartScreen warns on the first run.
- **Preferred:** sign `MediaReaderX.exe`, `Setup.exe` and the DLL and PYD files with the company's
  code-signing certificate, then allow them with a publisher (certificate) rule in App Control for Business
  (WDAC) or AppLocker. A publisher rule keeps working for new releases.
- **Without signing:** use hash rules, made again for each release from the installed files (for example with
  `Get-AppLockerFileInformation` and `New-AppLockerPolicy -RuleType Hash`). First check `Setup.exe` or the zip
  against `SHA256SUMS.txt`.
- **Do not use a path (folder) rule** for `%LOCALAPPDATA%\Programs\Media Reader x`. Users can write to that
  folder, so a path rule allows any program that a user, or malware, puts there.
- If DLL rules are on, the rules must also cover the `.dll` and `.pyd` files (Python extension DLLs) in
  `_internal`. The protected-decoding helpers are the same `MediaReaderX.exe`, started with `--sandbox-helper`.

## Licences

- Media Reader x's own code is MIT. The binary also contains LGPL components (Qt, Qt for Python, FFmpeg,
  libheif, libde265, LibRaw), one GPL component (x265, an HEVC encoder that the app never calls) and
  components under BSD, MIT, zlib and Apache-2.0 licences. Taken as a whole, the binary distribution is
  conveyed under the GPL-3.0. These licences set conditions for passing the app on. They do not restrict use.
- [THIRD_PARTY_NOTICES.txt](../THIRD_PARTY_NOTICES.txt) lists each component with its version, licence,
  copyright and source link, and holds the full licence texts. The source of the GPL and LGPL components is in
  the `source` folder of each release. If you pass the app on, pass on the notices and the source folder too.

## Patents

- No patent licence is granted for any codec or format (HEVC/H.265, AV1, VP9 and others). Some of these are
  covered by patents in some countries. The organisation is responsible for any licence it needs.
- The app decodes only. It does not encode HEVC or AV1: saved copies are JPG or PNG.

## Support status

- A personal project, maintained by one person in spare time. No SLA, no guaranteed response or fix time and
  no commercial support. Provided "as is", without warranty, as the MIT licence says.
- No auto-update: a new version is a new `Setup.exe`, shared with its notices, SBOM, source and checksums.

## Report a vulnerability

Use a private GitHub security advisory (https://github.com/xav-art/Media-reader-x/security/advisories/new),
not a public issue. See [SECURITY.md](../SECURITY.md).
