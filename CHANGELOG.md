# Changelog

## Unreleased

Security:

- Helpers now run in a new window station of the app's own (random name, create-only), which only the user,
  SYSTEM and the helpers can open; the helpers cannot change who may open it. Where Windows refuses that, as
  on a standard user's PC, they use the session's service station as before. The service station did not work for an elevated
  built-in Administrator (GitHub's Windows build machines): helpers could not start there.
- The self-test is stricter: opening the user's window station must fail with "access denied" (any other
  error fails the test), and the helper must report a window station other than the user's.

Build:

- A security scan on GitHub on the 1st and 15th of each month, Dependabot update pull requests, a full build
  of every push and pull request, and draft releases for `v*` tags (SECURITY.md).

## 1.6.1 - 2026-11-05

Memory: same speed, less memory.

- The app's own process no longer loads numpy, Pillow or any codec (it decodes nothing since 1.6.0).
- Fewer idle helpers: one spare video checker instead of two, no idle thumbnail helper, and extra helpers
  exit after 30 to 60 seconds without work. A minute after you stop browsing, the helpers use about 70 MB
  instead of about 280 MB (measured). The video checker itself is half the size (20 MB).
- Opening a photo in a folder starts a second viewer helper at once, so slow photos (HEIC, RAW) preload in
  parallel: in the benchmark every next and previous step was instant, where 1.6.0 waited about 1.5 s in
  total. A busy helper is given a moment to finish before another one starts.

Security (from a fresh adversarial review of 1.6.0):

- Fixed: the video check stopped listing streams at 32, so stream 33 of a crafted MPEG-TS file could carry
  any codec into the player. FFmpeg's own limit is used now, and more than 32 streams are refused. For
  MPEG-TS, MPEG-PS, FLV and Ogg the check also reads on through 32 MB for streams that appear later.
- Fixed: a taken-over helper could switch to the user's desktop and capture the screen. Helpers now run in
  a window station of their own; the self-test checks that the user's desktop cannot be opened.
- When protected decoding never worked on a PC, video thumbnails now pass the codec list too, the first
  start gets two tries, and a new start is tried every 2 minutes.
- Helper warnings are no longer written to the log (only their number). Pictures a helper returns may not be
  larger than asked. Thumbnails no longer run FFmpeg's format probe in the app.
- Helper slots are claimed with lock files in the user's own folder (no other user on a shared PC can block
  them).
- A codec named in a video's header is refused before FFmpeg's stream analysis decodes anything. A first
  start that the window waits for no longer decides "protection does not work here" after 25 seconds.
- docs/SANDBOX.md now says plainly what is left: video playback in the app (the largest remaining risk,
  kept by choice), hidden data in returned pixels, and the Windows kernel as an escape route.

## 1.6.0 - 2026-11-05

Security: protected decoding. Pictures, thumbnails and video checks run in locked-down helper processes.

- Every decoder that reads a file (LibRaw, libheif, libde265, libjxl, Pillow, psd-tools, Qt SVG and
  FFmpeg for thumbnails and checks) now runs in a separate helper process inside a Windows
  AppContainer. A helper gets a read-only handle to one file, never its name. It cannot read your
  other files, write outside its own folder, use the network, start programs, use the clipboard or
  see your screen (it runs on its own empty desktop). Each helper has its own container, so one
  helper cannot reach another, and a memory limit. If a crafted file takes over a helper, the app
  says the file is damaged and starts a new helper. No admin rights are needed.
- Fail closed: once protected decoding has worked on a PC, the app never silently goes back to
  decoding in its own process. Only on a PC where helpers have never started (for example a policy
  blocks AppContainers) does it decode as before, and it says so. Help > About shows the state, and
  `--self-test` checks that a helper decodes and is contained.
- Videos play only with common codecs (H.264, HEVC, VP8, VP9, AV1, MPEG-1/2/4, WMV and the sound
  formats that go with them). A fresh helper reads the codec list before the player gets the file.
  Qt's FFmpeg has about 500 decoders; a crafted video can now reach about 40. Videos with DVD,
  Blu-ray or DVB picture subtitles are refused (FFmpeg opens a subtitle decoder even when subtitles
  are off). Video thumbnails are now made by a helper, without the graphics card, so AV1 videos show
  no thumbnail picture.
- Text from files (metadata, names, error messages) is always shown as plain text. Before, a crafted
  EXIF field shown as a tooltip could make Qt load a picture, including from a network share.
- JPEG XL: the size check now reads codestreams split over several boxes (a crafted file could
  report 1 x 1 pixels and then decode at full size). Animations show their first frame: libjxl decodes
  every frame at once, before any memory cap applies.
- Multi-page TIFF: every page is size-checked, not only the first.
- A video that cannot be read is refused instead of being handed to the player unchecked.
- Logs never contain paths or file names, also not in error messages, tracebacks or Qt warnings.
- Delete refuses files on drives without a Recycle Bin (network folders, USB drives, memory cards,
  drives set to delete at once, files larger than the Recycle Bin). Before, Windows deleted those
  permanently, without a prompt.
- The installer never runs the old 1.0 uninstaller when it is started with admin rights, and the
  uninstaller removes the helpers' AppContainer profiles.

Privacy:

- "Save a copy" removes the location (GPS) by default. You can turn this off in the File menu.
- File > Open recent > "Remember recent files and folders" can be turned off: the app then stores no
  file paths in its settings.

Licences:

- THIRD_PARTY_NOTICES.txt now has the full GPL and LGPL texts, a copyright line and an exact source
  link for every bundled library. It no longer says x265 is "statically included" (it is a separate,
  never-called DLL that libheif links). The binary distribution as a whole is conveyed under GPL-3.0;
  Media Reader x's own code stays MIT. The matching source archives (x265, libheif, libde265, LibRaw,
  FFmpeg, the pillow-heif and rawpy build scripts) ship in `dist\source`.
- The SBOM lists an SPDX licence for every component.
- The README no longer claims HEVC plays "without the paid Microsoft extension" and says that no
  codec patent licence comes with the app.

## 1.5.0 - 2026-11-04

Security: each file reaches only the one decoder for its content.

- The app reads the first bytes of each file and picks the one decoder for that content. Pillow's
  other 29 openers (EPS through Ghostscript, WMF through Windows GDI, FITS, PCD...) can no longer be
  reached by a crafted file. A file whose content does not match its extension (WebP saved as .jpg,
  a PNG favicon named .ico) still opens, and the status bar says what it really is, for example
  "PNG file named .heic". Programs, archives, PDFs and text files with a picture extension are
  refused with a message that says what they are.
- Videos must start with a known container signature, and FFmpeg's own probe must agree on that
  container, before the player gets them. Playlists (HLS, concat, SDP) and pictures named as videos
  are refused. FFmpeg may open local files only (protocol whitelist "file"), never a URL.
- SVG files may embed only PNG and JPEG pictures, checked by content. Links to other files or web
  addresses, other embedded formats and DTD entity declarations are refused, and .svgz files are
  inflated with a 64 MB cap. Before, an SVG could make Qt load any picture file on the PC.
- The HEIF writer is unregistered: nothing in the app can reach the bundled x265 encoder. The
  self-test now decodes committed HEIC and AVIF samples instead of encoding them.
- Six unused Qt image plugins are no longer shipped (GIF, ICNS, TGA, TIFF, WBMP, WebP).
- Built with Python 3.14.10 (3.12 gets no more Windows installers, so its security fixes never ship)
  and rawpy 0.28.0 (LibRaw 0.22.2).
- The SBOM now lists the 28 native libraries inside the wheels (libheif, libde265, LibRaw, libjxl,
  dav1d, libwebp, FFmpeg, Qt and others) with CPE ids, so vulnerability scanners can match them.
- The build runs pip-audit and grype and stops on any High or Critical finding without a dated
  exception in `packaging/vuln-exceptions.json`.

Fixed:

- AV1 video on a PC without AV1 graphics hardware showed a black picture with no message. Qt's
  FFmpeg decodes AV1 only on the GPU and reports nothing when it cannot. The app now notices when an
  AV1 clip plays with no picture (for 3 seconds, or most of a short clip, or to its end), stops it
  and says why. After that, thumbnails skip AV1 files at once instead of waiting 4 seconds each,
  until an AV1 clip shows a picture. A Live Photo clip that fails goes back to the photo. The README
  and INSTALL.txt no longer say that AV1 plays everywhere.
- Delete sometimes said a file "could not be moved to the Recycle Bin" when it was only open for a
  moment: by an antivirus scan, or by the app itself (a RAW or PSD still loading, a video thumbnail).
  The app now waits up to 2 seconds for the file to close. A Live Photo video now goes to the Recycle
  Bin only together with its photo, and if the video cannot move, the status bar says so.

## 1.4.1 - 2026-10-21

- Fixed: iPhone JPEGs saved with an HDR gain map (MPO files, also some Samsung, Fuji and Nikon photos)
  flickered and showed sideways. The app played the photo and its grey gain map as a two-frame
  animation and skipped the EXIF rotation. They now open as one upright photo, with a screen copy
  and fast JPEG decoding. The status bar shows "JPEG (MPO)".

## 1.4.0 - 2026-10-21

- Photoshop files: PSD and PSB, 8, 16 and 32-bit, through psd-tools (MIT). Before, only 8-bit PSD
  opened, and 16-bit files (Lightroom's "Edit in Photoshop" default) failed.
- The app shows the flattened image that Photoshop saves in the file. If a file has none
  ("Maximize compatibility" off), a message says how to fix it in Photoshop.
- The metadata panel shows the colour mode, the bit depth, and the number of layers.
- The thumbnail strip uses Photoshop's own preview when the file has one, so a large document is
  not flattened just for a tile.
- PSD memory is freed as soon as the picture is ready: a 161 MB, 16-bit, 24 MP file keeps 16 MB
  for display (peak 596 MB while it decodes, 61 MB after).

## 1.3.1 - 2026-10-21

Memory: measured with ten 48 MP photos, the app now uses about 170 MB instead of 1,230 MB.

- Fixed: the app reserved about 430 MB at startup for numpy's maths library (OpenBLAS), which it
  never uses. The idle app now uses about 65 MB instead of 485 MB.
- Photos open and preload as screen-size copies (about 19 MB each on a 2560 px screen instead of
  183 MB for a 48 MP photo). JPEGs decode straight at reduced size, which is also faster, and RAW
  files use LibRaw's half-size mode when it still covers the screen.
- Zoom past the screen copy decodes full resolution in the background for the open photo only.
  Copy picture and Save a copy always use full resolution. Print decodes at the size it prints.
- The preload cache is sized to the screen (about 98 MB at 2560 px, was 768 MB). Previous and next
  stay instant: every step was preloaded in the measurement.
- The metadata panel and status bar still show the real size of the photo.

From an adversarial optimisation review (CPU, GPU, disk, memory; 16 findings, measured):
- Screen copies shrink with a whole-number reduce, then one short resample: 24 MP PNG 354 -> 200 ms,
  HEIC 439 -> 347 ms, 48 MP JPEG 207 -> 145 ms.
- Photos with an sRGB colour profile skip the colour conversion (50 ms per photo, 530 ms at 48 MP).
- JPEG XL decodes on 4 threads (3.4x faster). Opaque PNGs no longer get a second copy on screen
  (up to 183 MB). Converting a decoded photo for Qt peaks 139 MB lower at 48 MP.
- Full resolution is decoded only when zoom enlarges the screen copy by more than 25%.
- The thumbnail strip uses the preview a camera or phone stores in a JPEG (reads 128 KB, not the
  whole file), when the preview is large enough to stay sharp.
- Moving between photos in one folder no longer rewrites settings.ini each time.
- The download is smaller: Setup 56.7 -> 42.8 MB, portable zip 83.6 -> 62.3 MB, installed 203 -> 163 MB
  (software OpenGL, the virtual keyboard and Qt Quick, and OpenSSL were never loaded).
- Build: the frozen self-test report is deleted before each run, so an old report is never shown as new.

## 1.3.0 - 2026-10-21

- Print (Ctrl+P): put several pictures on one A4 page. Layouts: Auto, 1, 2, 4, 6 or 9 per page, portrait or
  landscape. Fit shows the whole picture; Fill crops it to the space. Margin from 0 to 30 mm, optional file names.
- A live preview shows each page as it will print. More pictures than one page holds go onto more pages.
- Save as PDF writes the same pages to a PDF file (300 dpi), with no printer needed.
- Choose the pictures in the dialog's checklist, or Ctrl+click and Shift+click thumbnails in the strip first.
  A plain click still opens the file. Videos are not offered.
- A picture rotated in the viewer prints with that rotation (the file is not changed).
- Pictures are decoded one page at a time and scaled to their space, so large RAW files do not fill the memory.
  A file that cannot be decoded leaves an empty space and is named after printing.
- The print settings are remembered. The self-test also checks print and PDF support.

## 1.2.0 - 2026-10-21

- Instant previous/next: the next, previous and second-next photos decode in the background
  (memory-capped cache, about 768 MB).
- Welcome screen with the logo, **Open file** and **Open folder** buttons, and recent files.
- Open folder (Ctrl+Shift+O). Dropping a folder or passing one on the command line opens its first file.
- Distraction-free full screen (F11): only the photo or video shows. The pointer and the video controls hide
  after 2.5 s without mouse movement, and a short overlay shows the file name and position.
- Thumbnail strip badges: the format (HEIC, RAW, JPG...) and the video length.
- Slideshow (F5): full screen, 3, 5 or 10 seconds per photo, videos play to the end, wraps to the start. Esc stops it.
- The welcome screen hides the empty panels; full screen uses a black backdrop and returns to a maximized window.
- Hardening and speed from an adversarial review (21 findings): no more stuck "Loading" on unexpected errors;
  stale decodes are cancelled (rapid navigation 2.2 s -> 1.0 s to the last file) and an in-flight preload is reused;
  thumbnail strip memory for a 5,000-file folder +731 MB -> +42 MB; stepping in that folder 177 ms -> 14 ms;
  the pixel limit is enforced exactly (also for JPEG XL headers and thumbnails); animation caps 300 frames / 384 MB;
  unreachable network paths no longer freeze the welcome screen; a damaged settings file no longer stops startup;
  photos show without a pixel-format conversion; codecs load while the welcome screen is idle.

## 1.1.0 - 2026-10-21

- Renamed to **Media Reader x** (`MediaReaderX.exe`). The installer removes 1.0 automatically and imports its settings. Default apps must be set again once.
- New "Pixel x" logo and app icon, pixel-snapped at 16 and 32 px.
- Redesigned UI: icon toolbar with shortcut tooltips, image-only tools hidden for video, a dark theme tinted to the
  brand ink, a coral ring on the current thumbnail, a 300 px metadata panel, and middle-elided long values.
- Folder navigation: Left/Right, PgUp/PgDn, Home/End, Space, mouse back/forward, plus a "3 / 42" counter. The video half of a Live Photo is hidden from the list.
- Thumbnail strip (T) of the current folder, centred on the open file, with real frames for videos.
- Rotate view (R / Shift+R), Copy picture (Ctrl+C), Copy path (Ctrl+Shift+C), Open recent.
- Move to Recycle Bin (Del). A Live Photo's video goes with it. Nothing is ever deleted permanently.
- Save a copy as JPG/PNG (Ctrl+S). EXIF is kept, with optional GPS removal.
- Video: speed 0.25x to 2x ([ / ]) and loop (Ctrl+L). Seeking moves to Shift+Left/Right.
- Theme: Light, Dark or Same as Windows.
- Fixed: camera RAW files did not open (EXIF was read after the file closed).
- Fixed: Save a copy as JPG failed for RAW files whose camera MakerNote made the EXIF larger than 64 KB.
- Fixed: on first start, the window could be taller than the screen at 150% scaling.
- README screenshots.

## 1.0.0 - 2026-10-21

- First release.
- Images: HEIC/HEIF, AVIF, JPEG XL, WebP, PSD, QOI, SVG, camera RAW, and the common formats. Colour-managed to sRGB, with EXIF orientation applied.
- Video: HEVC, H.264, VP9, AV1 and more via Qt Multimedia (bundled FFmpeg).
- iPhone Live Photo playback, plus animated GIF/WebP/AVIF/JXL.
- Metadata panel: file, image, camera, GPS, full EXIF, and video stream details.
- Per-user Inno Setup installer (no admin) with "Open with" registration, a portable zip, an SBOM, checksums, and a `--self-test` health check.
