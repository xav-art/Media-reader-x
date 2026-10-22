# Changelog

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
