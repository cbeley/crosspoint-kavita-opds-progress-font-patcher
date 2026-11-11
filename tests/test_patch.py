"""Tests for cpfont_patch.py.

Unit tests run anywhere. Tests marked `integration` run CrossPoint's converter
from the pinned checkout in vendor/crosspoint-reader (run
`python3 cpfont_patch.py setup` first) and are skipped if it's missing.
`test_stock_family` also downloads SourceSerif4. Skip them with
`pytest -m "not integration"`.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cpfont_patch as cp  # noqa: E402

ICONS = [0x2B58, 0x25D4, 0x25D1, 0x25D5, 0x2B24]
LITERATA = {s: ROOT / "fonts" / "Literata" / f"Literata-{n}.ttf"
            for s, n in [("regular", "Regular"), ("bold", "Bold"),
                         ("italic", "Italic"), ("bolditalic", "BoldItalic")]}
HAVE_CONVERTER = (cp.CHECKOUT_DIR / cp.CONVERTER_REL).exists()
integration = pytest.mark.integration
needs_converter = pytest.mark.skipif(
    not HAVE_CONVERTER, reason="run `python3 cpfont_patch.py setup` first")


# ---------------------------------------------------------------- helpers

def write_fake_cpfont(path: Path, styles: dict[int, list[tuple[int, int]]],
                      magic=cp.CPFONT_MAGIC, version=cp.CPFONT_VERSION) -> Path:
    """Write a minimal .cpfont with only header, style table and interval
    tables (all the checker reads)."""
    header = struct.pack(cp.HEADER_FORMAT, magic, version, 1, len(styles), bytes(19))
    offset = 32 + 32 * len(styles)
    toc, body = b"", b""
    for sid, ivs in styles.items():
        glyphs = sum(e - s + 1 for s, e in ivs)
        toc += struct.pack(cp.TOC_FORMAT, sid, len(ivs), glyphs, 20, 15, -5, 0, 0, 0, 0, 0,
                           offset + len(body))
        g = 0
        for s, e in ivs:
            body += struct.pack(cp.INTERVAL_FORMAT, s, e, g)
            g += e - s + 1
    path.write_bytes(header + toc + body)
    return path


def full_ranges():
    return [(0x20, 0x7E), (0x03B1, 0x03B1)] + cp.merge_ranges(ICONS)


# ---------------------------------------------------------------- unit tests

def test_merge_ranges():
    assert cp.merge_ranges(ICONS) == [(0x25D1, 0x25D1), (0x25D4, 0x25D5),
                                      (0x2B24, 0x2B24), (0x2B58, 0x2B58)]
    assert cp.merge_ranges([3, 1, 2, 2]) == [(1, 3)]


def test_build_intervals_matches_tested_command():
    cmap = cp.font_cmap(LITERATA["regular"])
    expected = ("latin-ext,greek,cyrillic,ipa-chars,(0x25D1-0x25D1),"
                "(0x25D4-0x25D5),(0x2B24-0x2B24),(0x2B58-0x2B58)")
    assert cp.build_intervals(None, cmap, ICONS) == expected
    assert cp.build_intervals("latin-ext,greek,cyrillic,ipa-chars", cmap, ICONS) == expected


def test_profile_loads():
    prof = cp.load_profile("kavita-progress")
    assert sorted(prof.codepoints) == sorted(ICONS)
    fb = cp.resolve_font(prof.fallback, ROOT, "fallback")
    assert set(ICONS) <= cp.font_cmap(fb)


def test_parse_codepoint():
    assert cp.parse_codepoint("U+2B58") == 0x2B58
    assert cp.parse_codepoint("0x25D4") == 0x25D4
    assert cp.parse_codepoint("◑") == 0x25D1
    assert cp.parse_codepoint(0x2B24) == 0x2B24


def test_parse_probe_list_matches_tool():
    src = """
  static constexpr uint32_t kFallbackProbes[] = {
      0x4E00,  // CJK
      0x03B1,  // Greek
  };"""
    assert cp.parse_probe_list(src) == [0x4E00, 0x03B1]
    assert cp.parse_probe_list("nothing here") is None


@needs_converter
def test_probe_list_in_sync_with_checkout():
    assert cp.check_probe_list(cp.CHECKOUT_DIR)


def test_literata_sha256_matches_recipe():
    data = cp.load_yaml(ROOT / "families" / "literata.yaml")
    for style, spec in data["styles"].items():
        assert cp.sha256_file(ROOT / spec["path"]) == spec["sha256"], style


def test_read_and_check_good_family(tmp_path):
    for size in (8, 10, 12, 14):
        write_fake_cpfont(tmp_path / f"Fake_{size}.cpfont", {i: full_ranges() for i in range(4)})
    res = cp.check_family(tmp_path, ICONS, sizes=[8, 10, 12, 14], styles=4)
    assert res.ok, res.errors


def test_check_detects_missing_icon(tmp_path):
    for size in (8, 10, 12):
        ivs = full_ranges() if size != 10 else [(0x20, 0x7E), (0x03B1, 0x03B1)]
        write_fake_cpfont(tmp_path / f"Fake_{size}.cpfont", {0: ivs, 1: full_ranges()})
    res = cp.check_family(tmp_path, ICONS)
    assert not res.ok
    assert any("Fake_10.cpfont [regular]: missing" in e for e in res.errors)


def test_check_requires_ui_sizes(tmp_path):
    for size in (12, 14):
        write_fake_cpfont(tmp_path / f"Fake_{size}.cpfont", {0: full_ranges()})
    res = cp.check_family(tmp_path, ICONS)
    assert any("missing 8 pt" in e for e in res.errors)
    assert any("missing 10 pt" in e for e in res.errors)


def test_check_requires_probe(tmp_path):
    for size in (8, 10, 12):
        write_fake_cpfont(tmp_path / f"Fake_{size}.cpfont",
                          {0: [(0x20, 0x7E)] + cp.merge_ranges(ICONS)})
    res = cp.check_family(tmp_path, ICONS)
    assert any("no probe-script" in e for e in res.errors)


def test_check_rejects_bad_magic_and_version(tmp_path):
    write_fake_cpfont(tmp_path / "A_8.cpfont", {0: full_ranges()}, magic=b"NOTAFONT")
    write_fake_cpfont(tmp_path / "A_10.cpfont", {0: full_ranges()}, version=3)
    write_fake_cpfont(tmp_path / "A_12.cpfont", {0: full_ranges()})
    res = cp.check_family(tmp_path, ICONS)
    assert any("bad magic" in e for e in res.errors)
    assert any("version 3" in e for e in res.errors)


def test_check_style_count(tmp_path):
    for size in (8, 10, 12):
        write_fake_cpfont(tmp_path / f"Fake_{size}.cpfont", {0: full_ranges()})
    assert not cp.check_family(tmp_path, ICONS, styles=4).ok


def latin_only_font(tmp_path) -> Path:
    out = tmp_path / "LatinOnly-Regular.ttf"
    return cp.subset_font(LITERATA["regular"], set(range(0x20, 0x7F)), out)


def test_probe_gate_rejects_latin_only_font(tmp_path, capsys):
    font = latin_only_font(tmp_path)
    assert not (cp.font_cmap(font) & set(cp.FALLBACK_PROBES))
    rc = cp.main(["patch", "--regular", str(font), "--name", "LatinOnlyPlus",
                  "--out", str(tmp_path / "out"), "--dry-run"])
    assert rc == 2
    assert "probe-script" in capsys.readouterr().err


def test_fallback_subset_has_only_profile_glyphs(tmp_path):
    prof = cp.load_profile("kavita-progress")
    src = cp.resolve_font(prof.fallback, ROOT, "fallback")
    out = cp.subset_font(src, set(ICONS), tmp_path / "fb.ttf")
    assert cp.font_cmap(out) == set(ICONS)


def test_dry_run_command(tmp_path, capsys):
    rc = cp.main(["build", str(ROOT / "families" / "literata.yaml"),
                  "--out", str(tmp_path / "out"), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    for flag in ("--regular", "--bold", "--italic", "--bolditalic",
                 "--fallback-regular", "--fallback-bold", "--fallback-italic",
                 "--fallback-bolditalic", "--name LiterataPlus", "--sizes 8,10,12,14,16,18"):
        assert flag in out
    assert "(0x2B58-0x2B58)" in out


def test_sizes_always_include_ui_sizes():
    assert cp.normalize_sizes([14, 16]) == [8, 10, 12, 14, 16]


# ---------------------------------------------------------------- integration

@integration
@needs_converter
def test_build_literata_plus(tmp_path):
    out = tmp_path / "LiterataPlus"
    rc = cp.main(["build", str(ROOT / "families" / "literata.yaml"),
                  "--out", str(out), "--sizes", "8,10,12"])
    assert rc == 0
    res = cp.check_family(out, ICONS, name="LiterataPlus", styles=4)
    assert res.ok, res.errors
    lic = (out / "LICENSE-fonts.txt").read_text(encoding="utf-8")
    assert "The Literata Project Authors" in lic
    assert "The Noto Project Authors" in lic
    assert "SIL OPEN FONT LICENSE Version 1.1" in lic


@integration
@needs_converter
def test_without_fallback_has_no_icons(tmp_path):
    """Negative control: the same conversion without the fallback has no
    icons, so the check above is not vacuous."""
    converter = cp.CHECKOUT_DIR / cp.CONVERTER_REL
    cmap = cp.font_cmap(LITERATA["regular"])
    subprocess.run([sys.executable, str(converter), "--regular", str(LITERATA["regular"]),
                    "--intervals", cp.build_intervals(None, cmap, ICONS),
                    "--sizes", "8,10,12", "--name", "Control", "--output-dir", str(tmp_path)],
                   check=True, capture_output=True)
    res = cp.check_family(tmp_path, ICONS, name="Control")
    assert not res.ok
    for size in (8, 10, 12):
        style = cp.read_cpfont(tmp_path / f"Control_{size}.cpfont").styles[0]
        assert not any(style.has(c) for c in ICONS)


@integration
@needs_converter
def test_stock_family(tmp_path):
    """Patch another family straight from CrossPoint's sd-fonts.yaml."""
    out = tmp_path / "SourceSerif4Plus"
    rc = cp.main(["stock", "SourceSerif4", "--out", str(out), "--sizes", "8,10,12"])
    assert rc == 0
    assert cp.check_family(out, ICONS, name="SourceSerif4Plus", styles=4).ok
