from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional

from lxml import html

DEFAULT_IMAGE_DISPLAY_SCALE = 0.85


OFFICE_FRIENDLY_FONT_STACK = [
    "Calibri",
    "Arial",
    "Songti SC",
    "PingFang SC",
    "Hiragino Sans GB",
    "Heiti SC",
    "Microsoft YaHei",
    "SimSun",
    "sans-serif",
]


def _class_token_pred(cls: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {cls} ')"


def _parse_page_size(css_text: str) -> dict[str, float]:
    m = re.search(r"@page\s*\{\s*size:\s*([0-9.]+)pt\s+([0-9.]+)pt\s*;", css_text)
    if not m:
        return {"w": 959.76, "h": 540.0}
    return {"w": float(m.group(1)), "h": float(m.group(2))}


def _parse_ts_styles(css_text: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in re.finditer(r"\.ts(\d+)\s*\{([^}]*)\}", css_text):
        cls = f"ts{m.group(1)}"
        body = m.group(2)
        st: dict[str, Any] = {}
        mfs = re.search(r"font-size:([0-9.]+)pt", body)
        if mfs:
            st["font_size_pt"] = float(mfs.group(1))
        mc = re.search(r"color:([^;]+);", body)
        if mc:
            st["color"] = mc.group(1).strip()
        out[cls] = st
    return out


def _parse_style_pt(style: str, key: str) -> Optional[float]:
    if not style:
        return None
    m = re.search(rf"\b{re.escape(key)}\s*:\s*([0-9.]+)pt", style)
    return float(m.group(1)) if m else None


def _normalize_text(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _looks_like_noise(text: str) -> bool:
    t = _normalize_text(text)
    if not t:
        return True
    if re.fullmatch(r"[\s\.:。·•●⚫…，,、;；!！?？\-—_]+", t):
        return True
    return False


BULLETS = {"⚫", "•", "●", "·"}


def _first_ts_class(class_attr: Optional[str]) -> Optional[str]:
    if not class_attr:
        return None
    for part in class_attr.split():
        if part.startswith("ts"):
            return part
    return None


def _extract_runs(p_el, ts_styles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []

    def emit(text: str, bold: bool, ts: Optional[str]) -> None:
        text = (text or "").replace("\n", " ")
        if not text or text.strip() == "":
            return
        st = ts_styles.get(ts or "", {})
        run: dict[str, Any] = {
            "text": text,
            "bold": bool(bold),
        }
        if "color" in st:
            run["color"] = st["color"]
        if "font_size_pt" in st:
            run["font_size_pt"] = st["font_size_pt"]

        if runs:
            prev = runs[-1]
            if (
                prev.get("bold") == run.get("bold")
                and prev.get("color") == run.get("color")
                and prev.get("font_size_pt") == run.get("font_size_pt")
            ):
                prev["text"] += run["text"]
                return
        runs.append(run)

    def walk(node, bold: bool, ts: Optional[str]) -> None:
        if getattr(node, "text", None):
            emit(node.text, bold, ts)
        for child in node:
            tag = (child.tag or "").lower() if isinstance(child.tag, str) else ""
            child_bold = bold or (tag in ("b", "strong"))
            child_ts = ts
            if tag == "span":
                child_ts = _first_ts_class(child.get("class")) or child_ts
            walk(child, child_bold, child_ts)
            if getattr(child, "tail", None):
                emit(child.tail, bold, ts)

    walk(p_el, False, None)
    return [r for r in runs if r.get("text", "").strip() != ""]


def _runs_plain_text(runs: list[dict[str, Any]]) -> str:
    return _normalize_text("".join(r.get("text", "") for r in runs))


def _strip_leading_bullet(runs: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    if not runs:
        return False, runs
    out: list[dict[str, Any]] = []
    removed = False
    for r in runs:
        t = r.get("text", "")
        if not removed:
            t2 = t.lstrip()
            if t2 and t2[0] in BULLETS:
                removed = True
                t2 = t2[1:].lstrip()
                if t2:
                    r2 = dict(r)
                    r2["text"] = t2
                    out.append(r2)
                continue
            if t.strip() in BULLETS:
                removed = True
                continue
        out.append(r)
    return removed, out


def _join_runs(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not a:
        return list(b)
    if not b:
        return list(a)

    def last_visible_char(runs: list[dict[str, Any]]) -> str:
        for r in reversed(runs):
            t = r.get("text", "")
            for ch in reversed(t):
                if not ch.isspace():
                    return ch
        return ""

    def first_visible_char(runs: list[dict[str, Any]]) -> str:
        for r in runs:
            t = r.get("text", "")
            for ch in t:
                if not ch.isspace():
                    return ch
        return ""

    out = list(a)
    last = last_visible_char(a)
    first = first_visible_char(b)
    if last and first and last.isascii() and first.isascii() and last.isalnum() and first.isalnum():
        out.append({"text": " ", "bold": False})
    out.extend(b)
    return out


def _max_font_size(runs: list[dict[str, Any]]) -> float:
    mx = 0.0
    for r in runs:
        fs = r.get("font_size_pt")
        if isinstance(fs, (int, float)):
            mx = max(mx, float(fs))
    return mx


def _classify_block(runs: list[dict[str, Any]]) -> str:
    fs = _max_font_size(runs)
    bold = any(bool(r.get("bold")) for r in runs)
    if fs >= 40:
        return "title"
    if fs >= 30:
        return "heading"
    if fs >= 22 and bold:
        return "subheading"
    if fs >= 20 and bold:
        return "label"
    return "body"


def _lines_to_blocks(
    lns: list[dict[str, Any]],
    *,
    paragraph_left_tol: float,
    paragraph_gap_mult: float,
) -> list[dict[str, Any]]:
    """
    Convert sorted line objects into semantic blocks deterministically.

    - Bullet blocks: {"kind":"bullets","items":[{"text":...}, ...]}
    - Paragraph/title/etc blocks: {"kind":..., "text": ...}

    NOTE: We intentionally do NOT emit rich text runs; downstream steps only need plain text.
    """
    blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(lns):
        ln = lns[i]
        is_bul, _runs2 = _strip_leading_bullet(ln["runs"])
        if is_bul:
            items: list[dict[str, Any]] = []
            bullet_left = float(ln.get("left") or 0.0)

            def consume_one_bullet(start_idx: int) -> tuple[int, list[dict[str, Any]]]:
                first_ln = lns[start_idx]
                _, rr = _strip_leading_bullet(first_ln["runs"])
                item_runs = rr
                j = start_idx + 1
                while j < len(lns):
                    nxt = lns[j]
                    is_bul2, _ = _strip_leading_bullet(nxt["runs"])
                    if is_bul2:
                        break
                    if float(nxt.get("left") or 0.0) >= bullet_left + 8:
                        item_runs = _join_runs(item_runs, nxt["runs"])
                        j += 1
                        continue
                    break
                return j, item_runs

            j, item_runs = consume_one_bullet(i)
            txt = _runs_plain_text(item_runs)
            if txt:
                items.append({"text": txt})

            k = j
            while k < len(lns):
                nxt = lns[k]
                is_bul3, _ = _strip_leading_bullet(nxt["runs"])
                if not is_bul3:
                    break
                k2, item_runs2 = consume_one_bullet(k)
                txt2 = _runs_plain_text(item_runs2)
                if txt2:
                    items.append({"text": txt2})
                k = k2

            if items:
                blocks.append({"kind": "bullets", "items": items})
            i = k
            continue

        # paragraph merge (same left + tight vertical spacing until strong-ending punctuation)
        para_runs = ln["runs"]
        kind0 = _classify_block(para_runs)
        left0 = float(ln.get("left") or 0.0)
        top0 = float(ln.get("top") or 0.0)
        lh0 = float(ln.get("lh") or 0.0)
        j = i + 1
        while j < len(lns):
            # Titles/headings should stay as-is (often intentionally line-broken).
            if kind0 in {"title", "heading"}:
                break
            nxt = lns[j]
            is_bul2, _ = _strip_leading_bullet(nxt["runs"])
            if is_bul2:
                break
            # Only merge within the same "kind" to avoid mixing title/subtitle/body.
            if _classify_block(nxt["runs"]) != kind0:
                break
            if abs(float(nxt.get("left") or 0.0) - left0) > paragraph_left_tol:
                break
            if lh0 and (float(nxt.get("top") or 0.0) - top0) > (lh0 * paragraph_gap_mult):
                break
            prev_text = _runs_plain_text(para_runs)
            if prev_text.endswith(("。", "！", "？", "；", ":", "：")):
                break
            para_runs = _join_runs(para_runs, nxt["runs"])
            top0 = float(nxt.get("top") or 0.0)
            lh0 = float(nxt.get("lh") or 0.0) or lh0
            j += 1

        txt = _runs_plain_text(para_runs)
        if txt:
            blocks.append({"kind": kind0, "text": txt})
        i = j

    return blocks


def _area(bb: dict[str, float]) -> float:
    return float(bb.get("w") or 0.0) * float(bb.get("h") or 0.0)


def _placement_from_bbox(bb: dict[str, float], pw: float, ph: float) -> str:
    x = float(bb.get("x") or 0)
    y = float(bb.get("y") or 0)
    w = float(bb.get("w") or 0)
    h = float(bb.get("h") or 0)
    wr = w / pw if pw else 0
    hr = h / ph if ph else 0
    cx = (x + w / 2) / pw if pw else 0.5
    cy = (y + h / 2) / ph if ph else 0.5

    if wr >= 0.95 and hr >= 0.95:
        return "full"
    if wr >= 0.9 and hr <= 0.35:
        if cy < 0.35:
            return "band_top"
        if cy > 0.65:
            return "band_bottom"
        return "band_middle"

    horiz = "left" if cx < 0.38 else ("right" if cx > 0.62 else "center")
    vert = "top" if cy < 0.38 else ("bottom" if cy > 0.62 else "middle")
    return f"{vert}_{horiz}"


def _size_bucket(bb: dict[str, float], pw: float, ph: float) -> str:
    ar = _area(bb) / (pw * ph)
    if ar >= 0.35:
        return "xl"
    if ar >= 0.18:
        return "lg"
    if ar >= 0.08:
        return "md"
    return "sm"


def _palette_from_containers(containers: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, float] = {}
    for c in containers:
        fill = c.get("fill")
        if not fill:
            continue
        total[fill] = total.get(fill, 0.0) + _area(c.get("bbox") or {})
    colors = [c for c, _ in sorted(total.items(), key=lambda kv: kv[1], reverse=True)]
    out: dict[str, Any] = {}
    if colors:
        out["primary"] = colors[0]
        out["fills"] = colors[:8]
    return out


_re_page_id = re.compile(r"^page(\d+)$", flags=re.IGNORECASE)
_re_chunk_name = re.compile(r"^chunk_(\d{3})_(\d{3})\.html$", flags=re.IGNORECASE)


def _page_number_0based(page_id: str) -> int:
    m = _re_page_id.match(page_id or "")
    return int(m.group(1)) if m else 0


def _infer_chunk_rel(bundle_dir: Path, page_1based: int) -> str:
    chunks_dir = bundle_dir / "chunks"
    if chunks_dir.exists():
        candidates: list[tuple[int, str]] = []
        for cf in chunks_dir.glob("chunk_*.html"):
            m = _re_chunk_name.match(cf.name)
            if not m:
                continue
            start_1 = int(m.group(1))
            end_1 = int(m.group(2))
            if start_1 <= page_1based <= end_1:
                # Prefer the most specific chunk (smallest range).
                candidates.append((end_1 - start_1, str(cf.relative_to(bundle_dir))))
        if candidates:
            return sorted(candidates, key=lambda x: (x[0], x[1]))[0][1]
    return f"chunks/chunk_{page_1based:03d}_{page_1based:03d}.html"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a coordinate-free plan.json from a reference HTML bundle.")
    parser.add_argument(
        "bundle",
        type=str,
        help="Bundle directory containing index.html + styles.css (+ images/ + assets/).",
    )
    parser.add_argument("--out", type=str, default="", help="Output plan.json path (default: <bundle>/plan.json)")
    args = parser.parse_args()

    bundle_dir = Path(args.bundle).expanduser().resolve()
    if bundle_dir.is_file():
        bundle_dir = bundle_dir.parent
    index_path = bundle_dir / "index.html"
    css_path = bundle_dir / "styles.css"
    if not index_path.exists():
        raise SystemExit(f"index.html not found: {index_path}")
    if not css_path.exists():
        raise SystemExit(f"styles.css not found: {css_path}")

    out_path = Path(args.out).expanduser().resolve() if args.out else (bundle_dir / "plan.json")

    css_text = css_path.read_text(encoding="utf-8", errors="replace")
    page_size = _parse_page_size(css_text)
    pw, ph = float(page_size["w"]), float(page_size["h"])
    ts_styles = _parse_ts_styles(css_text)

    doc = html.fromstring(index_path.read_text(encoding="utf-8", errors="replace"))
    pages = doc.xpath(f"//div[{_class_token_pred('page')}]")

    pages_out: list[dict[str, Any]] = []

    for pg in pages:
        pid = pg.get("id") or ""

        # containers for palette and group placement/size
        containers: list[dict[str, Any]] = []
        container_by_id: dict[str, dict[str, Any]] = {}
        for c in pg.xpath(f".//div[{_class_token_pred('container-layer')}]//div[{_class_token_pred('container')}]"):
            cid = c.get("id")
            if not cid:
                continue
            x = float(c.get("data-x") or 0.0)
            y = float(c.get("data-y") or 0.0)
            w = float(c.get("data-w") or 0.0)
            h = float(c.get("data-h") or 0.0)
            fill = (c.get("data-fill") or "").strip() or None
            bb = {"x": x, "y": y, "w": w, "h": h}
            obj: dict[str, Any] = {"id": cid, "bbox": bb}
            if fill:
                obj["fill"] = fill
            containers.append(obj)
            container_by_id[cid] = obj

        palette = _palette_from_containers(containers)

        # images: require data-src (no SVG/base64 parsing here)
        images_out: list[dict[str, Any]] = []
        for im in pg.xpath(f".//div[{_class_token_pred('image-layer')}]//div[{_class_token_pred('image')}]"):
            iid = im.get("id")
            if not iid:
                continue
            src = (im.get("data-src") or "").strip()
            if not src:
                raise SystemExit(
                    f"Missing data-src for image '{iid}' on '{pid}'. "
                    f"Regenerate the bundle using the updated pdf_to_html.py which writes images/ and data-src."
                )
            x = float(im.get("data-x") or 0.0)
            y = float(im.get("data-y") or 0.0)
            w = float(im.get("data-w") or 0.0)
            h = float(im.get("data-h") or 0.0)
            bb = {"x": x, "y": y, "w": w, "h": h}
            img_out: dict[str, Any] = {
                "id": iid,
                "src": src,
                "placement": _placement_from_bbox(bb, pw, ph),
                "size": _size_bucket(bb, pw, ph),
            }
            # Keep reference size hints (no position/bbox) for better downstream sizing decisions.
            if w > 0 and h > 0:
                img_out["display_scale"] = DEFAULT_IMAGE_DISPLAY_SCALE
                img_out["display_w_pt"] = round(w * DEFAULT_IMAGE_DISPLAY_SCALE, 2)
                img_out["display_h_pt"] = round(h * DEFAULT_IMAGE_DISPLAY_SCALE, 2)
            images_out.append(img_out)

        # Extract and group text
        lines_by_container: dict[str, list[dict[str, Any]]] = {}
        floating_lines: list[dict[str, Any]] = []

        for p in pg.xpath(".//p"):
            runs = _extract_runs(p, ts_styles)
            text = _runs_plain_text(runs)
            if _looks_like_noise(text):
                continue

            pstyle = p.get("style") or ""
            top = _parse_style_pt(pstyle, "top") or 0.0
            left = _parse_style_pt(pstyle, "left") or 0.0
            lh = _parse_style_pt(pstyle, "line-height") or 0.0
            dc = p.get("data-container")

            ln = {"top": top, "left": left, "lh": lh, "runs": runs}
            if dc and dc in container_by_id:
                lines_by_container.setdefault(dc, []).append(ln)
            else:
                floating_lines.append(ln)

        def container_sort_key(cid: str) -> tuple[float, float]:
            bb = container_by_id[cid].get("bbox") or {}
            return (float(bb.get("y") or 0.0), float(bb.get("x") or 0.0))

        groups: list[dict[str, Any]] = []
        for cid in sorted(lines_by_container.keys(), key=container_sort_key):
            lns = sorted(lines_by_container[cid], key=lambda x: (x["top"], x["left"]))
            blocks = _lines_to_blocks(lns, paragraph_left_tol=3.0, paragraph_gap_mult=1.9)

            cobj = container_by_id[cid]
            bb = cobj.get("bbox") or {}
            group_placement = _placement_from_bbox(bb, pw, ph)
            g: dict[str, Any] = {
                "group_id": cid,
                "placement": group_placement,
                "size": _size_bucket(bb, pw, ph),
                "blocks": blocks,
            }
            if cobj.get("fill"):
                g["fill"] = cobj["fill"]
            # Propagate approximate placement to each block to make downstream layout easier,
            # while still keeping the plan coordinate-free.
            for blk in g.get("blocks") or []:
                if isinstance(blk, dict) and "placement" not in blk:
                    blk["placement"] = group_placement
            groups.append(g)

        # floating groups: bucket by approximate placement; keep blocks in natural order
        floating_groups: list[dict[str, Any]] = []
        if floating_lines:
            tmp: dict[str, list[dict[str, Any]]] = {}
            for ln in floating_lines:
                px = (ln["left"] / pw) if pw else 0.5
                py = (ln["top"] / ph) if ph else 0.5
                horiz = "left" if px < 0.38 else ("right" if px > 0.62 else "center")
                vert = "top" if py < 0.38 else ("bottom" if py > 0.62 else "middle")
                plc = f"{vert}_{horiz}"
                tmp.setdefault(plc, []).append(ln)

            for plc, lns in sorted(tmp.items(), key=lambda kv: kv[0]):
                lns = sorted(lns, key=lambda x: (x["top"], x["left"]))
                # Floating text is often line-wrapped paragraphs; use a slightly looser left tolerance.
                blocks = _lines_to_blocks(lns, paragraph_left_tol=8.0, paragraph_gap_mult=1.9)
                if blocks:
                    for blk in blocks:
                        blk["placement"] = plc
                    floating_groups.append({"group_id": f"floating:{plc}", "placement": plc, "blocks": blocks})

        page_out: dict[str, Any] = {
            "page_id": pid,
            "layout_notes": "",
            **({"palette": palette} if palette else {}),
            **({"images": images_out} if images_out else {}),
            **({"groups": groups} if groups else {}),
            **({"floating_groups": floating_groups} if floating_groups else {}),
        }
        pages_out.append(page_out)

    plan = {
        "source": {
            "ref_html": str(index_path),
            "assets_dir": "assets",
            "images_dir": "images",
        },
        "constraints": {
            "page_size_fixed_pt": page_size,
            "no_absolute_positioning": True,
            "image_ref_scale_range": {
                "min": 0.85,
                "max": 0.85,
                "relative_to": "images[].display_w_pt/display_h_pt",
            },
            "text_overflow_policy": {
                "priority": "no_overflow",
                "autoshrink": {
                    "enabled": True,
                    "mode": "shrink_only",
                    "min_font_pt": 11.0,
                },
            },
            "background_decorations": {
                "allowed": True,
                "max_coverage_ratio": 0.4,
                "must_be_background_only": True,
            },
            "css_library": {
                "policy": "prefer_library_only",
                "path_hint": "testHTMLPathV3/v3_css_library.css",
            },
            "fonts": {
                "policy": "office_compatible_only",
                "stack": OFFICE_FRIENDLY_FONT_STACK,
                "css": '.page, .page * { font-family: Calibri, Arial, "Songti SC", "PingFang SC", "Hiragino Sans GB", "Heiti SC", "Microsoft YaHei", SimSun, sans-serif !important; }',
            },
            "notes": [
                "Page size must match the original PDF exactly (pt).",
                "In the generated HTML, do not use position:absolute/top/left on content elements; use only grid/flex/normal flow.",
                "All text must use an Office-compatible font stack (no @font-face / custom fonts).",
                "In plan.json, images[].src comes from HTML image-layer[data-src]; do not parse SVG/base64.",
                "Image display sizes in the new layout should be based on the reference HTML size and uniformly scaled to 0.85; only write display_w_pt/display_h_pt in images[] for direct use (do not output ref_*).",
                "Background decorations can be more aggressive: you may use pure CSS background blocks/gradients covering ~30–40% of the page; but they must remain background-only and must not cover any text/images.",
                "Text must not overflow: you may typeset with a larger, comfortable font size first; before exporting, use shrink-only autoshrink as a fallback (only shrink when overflow occurs).",
                "Prefer a fixed CSS class library (e.g., testHTMLPathV3/v3_css_library.css); when generating HTML, prefer existing class names and do not create new CSS ad-hoc.",
                "Treat each page as a presentation slide: all information points must be covered; re-layout for aesthetics is allowed.",
                "Use pages[].palette and groups[].fill (if present) as tone/mood references.",
                "Always ignore any text inside images (do not extract/translate/restate).",
                "This plan is the baseline: it only contains text grouping, image paths, and coarse placement/size hints; it contains no layout decisions. Later, the model will add per-page layout notes to ensure aesthetics.",
                "This plan only provides content grouping and coarse placement hints; it outputs no coordinates.",
            ],
        },
        "pages": pages_out,
    }

    out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    print(str(out_path))


if __name__ == "__main__":
    main()

