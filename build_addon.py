"""build_addon.py [--lzo PATH]: build dist/acb_map_editor.zip, a self-contained Blender add-on.

The zip holds the add-on plus copies of acbmap and anvilforge under acb_map_editor/_vendor, so it installs on any
machine via Preferences > Add-ons > Install from Disk. The only outside requirement is liblzo2 (the system library
on Linux, e.g. `pacman -S lzo` / `apt install liblzo2-2`); --lzo bundles a library file (lzo2.dll on Windows)
next to anvilforge, where it is found automatically.
"""
import argparse
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = [
    (os.path.join(HERE, "blender", "acb_map_editor"), "acb_map_editor"),
    (os.path.join(HERE, "acbmap"), "acb_map_editor/_vendor/acbmap"),
    (os.path.join(HERE, "vendor", "anvilforge-py", "src", "anvilforge"), "acb_map_editor/_vendor/anvilforge"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lzo", help="liblzo2 library file to bundle (e.g. lzo2.dll)")
    ap.add_argument("--out", default=os.path.join(HERE, "dist", "acb_map_editor.zip"))
    a = ap.parse_args()
    for src, _ in SOURCES:
        if not os.path.isdir(src):
            raise SystemExit(f"missing {src} (run: git submodule update --init)")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    n = 0
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, dst in SOURCES:
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d != "__pycache__" and d != "_vendor"]
                for f in files:
                    if f.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(root, f)
                    z.write(full, os.path.join(dst, os.path.relpath(full, src)))
                    n += 1
        if a.lzo:
            z.write(a.lzo, f"acb_map_editor/_vendor/anvilforge/{os.path.basename(a.lzo)}")
            n += 1
    print(f"wrote {a.out} ({n} files, {os.path.getsize(a.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
