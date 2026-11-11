#!/usr/bin/env python3
"""cpfont_patch - build CrossPoint .cpfont font families with extra glyphs.

CrossPoint's built-in interface fonts can't draw some characters (for
example Kavita's OPDS reading-progress icons). When an interface string
contains such a character, CrossPoint redraws the whole string with the
selected SD-card reading font, as long as that font:

  * contains at least one "probe" script character (kFallbackProbes), and
  * ships the interface sizes 8, 10 and 12 pt.

This tool builds such a family. It wraps CrossPoint's own converter
(lib/EpdFont/scripts/fontconvert_sdcard.py at a pinned commit) rather than
reimplementing the .cpfont format, adds the missing glyphs from a fallback
font described by a "profile", and checks the result.

Commands:
  setup                       clone/update the pinned crosspoint-reader checkout
  build RECIPE.yaml           build a family from a recipe file
  stock FAMILY                patch a family listed in CrossPoint's sd-fonts.yaml
  patch --regular A.ttf ...   patch local font files
  check DIR                   check an existing .cpfont family

Run `cpfont_patch.py <command> --help` for options.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENDOR_DIR = REPO_ROOT / "vendor"
CHECKOUT_DIR = VENDOR_DIR / "crosspoint-reader"
FONT_CACHE_DIR = VENDOR_DIR / "fonts"
WORK_DIR = VENDOR_DIR / "work"
PROFILES_DIR = REPO_ROOT / "profiles"
DIST_DIR = REPO_ROOT / "dist"

CROSSPOINT_URL = "https://github.com/crosspoint-reader/crosspoint-reader.git"
# Pinned crosspoint-reader commit. The converter there defines the .cpfont
# format; bump this after a firmware update and rebuild.
CROSSPOINT_REF = "3a5bfac375c293cb38f7e6e28f7736e7d451ef57"
CONVERTER_REL = Path("lib/EpdFont/scripts/fontconvert_sdcard.py")
SD_FONTS_YAML_REL = Path("lib/EpdFont/scripts/sd-fonts.yaml")
PROBES_CPP_REL = Path("src/SdCardFontSystem.cpp")

CPFONT_VERSION = 4
CPFONT_MAGIC = b"CPFONT\x00\x00"
HEADER_FORMAT = "<8sHHB19s"
TOC_FORMAT = "<B3xIIBhhHHBBBI4x"
INTERVAL_FORMAT = "<III"

# Interface sizes CrossPoint loads from the SD font for the UI fallback.
UI_SIZES = (8, 10, 12)
DEFAULT_SIZES = (8, 10, 12, 14, 16, 18)

STYLES = ("regular", "bold", "italic", "bolditalic")
STYLE_IDS = {name: i for i, name in enumerate(STYLES)}

# Copy of kFallbackProbes in src/SdCardFontSystem.cpp (setupUiFallbacks).
# `setup` warns if the pinned checkout's list differs.
# codepoint -> (script, converter interval preset that covers it)
FALLBACK_PROBES = {
    0x4E00: ("Han", "cjk"),
    0x3042: ("Hiragana", "cjk"),
    0x30A2: ("Katakana", "cjk"),
    0xAC00: ("Hangul", "hangul"),
    0x03B1: ("Greek", "greek"),
    0x0430: ("Cyrillic", "cyrillic"),
    0x05D0: ("Hebrew", "hebrew"),
    0x0627: ("Arabic", "arabic"),
    0x0531: ("Armenian", "armenian"),
    0x10D0: ("Georgian", "georgian"),
    0x1200: ("Ethiopic", "ethiopic"),
    0x13A0: ("Cherokee", "cherokee"),
    0x2D30: ("Tifinagh", "tifinagh"),
    0x0259: ("IPA", "ipa-chars"),
}

DEFAULT_PROFILE = "kavita-progress"


class PatchError(Exception):
    """A problem the user can fix; printed without a traceback."""


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    log(f"warning: {msg}")


def fmt_cp(cp: int) -> str:
    return f"U+{cp:04X} {chr(cp)}"


# --------------------------------------------------------------------------
# YAML / profiles
# --------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    import yaml

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise PatchError(f"{path}: file not found") from None
    if not isinstance(data, dict):
        raise PatchError(f"{path}: expected a mapping at the top level")
    return data


@dataclass
class Profile:
    name: str
    codepoints: list[int]
    fallback: dict | None
    description: str = ""
    source_path: Path | None = None


def parse_codepoint(value) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.upper().startswith("U+"):
        return int(s[2:], 16)
    if len(s) == 1:
        return ord(s)
    return int(s, 0)


def load_profile(name_or_path: str) -> Profile:
    p = Path(name_or_path)
    if p.suffix in (".yaml", ".yml") or p.exists():
        path = p.resolve()
    else:
        path = PROFILES_DIR / f"{name_or_path}.yaml"
        if not path.exists():
            available = ", ".join(sorted(x.stem for x in PROFILES_DIR.glob("*.yaml")))
            raise PatchError(f"unknown profile '{name_or_path}' (available: {available})")
    data = load_yaml(path)
    try:
        cps = [parse_codepoint(c) for c in data["codepoints"]]
    except (KeyError, TypeError, ValueError) as e:
        raise PatchError(f"{path}: 'codepoints' must be a list of codepoints ({e})") from None
    if not cps:
        raise PatchError(f"{path}: 'codepoints' is empty")
    return Profile(
        name=data.get("name", path.stem),
        codepoints=cps,
        fallback=data.get("fallback"),
        description=data.get("description", ""),
        source_path=path,
    )


# --------------------------------------------------------------------------
# CrossPoint checkout
# --------------------------------------------------------------------------

def git(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    try:
        res = subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                             stdout=subprocess.PIPE if capture else None)
    except FileNotFoundError:
        raise PatchError("git is required for `setup`") from None
    except subprocess.CalledProcessError as e:
        raise PatchError(f"git {' '.join(args)} failed (exit {e.returncode})") from None
    return (res.stdout or "").strip()


def checkout_head(checkout: Path) -> str | None:
    if not (checkout / ".git").exists():
        return None
    try:
        return git("rev-parse", "HEAD", cwd=checkout, capture=True)
    except PatchError:
        return None


def resolve_ref(checkout: Path, ref: str) -> str | None:
    try:
        return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=checkout, capture=True)
    except PatchError:
        return None


def setup_checkout(ref: str = CROSSPOINT_REF, checkout: Path = CHECKOUT_DIR) -> Path:
    """Clone crosspoint-reader into vendor/ (if needed) and check out `ref`."""
    if not (checkout / ".git").exists():
        log(f"Cloning {CROSSPOINT_URL} into {checkout} ...")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--filter=blob:none", "--no-checkout", CROSSPOINT_URL, str(checkout))
    target = resolve_ref(checkout, ref)
    if target is None:
        log(f"Fetching {ref} ...")
        git("fetch", "--filter=blob:none", "origin", ref, cwd=checkout)
        target = resolve_ref(checkout, ref) or resolve_ref(checkout, "FETCH_HEAD")
        if target is None:
            raise PatchError(f"could not find crosspoint-reader ref {ref}")
    if checkout_head(checkout) != target:
        git("-c", "advice.detachedHead=false", "checkout", "--detach", target, cwd=checkout)
    log(f"crosspoint-reader at {target[:12]} ({checkout})")
    check_probe_list(checkout)
    return checkout


def ensure_checkout(ref: str) -> Path:
    head = checkout_head(CHECKOUT_DIR)
    if head is None or not (CHECKOUT_DIR / CONVERTER_REL).exists():
        return setup_checkout(ref)
    wanted = resolve_ref(CHECKOUT_DIR, ref)
    if wanted != head:
        return setup_checkout(ref)
    return CHECKOUT_DIR


def parse_probe_list(cpp_source: str) -> list[int] | None:
    m = re.search(r"kFallbackProbes\[\]\s*=\s*\{(.*?)\};", cpp_source, re.S)
    if not m:
        return None
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]+)", body)]


def check_probe_list(checkout: Path) -> bool:
    cpp = checkout / PROBES_CPP_REL
    try:
        probes = parse_probe_list(cpp.read_text(encoding="utf-8"))
    except OSError:
        probes = None
    if probes is None:
        warn(f"could not find kFallbackProbes in {cpp}; the probe check may be out of date")
        return False
    if set(probes) != set(FALLBACK_PROBES):
        added = ", ".join(fmt_cp(c) for c in sorted(set(probes) - set(FALLBACK_PROBES))) or "none"
        removed = ", ".join(fmt_cp(c) for c in sorted(set(FALLBACK_PROBES) - set(probes))) or "none"
        warn("kFallbackProbes in the crosspoint-reader checkout differs from this tool's copy "
             f"(added upstream: {added}; removed upstream: {removed}). "
             "Update FALLBACK_PROBES in cpfont_patch.py.")
        return False
    return True


# --------------------------------------------------------------------------
# Font sources
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cpfont-patch"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise PatchError(f"download failed: {url}: {e}") from None
    tmp.replace(dest)
    return dest


def instance_variable_font(src: Path, axes: dict) -> Path:
    """Pin variable-font axes to make a static instance (as CrossPoint's
    build-sd-fonts.py does: freetype-py can't set axis values)."""
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    key = "_".join(f"{k}{v}" for k, v in sorted(axes.items()))
    out = VENDOR_DIR / "instances" / f"{src.stem}_{sha256_file(src)[:12]}_{key}.ttf"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"Instancing {src.name} at {axes}")
    source = TTFont(str(src))
    try:
        font = instantiateVariableFont(source, dict(axes), updateFontNames=True, optimize=False)
        tmp = out.with_suffix(".tmp")
        font.save(str(tmp))
        font.close()
        tmp.replace(out)
    finally:
        source.close()
    return out


def resolve_font(spec, base_dir: Path, label: str, cache_subdir: str = "") -> Path:
    """Resolve a style spec ({path|url, sha256?, variable?} or a plain path)."""
    if isinstance(spec, (str, Path)):
        spec = {"path": str(spec)}
    if not isinstance(spec, dict):
        raise PatchError(f"{label}: expected a path or a mapping with 'path'/'url'")
    path = None
    if spec.get("path"):
        p = Path(spec["path"]).expanduser()
        p = p if p.is_absolute() else base_dir / p
        if p.exists():
            path = p
        elif not spec.get("url"):
            raise PatchError(f"{label}: font file not found: {p}")
    if path is None:
        url = spec.get("url")
        if not url:
            raise PatchError(f"{label}: needs 'path' or 'url'")
        dest = FONT_CACHE_DIR / cache_subdir / url.rsplit("/", 1)[-1].split("?")[0]
        expected = spec.get("sha256")
        if not dest.exists() or (expected and sha256_file(dest) != expected.lower()):
            download(url, dest)
        path = dest
    expected = spec.get("sha256")
    if expected:
        actual = sha256_file(path)
        if actual != str(expected).lower():
            raise PatchError(f"{label}: SHA-256 mismatch for {path}\n  expected {expected}\n  actual   {actual}")
    if spec.get("variable"):
        path = instance_variable_font(path, spec["variable"])
    return path.resolve()


def font_cmap(path: Path) -> set[int]:
    from fontTools.ttLib import TTFont

    try:
        with TTFont(str(path), lazy=True) as f:
            return set((f.getBestCmap() or {}).keys())
    except Exception as e:
        raise PatchError(f"{path}: not a readable TTF/OTF font ({e})") from None


def font_name_record(path: Path, name_id: int) -> str | None:
    from fontTools.ttLib import TTFont

    try:
        with TTFont(str(path), lazy=True) as f:
            return f["name"].getDebugName(name_id)
    except Exception:
        return None


def subset_font(src: Path, codepoints: set[int], out: Path) -> Path:
    """Write a copy of `src` that keeps only `codepoints`, so the converter's
    fallback fills in exactly the profile's glyphs and nothing else."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    out.parent.mkdir(parents=True, exist_ok=True)
    options = subset.Options()
    options.notdef_outline = True
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.layout_features = []
    options.hinting = True
    font = TTFont(str(src))
    try:
        sub = subset.Subsetter(options=options)
        sub.populate(unicodes=sorted(codepoints))
        sub.subset(font)
        font.save(str(out))
    finally:
        font.close()
    return out


# --------------------------------------------------------------------------
# Intervals
# --------------------------------------------------------------------------

def merge_ranges(codepoints) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for cp in sorted(set(codepoints)):
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], cp)
        else:
            ranges.append((cp, cp))
    return ranges


def auto_presets(cmap: set[int]) -> list[str]:
    """latin-ext, plus every probe-script preset the font covers."""
    presets = ["latin-ext"]
    for cp, (_script, preset) in FALLBACK_PROBES.items():
        if cp in cmap and preset not in presets:
            presets.append(preset)
    return presets


def build_intervals(base: str | None, cmap: set[int], extra_codepoints) -> str:
    parts = [p.strip() for p in (base.split(",") if base else auto_presets(cmap)) if p.strip()]
    parts += [f"(0x{a:04X}-0x{b:04X})" for a, b in merge_ranges(extra_codepoints)]
    return ",".join(parts)


# --------------------------------------------------------------------------
# .cpfont reading and checks
# --------------------------------------------------------------------------

@dataclass
class CpfontStyle:
    style_id: int
    intervals: list[tuple[int, int]]
    glyph_count: int

    @property
    def name(self) -> str:
        return STYLES[self.style_id] if 0 <= self.style_id < len(STYLES) else str(self.style_id)

    def has(self, cp: int) -> bool:
        return any(s <= cp <= e for s, e in self.intervals)


@dataclass
class Cpfont:
    path: Path
    version: int
    styles: list[CpfontStyle]


def read_cpfont(path: Path) -> Cpfont:
    """Parse the header, style table and interval tables of a v4 .cpfont
    (layout from generate_cpfont_multistyle in fontconvert_sdcard.py)."""
    data = Path(path).read_bytes()
    hsize = struct.calcsize(HEADER_FORMAT)
    tsize = struct.calcsize(TOC_FORMAT)
    isize = struct.calcsize(INTERVAL_FORMAT)
    if len(data) < hsize:
        raise PatchError(f"{path}: too small to be a .cpfont")
    magic, version, _flags, count, _ = struct.unpack_from(HEADER_FORMAT, data, 0)
    if magic != CPFONT_MAGIC:
        raise PatchError(f"{path}: bad magic {magic!r}")
    if version != CPFONT_VERSION:
        raise PatchError(f"{path}: .cpfont version {version}, this tool reads version {CPFONT_VERSION}")
    if len(data) < hsize + count * tsize:
        raise PatchError(f"{path}: truncated style table")
    styles = []
    for i in range(count):
        sid, n_iv, n_glyphs, *_rest, offset = struct.unpack_from(TOC_FORMAT, data, hsize + i * tsize)
        if offset + n_iv * isize > len(data):
            raise PatchError(f"{path}: style {sid} interval table runs past the end of the file")
        ivs = [struct.unpack_from(INTERVAL_FORMAT, data, offset + k * isize)[:2] for k in range(n_iv)]
        styles.append(CpfontStyle(sid, ivs, n_glyphs))
    return Cpfont(Path(path), version, styles)


_CPFONT_NAME = re.compile(r"^(?P<name>.+)_(?P<size>\d+)\.cpfont$")


def find_family_files(directory: Path, name: str | None = None) -> dict[str, dict[int, Path]]:
    families: dict[str, dict[int, Path]] = {}
    for p in sorted(Path(directory).glob("*.cpfont")):
        m = _CPFONT_NAME.match(p.name)
        if not m or (name and m.group("name") != name):
            continue
        families.setdefault(m.group("name"), {})[int(m.group("size"))] = p
    return families


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_family(directory: Path, codepoints, name: str | None = None,
                 sizes=None, styles: int | None = None) -> CheckResult:
    """Check a built family: UI sizes present, v4 files, every codepoint in
    every style at every size, and a probe script in the regular style."""
    res = CheckResult()
    codepoints = list(dict.fromkeys(codepoints))
    families = find_family_files(directory, name)
    if not families:
        res.errors.append(f"no {name + '_' if name else ''}*.cpfont files in {directory}")
        return res
    if len(families) > 1:
        res.errors.append(f"more than one family in {directory} ({', '.join(families)}); pass --name")
        return res
    fam, files = next(iter(families.items()))
    res.lines.append(f"{fam}: sizes {', '.join(str(s) for s in sorted(files))}")
    for s in sorted(set(UI_SIZES) - set(files)):
        res.errors.append(f"{fam}: missing {s} pt (CrossPoint needs 8, 10 and 12 pt for the interface fallback)")
    for s in sorted(set(sizes or ()) - set(files)):
        if s not in UI_SIZES:
            res.errors.append(f"{fam}: missing requested size {s} pt")
    style_counts = set()
    for size, path in sorted(files.items()):
        try:
            cf = read_cpfont(path)
        except PatchError as e:
            res.errors.append(str(e))
            continue
        style_counts.add(len(cf.styles))
        if styles is not None and len(cf.styles) != styles:
            res.errors.append(f"{path.name}: {len(cf.styles)} styles, expected {styles}")
        parts = []
        for st in cf.styles:
            missing = [cp for cp in codepoints if not st.has(cp)]
            if missing:
                res.errors.append(f"{path.name} [{st.name}]: missing {', '.join(fmt_cp(c) for c in missing)}")
            if st.glyph_count == 0:
                res.errors.append(f"{path.name} [{st.name}]: no glyphs")
            parts.append(f"{st.name} {len(codepoints) - len(missing)}/{len(codepoints)}")
        regular = next((st for st in cf.styles if st.style_id == 0), cf.styles[0] if cf.styles else None)
        probe = next((cp for cp in FALLBACK_PROBES if regular and regular.has(cp)), None)
        if probe is None:
            res.errors.append(f"{path.name}: no probe-script character in the regular style, "
                              "so CrossPoint will not use it for the interface fallback")
        probe_txt = f"probe {FALLBACK_PROBES[probe][0]}" if probe is not None else "NO PROBE"
        res.lines.append(f"  {path.name}: v{cf.version}, {len(cf.styles)} styles, {probe_txt}; "
                         f"glyphs present: {', '.join(parts)}")
    if len(style_counts) > 1:
        res.errors.append(f"{fam}: style count differs between sizes ({sorted(style_counts)})")
    return res


def print_check(res: CheckResult) -> None:
    for line in res.lines:
        log(line)
    for err in res.errors:
        log(f"FAIL: {err}")
    log("check: OK" if res.ok else f"check: {len(res.errors)} problem(s)")


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

@dataclass
class LicenseSource:
    role: str
    name: str
    copyright: str | None = None
    license: str | None = None
    license_file: Path | None = None
    font_path: Path | None = None


@dataclass
class BuildPlan:
    name: str
    styles: dict[str, Path]
    sizes: list[int]
    intervals: str | None
    profiles: list[Profile]
    out_dir: Path
    licenses: list[LicenseSource] = field(default_factory=list)
    base_dir: Path = REPO_ROOT


def normalize_sizes(sizes) -> list[int]:
    try:
        sizes = sorted({int(s) for s in sizes})
    except (TypeError, ValueError):
        raise PatchError(f"sizes must be integers: {sizes!r}") from None
    added = [s for s in UI_SIZES if s not in sizes]
    if added:
        log(f"Adding interface size(s) {', '.join(map(str, added))} pt "
            "(CrossPoint needs 8, 10 and 12 pt for the interface fallback)")
    return sorted(set(sizes) | set(UI_SIZES))


def parse_sizes(text: str | None):
    if text is None:
        return None
    return [int(s) for s in text.split(",") if s.strip()]


def plan_coverage(plan: BuildPlan) -> tuple[list[int], dict[str, set[int]], Profile | None]:
    """Return (all profile codepoints, per-style codepoints needing the
    fallback, the one profile whose fallback is used)."""
    all_cps: list[int] = []
    for prof in plan.profiles:
        all_cps += [c for c in prof.codepoints if c not in all_cps]

    cmaps = {style: font_cmap(path) for style, path in plan.styles.items()}

    regular_cmap = cmaps[min(plan.styles, key=lambda s: STYLE_IDS[s])]
    probes = [cp for cp in FALLBACK_PROBES if cp in regular_cmap]
    if not probes:
        names = ", ".join(f"{s} ({fmt_cp(c)})" for c, (s, _) in FALLBACK_PROBES.items())
        raise PatchError(
            "the main font has none of CrossPoint's probe-script characters, so CrossPoint would "
            "never use it for the interface fallback and the extra glyphs would stay blank in menus.\n"
            f"  It needs at least one of: {names}.\n"
            "  Pick a font that also covers Greek, Cyrillic or IPA (most book fonts with broad "
            "Latin coverage do).")
    log("Probe scripts in main font: " + ", ".join(FALLBACK_PROBES[c][0] for c in probes))

    needs: dict[str, set[int]] = {}
    used_profiles: list[Profile] = []
    for style, cmap in cmaps.items():
        missing = {c for c in all_cps if c not in cmap}
        if missing:
            needs[style] = missing
        have = [c for c in all_cps if c in cmap]
        log(f"  [{style}] from main font: {len(have)}, from fallback: {len(missing)}")
        for prof in plan.profiles:
            if missing & set(prof.codepoints) and prof not in used_profiles:
                used_profiles.append(prof)

    fallback_profiles = [p for p in used_profiles if p.fallback]
    no_fallback = [p for p in used_profiles if not p.fallback]
    if no_fallback:
        p = no_fallback[0]
        missing = sorted(set().union(*needs.values()) & set(p.codepoints))
        raise PatchError(f"profile '{p.name}' has no fallback font, and the main font lacks "
                         f"{', '.join(fmt_cp(c) for c in missing)}")
    keys = {(p.fallback.get("path"), p.fallback.get("url")) for p in fallback_profiles}
    if len(keys) > 1:
        raise PatchError("these profiles need different fallback fonts ("
                         + ", ".join(p.name for p in fallback_profiles)
                         + "); CrossPoint's converter takes one fallback font per style. "
                           "Merge them into one profile with one fallback font.")
    return all_cps, needs, (fallback_profiles[0] if fallback_profiles else None)


def run_build(plan: BuildPlan, ref: str = CROSSPOINT_REF, dry_run: bool = False,
              verbose: bool = False, run_check: bool = True) -> int:
    for style in plan.styles:
        if style not in STYLE_IDS:
            raise PatchError(f"unknown style '{style}' (use {', '.join(STYLES)})")
    if "regular" not in plan.styles:
        raise PatchError("a regular style is required")
    sizes = normalize_sizes(plan.sizes)

    all_cps, needs, fb_profile = plan_coverage(plan)

    fallbacks: dict[str, Path] = {}
    if fb_profile is not None and needs:
        fb_src = resolve_font(fb_profile.fallback, REPO_ROOT, f"profile {fb_profile.name} fallback",
                              cache_subdir=fb_profile.name)
        fb_cmap = font_cmap(fb_src)
        for style, missing in needs.items():
            lacking = sorted(c for c in missing if c not in fb_cmap)
            if lacking:
                raise PatchError(f"[{style}] neither the main font nor the fallback "
                                 f"({fb_src.name}) has {', '.join(fmt_cp(c) for c in lacking)}")
            out = WORK_DIR / plan.name / f"fallback-{style}.ttf"
            if not dry_run:
                subset_font(fb_src, missing, out)
            fallbacks[style] = out
        plan.licenses.append(LicenseSource(
            role=f"fallback glyphs ({fb_profile.name}: "
                 + ", ".join(fmt_cp(c) for c in sorted(set().union(*needs.values()))) + ")",
            name=fb_profile.fallback.get("name") or fb_src.stem,
            copyright=fb_profile.fallback.get("copyright"),
            license=fb_profile.fallback.get("license"),
            license_file=(REPO_ROOT / fb_profile.fallback["license_file"])
            if fb_profile.fallback.get("license_file") else None,
            font_path=fb_src,
        ))

    regular_cmap = font_cmap(plan.styles["regular"])
    intervals = build_intervals(plan.intervals, regular_cmap, all_cps)

    checkout = CHECKOUT_DIR if dry_run else ensure_checkout(ref)
    converter = checkout / CONVERTER_REL
    cmd = [sys.executable, str(converter)]
    for style in STYLES:
        if style in plan.styles:
            cmd += [f"--{style}", str(plan.styles[style])]
    for style in STYLES:
        if style in fallbacks:
            cmd += [f"--fallback-{style}", str(fallbacks[style])]
    cmd += ["--intervals", intervals, "--sizes", ",".join(map(str, sizes)),
            "--name", plan.name, "--output-dir", str(plan.out_dir)]

    log(f"Intervals: {intervals}")
    if dry_run:
        log("Dry run; converter command:")
        print(" ".join(_shell_quote(c) for c in cmd))
        return 0

    plan.out_dir.mkdir(parents=True, exist_ok=True)
    for old in plan.out_dir.glob(f"{plan.name}_*.cpfont"):
        old.unlink()
    log(f"Converting {plan.name} at {', '.join(map(str, sizes))} pt ...")
    run_converter(cmd, verbose=verbose)

    write_license_file(plan)

    rc = 0
    if run_check:
        log("")
        res = check_family(plan.out_dir, all_cps, name=plan.name, sizes=sizes, styles=len(plan.styles))
        print_check(res)
        rc = 0 if res.ok else 1

    log("")
    log(f"Built {plan.out_dir}/")
    log(f"Copy every {plan.name}_*.cpfont file (including 8, 10 and 12 pt) to /.fonts/{plan.name}/ "
        f"on the SD card, then pick {plan.name} under Settings > Reader > Font Family.")
    return rc


def _shell_quote(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z0-9_./,=:+-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def run_converter(cmd: list[str], verbose: bool = False) -> None:
    # The converter imports a sibling module (cpfont_version), so it is run
    # by path as a normal script, never with `python -I`.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    hidden = 0
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line])[-40:]
        if not verbose and ("ligature" in line or ".liga" in line):
            hidden += 1
            continue
        if verbose or not line.startswith("  [") or "Validated" in line:
            sys.stderr.write(line)
    rc = proc.wait()
    if hidden:
        log(f"({hidden} ligature warning line(s) hidden; these are normal. Use --verbose to see them.)")
    if rc != 0:
        if not verbose:
            sys.stderr.write("".join(tail))
        raise PatchError(f"converter failed (exit {rc})")


def write_license_file(plan: BuildPlan) -> Path:
    out = plan.out_dir / "LICENSE-fonts.txt"
    chunks = [
        f"{plan.name} is a modified version of the fonts listed below, converted to CrossPoint's\n"
        ".cpfont format with extra glyphs. Each font's license applies to its glyphs.\n"
    ]
    for src in plan.licenses:
        copyright_ = src.copyright or (font_name_record(src.font_path, 0) if src.font_path else None)
        lic = src.license or (font_name_record(src.font_path, 13) if src.font_path else None)
        head = [f"== {src.name} ({src.role}) ==", ""]
        text = ""
        if src.license_file and src.license_file.exists():
            text = src.license_file.read_text(encoding="utf-8").strip()
        if copyright_ and copyright_ not in text:
            head.append(copyright_)
        if lic:
            head.append(f"License: {lic}")
        elif src.font_path:
            url = font_name_record(src.font_path, 14)
            if url:
                head.append(f"License URL: {url}")
            head.append("(No license file was given; check the font's license before sharing these files.)")
        chunks.append("\n".join(head) + ("\n\n" + text if text else "") + "\n")
    out.write_text("\n\n".join(chunks), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

def plan_from_recipe(recipe_path: Path, args) -> BuildPlan:
    recipe_path = recipe_path.resolve()
    data = load_yaml(recipe_path)
    name = args.name or data.get("name")
    if not name:
        raise PatchError(f"{recipe_path}: 'name' is required")
    styles_spec = data.get("styles") or {}
    if not isinstance(styles_spec, dict) or not styles_spec:
        raise PatchError(f"{recipe_path}: 'styles' must map style names to font sources")
    styles = {s: resolve_font(spec, REPO_ROOT, f"{recipe_path.name} {s}", cache_subdir=name)
              for s, spec in styles_spec.items()}
    profile_names = args.profile or data.get("profiles") or [DEFAULT_PROFILE]
    src = data.get("source") or {}
    lic = LicenseSource(
        role="main font",
        name=src.get("name", name),
        copyright=src.get("copyright"),
        license=src.get("license"),
        license_file=(REPO_ROOT / src["license_file"]) if src.get("license_file") else None,
        font_path=styles.get("regular"),
    )
    return BuildPlan(
        name=name,
        styles=styles,
        sizes=parse_sizes(args.sizes) or data.get("sizes") or list(DEFAULT_SIZES),
        intervals=args.intervals or data.get("intervals"),
        profiles=[load_profile(p) for p in profile_names],
        out_dir=Path(args.out) if args.out else DIST_DIR / name,
        licenses=[lic],
    )


def cmd_build(args) -> int:
    plan = plan_from_recipe(Path(args.recipe), args)
    return run_build(plan, ref=args.crosspoint_ref, dry_run=args.dry_run, verbose=args.verbose,
                     run_check=not args.no_check)


def load_stock_family(family: str, checkout: Path) -> dict:
    data = load_yaml(checkout / SD_FONTS_YAML_REL)
    fams = data.get("families") or []
    for f in fams:
        if str(f.get("name", "")).lower() == family.lower():
            return f
    names = ", ".join(str(f.get("name")) for f in fams)
    raise PatchError(f"'{family}' is not in sd-fonts.yaml (available: {names})")


def cmd_stock(args) -> int:
    checkout = ensure_checkout(args.crosspoint_ref)
    fam = load_stock_family(args.family, checkout)
    base = checkout / "lib" / "EpdFont"
    name = args.name or f"{fam['name']}Plus"
    styles = {s: resolve_font(spec, base, f"{fam['name']} {s}", cache_subdir=fam["name"])
              for s, spec in fam["styles"].items()}
    plan = BuildPlan(
        name=name,
        styles=styles,
        sizes=parse_sizes(args.sizes) or list(fam.get("sizes") or DEFAULT_SIZES),
        intervals=args.intervals or fam.get("intervals"),
        profiles=[load_profile(p) for p in (args.profile or [DEFAULT_PROFILE])],
        out_dir=Path(args.out) if args.out else DIST_DIR / name,
        licenses=[LicenseSource(role="main font", name=fam["name"], font_path=styles.get("regular"))],
    )
    return run_build(plan, ref=args.crosspoint_ref, dry_run=args.dry_run, verbose=args.verbose,
                     run_check=not args.no_check)


def cmd_patch(args) -> int:
    styles = {}
    for s in STYLES:
        v = getattr(args, s)
        if v:
            styles[s] = resolve_font(v, Path.cwd(), f"--{s}")
    plan = BuildPlan(
        name=args.name,
        styles=styles,
        sizes=parse_sizes(args.sizes) or list(DEFAULT_SIZES),
        intervals=args.intervals,
        profiles=[load_profile(p) for p in (args.profile or [DEFAULT_PROFILE])],
        out_dir=Path(args.out) if args.out else DIST_DIR / args.name,
        licenses=[LicenseSource(role="main font", name=args.name, font_path=styles.get("regular"))],
    )
    return run_build(plan, ref=args.crosspoint_ref, dry_run=args.dry_run, verbose=args.verbose,
                     run_check=not args.no_check)


def cmd_check(args) -> int:
    cps: list[int] = []
    for p in (args.profile or [DEFAULT_PROFILE]):
        cps += load_profile(p).codepoints
    cps += [parse_codepoint(c) for c in (args.codepoint or [])]
    res = check_family(Path(args.dir), cps, name=args.name, sizes=parse_sizes(args.sizes))
    print_check(res)
    return 0 if res.ok else 1


def cmd_setup(args) -> int:
    setup_checkout(args.crosspoint_ref)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cpfont_patch.py",
        description="Build CrossPoint .cpfont font families with extra glyphs "
                    "(e.g. Kavita's OPDS reading-progress icons).")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, build=True):
        p.add_argument("--crosspoint-ref", default=CROSSPOINT_REF,
                       help="crosspoint-reader commit/branch/tag whose converter to use "
                            f"(default: pinned {CROSSPOINT_REF[:12]})")
        if not build:
            return
        p.add_argument("--profile", action="append",
                       help=f"glyph profile name (profiles/<name>.yaml) or YAML path; repeatable "
                            f"(default: {DEFAULT_PROFILE}, or the recipe's list)")
        p.add_argument("--sizes", help="comma-separated point sizes (8, 10 and 12 are always added)")
        p.add_argument("--intervals",
                       help="converter interval presets, e.g. latin-ext,greek (default: from the "
                            "recipe, or worked out from the font's coverage); profile codepoints "
                            "are always added")
        p.add_argument("--out", help="output directory (default: dist/<Name>/)")
        p.add_argument("--dry-run", action="store_true",
                       help="analyse coverage and print the converter command without running it")
        p.add_argument("--no-check", action="store_true", help="skip the post-build check")
        p.add_argument("-v", "--verbose", action="store_true", help="show all converter output")

    p = sub.add_parser("build", help="build a family from a recipe file")
    p.add_argument("recipe", help="recipe YAML, e.g. families/literata.yaml")
    p.add_argument("--name", help="override the output family name")
    common(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("stock", help="patch a family from CrossPoint's sd-fonts.yaml")
    p.add_argument("family", help="family name as listed in sd-fonts.yaml, e.g. Literata")
    p.add_argument("--name", help="output family name (default: <Family>Plus)")
    common(p)
    p.set_defaults(func=cmd_stock)

    p = sub.add_parser("patch", help="patch local TTF/OTF files")
    for s in STYLES:
        p.add_argument(f"--{s}", required=(s == "regular"), metavar="FONT",
                       help=f"{s} font file" + (" (required)" if s == "regular" else ""))
    p.add_argument("--name", required=True,
                   help="output family name; don't reuse the original font's name")
    common(p)
    p.set_defaults(func=cmd_patch)

    p = sub.add_parser("check", help="check an existing .cpfont family")
    p.add_argument("dir", help="directory containing <Name>_<size>.cpfont files")
    p.add_argument("--name", help="family name, if the directory holds several")
    p.add_argument("--profile", action="append",
                   help=f"profile whose codepoints must be present (default: {DEFAULT_PROFILE})")
    p.add_argument("--codepoint", action="append", help="extra codepoint to require, e.g. U+2B58")
    p.add_argument("--sizes", help="sizes that must be present besides 8, 10, 12")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("setup", help="clone/update the pinned crosspoint-reader checkout in vendor/")
    common(p, build=False)
    p.set_defaults(func=cmd_setup)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PatchError as e:
        log(f"error: {e}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
