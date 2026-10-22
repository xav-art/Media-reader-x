# Design context

This file tells the `ui-design` skill how Media Reader x looks and speaks. Read it before every design task.

## Audience and job

- **Users**: Xavier and colleagues on locked-down Windows 10/11 work PCs, mouse and keyboard, 1080p to 4K screens.
- **Main task**: open a photo or video that Windows cannot open, look at it, check its details, move to the next file.
- **Surface job**: operate. Speed and scanning come first. The photo or video is the focal point; chrome stays quiet.

## Direction

- **Mood**: restrained, exact, technical.
- **References**: Windows Photos (calm canvas), IrfanView and JPEGView (keyboard-first speed).
- **Avoid**: gradients, glow, glass effects, text-only toolbars, emoji, neutral grey that ignores the brand ink.

## Tokens

Defined in code in `src/mediareaderx/theme.py`. Change them there and here together.

```css
:root {
  /* Brand, from the "Pixel x" icon */
  --ink: #1C2540;      /* icon tile, dark surfaces are tinted toward it */
  --coral: #FF7A59;    /* the one accent: selection, current thumbnail, highlight */
  --teal: #3CC8B4;     /* icon only (photo diagonal); not a UI state colour */
  --ivory: #FFF6EC;    /* icon only (play pixel) */

  /* Dark theme surfaces */
  --bg: #11172A;       /* base: lists, canvas */
  --surface: #171E31;  /* window */
  --raised: #212A42;   /* buttons, tooltips */
  --border: #3A4560;
  --text: #E8ECF4;
  --text-muted: #8B95AB;
  --text-disabled: #6B7590;
  --on-accent: #1C2540; /* text on coral: 6:1 */

  /* Space: 4 px scale */
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-6: 24px; --space-8: 32px;

  /* Shape */
  --radius: 8px;       /* thumbnails, rings */
}
```

Light theme values: Qt Fusion's standard light palette; the coral ring and brand icon stay the same.

## Components

- **Toolbar**: icon-only line icons (`icons.py`, 24 px grid, 1.8 stroke), 20 px, tooltip = name + shortcut.
  Tools that do not apply to the open file are hidden, not greyed out.
- **Thumbnail strip**: no title bar, 3 px coral ring on the open file, 8 px scrollbar.
- **Metadata panel**: 300 px by default, long values elide in the middle.

## Motion

- **Level**: none. The app shows content instantly.

## Voice

UI text follows ASD-STE100 and the Google developer documentation style (see the skill's `ui-copy.md`).

- **Product terms**: "file" (never "item" or "asset"), "Recycle Bin" (never "trash"), "Live Photo", "thumbnail strip",
  "metadata panel", "Save a copy". Errors name the app: "Media Reader x cannot open this image."
- **Languages**: English only.

## Exceptions

- Qt Fusion, not native Windows styling: it gives the same look on every PC and supports the brand dark palette.
