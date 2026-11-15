"""Generate cross-platform application icons (512x512 PNG + multi-size ICO) for BillApp."""

from __future__ import annotations

import struct
from pathlib import Path
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


def main() -> None:
    assets_dir = Path(__file__).resolve().parent.parent / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    svg_doc = pymupdf.open(stream=ICON_SVG.encode("utf-8"), filetype="svg")
    page = svg_doc[0]

    ico_sizes = [16, 32, 48, 64, 128, 256]
    ico_blobs: list[tuple[int, bytes]] = []
    for size in [*ico_sizes, 512]:
        scale = size / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        png_bytes = pix.tobytes("png")
        if size == 512:
            (assets_dir / "icon.png").write_bytes(png_bytes)
        if size in ico_sizes:
            ico_blobs.append((size, png_bytes))

    (assets_dir / "icon.ico").write_bytes(build_ico(ico_blobs))
    print(f"Generated icons in {assets_dir}")


if __name__ == "__main__":
    main()
