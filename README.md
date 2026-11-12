# crosspoint-kavita-opds-progress-font-patcher

Show [Kavita](https://github.com/Kareadita/Kavita)'s OPDS reading-progress icons
(⭘ ◔ ◑ ◕ ⬤) on a [CrossPoint](https://github.com/crosspoint-reader/crosspoint-reader)
e-reader instead of blank boxes.

This repo has a small tool, `cpfont_patch.py`, that builds a CrossPoint `.cpfont`
font family with extra glyphs, plus a ready-made recipe for **LiterataPlus**
(Literata with Kavita's five icons added). No firmware change is needed.

## Why the icons are blank

Kavita puts one icon in front of every OPDS entry title to show your reading
progress (on by default; per-user OPDS preference *Embed Progress Indicator*):

| Icon | Codepoint | Meaning in Kavita |
| --- | --- | --- |
| ⭘ | U+2B58 | Not started |
| ◔ | U+25D4 | Up to 25% |
| ◑ | U+25D1 | Over 25%, up to 50% |
| ◕ | U+25D5 | Over 50%, under 100% |
| ⬤ | U+2B24 | Finished |

CrossPoint's built-in interface fonts don't have these glyphs, so they show as
boxes. However, when an interface string contains a character the built-in
font can't draw, CrossPoint redraws the **whole string** with the selected
SD-card reading font, provided that font:

1. is an SD-card font (not a built-in one),
2. contains at least one "probe" script CrossPoint checks for (Greek, Cyrillic,
   IPA ə, Hebrew, Arabic, CJK, …; see `kFallbackProbes` in
   `src/SdCardFontSystem.cpp`), and
3. includes the interface sizes **8, 10 and 12 pt**.

Literata itself has none of the icons, and CrossPoint's stock Literata download
only ships 12–18 pt. LiterataPlus fixes both: Literata glyphs, the five icons
taken from Noto Sans Symbols 2, at 8–18 pt.

Because the fallback works per string, a title like `◔ Dune` is drawn entirely
in LiterataPlus. Titles without an icon still use the built-in interface font.

## Quick start: download LiterataPlus

Prebuilt files are in [`dist/LiterataPlus/`](dist/LiterataPlus/). Download the
six `.cpfont` files and follow [Installing on CrossPoint](#installing-on-crosspoint).
They were built from the pinned crosspoint-reader commit; rebuild (below) if a
CrossPoint firmware update changes the font format.

## Build LiterataPlus yourself

Needs Python 3.10+ and git.

```bash
python3 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
python3 cpfont_patch.py build families/literata.yaml
```

The first run clones CrossPoint's converter into `vendor/crosspoint-reader` at
the pinned commit. The build takes about 20 s and writes:

```
dist/LiterataPlus/
├── LiterataPlus_8.cpfont    (0.44 MB)
├── LiterataPlus_10.cpfont
├── LiterataPlus_12.cpfont
├── LiterataPlus_14.cpfont
├── LiterataPlus_16.cpfont
├── LiterataPlus_18.cpfont   (1.06 MB)
└── LICENSE-fonts.txt
```

It then checks every file: version-4 `.cpfont`, 8/10/12 pt present, all five
icons in every style at every size, and a probe script in the regular style.
Ligature warnings from the converter (`T_h.liga`, `f_f_b`, …) are normal and
hidden unless you pass `--verbose`.

## Installing on CrossPoint

Copy **all six** `LiterataPlus_*.cpfont` files, including 8, 10 and 12 pt.
Without those sizes the icons stay blank.

**Option A: SD card (most reliable)**

1. Power the reader off and put its SD card in a computer.
2. Create `/.fonts/LiterataPlus/` on the card (or `/fonts/LiterataPlus/` if your
   computer hides dot-folders; both are scanned at boot).
3. Copy the six `.cpfont` files into it (and `LICENSE-fonts.txt` if you like).
4. Eject the card, put it back and power the reader on.

**Option B: web upload over Wi-Fi**

1. On the reader, start *File Transfer* and connect with *Join Network* or
   *Create Hotspot*.
2. Open the address it shows in a browser and go to the *Fonts* tab.
3. Upload all six `.cpfont` files (this tab accepts `.cpfont` only).

**Then, on the reader**

1. *Settings > Reader > Font Family* → **LiterataPlus**.
2. *Settings > Reader > Font Size* → your usual size. The list also shows 8 and
   10 pt because the family ships them for the interface; ignore them unless
   you want to read that small.
3. Open the Kavita OPDS browser. Titles should start with ⭘ ◔ ◑ ◕ or ⬤.

**If the icons are still blank**, check that:

- LiterataPlus is the selected font family (not a built-in one);
- 8, 10 and 12 pt files are all on the card;
- only one family named LiterataPlus exists across `/.fonts/` and `/fonts/`
  (`/.fonts/` wins);
- Kavita's *Embed Progress Indicator* OPDS preference is on for your user.

**Tip:** the icon also ends up in the downloaded book's filename. If
CrossPoint's KOReader Sync *Document matching* is set to *Filename*,
re-downloading a book after its icon changes creates a new sync identity. Set
*Document matching* to *Binary* to avoid that.

## Patching other fonts

The main font must contain at least one probe script (Greek, Cyrillic, IPA ə,
Hebrew, Arabic, Armenian, Georgian, Ethiopic, Cherokee, Tifinagh, Hangul or
CJK). Most book fonts with broad Latin coverage include Greek or Cyrillic. The
tool refuses fonts that don't, since CrossPoint would never use them for the
interface fallback. The tool always adds 8, 10 and 12 pt.

Give the result a **new family name** (e.g. `<Font>Plus`). A family with the
same name as one already on the card, or as CrossPoint's stock downloads,
collides with it.

### A font CrossPoint already offers for download

Any family in CrossPoint's
[`sd-fonts.yaml`](https://github.com/crosspoint-reader/crosspoint-reader/blob/master/lib/EpdFont/scripts/sd-fonts.yaml)
(sources, intervals and variable-font axes are read from there):

```bash
python3 cpfont_patch.py stock SourceSerif4
```

Output: `dist/SourceSerif4Plus/`. Use `--name` to pick another name.

### Your own TTF/OTF files

```bash
python3 cpfont_patch.py patch --regular MyFont-Regular.ttf --bold MyFont-Bold.ttf --italic MyFont-Italic.ttf --bolditalic MyFont-BoldItalic.ttf --name MyFontPlus
```

Only `--regular` is required.

### A reusable recipe

Copy `families/literata.yaml` and edit it. `styles` uses the same shape as
CrossPoint's `sd-fonts.yaml`: each style has a `path` (relative to this repo)
and/or a `url`, an optional `sha256`, and optional `variable: {wght: 400}` for
variable fonts. Then:

```bash
python3 cpfont_patch.py build families/myfont.yaml
```

### Options (build, stock, patch)

| Option | Meaning |
| --- | --- |
| `--sizes 8,10,12,14,16,18` | Point sizes. 8, 10 and 12 are always added. |
| `--intervals latin-ext,greek` | Converter interval presets. Default: the recipe's, or `latin-ext` plus every probe script the font covers. Profile codepoints are always added. |
| `--profile NAME` | Glyph profile from `profiles/` (or a YAML path). Default `kavita-progress`. Repeatable. |
| `--out DIR` | Output directory. Default `dist/<Name>/`. |
| `--dry-run` | Show coverage and the converter command without running it. |
| `--crosspoint-ref REF` | crosspoint-reader commit to take the converter from. |
| `--verbose` | Show all converter output. |

### Checking a family

```bash
python3 cpfont_patch.py check dist/LiterataPlus/
```

### Adding other glyphs (profiles)

A profile lists the codepoints to add and the font that supplies them, e.g.
`profiles/kavita-progress.yaml`. Add a new YAML file for other markers (say,
another server's status icons) and pass `--profile <name>`; no code changes.

Glyphs the main font already has are kept from the main font; only missing
ones come from the fallback. The fallback font is subset to exactly those
codepoints first, so nothing else from it leaks into the family. CrossPoint's
converter takes one fallback font per style, so all profiles used in one build
must share the same fallback font.

## How it works

`cpfont_patch.py` doesn't implement the `.cpfont` format itself. It runs
CrossPoint's own converter, `lib/EpdFont/scripts/fontconvert_sdcard.py`, from a
pinned crosspoint-reader commit (`CROSSPOINT_REF` in the script), using its
`--fallback-<style>` options. A build:

1. resolves and verifies the font sources (SHA-256), instancing variable fonts;
2. checks coverage with fontTools: probe script present, which profile glyphs
   need the fallback;
3. subsets the fallback font to those glyphs;
4. runs the converter in multi-style mode;
5. checks the output and writes `LICENSE-fonts.txt`.

`setup` also compares the tool's copy of `kFallbackProbes` with the pinned
checkout and warns if upstream has changed it. After a CrossPoint firmware
update that changes the font format, bump `CROSSPOINT_REF` and rebuild.

## Tests

```bash
python3 -m pytest
```

Integration tests run the real converter (LiterataPlus build, a no-fallback
control build that must have 0 icons, and a stock SourceSerif4 build, which
downloads it). Skip them with `python3 -m pytest -m "not integration"`.

## Repository layout

| Path | What |
| --- | --- |
| `cpfont_patch.py` | The CLI |
| `families/literata.yaml` | LiterataPlus recipe |
| `profiles/kavita-progress.yaml` | The five Kavita icons + Noto Sans Symbols 2 as fallback |
| `fonts/Literata/` | Unmodified Literata 3.103 static TTFs + OFL |
| `fonts/NotoSansSymbols2/` | Unmodified Noto Sans Symbols 2 2.008 + OFL |
| `tests/` | pytest suite |
| `dist/LiterataPlus/` | Prebuilt LiterataPlus `.cpfont` files + `LICENSE-fonts.txt` |
| `vendor/`, other `dist/` folders | Git-ignored: converter checkout, downloads, build output |

## Licensing

- **Tool code** (`cpfont_patch.py`, tests, recipes, profiles): MIT, see
  [`LICENSE`](LICENSE).
- **Literata** (`fonts/Literata/`): Copyright 2017 The Literata Project
  Authors, [SIL Open Font License 1.1](fonts/Literata/OFL.txt).
  From [googlefonts/literata](https://github.com/googlefonts/literata).
- **Noto Sans Symbols 2** (`fonts/NotoSansSymbols2/`): Copyright 2022 The Noto
  Project Authors, [SIL Open Font License 1.1](fonts/NotoSansSymbols2/OFL.txt).
  From [notofonts/symbols](https://github.com/notofonts/symbols).
- **Built families** such as LiterataPlus are Modified Versions of those fonts
  and are licensed under the OFL 1.1. Neither font declares a Reserved Font
  Name; the new name is used anyway to avoid clashing with CrossPoint's stock
  Literata. Keep `LICENSE-fonts.txt` with the files when you share them, and
  don't sell the fonts by themselves.
- The converter is not part of this repo; it is fetched from
  crosspoint-reader (MIT) at build time.
