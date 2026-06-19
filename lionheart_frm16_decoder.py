#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lionheart: Legacy of the Crusader - FRM16 Decoder  [SOLVED]
============================================================
Decodes Lionheart FRM16 images (sprites/renders with transparency),
both standalone files and those embedded inside MDL16 model files.

Project: "Myth Drannor" mod for Baldur's Gate 2 EE.

=====================================================================
FRM16 FORMAT - FULL SPECIFICATION
=====================================================================

HEADER (32 bytes):
    +0   2/3 bytes  magic:  32 10       (standalone FRM16)
                            01 32 10     (FRM16 embedded in MDL16)
  Fields (FRM16 in MDL16, magic 01 32 10):
    +3   u16   visible_width   (wv)
    +5   u16   visible_height  (hv)
    +7   u16   stored_width    (ws)   <- DECODING WIDTH
    +9   u16   stored_height   (hs)
    +13  u16   flags           (0x0044 = RLE)
    +17  u32   data_size

  NOTE: in some files visible_width is corrupted (e.g. 65529, a 16-bit
        overflow). ALWAYS use stored_width as the row width.

SUB-HEADER (10 bytes, immediately after the 32-byte header):
    Layout: 00 + two DWORDs (the 2nd is related to data_size).
    The byte at offset 40 is 0x00 (a "RUN 0" filler) and offsets 41-42
    usually contain FF FF (transparent). The actual RLE pixel data
    starts at offset 42. Starting at 42 removes the colored filler line
    that otherwise appears at the top/edge of the image.

DATA: 3-case RLE (Edheldil's scheme). Pixels are RGB565 little-endian.
    ctrl & 0x80          -> SKIP    (ctrl & 0x7F) transparent pixels
    ctrl & 0x40 (no 0x80)-> LITERAL (ctrl & 0x3F) distinct pixels
    otherwise            -> RUN     (ctrl & 0x3F) repeats of the next px

  SKIP advances the X position (= transparency). When X reaches the
  width 'ws', decoding wraps to the next row automatically.

TAIL: there is trailing metadata (04 00 00 00 XX 00 00) but the RLE
  stops on its own once the pixel data is consumed; no manual trimming
  of the end of the file is required.

TRANSPARENCY: 0xFFFF (white) = transparent. Black 0x0000 is a real color
  (shadows / armor), so it is NOT made transparent by default.

CENTERING (key step): rows are stored circularly shifted. Each row must
  be rotated by a 'shift' amount to reassemble the image. The shift is
  detected automatically: it is the amount that moves the LEAST opaque
  column (the sprite's natural "seam") to the edge. This centers the
  image without needing a per-file constant.

  (Validated against King_Statue, House1, Hamlet_Inn, 90_corner, main,
   towerwreck, Head1, Bottle_Broke, Stairs_down_D, Block_45_in_A:
   all decode correctly.)
"""

import struct
import sys
from PIL import Image


def rgba565(v, black_transparent=False):
    """Convert an RGB565 value to an RGBA tuple. 0xFFFF is transparent."""
    if v is None or v == 0xFFFF:
        return (0, 0, 0, 0)
    if black_transparent and v == 0x0000:
        return (0, 0, 0, 0)
    return (((v >> 11) & 31) * 255 // 31,
            ((v >> 5)  & 63) * 255 // 63,
            ( v        & 31) * 255 // 31, 255)


def find_internal_frm16(d):
    """Return the offset of the 01 32 10 signature inside an MDL16
    (or 0 for a standalone FRM16). Returns None if not found."""
    if d[0] == 0x32 and d[1] == 0x10:
        return 0
    if d[0] == 0x01 and d[1] == 0x32 and d[2] == 0x10:
        return 0
    for i in range(len(d) - 2):
        if d[i] == 0x01 and d[i+1] == 0x32 and d[i+2] == 0x10:
            ws = d[i+7] | (d[i+8] << 8)
            hs = d[i+9] | (d[i+10] << 8)
            if 0 < ws <= 4096 and 0 < hs <= 4096:
                return i
    return None


def decode_rle_wrap(d, start, end, width):
    """3-case RLE with automatic row wrap at 'width'. SKIP advances X."""
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
        if ctrl & 0x80:                          # SKIP (transparent)
            n = ctrl & 0x7F
            for _ in range(n):
                x += 1
                if x >= width:
                    flush()
        elif ctrl & 0x40:                        # LITERAL (distinct pixels)
            c = ctrl & 0x3F
            for _ in range(c):
                if pos + 1 >= len(d):
                    break
                row[x] = d[pos] | (d[pos+1] << 8); pos += 2; x += 1
                if x >= width:
                    flush()
        else:                                    # RUN (repeated pixel)
            c = ctrl & 0x3F
            if pos + 1 < len(d):
                v = d[pos] | (d[pos+1] << 8); pos += 2
                for _ in range(c):
                    row[x] = v; x += 1
                    if x >= width:
                        flush()
    if any(p is not None for p in row):
        rows.append(row)
    return rows


def best_shift(rows, width):
    """Return the shift that moves the least-opaque column to the edge,
    which centers the sprite (rows are stored circularly shifted)."""
    col = [0] * width
    for row in rows:
        for x in range(width):
            if x < len(row) and row[x] is not None and row[x] != 0xFFFF:
                col[x] += 1
    min_x = min(range(width), key=lambda x: col[x])
    return (width - min_x) % width


def decode_frm16(path, out_path=None, black_transparent=False,
                 manual_shift=None, flip_v=False):
    """Decode a FRM16 (or the FRM16 inside an MDL16) to an RGBA image."""
    d = open(path, 'rb').read()
    base = find_internal_frm16(d)
    if base is None:
        print("Not a recognizable FRM16/MDL16 file.")
        return None

    ws = d[base+7] | (d[base+8] << 8)
    hs = d[base+9] | (d[base+10] << 8)
    flags = d[base+13] | (d[base+14] << 8)
    print(f"FRM16 @ 0x{base:X}: stored={ws}x{hs} flags=0x{flags:04X}")

    data_start = base + 42          # 32-byte header + 10-byte sub-header
    data_end = len(d)               # RLE stops on its own; tail is metadata

    rows = decode_rle_wrap(d, data_start, data_end, ws)
    H = len(rows)

    shift = manual_shift if manual_shift is not None else best_shift(rows, ws)
    print(f"  rows={H} shift={shift}")

    img = Image.new('RGBA', (ws, H), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(rows):
        dy = (H - 1 - y) if flip_v else y
        for x in range(min(ws, len(row))):
            c = rgba565(row[x], black_transparent)
            if c[3] > 0:
                nx = (x + shift) % ws
                px[nx, dy] = c

    if out_path:
        img.save(out_path)
        print(f"  saved: {out_path}  ({ws}x{H})")
    return img


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python lionheart_frm16_decoder.py file.frm16|.mdl16 "
              "[output.png] [--black-transp] [--shift N] [--flip]")
        print()
        print("Options:")
        print("  --black-transp   treat black (0x0000) as transparent too")
        print("  --shift N        force a manual horizontal shift instead of auto")
        print("  --flip           flip the image vertically")
        sys.exit(1)

    inp = sys.argv[1]
    out = None
    black = '--black-transp' in sys.argv
    flip = '--flip' in sys.argv
    mshift = None
    if '--shift' in sys.argv:
        mshift = int(sys.argv[sys.argv.index('--shift') + 1])
    # First non-flag argument after the input path is the output filename
    for a in sys.argv[2:]:
        if not a.startswith('--') and a != (str(mshift) if mshift is not None else None):
            out = a
            break
    if out is None:
        out = inp.rsplit('.', 1)[0] + '_decoded.png'

    decode_frm16(inp, out, black_transparent=black,
                 manual_shift=mshift, flip_v=flip)
