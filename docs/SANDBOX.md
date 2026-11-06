# Protected decoding (sandbox)

Media Reader x 1.6 reads pictures, makes thumbnails and checks videos in separate helper processes that
Windows locks down. If a crafted file exploits a bug in a decoder (LibRaw, libheif, libde265, libjxl,
FFmpeg, Pillow, psd-tools, Qt), the attacker gets a process that cannot read your files, write anything
outside its own folder, use the network, start programs, use the clipboard or see your screen. The app
notices that the helper stopped, says the file is damaged, and starts a new helper.

Code: `src/mediareaderx/sandbox/` (`winapi.py` Windows calls, `protocol.py` messages, `worker.py` the
helper, `__init__.py` the pool and the policy), `videoprobe.py` (video codec check).

## What runs where

| Work | Where |
|---|---|
| Photo decode (screen copy; full resolution for zoom, copy, save, print) | Viewer helper |
| Thumbnails (photos, RAW previews, PSD previews, SVG) and video thumbnails | Thumbnail helper |
| Container signature and FFmpeg's format probe (a short, fixed parser) | Main app, and again in the helper |
| Video codec check before playing (stream list, FFmpeg's stream analysis) | A fresh checker helper, used once |
| Video playback | Main app (Qt needs the graphics card): only known containers and allowed codecs |
| File name, size, date, opening the file, Save a copy, print, Recycle Bin | Main app |

## How a helper is locked down

- **One AppContainer per helper.** Helpers run in slots `xav-art.MediaReaderX.Helper.0` to `.15`, shared by
  every running copy of the app. A copy claims a slot with a lock file in the user's own
  `%LOCALAPPDATA%\MediaReaderX\helper-slots` (no other user can take it) before it uses it, and the slot's
  profile is deleted and created again before each start. So two live helpers never share a container (a
  shared container would let one helper control the other), and nothing an earlier helper wrote survives.
  No capabilities except one custom capability, `xavArtMediaReaderXFiles`, which the app's own folder
  grants read and execute. Nothing else on the PC grants it.
- **No network, no files.** No `internetClient` capability. The helper can read Windows system files and
  the app's folder, and write only its own `%LOCALAPPDATA%\Packages\xav-art.mediareaderx.helper.N\AC`.
- **Another window station.** Helpers run on an empty desktop of this app copy's own (`Helpers-<pid>`) in
  the window station Windows hands out for an unnamed request: the logon session's service station, as
  Chromium's sandbox does (this PC refuses named stations). The app adds the capability to that station's
  access list. A desktop on the user's station alone was not enough: an AppContainer process can open the
  user's desktop (WinSta0\Default) by name and capture the screen. From another window station it cannot
  open WinSta0 at all. `--self-test` checks both: a screen capture shows nothing, and WinSta0\Default
  cannot be opened.
- **Jobs.** All helpers share one job: killed when the app exits or crashes, a total memory limit (half
  the PC's memory, between 2 and 8 GB), no clipboard, no other windows, no desktop or display changes, no
  global atoms, no logoff. Each helper also has its own nested job: one process, its own memory limit
  (viewer 4 GB, thumbnails and checks 1 GB). A file that needs more is reported as too large for this PC.
- **No child processes** (`PROCESS_CREATION_CHILD_PROCESS_RESTRICTED`).
- **Exploit mitigations:** terminate on heap corruption, bottom-up and high-entropy ASLR, strict handle
  checks, no legacy extension points, system fonts only, no DLLs from network shares or with a low
  integrity label (a DLL a helper writes into its own folder does not load), System32 DLLs first. The
  working folder is the app folder, which the helper cannot write.
- **No graphics driver.** Helpers decode video in software (`QT_FFMPEG_DECODING_HW_DEVICE_TYPES=,`). AV1
  needs the GPU in Qt's FFmpeg, so AV1 videos get no thumbnail picture (their length still shows).
- **One file at a time.** The app opens the file and moves a read-only handle into the helper. The
  helper is never told the path or name, only the extension, and returns nothing path-like. (A helper
  that is taken over can still ask Windows for the path of the handle it was given.)

## Messages (app and helper)

Two anonymous pipes; only the helper's ends are inheritable, and only those two handles reach it. Every
message is a 4-byte length plus UTF-8 JSON (at most 1 MB), followed by binary blobs whose sizes the JSON
declares. No pickle, no code. The app treats every reply as hostile:

- frames: at most 300; width and height 1 to 65535; within the pixel limit; format RGB32 or ARGB32
  premultiplied; exactly width x height x 4 bytes, read straight into a new QImage;
- durations and sizes are integers in range; metadata is at most 1000 rows of three strings (2000
  characters at most), with control and text-direction characters removed;
- EXIF for "Save a copy" is rebuilt in the app from a short list of camera, exposure, date and GPS tags
  with plain values (text at most 64 characters): nothing else a helper sends reaches a saved file;
- text from a helper (and from files in general) is shown as plain text only, never Qt rich text.

A reply that breaks a rule ends that helper, and the file shows as damaged.

## Lifecycle

- At start the app prepares one viewer and one fresh checker. Opening a photo in a folder starts a second
  viewer (slow photos such as HEIC and RAW then preload in parallel) and a thumbnail helper. Up to 2 viewer,
  2 thumbnail and 3 checker helpers run at once. A request waits up to 0.6 s for a busy helper before it
  starts another: a photo is usually done sooner than a helper starts. Idle helpers beyond the spares exit
  (thumbnails after 30 s, the second viewer after 60 s), so memory goes back to the PC. A helper that ended
  on its own is replaced before it gets a file. When every helper stays busy for 30 seconds, the file shows
  "still busy, try again" (the window never waits longer).
- Memory (measured 2026-11-05, 17 large photos at 1440p, development build): while browsing, the helpers
  peak at about 400 MB together; a minute after browsing stops, one viewer and one checker remain (about
  70 MB). The app itself uses about 200 MB, most of it the preloaded neighbours that make next and previous
  instant. It no longer loads numpy, Pillow or any codec.
- Time limits per request: thumbnail 20 s, video 25 s, screen copy 60 s, full resolution 120 s.
- A checker handles one video, then exits, so its verdict depends only on that file.
- Delete does not wait for helpers: their handles allow the move to the Recycle Bin.
- On first start the app grants its own folder (read and execute) to the capability. This takes a few
  seconds once. Besides that one permission entry, the app creates the helper profiles (up to 16, under
  `%LOCALAPPDATA%\Packages`), an access entry for the capability on the session's service window station,
  and, while it runs, a desktop. The uninstaller runs
  `MediaReaderX.exe --remove-sandbox`, which deletes the profiles.

## When no helper can start

- **Never worked on this PC** (for example a policy blocks AppContainers; the first start gets two tries and
  90 seconds each in the background, as an antivirus may scan the first helper slowly; a start the window
  itself waits for, such as opening a video, stops waiting after 25 seconds and shows "still busy" while the
  background start decides): the app decodes in its own process, as Media Reader x 1.5 did, and says so in the
  status bar and in Help > About. Videos still pass the codec list, thumbnails included; a codec the file's
  header names is refused before FFmpeg analyses the streams (codecs that only the analysis finds, as in some
  MPEG program streams, are decoded during it). Every 2 minutes a helper start is tried again in the
  background; the first one that works turns protection on.
- **Worked before** (on this PC, stored in settings.ini, or earlier in this session): the app fails closed.
  Files do not open, and it tries again after 30 seconds. So a helper that is taken over cannot make the
  app drop the protection. "All slots busy" is never counted as a failure.
- The environment variable `MEDIAREADERX_SANDBOX=off` turns protected decoding off (for development). It
  works in the installed app too: anything that can set it already runs with the user's rights.

## Video codec allow-list

Before a video plays, a fresh checker opens it with the demuxer the container check picked, through a
read function (no path, no protocols), runs FFmpeg's stream analysis (which decodes a few frames, inside
the helper) and lists every stream, with FFmpeg's default stream limit (as the player uses). More than 32
streams are refused. MPEG-TS, MPEG-PS, FLV and Ogg can add streams later in the file, so for those the
checker reads on through the first 32 MB and lists those streams too. The app plays the file only when every video, audio and subtitle stream
uses a listed codec:

- video: H.264, HEVC, VP8, VP9, AV1, MPEG-1, MPEG-2, MPEG-4 Part 2, H.263, Sorenson (FLV), Motion JPEG,
  PNG (cover art), WMV 7, 8 and 9, VC-1, Theora;
- audio: AAC, MP3, MP2, AC-3, E-AC-3, Opus, Vorbis, FLAC, ALAC, AMR-NB, WMA 1, 2 and Pro, PCM;
- subtitles: text only (MP4 text, SubRip, WebVTT, ASS). FFmpeg's stream analysis opens a decoder for every
  subtitle stream, in the helper and again in the app's player, even though the app never shows subtitles.
  So videos with DVD, Blu-ray or DVB picture subtitles are refused.

Data and attachment streams are never decoded. A crash during the check ends only the checker, and the
video does not play.

## What it does not protect

- **Video playback runs in the main app. This is the largest remaining risk.** Qt plays the file with
  FFmpeg's decoders and the graphics card, outside any helper. The container check, the codec list and the
  checker reduce what reaches FFmpeg and the graphics driver there, but the allowed decoders (H.264, HEVC,
  VP9, AV1 and others) are complex, and a bug in one of them can still affect the main app. A checker that a
  file takes over can approve that same file. Moving playback into a helper was considered (2026-11-05) and
  not done: AV1 would stop playing, and 4K video would need its frames copied between processes.
- **The file can change between the check and playback.** That needs write access to the folder, or a
  hostile file server that serves different bytes on the second read.
- **Streams that appear after the first 32 MB** of an MPEG-TS, MPEG-PS, FLV or Ogg file are not on the
  checker's list. The player opens a decoder only for the tracks it plays, but FFmpeg's parsers still run.
- **FFmpeg's format probe runs in the app** before a video plays (on the first 64 KB, through every demuxer's
  probe function, as FFmpeg itself would). Thumbnails do not need it: their helper checks the container.
- Bugs in Windows itself (the AppContainer boundary) or in the main app's own code.
- A helper that is taken over can show wrong pixels or text for the files it is given, and read those
  files (a reused viewer gets the next files too). It has no network and cannot write anywhere you can
  see, and its warnings are not written to the log (only their number). But it could hide data in the
  pixels it returns, and those reach the outside when you save, copy or print that picture and share it.
  Pictures it returns are never larger than asked for a screen copy (4096 px for a picture that was not
  reduced).
- Helpers run with the normal AppContainer token (not "less privileged"), with Windows' graphics kernel
  (win32k) reachable: Python's ctypes, Pillow and Qt need it. A decoder bug combined with a Windows kernel
  bug would escape the sandbox.
- A slot's profile that cannot be deleted (a file in it still open, for example by an antivirus) is reused
  as it is.
- **The install folder is writable by the user** (a per-user install). Code that already runs as the user
  can change the app's files; allow-listing by signature or hash does not cover the Python files inside.
