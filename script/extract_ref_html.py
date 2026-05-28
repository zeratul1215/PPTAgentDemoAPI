from __future__ import annotations

"""
Convert PDF to editable HTML (text layer + hybrid images).

Images **without** a PDF soft mask use ``extract_image`` (full embedded resolution).
Images **with** ``/SMask`` (or ``has-mask`` from ``get_image_info``) are decoded from the
image XObject (xref) into a standalone pixmap (NOT a page-clip raster). This avoids
capturing unrelated overlay content (e.g., text drawn on top of the image bbox) into the
extracted asset.

``ImageBox`` / ``data-w``/``data-h`` stay in PDF pt.

Default output directory (when --out is omitted):
  ./result/<pdf_stem>_temp/ref_html
"""

import argparse
import base64
import hashlib
import html
import re
import shutil
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable, Optional

import fitz  # PyMuPDF

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore


OFFICE_FRIENDLY_FONTS_CSS = r"""
/* Office/PPT-friendly font override (macOS target).
   Requirement: generated HTML must use Office-compatible fonts from the start.
*/
.page p, .page span {
  font-family:
    Calibri,
    Arial,
    "Songti SC",
    "PingFang SC",
    "Hiragino Sans GB",
    "Heiti SC",
    "Microsoft YaHei",
    SimSun,
    sans-serif !important;
}
"""


@dataclass(frozen=True)
class ExtractedFont:
    canonical_family: str
    rel_path: str
    weight: int
    style: str


@dataclass(frozen=True)
class ContainerBox:
    cid: str
    x: float
    y: float
    w: float
    h: float
    fill: Optional[str]
    fill_opacity: float


@dataclass(frozen=True)
class ImageBox:
    iid: str
    x: float
    y: float
    w: float
    h: float


def _write_image_bytes_as_png(buf: bytes, out_path: Path) -> bool:
    """
    Best-effort convert raw image bytes to PNG.
    Returns True if a PNG file was written.
    """
    if Image is None:
        return False
    try:
        img = Image.open(BytesIO(buf))
        if img.mode not in {"RGB", "RGBA"}:
            img = img.convert("RGBA")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG", optimize=True)
        return True
    except Exception:
        return False


def _safe_filename(s: str) -> str:
    s = s.strip().replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9._+-]+", "_", s)
    return s or "file"


def _rgb_to_hex(rgb: Optional[tuple[float, float, float]]) -> Optional[str]:
    if not rgb:
        return None
    r, g, b = rgb
    rr = max(0, min(255, int(round(r * 255))))
    gg = max(0, min(255, int(round(g * 255))))
    bb = max(0, min(255, int(round(b * 255))))
    return f"#{rr:02x}{gg:02x}{bb:02x}"


def _infer_font_weight_and_style(font_name: str) -> tuple[str, int, str]:
    """
    Returns (canonical_family, weight, style).
    We try to map common suffixes like "-Bold" to CSS weights so <b> works.
    """
    name = font_name
    style = "normal"
    if re.search(r"(?i)(italic|oblique)", name):
        style = "italic"

    weight = 400
    # order matters: Black before Bold
    weight_rules = [
        (r"(?i)(?:-|\s)black$", 900),
        (r"(?i)(?:-|\s)extrabold$", 800),
        (r"(?i)(?:-|\s)ultrabold$", 800),
        (r"(?i)(?:-|\s)bold$", 700),
        (r"(?i)(?:-|\s)semibold$", 600),
        (r"(?i)(?:-|\s)medium$", 500),
        (r"(?i)(?:-|\s)light$", 300),
        (r"(?i)(?:-|\s)extralight$", 200),
        (r"(?i)(?:-|\s)thin$", 100),
    ]
    for pat, w in weight_rules:
        if re.search(pat, name):
            weight = w
            break

    # Strip style/weight suffixes for canonical family.
    canonical = re.sub(
        r"(?i)(?:-|\s)(black|extrabold|ultrabold|bold|semibold|medium|light|extralight|thin|italic|oblique)$",
        "",
        name,
    ).strip()
    canonical = canonical or name
    return canonical, weight, style


def _svg_style_attrs(d: dict) -> str:
    fill = _rgb_to_hex(d.get("fill"))
    stroke = _rgb_to_hex(d.get("color"))
    fill_opacity = d.get("fill_opacity")
    stroke_opacity = d.get("stroke_opacity")
    stroke_width = d.get("width")

    cap_map = {0: "butt", 1: "round", 2: "square"}
    join_map = {0: "miter", 1: "round", 2: "bevel"}

    attrs: list[str] = []
    attrs.append(f'fill="{fill or "none"}"')
    if fill_opacity is not None and fill is not None:
        attrs.append(f'fill-opacity="{float(fill_opacity):.4f}"')

    attrs.append(f'stroke="{stroke or "none"}"')
    if stroke is not None and stroke_width is not None:
        attrs.append(f'stroke-width="{float(stroke_width):.4f}"')
    if stroke_opacity is not None and stroke is not None:
        attrs.append(f'stroke-opacity="{float(stroke_opacity):.4f}"')

    line_cap = d.get("lineCap")
    if line_cap in cap_map:
        attrs.append(f'stroke-linecap="{cap_map[line_cap]}"')

    line_join = d.get("lineJoin")
    if line_join in join_map:
        attrs.append(f'stroke-linejoin="{join_map[line_join]}"')

    dashes = d.get("dashes")
    if dashes and isinstance(dashes, (list, tuple)) and len(dashes) >= 1:
        dash_vals = [str(float(x)) for x in dashes if isinstance(x, (int, float))]
        if dash_vals:
            attrs.append(f'stroke-dasharray="{" ".join(dash_vals)}"')

    return " ".join(attrs)


def _build_path_d(items: Iterable[tuple], close_path: bool) -> str:
    parts: list[str] = []
    current_end: Optional[tuple[float, float]] = None

    def pt(p) -> tuple[float, float]:
        return (float(p.x), float(p.y))

    for it in items:
        if not it:
            continue
        cmd = it[0]
        if cmd == "l":
            start, end = pt(it[1]), pt(it[2])
            if current_end != start:
                parts.append(f"M {start[0]:.4f} {start[1]:.4f}")
            parts.append(f"L {end[0]:.4f} {end[1]:.4f}")
            current_end = end
        elif cmd == "c":
            start = pt(it[1])
            c1, c2, end = pt(it[2]), pt(it[3]), pt(it[4])
            if current_end != start:
                parts.append(f"M {start[0]:.4f} {start[1]:.4f}")
            parts.append(
                f"C {c1[0]:.4f} {c1[1]:.4f} {c2[0]:.4f} {c2[1]:.4f} {end[0]:.4f} {end[1]:.4f}"
            )
            current_end = end

    if close_path:
        parts.append("Z")
    return " ".join(parts)


def _page_text_inner_html(page: fitz.Page) -> str:
    fragment = page.get_text("html")
    open_end = fragment.find(">")
    close_start = fragment.rfind("</div>")
    if open_end == -1 or close_start == -1 or close_start <= open_end:
        return ""
    inner = fragment[open_end + 1 : close_start]
    inner = re.sub(r"<img\b[\s\S]*?>", "", inner, flags=re.IGNORECASE)
    return inner.strip()


_re_p_open_tag = re.compile(r"<p\b([^>]*)>", flags=re.IGNORECASE)
_re_style_top = re.compile(r"\btop:\s*([0-9.]+)pt", flags=re.IGNORECASE)
_re_style_left = re.compile(r"\bleft:\s*([0-9.]+)pt", flags=re.IGNORECASE)


def _extract_container_boxes(page: fitz.Page, *, page_no: int) -> list[ContainerBox]:
    """
    Derive layout containers (typically colored rectangles / cards) from PDF vector drawings.

    These are emitted as DOM divs for LLM-friendly container-boundary reasoning, so downstream
    agents don't have to parse <svg><rect> tags to infer container boxes.
    """
    w = float(page.rect.width)
    h = float(page.rect.height)
    page_area = w * h

    # Heuristics: keep meaningful filled rectangles, drop tiny decoration and full-page backgrounds.
    min_w = 12.0
    min_h = 12.0
    min_area = 800.0
    max_area_ratio = 0.95

    raw: list[tuple[float, float, float, float, Optional[str], float]] = []
    seen: set[tuple[float, float, float, float, str, float]] = set()

    for d in page.get_drawings():
        fill = _rgb_to_hex(d.get("fill"))
        fill_opacity = d.get("fill_opacity")
        if not fill:
            continue
        op = float(fill_opacity) if fill_opacity is not None else 1.0
        if op <= 0.0:
            continue

        items = d.get("items", [])
        rect_items = [it for it in items if it and it[0] == "re"]
        other_items = [it for it in items if it and it[0] != "re"]
        if not rect_items or other_items:
            continue

        for it in rect_items:
            r = it[1]  # fitz.Rect
            x0 = float(r.x0)
            y0 = float(r.y0)
            rw = float(r.x1 - r.x0)
            rh = float(r.y1 - r.y0)
            if rw <= 0 or rh <= 0:
                continue
            if rw < min_w or rh < min_h:
                continue
            area = rw * rh
            if area < min_area:
                continue
            if area > page_area * max_area_ratio:
                continue
            if fill.lower() in {"#ffffff", "#fff"} and area > page_area * 0.5:
                # Avoid treating the main white background as a "container".
                continue

            key = (round(x0, 2), round(y0, 2), round(rw, 2), round(rh, 2), fill, round(op, 3))
            if key in seen:
                continue
            seen.add(key)
            raw.append((x0, y0, rw, rh, fill, op))

    raw.sort(key=lambda t: (t[2] * t[3], t[0], t[1], t[4] or ""))
    out: list[ContainerBox] = []
    for i, (x, y, rw, rh, fill, op) in enumerate(raw):
        out.append(ContainerBox(cid=f"p{page_no}_c{i}", x=x, y=y, w=rw, h=rh, fill=fill, fill_opacity=op))
    return out


def _extract_image_boxes(page: fitz.Page, *, page_no: int) -> list[ImageBox]:
    """
    Derive image bounding boxes for non-visual layout reasoning (rule 1.6).

    We only emit "meaningful" images: drop tiny icons and full-page backgrounds.
    Coordinates are in pt, aligned with the HTML text layer.
    """
    w = float(page.rect.width)
    h = float(page.rect.height)
    page_area = w * h

    min_w = 12.0
    min_h = 12.0
    min_area = 800.0
    max_area_ratio = 0.95

    out: list[ImageBox] = []
    try:
        td = page.get_text("dict")
    except Exception:
        return out

    blocks = td.get("blocks", []) if isinstance(td, dict) else []
    seen: set[tuple[float, float, float, float]] = set()
    idx = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") != 1:
            continue
        bbox = b.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        try:
            x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except Exception:
            continue
        bw = max(0.0, x1 - x0)
        bh = max(0.0, y1 - y0)
        if bw < min_w or bh < min_h:
            continue
        area = bw * bh
        if area < min_area:
            continue
        if area > page_area * max_area_ratio:
            continue
        key = (round(x0, 2), round(y0, 2), round(bw, 2), round(bh, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(ImageBox(iid=f"p{page_no}_i{idx}", x=x0, y=y0, w=bw, h=bh))
        idx += 1
    return out


# Fixed scale for pixmap clips when SMask (or unknown xref) requires rasterization.
# 192/72 ≈ 2.67 px/pt (~192 dpi along one edge of a 1pt square); sharper than 96/72 without CLI knobs.
RASTER_PX_PER_PT = 192.0 / 72.0


def _xref_has_smask(doc: fitz.Document, xref: int) -> bool:
    if xref is None or xref < 1:
        return False
    try:
        _k, v = doc.xref_get_key(int(xref), "SMask")
        if not v:
            return False
        v2 = v.strip().lower()
        if v2 in ("null", "none", ""):
            return False
        return True
    except Exception:
        return False


def _image_draw_entries(page: fitz.Page) -> list[dict]:
    try:
        return list(page.get_image_info(xrefs=True))
    except Exception:
        return []


def _match_image_xref_for_bbox(
    draw_entries: list[dict], bbox: tuple[float, float, float, float]
) -> tuple[Optional[int], Optional[bool]]:
    """
    Match a text-dict image block bbox to get_image_info entry; returns (xref, has-mask).
    """
    x0, y0, x1, y1 = bbox
    best_dist = 1e9
    best_xref: Optional[int] = None
    best_hm: Optional[bool] = None
    for im in draw_entries:
        ib = im.get("bbox")
        if not (isinstance(ib, (list, tuple)) and len(ib) >= 4):
            continue
        try:
            ix0, iy0, ix1, iy1 = (float(ib[0]), float(ib[1]), float(ib[2]), float(ib[3]))
        except Exception:
            continue
        dist = abs(x0 - ix0) + abs(y0 - iy0) + abs(x1 - ix1) + abs(y1 - iy1)
        if dist < best_dist:
            best_dist = dist
            xr = im.get("xref")
            best_xref = int(xr) if xr is not None else None
            best_hm = bool(im.get("has-mask")) if im.get("has-mask") is not None else None
    if best_dist > 2.0:
        return None, None
    return best_xref, best_hm


def _need_raster_clip(doc: fitz.Document, xref: Optional[int], has_mask_flag: Optional[bool]) -> bool:
    """Use pixmap when soft mask is present or xref cannot be trusted for raw decode."""
    if has_mask_flag is True:
        return True
    if xref is None or xref < 1:
        return True
    return _xref_has_smask(doc, int(xref))


def _pixmap_looks_like_effect_shadow(*, pix: fitz.Pixmap) -> bool:
    """
    Heuristic: PPT->PDF exports sometimes emit large masked 'shadow' images (nearly solid dark RGB
    + soft alpha) behind vector/text content. These are not meaningful "images" for our pipeline
    and can be safely skipped.

    Intentionally conservative: only skip when the image is large and RGB is almost uniform dark.
    """
    try:
        total = int(pix.width) * int(pix.height)
        if total < 20_000:
            return False
        if int(getattr(pix, "n", 0)) < 3:
            return False

        # Sample up to ~2048 pixels.
        step = max(1, total // 2048)
        s = pix.samples
        n = int(pix.n)

        minv = 255
        maxv = 0
        for i in range(0, total, step):
            j = i * n
            r = s[j]
            g = s[j + 1]
            b = s[j + 2]
            if r < minv:
                minv = r
            if g < minv:
                minv = g
            if b < minv:
                minv = b
            if r > maxv:
                maxv = r
            if g > maxv:
                maxv = g
            if b > maxv:
                maxv = b
            # Strong variation => not a near-solid shadow.
            if (maxv - minv) > 24:
                return False

        # Nearly-uniform and dark.
        return maxv <= 40
    except Exception:
        return False


def _write_xref_pixmap_as_png(doc: fitz.Document, xref: int, out_path: Path) -> bool:
    """Decode the image XObject and write a standalone PNG (no page-clip rendering)."""
    try:
        pix = fitz.Pixmap(doc, int(xref))
        # Convert CMYK/etc to RGB for PNG safety.
        if pix.colorspace and pix.colorspace.n > 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_path.as_posix())
        return True
    except Exception:
        return False


def _raster_clip_to_png(
    page: fitz.Page,
    clip: fitz.Rect,
    out_path: Path,
    *,
    px_per_pt: float,
) -> bool:
    """
    Rasterize final PDF appearance inside ``clip`` (page space, pt).

    Pixel dimensions: round(clip_width * px_per_pt) x round(clip_height * px_per_pt).
    """
    try:
        bw = max(1e-6, float(clip.x1 - clip.x0))
        bh = max(1e-6, float(clip.y1 - clip.y0))
        wpx = max(1, int(round(bw * px_per_pt)))
        hpx = max(1, int(round(bh * px_per_pt)))
        mat = fitz.Matrix(wpx / bw, hpx / bh)
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=True)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_path.as_posix())
        return True
    except Exception:
        return False


def _extract_image_boxes_and_write_files(
    page: fitz.Page, *, page_no: int, images_root_dir: Path
) -> tuple[list[ImageBox], dict[str, str]]:
    """
    Extract image boxes (same filters as _extract_image_boxes) and write PNGs.

    Hybrid: if the matched PDF image xref has **no** soft mask, write ``extract_image``
    bytes (embedded stream decode). If it has a soft mask (or needs raster previously), we
    decode the image XObject (xref) into a standalone pixmap PNG (NOT a page-clip render),
    so overlay text/shapes are never baked into extracted assets. Large near-solid dark
    shadow-mask effects are skipped as non-meaningful images. Fallback order: xref → raw bytes.

    ``ImageBox`` / ``data-w``/``data-h`` stay in PDF pt for ``html_to_plan`` / AI.

    Output paths:
      <images_root_dir>/page{page_no}/{idx:02d}.png
    Returns:
      (boxes, iid_to_src_relpath) where src is relative to bundle dir, e.g. "images/page1/00.png".
    """
    w = float(page.rect.width)
    h = float(page.rect.height)
    page_area = w * h

    min_w = 12.0
    min_h = 12.0
    min_area = 800.0
    max_area_ratio = 0.95

    out: list[ImageBox] = []
    src_map: dict[str, str] = {}

    try:
        td = page.get_text("dict")
    except Exception:
        return out, src_map

    doc = page.parent
    draw_entries = _image_draw_entries(page)

    blocks = td.get("blocks", []) if isinstance(td, dict) else []
    seen: set[tuple[float, float, float, float]] = set()
    idx = 0

    page_dir = images_root_dir / f"page{page_no}"

    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") != 1:
            continue
        bbox = b.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        buf = b.get("image")
        if not isinstance(buf, (bytes, bytearray)):
            continue
        try:
            x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except Exception:
            continue
        bw = max(0.0, x1 - x0)
        bh = max(0.0, y1 - y0)
        if bw < min_w or bh < min_h:
            continue
        a = bw * bh
        if a < min_area:
            continue
        if a > page_area * max_area_ratio:
            continue
        key = (round(x0, 2), round(y0, 2), round(bw, 2), round(bh, 2))
        if key in seen:
            continue
        seen.add(key)

        xref, has_mask_flag = _match_image_xref_for_bbox(draw_entries, (x0, y0, x1, y1))
        need_raster = _need_raster_clip(doc, xref, has_mask_flag)

        # Skip large near-solid dark masked shadows (PPT effect artifacts).
        if need_raster and xref is not None and int(xref) > 0:
            try:
                pix0 = fitz.Pixmap(doc, int(xref))
                if _pixmap_looks_like_effect_shadow(pix=pix0):
                    continue
            except Exception:
                pass

        iid = f"p{page_no}_i{idx}"
        out.append(ImageBox(iid=iid, x=x0, y=y0, w=bw, h=bh))

        out_name = f"{idx:02d}.png"
        out_path = page_dir / out_name

        wrote = False

        # Prefer embedded bytes for non-masked images.
        if (not need_raster) and xref is not None and int(xref) > 0:
            try:
                imgd = doc.extract_image(int(xref))
                raw = imgd.get("image")
                if isinstance(raw, (bytes, bytearray)):
                    wrote = _write_image_bytes_as_png(bytes(raw), out_path)
            except Exception:
                wrote = False

        # For masked images (and as a PIL-free fallback), decode the XObject pixmap directly.
        if not wrote and xref is not None and int(xref) > 0:
            wrote = _write_xref_pixmap_as_png(doc, int(xref), out_path)

        if not wrote:
            wrote_png = _write_image_bytes_as_png(bytes(buf), out_path)
            if not wrote_png:
                ext = (b.get("ext") or "bin").strip().lower()
                out_name = f"{idx:02d}.{_safe_filename(ext)}"
                out_path = page_dir / out_name
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(bytes(buf))

        src_map[iid] = f"images/page{page_no}/{out_name}"
        idx += 1

    return out, src_map


def _containers_layer_html(containers: list[ContainerBox]) -> str:
    if not containers:
        return ""
    parts: list[str] = ['<div class="container-layer" aria-hidden="true">']
    for c in containers:
        parts.append(
            "\n".join(
                [
                    (
                        f'<div class="container" id="{html.escape(c.cid)}" '
                        f'data-bbox="{c.x:.4f},{c.y:.4f},{c.w:.4f},{c.h:.4f}" '
                        f'data-x="{c.x:.4f}" data-y="{c.y:.4f}" data-w="{c.w:.4f}" data-h="{c.h:.4f}" '
                        f'data-fill="{html.escape(c.fill or "")}" data-fill-opacity="{c.fill_opacity:.4f}" '
                        f'style="left:{c.x:.4f}pt;top:{c.y:.4f}pt;width:{c.w:.4f}pt;height:{c.h:.4f}pt;"></div>'
                    )
                ]
            )
        )
    parts.append("</div>")
    return "\n".join(parts)


def _images_layer_html(images: list[ImageBox], *, src_map: Optional[dict[str, str]] = None) -> str:
    if not images:
        return ""
    parts: list[str] = ['<div class="image-layer" aria-hidden="true">']
    for im in images:
        src_attr = ""
        if src_map:
            src = src_map.get(im.iid)
            if src:
                src_attr = f' data-src="{html.escape(src)}"'
        parts.append(
            (
                f'<div class="image" id="{html.escape(im.iid)}" '
                f"{src_attr} "
                f'data-bbox="{im.x:.4f},{im.y:.4f},{im.w:.4f},{im.h:.4f}" '
                f'data-x="{im.x:.4f}" data-y="{im.y:.4f}" data-w="{im.w:.4f}" data-h="{im.h:.4f}" '
                f'style="left:{im.x:.4f}pt;top:{im.y:.4f}pt;width:{im.w:.4f}pt;height:{im.h:.4f}pt;"></div>'
            )
        )
    parts.append("</div>")
    return "\n".join(parts)


def _pick_container_id(*, left: float, top: float, containers: list[ContainerBox]) -> Optional[str]:
    best: Optional[ContainerBox] = None
    best_area: Optional[float] = None
    for c in containers:
        if left < c.x or left > (c.x + c.w):
            continue
        if top < c.y or top > (c.y + c.h):
            continue
        area = c.w * c.h
        if best_area is None or area < best_area:
            best = c
            best_area = area
    return best.cid if best else None


def _annotate_p_tags_with_containers(text_inner: str, containers: list[ContainerBox]) -> str:
    if not text_inner or not containers:
        return text_inner

    def repl(m: re.Match) -> str:
        attrs = m.group(1) or ""
        if re.search(r"\bdata-container\s*=", attrs):
            return m.group(0)
        mt = _re_style_top.search(attrs)
        ml = _re_style_left.search(attrs)
        if not mt or not ml:
            return m.group(0)
        try:
            top = float(mt.group(1))
            left = float(ml.group(1))
        except Exception:
            return m.group(0)

        cid = _pick_container_id(left=left, top=top, containers=containers)
        if not cid:
            return m.group(0)

        return f'<p data-container="{html.escape(cid)}"{attrs}>'

    return _re_p_open_tag.sub(repl, text_inner)


def _extract_fonts(doc: fitz.Document, page_nos: range, fonts_dir: Path) -> list[ExtractedFont]:
    fonts_dir.mkdir(parents=True, exist_ok=True)
    # key: (canonical_family, weight, style)
    seen: set[tuple[str, int, str]] = set()
    out: list[ExtractedFont] = []

    for pno in page_nos:
        for xref, _ext, _kind, basefont, _name, _enc, *_ in doc.get_page_fonts(pno, full=True):
            family = basefont.split("+", 1)[1] if "+" in basefont else basefont
            canonical, weight, style = _infer_font_weight_and_style(family)
            key = (canonical, weight, style)
            if key in seen:
                continue
            seen.add(key)

            _font_name, font_ext, _font_type, font_buf = doc.extract_font(xref)
            out_name = _safe_filename(f"xref{xref}_{family}.{font_ext}")
            (fonts_dir / out_name).write_bytes(font_buf)
            out.append(
                ExtractedFont(
                    canonical_family=canonical,
                    rel_path=f"fonts/{out_name}",
                    weight=weight,
                    style=style,
                )
            )

    return out


_re_data_image_attr = re.compile(
    r"""(?P<attr>\b(?:src|href))=(?P<q>["'])data:image/(?P<mime>[^;]+);base64,(?P<b64>[A-Za-z0-9+/=\s]+?)(?P=q)""",
    flags=re.IGNORECASE,
)


_re_charref_hex = re.compile(r"&#x([0-9A-Fa-f]{1,6});")
_re_charref_dec = re.compile(r"&#([0-9]{1,7});")


def _decode_numeric_charrefs_non_ascii(s: str) -> str:
    """
    PyMuPDF's get_text("html") often emits non-ASCII characters as numeric character references
    (e.g., '&#x6df1;'). Browsers render them fine, but they bloat the HTML and add noise for LLM editing.

    We decode only non-ASCII code points (>= 0x80) to avoid accidentally turning '&#x3c;'
    into a literal '<' that could break HTML structure.
    """

    def hex_repl(m: re.Match) -> str:
        try:
            cp = int(m.group(1), 16)
        except Exception:
            return m.group(0)
        if cp < 0x80 or cp > 0x10FFFF:
            return m.group(0)
        return chr(cp)

    def dec_repl(m: re.Match) -> str:
        try:
            cp = int(m.group(1), 10)
        except Exception:
            return m.group(0)
        if cp < 0x80 or cp > 0x10FFFF:
            return m.group(0)
        return chr(cp)

    s = _re_charref_hex.sub(hex_repl, s)
    s = _re_charref_dec.sub(dec_repl, s)
    return s


def _mime_to_ext(mime: str) -> str:
    m = mime.lower().strip()
    if m in {"jpeg", "jpg"}:
        return "jpg"
    if m == "png":
        return "png"
    if m == "webp":
        return "webp"
    if m in {"svg+xml", "svg"}:
        return "svg"
    if m == "gif":
        return "gif"
    return "bin"


def _externalize_data_image_uris(index_html: str, assets_dir: Path) -> str:
    """
    Replace data:image/*;base64,... URIs in src/href attributes with files under assets/.
    This keeps the HTML readable and prevents huge base64 blobs from polluting the context.
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[tuple[str, str], str] = {}

    def repl(m: re.Match) -> str:
        attr = m.group("attr")
        q = m.group("q")
        mime = (m.group("mime") or "").strip()
        b64 = (m.group("b64") or "").strip()
        b64_compact = re.sub(r"\s+", "", b64)
        try:
            data = base64.b64decode(b64_compact, validate=False)
        except Exception:
            return m.group(0)

        ext = _mime_to_ext(mime)
        digest = hashlib.sha1(data).hexdigest()[:16]
        key = (digest, ext)
        rel = cache.get(key)
        if not rel:
            fname = f"embedded_{digest}.{ext}"
            (assets_dir / fname).write_bytes(data)
            rel = f"assets/{fname}"
            cache[key] = rel

        return f'{attr}={q}{html.escape(rel)}{q}'

    return _re_data_image_attr.sub(repl, index_html)


def _compact_data_image_uris(s: str) -> str:
    """
    Remove whitespace/newlines from base64 data:image URIs.

    MuPDF may insert line breaks into long base64 strings; some HTML/SVG renderers will treat
    whitespace inside data: URIs as invalid and drop the image.
    """

    def repl(m: re.Match) -> str:
        attr = m.group("attr")
        q = m.group("q")
        mime = (m.group("mime") or "").strip()
        b64 = (m.group("b64") or "")
        b64_compact = re.sub(r"\s+", "", b64)
        return f'{attr}={q}data:image/{mime};base64,{b64_compact}{q}'

    return _re_data_image_attr.sub(repl, s)


_re_span_style_attr = re.compile(r'<span\s+style="([^"]+)">', flags=re.IGNORECASE)


def _is_symbol_font_family(font_family: str) -> bool:
    ff = (font_family or "").lower()
    return ("wingdings" in ff) or ("zapf" in ff) or re.search(r"\bsymbol\b", ff) is not None


def _strip_font_family_for_office(style: str) -> str:
    """
    Remove non-symbol font-family declarations from span styles so generated HTML uses Office fonts.
    Keep symbol/dingbat fonts (e.g., Wingdings) so bullet glyphs remain correct.
    """
    decls: list[str] = []
    for item in style.split(";"):
        item = item.strip()
        if not item:
            continue
        if item.lower().startswith("font-family:"):
            fam = item.split(":", 1)[1].strip()
            if _is_symbol_font_family(fam):
                decls.append(f"font-family:{fam}")
            continue
        decls.append(item)
    return ";".join(decls) + (";" if decls else "")


def _normalize_css_decl_list(style: str) -> str:
    # Keep order but normalize whitespace and ensure trailing semicolons.
    parts: list[str] = []
    for item in style.split(";"):
        item = item.strip()
        if not item:
            continue
        # collapse internal whitespace around ':'
        if ":" in item:
            k, v = item.split(":", 1)
            item = f"{k.strip()}:{v.strip()}"
        parts.append(item)
    return ";".join(parts) + (";" if parts else "")


def _dedupe_span_inline_styles(index_html: str) -> str:
    """
    Replace repeated <span style="..."> with <span class="tsN"> and inject CSS classes.
    This dramatically reduces HTML size and improves LLM editability.
    """
    style_to_class: dict[str, str] = {}
    ordered_styles: list[str] = []

    def repl(m: re.Match) -> str:
        raw = m.group(1) or ""
        norm = _normalize_css_decl_list(raw)
        norm = _strip_font_family_for_office(norm)
        cls = style_to_class.get(norm)
        if not cls:
            cls = f"ts{len(ordered_styles)}"
            style_to_class[norm] = cls
            ordered_styles.append(norm)
        return f'<span class="{cls}">'

    replaced = _re_span_style_attr.sub(repl, index_html)
    if not ordered_styles:
        return replaced

    css_lines = ["", "/* Deduped text span styles (generated) */"]
    css_lines.extend([f".ts{i} {{{s}}}" for i, s in enumerate(ordered_styles)])

    # Preserve symbol fonts (global Office override uses !important).
    preserve_lines: list[str] = []
    for i, s in enumerate(ordered_styles):
        m_ff = re.search(r"(?:^|;)font-family:([^;]+);", s, flags=re.IGNORECASE)
        if not m_ff:
            continue
        fam = m_ff.group(1).strip()
        if not _is_symbol_font_family(fam):
            continue
        preserve_lines.append(f".page span.ts{i} {{ font-family: {fam} !important; }}")
    if preserve_lines:
        css_lines.extend(["", "/* Preserve symbol fonts (auto-detected). */", *preserve_lines])
    css_blob = "\n".join(css_lines) + "\n"

    insert_at = replaced.lower().find("</style>")
    if insert_at == -1:
        return replaced
    return replaced[:insert_at] + css_blob + replaced[insert_at:]


def _extract_page_divs(full_html: str) -> list[tuple[int, str]]:
    """
    Extract full <div class="page" id="pageN">...</div> blocks from index.html.

    We cannot rely on a naive non-greedy regex because pages may contain nested <div> elements
    (e.g., container metadata divs), which would cause premature termination.
    """
    re_page_open = re.compile(r'(?is)<div\s+class="page"\s+id="page(\d+)"[^>]*>')
    re_div_tag = re.compile(r"(?is)</div>|<div\b")

    out: list[tuple[int, str]] = []
    for m in re_page_open.finditer(full_html):
        pno0 = int(m.group(1))
        start = m.start()
        pos = m.end()
        depth = 1  # we are inside the outer page div
        for t in re_div_tag.finditer(full_html, pos):
            tok = t.group(0).lower()
            if tok.startswith("<div"):
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    end = t.end()
                    out.append((pno0, full_html[start:end]))
                    break
        else:
            raise SystemExit(f"Unclosed page div detected for page{pno0} in index.html")

    return out


def pdf_to_html(
    pdf_path: Path,
    out_dir: Path,
    *,
    page_start_1: Optional[int] = None,
    page_end_1: Optional[int] = None,
    externalize_data_uris: bool = True,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    images_root_dir = out_dir / "images"

    doc = fitz.open(str(pdf_path))
    total = int(doc.page_count)
    start_1 = int(page_start_1) if page_start_1 is not None else 1
    end_1 = int(page_end_1) if page_end_1 is not None else total
    if start_1 < 1 or end_1 < start_1 or end_1 > total:
        raise SystemExit(f"Invalid page range: start={start_1}, end={end_1}, total={total}")
    page_nos = range(start_1 - 1, end_1)

    pages_html: list[str] = []
    page_sizes: list[tuple[float, float]] = []

    for pno in page_nos:
        page = doc[pno]
        w = float(page.rect.width)
        h = float(page.rect.height)
        page_sizes.append((w, h))

        containers = _extract_container_boxes(page, page_no=pno)
        containers_html = _containers_layer_html(containers)
        images, image_src_map = _extract_image_boxes_and_write_files(page, page_no=pno, images_root_dir=images_root_dir)
        images_html = _images_layer_html(images, src_map=image_src_map)
        text_inner = _page_text_inner_html(page)
        text_inner = _annotate_p_tags_with_containers(text_inner, containers)

        pages_html.append(
            "\n".join(
                [
                    f'<div class="page" id="page{pno}" style="width:{w:.4f}pt;height:{h:.4f}pt">',
                    containers_html,
                    images_html,
                    text_inner,
                    "</div>",
                ]
            )
        )

    # Use first page size for @page. (Most slide-like PDFs are consistent.)
    first_w, first_h = page_sizes[0] if page_sizes else (595.0, 842.0)

    index_html = "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh">',
            "<head>",
            '<meta charset="utf-8" />',
            '<meta name="viewport" content="width=device-width, initial-scale=1" />',
            f"<title>{html.escape(pdf_path.name)}</title>",
            "<style>",
            f"@page {{ size: {first_w:.4f}pt {first_h:.4f}pt; margin: 0; }}",
            "html, body { margin: 0; padding: 0; }",
            ".page { position: relative; overflow: hidden; break-after: page; }",
            ".page:last-child { break-after: auto; }",
            "/* Layout container metadata (hidden by default; for LLM/container-boundary reasoning). */",
            ".page .container-layer { position: absolute; left: 0; top: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; display: none; }",
            ".page .container { position: absolute; }",
            "/* Image bbox metadata (hidden by default; for rule 1.6 virtual right-boundary deduction). */",
            ".page .image-layer { position: absolute; left: 0; top: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; display: none; }",
            ".page .image { position: absolute; }",
            ".page p { position: absolute; margin: 0; padding: 0; z-index: 1; white-space: pre; }",
            ".page p.wrap { white-space: normal; }",
            ".page p.preline { white-space: pre-line; }",
            OFFICE_FRIENDLY_FONTS_CSS.strip(),
            "</style>",
            "</head>",
            "<body>",
            "\n".join(pages_html),
            "</body>",
            "</html>",
        ]
    )

    # Optionally externalize any data:image/*;base64,... blobs into assets/ to keep index.html small.
    # This is not required for the downstream pipeline (plan/layout/HTML regen), and can be disabled
    # to keep the bundle structure simpler.
    if externalize_data_uris:
        assets_dir = out_dir / "assets"
        index_html = _externalize_data_image_uris(index_html, assets_dir=assets_dir)
    # Decode non-ASCII numeric character references (e.g. &#x6df1;) to reduce noise for editing.
    index_html = _decode_numeric_charrefs_non_ascii(index_html)
    # Deduplicate repeated <span style="..."> inline styles into CSS classes.
    index_html = _dedupe_span_inline_styles(index_html)

    # Externalize the large inline <style> into styles.css to keep chunk HTML small for LLM editing.
    m_style = re.search(r"(?is)<style>(.*?)</style>", index_html)
    if m_style:
        css_text = (m_style.group(1) or "").strip() + "\n"
        (out_dir / "styles.css").write_text(css_text, encoding="utf-8")
        index_html = index_html[: m_style.start()] + '<link rel="stylesheet" href="styles.css" />' + index_html[m_style.end() :]

    out_path = out_dir / "index.html"
    out_path.write_text(index_html, encoding="utf-8")
    return out_path


def _default_out_dir(pdf_path: Path) -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "result" / f"{pdf_path.stem}_temp" / "ref_html"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert PDF to editable HTML (text layer; hybrid image extract; masked images decoded from xref pixmap)."
    )
    parser.add_argument("pdf", type=str, help="Input PDF path")
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Output directory (default: ./result/<pdf_stem>_temp/ref_html)",
    )
    parser.add_argument(
        "--page-start",
        type=int,
        default=0,
        help="1-based inclusive start page (default: 1)",
    )
    parser.add_argument(
        "--page-end",
        type=int,
        default=0,
        help="1-based inclusive end page (default: last page)",
    )
    parser.add_argument(
        "--no-ai-copy",
        action="store_true",
        help="Do not create an additional <out>_for_AI_editing copy of the bundle.",
    )
    parser.add_argument(
        "--no-assets",
        action="store_true",
        help="Do not externalize data:image;base64 URIs into assets/ (keeps inline data URIs; no assets/ folder).",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    out_dir = Path(args.out).expanduser().resolve() if args.out else _default_out_dir(pdf_path)
    page_start_1 = int(args.page_start) if int(args.page_start or 0) > 0 else None
    page_end_1 = int(args.page_end) if int(args.page_end or 0) > 0 else None
    out_html = pdf_to_html(
        pdf_path=pdf_path,
        out_dir=out_dir,
        page_start_1=page_start_1,
        page_end_1=page_end_1,
        externalize_data_uris=not bool(args.no_assets),
    )

    full = out_html.read_text(encoding="utf-8")
    m_head = re.search(r"(?is)<head>.*?</head>", full)
    head_html = m_head.group(0) if m_head else "<head></head>"
    if "<base" not in head_html.lower():
        head_html = re.sub(r"(?is)<head(\b[^>]*)>", r'<head\1>\n<base href="../">', head_html, count=1)

    page_divs = _extract_page_divs(full)
    if not page_divs:
        raise SystemExit("No page divs found in index.html; cannot split pages.")

    # Always write chunked HTML files (fixed 1 page per chunk) under <out>/chunks/.
    k = 1
    chunks_dir = out_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # page_divs are 0-indexed in ids; keep ordering by page number.
    page_divs_sorted = sorted(page_divs, key=lambda t: t[0])
    total_pages = len(page_divs_sorted)
    for start0 in range(0, total_pages, k):
        end0_excl = min(total_pages, start0 + k)
        # Use actual page numbers from ids so chunk names stay meaningful under page ranges.
        pno_first0 = page_divs_sorted[start0][0]
        pno_last0 = page_divs_sorted[end0_excl - 1][0]
        start_1 = pno_first0 + 1
        end_1 = pno_last0 + 1
        body_parts: list[str] = []
        for _pno0, div in page_divs_sorted[start0:end0_excl]:
            body_parts.extend(["<!-- PAGE_START -->", div, "<!-- PAGE_END -->"])

        chunk_html = "\n".join(
            [
                "<!doctype html>",
                '<html lang="zh">',
                head_html,
                "<body>",
                "\n".join(body_parts),
                "</body>",
                "</html>",
            ]
        )
        (chunks_dir / f"chunk_{start_1:03d}_{end_1:03d}.html").write_text(chunk_html, encoding="utf-8")

    # Optionally create an identical copy for AI editing (do not overwrite if it already exists).
    if not bool(args.no_ai_copy):
        ai_dir = out_dir.parent / f"{out_dir.name}_for_AI_editing"
        if ai_dir.exists():
            print(f"[info] AI editing bundle exists (not overwritten): {ai_dir}")
        else:
            shutil.copytree(out_dir, ai_dir)
            print(f"[info] AI editing bundle created: {ai_dir}")

    # Copy the stable CSS library next to the bundles so chunk HTML can reference ../v3_css_library.css
    css_src = Path(__file__).resolve().parent / "v3_css_library.css"
    css_dst = out_dir.parent / "v3_css_library.css"
    try:
        if css_src.exists():
            shutil.copyfile(css_src, css_dst)
    except Exception:
        # Best-effort; the pipeline can still run if callers handle CSS paths explicitly.
        pass

    print(f"[info] Reference bundle dir: {out_dir}")
    print(str(out_html))


if __name__ == "__main__":
    main()

