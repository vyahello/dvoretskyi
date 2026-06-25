#!/usr/bin/env python3
"""Build a custom espeak-ng data dir for Piper with Ukrainian stress overrides.

WHY: espeak's `uk` rules mis-stress some words (подано→«подА́но», баланс→«бА́ланс»), and
nothing in the *text* fed to Piper can fix it — U+0301 accents are ignored and
[[phoneme]] blocks are read literally (verified on the box). The only override that
reaches Piper's phonemizer is an espeak pronunciation dictionary. This script compiles
`scripts/uk_stress_overrides.txt` into a copy of Piper's bundled espeak data, leaving the
bundle untouched, and prints the dir to set as PIPER_ESPEAK_DATA.

RUN IT ON THE VPS (needs the system `espeak-ng` 1.51 binary + outbound internet):
    python3 scripts/build_espeak_stress.py
At the default path (~/.dvoretskyi/espeak-ng-data) the bot auto-detects the result on the
next voice reply — NO .env edit, no restart. Set PIPER_ESPEAK_DATA only for a non-default
path. Re-run after editing the overrides file (it recompiles uk_dict in place); idempotent
— the data dir is (re)seeded from the Piper bundle and uk_dict recompiled.

Risk: writes only to the out dir (default ~/.dvoretskyi/espeak-ng-data); never touches
Piper's bundle or the system espeak data. If anything is off, delete that dir (and clear
PIPER_ESPEAK_DATA if set) → Piper falls straight back to its bundled data.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# espeak-ng dictsource ref to compile from. Piper bundles espeak data built from `master`
# (a 1.52-dev snapshot), NOT a release tag — verified on the box: compiling the master uk
# dictsource against Piper's bundle reproduces its phonemes byte-for-byte, whereas 1.51
# source mismatches the bundle's phoneme table (Bad phoneme errors). The system espeak-ng
# 1.51 binary compiles the master source cleanly. uk has only uk_rules + uk_list.
ESPEAK_TAG = "master"
DICT_FILES = ["uk_rules", "uk_list", "uk_listx", "uk_emoji"]  # missing ones are skipped

APP_DIR = Path(__file__).resolve().parent.parent
OVERRIDES = APP_DIR / "scripts" / "uk_stress_overrides.txt"


def read_env(key: str, default: str = "") -> str:
    """Read one key from the app .env (no extra deps; secrets stay out of argv)."""
    env_path = APP_DIR / ".env"
    if env_path.exists():
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return os.environ.get(key, default)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    piper_bin = read_env("PIPER_BIN", "piper")
    piper_voice = read_env("PIPER_VOICE")

    # Piper loads espeak data from <piper-dir>/espeak-ng-data by default — that's the
    # bundle we copy from (it matches the phoneme tables this Piper build expects).
    bundle = Path(piper_bin).resolve().parent / "espeak-ng-data"
    if not (bundle / "phontab").exists():
        # Fall back to the system data dir if Piper has no bundle beside it.
        for cand in (
            Path("/usr/lib/x86_64-linux-gnu/espeak-ng-data"),
            Path("/usr/share/espeak-ng-data"),
        ):
            if (cand / "phontab").exists():
                bundle = cand
                break
    if not (bundle / "phontab").exists():
        die(f"could not find a Piper/espeak data bundle (looked near {piper_bin})")

    out_parent = Path(
        read_env("PIPER_ESPEAK_PARENT", str(Path.home() / ".dvoretskyi"))
    ).expanduser()
    out_data = out_parent / "espeak-ng-data"

    if not OVERRIDES.exists():
        die(f"overrides file not found: {OVERRIDES}")

    print(f"==> bundle (source)   : {bundle}")
    print(f"==> custom data dir   : {out_data}")

    # 1) (Re)seed the custom data dir from Piper's bundle, untouched.
    out_data.mkdir(parents=True, exist_ok=True)
    for item in bundle.iterdir():
        dst = out_data / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)

    # 2) Fetch the uk dictsource into a temp build dir, append our overrides to uk_list.
    src = Path(tempfile.mkdtemp(prefix="ukstress-"))
    base = (
        f"https://raw.githubusercontent.com/espeak-ng/espeak-ng/{ESPEAK_TAG}/dictsource/"
    )
    got = []
    for name in DICT_FILES:
        try:
            with urllib.request.urlopen(base + name, timeout=30) as r:
                (src / name).write_bytes(r.read())
            got.append(name)
        except Exception as exc:  # noqa: BLE001 - report and continue; some files optional
            print(f"    (skip {name}: {exc})")
    if "uk_rules" not in got or "uk_list" not in got:
        die("could not fetch uk_rules/uk_list — check the VPS has outbound internet")
    print(f"==> fetched dictsource : {', '.join(got)}")

    # Append only real entries — our file documents itself with `#` comments, but espeak's
    # uk_list comment marker is `//`, so a `#` line would be parsed as a (bad) entry.
    entries = [
        ln
        for ln in OVERRIDES.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    with open(src / "uk_list", "a", encoding="utf-8") as fh:
        fh.write("\n// --- dvoretskyi stress overrides ---\n")
        fh.write("\n".join(entries) + "\n")
    print(f"==> appended overrides : {len(entries)} word(s)")

    # 3) Compile uk_dict into the data dir (--path = the dir CONTAINING espeak-ng-data).
    res = subprocess.run(
        ["espeak-ng", "--compile=uk", "--path", str(out_parent)],
        cwd=str(src),
        capture_output=True,
        text=True,
    )
    tail = (res.stdout + res.stderr).strip()
    if res.returncode != 0:
        die(f"espeak-ng --compile failed (rc {res.returncode}):\n{tail}")
    if tail:
        print(f"    espeak: {tail[-400:]}")
    if not (out_data / "uk_dict").exists():
        die("compile reported success but uk_dict is missing")
    print("==> compiled uk_dict ✓")

    # 4) Validate through Piper's own phonemizer (the only oracle that matched reality).
    if piper_voice and Path(piper_voice).exists():
        print(
            "\n==> validation (Piper phonemes: bundled vs override) —"
            " the stress mark ˈ should move:"
        )
        _validate(piper_bin, piper_voice, out_data)
    else:
        print("\n(PIPER_VOICE not set/found — skipped phoneme validation)")

    default = Path.home() / ".dvoretskyi" / "espeak-ng-data"
    if out_data == default:
        print(
            f"\nDONE. {out_data} is the auto-detected default — the bot uses it on the"
            " next voice reply. No .env edit needed."
        )
    else:
        print(
            f"\nDONE. Non-default path — set in the VPS .env and restart:"
            f"\n    PIPER_ESPEAK_DATA={out_data}"
        )


def _validate(piper_bin: str, voice: str, out_data: Path) -> None:
    def phon(text: str, custom: bool) -> str:
        d = tempfile.mkdtemp()
        args = [piper_bin, "--model", voice, "--output_file", d + "/o.wav", "--debug"]
        if custom:
            args += ["--espeak_data", str(out_data)]
        p = subprocess.run(args, input=text.encode("utf-8"), capture_output=True)
        m = re.search(
            r"Converting \d+ phoneme\(s\) to ids:\s*(.+)",
            p.stderr.decode("utf-8", "replace"),
        )
        return m.group(1).strip() if m else "(no phonemes)"

    samples = ["подано", "баланс", "чіпав", "за червень уже все подано", "баланс не чіпав"]
    for t in samples:
        b, o = phon(t, False), phon(t, True)
        tag = " <- stress moved" if b != o else ""
        print(f"    {t!r}")
        print(f"        bundled : {b}")
        print(f"        override: {o}{tag}")
    # Regression guard: a phrase with NO override word must phonemize identically — if it
    # differs, the dictsource drifted from the bundle and the whole voice would change.
    control = "вода газ світло інтернет"
    cb, co = phon(control, False), phon(control, True)
    print(
        f"    regression control {control!r}: "
        f"{'OK (identical)' if cb == co else 'WARNING — DIFFERS, dictsource drift!'}"
    )


if __name__ == "__main__":
    main()
