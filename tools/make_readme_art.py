"""Generate the README hero and format map as light and dark SVG files.

Run from the repo root:  .venv\\Scripts\\python tools\\make_readme_art.py
Output: docs/brand/hero-{dark,light}.svg, docs/brand/formats-{dark,light}.svg and
docs/brand/architecture-{dark,light}.svg

Hero and formats follow DESIGN.md: ink-tinted surfaces and one coral accent. The light theme uses a
darker coral for text so it keeps 4.5:1 contrast. The architecture diagram uses Xavier's XWSpace
tokens (resolved values, both themes; the protected zone is the brand teal in both). The README
picks a file with <picture>.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediareaderx.formats import ALL_EXTS  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "brand"

THEMES = {
    "dark": {"bg": "#11172A", "text": "#E8ECF4", "muted": "#B4BDD0", "line": "#3A4560", "accent": "#FF7A59"},
    "light": {"bg": "#EEF1F7", "text": "#1C2540", "muted": "#4A5570", "line": "#C5CCDB", "accent": "#C23A1A"},
}
FONT = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"

# The "Pixel x" logo from src/mediareaderx/assets/logo.svg, drawn on a 256 px grid.
LOGO = (
    '<rect x="8" y="8" width="240" height="240" rx="56" fill="#1C2540"/>'
    '<rect x="20" y="20" width="72" height="72" rx="12" fill="#3CC8B4"/>'
    '<rect x="164" y="164" width="72" height="72" rx="12" fill="#3CC8B4"/>'
    '<rect x="164" y="20" width="72" height="72" rx="12" fill="#FF7A59"/>'
    '<rect x="20" y="164" width="72" height="72" rx="12" fill="#FF7A59"/>'
    '<rect x="92" y="92" width="72" height="72" rx="12" fill="#FFF6EC"/>'
    '<polygon points="117,110 117,146 147,128" fill="#1C2540"/>'
)

ROWS = [
    ("Images", "HEIC/HEIF, AVIF, JPEG XL, WebP, QOI, PSD, PSB, JPG, PNG, GIF, TIFF, BMP, TGA, DDS"),
    ("Camera RAW", "CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2 and 16 more"),
    ("Vector", "SVG, SVGZ"),
    ("Video", "HEVC, H.264, VP9, AV1, MPEG-4 in MP4, MOV, MKV, WebM, AVI, M2TS"),
    ("Motion", "iPhone Live Photos, animated GIF, WebP, AVIF, JXL"),
]


def hero(t: dict[str, str]) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 240" role="img" aria-labelledby="t d">
  <title id="t">Media Reader x</title>
  <desc id="d">Open the photos and videos Windows can't. Offline, no admin rights, no telemetry.</desc>
  <rect width="960" height="240" rx="16" fill="{t['bg']}"/>
  <g transform="translate(56 56) scale(0.5)">{LOGO}</g>
  <g font-family="{FONT}">
    <text x="224" y="118" font-size="56" font-weight="700" letter-spacing="-1" fill="{t['text']}">Media Reader <tspan fill="{t['accent']}">x</tspan></text>
    <text x="224" y="162" font-size="24" fill="{t['text']}">Open the photos and videos Windows can't.</text>
    <text x="224" y="198" font-size="18" fill="{t['muted']}">Offline  ·  No admin rights  ·  No telemetry</text>
  </g>
</svg>
"""


def formats(t: dict[str, str]) -> str:
    top, step = 156, 44
    height = top + step * len(ROWS) + 16
    rows = []
    for i, (label, value) in enumerate(ROWS):
        y = top + i * step
        rows.append(
            f'<line x1="40" x2="920" y1="{y - 28}" y2="{y - 28}" stroke="{t["line"]}" stroke-width="1"/>'
            f'<text x="40" y="{y}" font-size="18" font-weight="600" fill="{t["muted"]}">{label}</text>'
            f'<text x="200" y="{y}" font-size="17" fill="{t["text"]}">{value}</text>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 {height}" role="img" aria-labelledby="t d">
  <title id="t">Supported formats</title>
  <desc id="d">{len(ALL_EXTS)} file types: images, camera RAW, vector, video and motion.</desc>
  <rect width="960" height="{height}" rx="16" fill="{t['bg']}"/>
  <g font-family="{FONT}">
    <text x="40" y="96" font-size="72" font-weight="700" letter-spacing="-2" fill="{t['accent']}">{len(ALL_EXTS)}</text>
    <text x="144" y="92" font-size="28" fill="{t['text']}">file types in one viewer</text>
    {''.join(rows)}
  </g>
</svg>
"""


# XWSpace tokens (python ~/.claude/skills/xwspace/scripts/build_tokens.py --resolve), per theme.
XW = {
    "dark": {"page": "#161c21", "raised": "#2e3944", "text": "#eef1ee", "muted": "#9aafb3", "line": "#3d4a55",
             "accent": "#e0a07a", "brand": "#124e66", "on_brand": "#d3d9d4"},
    "light": {"page": "#d3d9d4", "raised": "#eef1ee", "text": "#212a31", "muted": "#4b5f64", "line": "#b3bdb7",
              "accent": "#8f4622", "brand": "#124e66", "on_brand": "#eef1ee"},
}
XW_SANS = "'Hanken Grotesk', 'Segoe UI', Arial, sans-serif"
XW_MONO = "'IBM Plex Mono', Consolas, monospace"
DEEP, DEEP_TEXT, DEEP_MUTED = "#0c3a4c", "#eef1ee", "#9aafb3"  # palette-teal-deep, lunar-light, haze-light

APP_STEPS = [
    ("01", "Check the content", "One decoder per format, chosen by content"),
    ("02", "Open the file here", "The helper gets a read-only handle, no name"),
    ("03", "Check every reply", "Sizes, pixel formats and text, field by field"),
    ("04", "Show or play", "Plain text only. Allowed video codecs only"),
]
HELPERS = [
    ("Viewer", "LibRaw, libheif, libjxl, Pillow,", "psd-tools and Qt SVG decode the photo"),
    ("Thumbnails", "Photo, RAW, PSD, SVG and video", "previews for the strip"),
    ("Video checker, one file each", "FFmpeg lists every stream: each must", "use one of about 40 common codecs"),
]


def architecture(t: dict[str, str]) -> str:
    """Main app on the left, protected helpers on the right, the handle and the checked reply between."""
    width, height = 960, 640
    app_x, app_w, box_y, box_h = 40, 400, 140, 440
    sbx_x, sbx_w = 540, 380
    parts = []
    # The app.
    parts.append(f'<rect x="{app_x}" y="{box_y}" width="{app_w}" height="{box_h}" rx="2" fill="{t["raised"]}" '
                 f'stroke="{t["line"]}"/>')
    parts.append(f'<text x="{app_x + 24}" y="{box_y + 34}" font-size="13" letter-spacing="1.6" '
                 f'fill="{t["muted"]}">MEDIA READER X, YOUR ACCOUNT</text>')
    step_y, step_h, gap = box_y + 56, 80, 16
    for i, (num, title, detail) in enumerate(APP_STEPS):
        y = step_y + i * (step_h + gap)
        parts.append(f'<rect x="{app_x + 24}" y="{y}" width="{app_w - 48}" height="{step_h}" rx="2" '
                     f'fill="{t["page"]}" stroke="{t["line"]}"/>')
        parts.append(f'<text x="{app_x + 44}" y="{y + 34}" font-family="{XW_MONO}" font-size="14" '
                     f'fill="{t["muted"]}">{num}</text>')
        parts.append(f'<text x="{app_x + 84}" y="{y + 34}" font-size="18" fill="{t["text"]}">{title}</text>')
        parts.append(f'<text x="{app_x + 84}" y="{y + 58}" font-size="14" fill="{t["muted"]}">{detail}</text>')
        if i:
            parts.append(f'<line x1="{app_x + 52}" x2="{app_x + 52}" y1="{y - gap}" y2="{y}" '
                         f'stroke="{t["line"]}" stroke-width="2"/>')
    # The protected zone: brand teal in both themes, like the other "space" surfaces.
    parts.append(f'<rect x="{sbx_x}" y="{box_y}" width="{sbx_w}" height="{box_h}" rx="2" fill="{t["brand"]}"/>')
    parts.append(f'<text x="{sbx_x + 24}" y="{box_y + 34}" font-size="13" letter-spacing="1.6" '
                 f'fill="{t["on_brand"]}">PROTECTED HELPERS, APPCONTAINER</text>')
    hy, hh, hgap = box_y + 56, 88, 14
    for i, (title, line1, line2) in enumerate(HELPERS):
        y = hy + i * (hh + hgap)
        parts.append(f'<rect x="{sbx_x + 24}" y="{y}" width="{sbx_w - 48}" height="{hh}" rx="2" fill="{DEEP}"/>')
        parts.append(f'<text x="{sbx_x + 44}" y="{y + 32}" font-size="18" fill="{DEEP_TEXT}">{title}</text>')
        parts.append(f'<text x="{sbx_x + 44}" y="{y + 56}" font-size="14" fill="{DEEP_MUTED}">{line1}</text>')
        parts.append(f'<text x="{sbx_x + 44}" y="{y + 76}" font-size="14" fill="{DEEP_MUTED}">{line2}</text>')
    by = hy + 3 * (hh + hgap) + 18
    for k, line in enumerate(("No access to your files, the network or the screen.",
                              "No programs, no clipboard. Own memory limit.")):
        parts.append(f'<text x="{sbx_x + 24}" y="{by + k * 22}" font-size="14" fill="{t["on_brand"]}">{line}</text>')
    # Between them: the handle goes in, the checked pixels come back (copper: the one highlight).
    mid_l, mid_r = app_x + app_w, sbx_x
    for y, label, rightward in ((step_y + step_h + gap + 40, "handle", True),
                                (step_y + 2 * (step_h + gap) + 40, "pixels", False)):
        x1, x2 = (mid_l + 6, mid_r - 10) if rightward else (mid_r - 6, mid_l + 10)
        head = f"{x2},{y} {x2 - 8 if rightward else x2 + 8},{y - 6} {x2 - 8 if rightward else x2 + 8},{y + 6}"
        parts.append(f'<line x1="{x1}" x2="{x2}" y1="{y}" y2="{y}" stroke="{t["accent"]}" stroke-width="2"/>')
        parts.append(f'<polygon points="{head}" fill="{t["accent"]}"/>')
        parts.append(f'<text x="{(mid_l + mid_r) // 2}" y="{y - 12}" text-anchor="middle" font-family="{XW_MONO}" '
                     f'font-size="13" fill="{t["muted"]}">{label}</text>')
    desc = ("Media Reader x checks each file's content, then hands a read-only handle to a protected helper "
            "process in a Windows AppContainer. The helpers decode photos, make thumbnails and check videos. "
            "The app checks every reply before it shows the picture, and plays only videos with common codecs.")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">
  <title id="t">How Media Reader x opens a file</title>
  <desc id="d">{desc}</desc>
  <rect width="{width}" height="{height}" rx="2" fill="{t['page']}"/>
  <g font-family="{XW_SANS}">
    <text x="40" y="70" font-size="32" letter-spacing="-0.6" fill="{t['text']}">How Media Reader x opens a file</text>
    <text x="40" y="104" font-size="17" fill="{t['muted']}">Decoders run in locked-down helper processes. The app shows only what passes its checks.</text>
    {''.join(parts)}
    <text x="40" y="{box_y + box_h + 36}" font-size="13" fill="{t['muted']}">Video playback stays in the app, because Qt needs the graphics card. Details: docs/SANDBOX.md</text>
  </g>
</svg>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        (OUT / f"hero-{name}.svg").write_text(hero(theme), encoding="utf-8", newline="\n")
        (OUT / f"formats-{name}.svg").write_text(formats(theme), encoding="utf-8", newline="\n")
        (OUT / f"architecture-{name}.svg").write_text(architecture(XW[name]), encoding="utf-8", newline="\n")
    print("wrote 6 files to", OUT)


if __name__ == "__main__":
    main()
