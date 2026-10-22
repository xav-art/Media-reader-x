"""Generate the README hero and format map as light and dark SVG files.

Run from the repo root:  .venv\\Scripts\\python tools\\make_readme_art.py
Output: docs/brand/hero-{dark,light}.svg and docs/brand/formats-{dark,light}.svg

Colours follow DESIGN.md: ink-tinted surfaces and one coral accent. The light theme uses a
darker coral for text so it keeps 4.5:1 contrast. The README picks a file with <picture>.
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
    ("Video", "HEVC, H.264, VP9, AV1, ProRes in MP4, MOV, MKV, WebM, AVI, M2TS"),
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        (OUT / f"hero-{name}.svg").write_text(hero(theme), encoding="utf-8", newline="\n")
        (OUT / f"formats-{name}.svg").write_text(formats(theme), encoding="utf-8", newline="\n")
    print("wrote 4 files to", OUT)


if __name__ == "__main__":
    main()
