#!/usr/bin/env python3
"""chafa wrapper — crop source image, then render to Unicode block art.

Requires: chafa ≥1.12, ImageMagick (convert)

Examples
--------
# All-sides 10px crop, 6 lines tall, 42 cols wide:
  python scripts/crop_art.py horse.png -c 10 -H 6

# Asymmetric crop:
  python scripts/crop_art.py horse.png --top 5 --bottom 5 --left 3 --right 3 -H 6

# Pass extra chafa options (after --):
  python scripts/crop_art.py horse.png -c 10 -H 6 -- --colors 256 --symbols block+sextant
"""
import argparse
import subprocess
import sys


def build_convert_cmd(image: str, top: int, bottom: int, left: int, right: int) -> list[str]:
    cmd = ["convert", image]
    # -chop removes pixels from a specific edge; +repage resets canvas origin
    if top:
        cmd += ["-gravity", "North", "-chop", f"0x{top}", "+repage"]
    if bottom:
        cmd += ["-gravity", "South", "-chop", f"0x{bottom}", "+repage"]
    if left:
        cmd += ["-gravity", "West",  "-chop", f"{left}x0", "+repage"]
    if right:
        cmd += ["-gravity", "East",  "-chop", f"{right}x0", "+repage"]
    cmd += ["PNG:-"]   # stdout as PNG
    return cmd


def build_chafa_cmd(
    width: int,
    height: int,
    fmt: str,
    symbols: str,
    colors: str,
    extra: list[str],
) -> list[str]:
    return [
        "chafa",
        "--format",  fmt,
        "--symbols", symbols,
        "--colors",  colors,
        "--size",    f"{width}x{height}",
        *extra,
        "-",   # read from stdin
    ]


def main() -> None:
    p = argparse.ArgumentParser(
        description="Crop image then render with chafa",
        epilog="Extra chafa options can be appended after --",
    )
    p.add_argument("image", help="Source image file (PNG/JPEG/…)")

    crop = p.add_argument_group("crop (pixels)")
    crop.add_argument("--crop",   "-c", type=int, default=0, metavar="PX",
                      help="Crop N px from all four sides")
    crop.add_argument("--top",    "-t", type=int, default=0, metavar="PX")
    crop.add_argument("--bottom", "-b", type=int, default=0, metavar="PX")
    crop.add_argument("--left",   "-l", type=int, default=0, metavar="PX")
    crop.add_argument("--right",  "-r", type=int, default=0, metavar="PX")

    out = p.add_argument_group("chafa output")
    out.add_argument("--height",  "-H", type=int, default=6,  metavar="LINES",
                     help="Output height in terminal lines (default: 6)")
    out.add_argument("--width",   "-W", type=int, default=42, metavar="COLS",
                     help="Output width in terminal columns (default: 42)")
    out.add_argument("--format",  default="symbols",
                     help="chafa --format  (default: symbols)")
    out.add_argument("--symbols", default="block+border",
                     help="chafa --symbols (default: block+border)")
    out.add_argument("--colors",  default="none",
                     help="chafa --colors  (default: none)")

    args, extra = p.parse_known_args()
    # strip leading '--' separator if present
    if extra and extra[0] == "--":
        extra = extra[1:]

    if args.crop:
        args.top = args.bottom = args.left = args.right = args.crop

    convert_cmd = build_convert_cmd(
        args.image, args.top, args.bottom, args.left, args.right
    )
    chafa_cmd = build_chafa_cmd(
        args.width, args.height, args.format, args.symbols, args.colors, extra
    )

    # Print the pipeline for reference
    print("# pipeline:", " ".join(convert_cmd), "|", " ".join(chafa_cmd), file=sys.stderr)

    p_convert = subprocess.Popen(convert_cmd, stdout=subprocess.PIPE)
    p_chafa   = subprocess.Popen(chafa_cmd,   stdin=p_convert.stdout,
                                              stdout=subprocess.PIPE)
    p_convert.stdout.close()
    out_bytes, _ = p_chafa.communicate()
    sys.stdout.write(out_bytes.decode())


if __name__ == "__main__":
    main()
