#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lionheart FRM16 -> PNG  ·  Batch Converter
===========================================
Recursively walks a folder, converts every .frm16 file to a .png with
the SAME name, saved in the SAME folder as the original.

Handles BOTH FRM16 variants automatically (detected by header flags):
  * RAW textures        (magic 32 10, flags 0x0040): terrain/UI tiles,
                         raw RGB565 pixels (e.g. the Textures/ folder).
  * Compressed models   (magic 01 32 10, flags 0x0044): isometric object
                         renders with 3-case RLE + circular row shift
                         (e.g. the Models/Environments/ folder).

USAGE
-----
    python batch_convert_frm16.py  "C:\\path\\to\\Lionheart\\Data"

    # options:
    python batch_convert_frm16.py  <folder>  [--black-transp] [--overwrite]
                                             [--flip] [--dry-run]

    --black-transp   treat black (0x0000) as transparent (models only)
    --overwrite      re-create PNGs that already exist (default: skip)
    --flip           flip images vertically
    --dry-run        list what would be converted, write nothing

Requires: Pillow   ( pip install pillow )
"""

import os
import sys
import struct

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow is required.  Install it with:  pip install pillow")
    sys.exit(1)


# ----------------------------------------------------------------------
# Pixel helpers
# ----------------------------------------------------------------------
def rgb565(v):
    return (((v >> 11) & 31) * 255 // 31,
            ((v >> 5)  & 63) * 255 // 63,
            ( v        & 31) * 255 // 31)


def rgba565(v, black_transparent=False):
    if v is None or v == 0xFFFF:
        return (0, 0, 0, 0)
    if black_transparent and v == 0x0000:
        return (0, 0, 0, 0)
    r, g, b = rgb565(v)
    return (r, g, b, 255)


# ----------------------------------------------------------------------
# RAW texture decoder  (magic 32 10, flags 0x0040)
# ----------------------------------------------------------------------
def decode_raw(d):
    w = d[6] | (d[7] << 8)
    h = d[8] | (d[9] << 8)
    img = Image.new('RGB', (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            p = 32 + (y * w + x) * 2
            if p + 1 < len(d):
                px[x, y] = rgb565(d[p] | (d[p + 1] << 8))
    return img


# ----------------------------------------------------------------------
# Compressed model decoder  (magic 01 32 10, flags 0x0044)
# ----------------------------------------------------------------------
def decode_rle_wrap(d, start, end, width):
    rows = []
    row = [None] * width
    x = 0
    pos = start

    def flush():
        nonlocal row, x
        rows.append(row)
        row = [None] * width
        x = 0

    while pos < end and pos < len(d):
        ctrl = d[pos]; pos += 1
        if ctrl & 0x80:                       # SKIP (transparent)
            for _ in range(ctrl & 0x7F):
                x += 1
                if x >= width:
                    flush()
        elif ctrl & 0x40:                     # LITERAL
            for _ in range(ctrl & 0x3F):
                if pos + 1 >= len(d):
                    break
                row[x] = d[pos] | (d[pos + 1] << 8); pos += 2; x += 1
                if x >= width:
                    flush()
        else:                                 # RUN
            c = ctrl & 0x3F
            if pos + 1 < len(d):
                v = d[pos] | (d[pos + 1] << 8); pos += 2
                for _ in range(c):
                    row[x] = v; x += 1
                    if x >= width:
                        flush()
    if any(p is not None for p in row):
        rows.append(row)
    return rows


def best_shift(rows, width):
    col = [0] * width
    for row in rows:
        for x in range(width):
            if x < len(row) and row[x] is not None and row[x] != 0xFFFF:
                col[x] += 1
    min_x = min(range(width), key=lambda x: col[x])
    return (width - min_x) % width


def decode_model(d, base, black_transparent=False, flip_v=False):
    ws = d[base + 7] | (d[base + 8] << 8)
    data_start = base + 42          # 32 header + 10 sub-header
    rows = decode_rle_wrap(d, data_start, len(d), ws)
    H = len(rows)
    shift = best_shift(rows, ws)
    img = Image.new('RGBA', (ws, H), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(rows):
        dy = (H - 1 - y) if flip_v else y
        for x in range(min(ws, len(row))):
            c = rgba565(row[x], black_transparent)
            if c[3] > 0:
                px[(x + shift) % ws, dy] = c
    return img


# ----------------------------------------------------------------------
# Dispatcher: detect variant and decode
# ----------------------------------------------------------------------
def find_frm16_base(d):
    """Return signature offset, or None if this is not a FRM16."""
    if len(d) < 18:
        return None
    if d[0] == 0x32 and d[1] == 0x10:
        return 0
    if d[0] == 0x01 and d[1] == 0x32 and d[2] == 0x10:
        return 0
    # FRM16 embedded somewhere inside an MDL16
    for i in range(min(len(d) - 12, 4096)):
        if d[i] == 0x01 and d[i + 1] == 0x32 and d[i + 2] == 0x10:
            ws = d[i + 7] | (d[i + 8] << 8)
            hs = d[i + 9] | (d[i + 10] << 8)
            if 0 < ws <= 4096 and 0 < hs <= 4096:
                return i
    return None


def convert_one(path, black_transparent=False, flip_v=False):
    d = open(path, 'rb').read()
    base = find_frm16_base(d)
    if base is None:
        return None

    if base == 0 and d[0] == 0x32 and d[1] == 0x10:
        flags = d[12] | (d[13] << 8)
        if flags == 0x0040:
            return decode_raw(d)               # RAW texture
        # 32 10 but compressed: treat as model body at offset 32+10
        # (rare; fall through to model path using base 0 won't work, so RAW)
        return decode_raw(d)
    else:
        return decode_model(d, base, black_transparent, flip_v)


# ----------------------------------------------------------------------
# Batch walk
# ----------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    folder = args[0]
    black = '--black-transp' in args
    overwrite = '--overwrite' in args
    flip = '--flip' in args
    dry = '--dry-run' in args

    if not os.path.isdir(folder):
        print(f"ERROR: not a folder: {folder}")
        sys.exit(1)

    total = ok = skipped = failed = 0
    for root, _, files in os.walk(folder):
        for fn in files:
            if not fn.lower().endswith('.frm16'):
                continue
            total += 1
            src = os.path.join(root, fn)
            dst = os.path.splitext(src)[0] + '.png'

            if os.path.exists(dst) and not overwrite:
                skipped += 1
                continue

            if dry:
                print(f"[dry] {src}  ->  {dst}")
                ok += 1
                continue

            try:
                img = convert_one(src, black_transparent=black, flip_v=flip)
                if img is None:
                    print(f"[skip non-FRM16] {src}")
                    failed += 1
                    continue
                img.save(dst)
                ok += 1
                print(f"[ok] {os.path.relpath(dst, folder)}  ({img.width}x{img.height})")
            except Exception as e:
                failed += 1
                print(f"[FAIL] {src}: {e}")

    print("\n" + "=" * 50)
    print(f"FRM16 found : {total}")
    print(f"Converted   : {ok}")
    print(f"Skipped     : {skipped}  (already had a .png; use --overwrite)")
    print(f"Failed      : {failed}")


if __name__ == '__main__':
    main()
