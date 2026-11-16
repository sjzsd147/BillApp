"""Generate cross-platform application icons (PNG + multi-size ICO + web favicon) for BillApp."""

from __future__ import annotations

import argparse
from io import BytesIO
import struct
from pathlib import Path
from PIL import Image
import numpy as np
import pymupdf

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect x="24" y="24" width="464" height="464" rx="108" fill="#2563EB"/>
  <rect x="32" y="32" width="448" height="448" rx="100" fill="none" stroke="#60A5FA" stroke-opacity="0.35" stroke-width="6"/>
  <rect x="112" y="96" width="288" height="320" rx="34" fill="#FFFFFF"/>
  <rect x="156" y="152" width="132" height="24" rx="12" fill="#93C5FD"/>
  <rect x="156" y="198" width="200" height="18" rx="9" fill="#CBD5E1"/>
  <rect x="156" y="236" width="160" height="18" rx="9" fill="#E2E8F0"/>
  <path d="M214 290 L246 340 L278 290 M246 340 L246 376 M212 344 L280 344 M212 364 L280 364"
        fill="none" stroke="#1D4ED8" stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="356" cy="160" r="38" fill="#10B981"/>
  <path d="M338 160 L351 173 L376 147"
        fill="none" stroke="#FFFFFF" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def build_ico(png_blobs: list[tuple[int, bytes]]) -> bytes:
    """Pack PNG-compressed icon images into a standard multi-size Windows .ico container."""
    count = len(png_blobs)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    payload = bytearray()
    offset = 6 + 16 * count
    for size, blob in png_blobs:
        dim = 0 if size >= 256 else size
        entries.extend(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        payload.extend(blob)
        offset += len(blob)
    return bytes(header + entries + payload)


def encode_png(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def extract_squircle_icon(source_path: Path, canvas_size: int = 1024) -> Image.Image:
    """Extract a centered rounded-square artwork onto a transparent RGBA canvas."""
    im = Image.open(source_path).convert("RGBA")
    rgb = np.array(im.convert("RGB"), dtype=np.int16)
    # Locate the colored icon body (exclude white margins)
    is_body = ((rgb[:, :, 2] - rgb[:, :, 0]) > 25) | (rgb[:, :, 0] < 180)
    ys, xs = np.where(is_body)
    if len(xs) > 0 and len(ys) > 0:
        x0, x1 = int(xs.min()) + 3, int(xs.max()) - 2
        y0, y1 = int(ys.min()) + 3, int(ys.max()) - 2
        if x1 > x0 and y1 > y0:
            im = im.crop((x0, y0, x1 + 1, y1 + 1))

    inner_size = int(round(canvas_size * 0.875))
    margin = (canvas_size - inner_size) // 2
    resized = im.resize((inner_size, inner_size), Image.Resampling.LANCZOS)

    # 4x supersampled superellipse squircle mask for crisp anti-aliased transparent corners
    ss = 4
    n = inner_size * ss
    coords = (np.arange(n, dtype=np.float32) + 0.5) - (n / 2.0)
    x_grid, y_grid = np.meshgrid(coords, coords)
    radius = (inner_size / 2.0 - 2.5) * ss
    dist = (np.abs(x_grid) / radius) ** 4.85 + (np.abs(y_grid) / radius) ** 4.85
    alpha_ss = np.clip((1.0 - dist) * (radius * 0.18), 0.0, 1.0)
    alpha_2d = alpha_ss.reshape(inner_size, ss, inner_size, ss).mean(axis=(1, 3))

    arr = np.array(resized)
    arr[:, :, 3] = np.clip(alpha_2d * 255.0, 0, 255).astype(np.uint8)
    masked = Image.fromarray(arr, mode="RGBA")

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    canvas.paste(masked, (margin, margin), masked)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate BillApp icons.")
    parser.add_argument("--source", type=Path, help="Optional raw AI-generated square icon path")
    parser.add_argument("--svg", action="store_true", help="Regenerate master icon from fallback SVG")
    args = parser.parse_args()

    electron_dir = Path(__file__).resolve().parent.parent
    assets_dir = electron_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    frontend_public_dir = electron_dir.parent / "frontend" / "public"
    frontend_public_dir.mkdir(parents=True, exist_ok=True)

    master_png = assets_dir / "icon.png"

    if args.source:
        master = extract_squircle_icon(args.source, canvas_size=1024)
        master.save(master_png, format="PNG", optimize=True)
    elif args.svg or not master_png.exists():
        svg_doc = pymupdf.open(stream=ICON_SVG.encode("utf-8"), filetype="svg")
        page = svg_doc[0]
        scale = 1024 / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        master_png.write_bytes(pix.tobytes("png"))
        master = Image.open(master_png).convert("RGBA")
    else:
        master = Image.open(master_png).convert("RGBA")

    ico_sizes = [16, 32, 48, 64, 128, 256]
    ico_blobs: list[tuple[int, bytes]] = []
    for size in ico_sizes:
        resized = master.resize((size, size), Image.Resampling.LANCZOS)
        ico_blobs.append((size, encode_png(resized)))

    (assets_dir / "icon.ico").write_bytes(build_ico(ico_blobs))
    master.resize((128, 128), Image.Resampling.LANCZOS).save(
        frontend_public_dir / "icon.png", format="PNG", optimize=True
    )
    print(f"Generated icons in {assets_dir} and {frontend_public_dir}")


if __name__ == "__main__":
    main()
