from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """You are a “single-page HTML code generator”.

You will receive `meta_json` (JSON) and `layout_notes` (plain text). `meta_json` provides:
- `text_paragraphs`: the bilingual paragraphs that must appear on this page, named as “第N段” (第N段.zh / 第N段.en).
- `images`: the page image list, named as image1/image2/..., with `src` paths and reference sizes (pt).
- `required_refs`: the ONLY allowed reference names for this page (only “第N段” and “imageN”), and the coverage list.
- `page_png`: a rendered image of the page, used to understand the intended layout; pixel-perfect replication is NOT required.

Your task:
- Generate the HTML for THIS page only, following the layout intent in `layout_notes`.

Beauty first (while satisfying all hard + structural constraints):
- The top priority is “beautiful, clear, strong information hierarchy”: balanced layout, consistent alignment, comfortable whitespace,
  and clear visual anchor.
- It does NOT need to match the original exactly; it only needs to satisfy the structure described by `layout_notes_brief`, and read like a good PPT/marketing slide.
- Readability / contrast: ensure strong contrast between text and background (page background or text container background).
  Dark background → light text; light background → dark text. Avoid low-contrast combos (e.g. dark bg + dark text).

CSS library (already loaded in the page):
- You will also receive `css_library_doc` (plain text) describing available classes and typical usage.
- Prefer using these classes to achieve layout and styling; do NOT invent new class names.

Output requirements (VERY IMPORTANT):
- Output HTML only (no explanation, no markdown, no ``` fences).
- Your output MUST start with `<!-- PAGE_START -->` and MUST end with `<!-- PAGE_END -->`.
  Do NOT output any characters outside these two markers.
- Required structure:
  <!-- PAGE_START -->
  <div class="page" id="..."> ... </div>
  <!-- PAGE_END -->
- Inside `<div class="page">`, you MUST use normal flow / flex / grid.
- Units: prefer `pt` (slide-accurate); do NOT use `px` as the primary unit.

Layout priority (MUST follow):
- Highest priority: `layout_notes_brief` (global structure / roles / relative positions). If it explicitly says “left/right/top/bottom/two-column/grid/etc.” you MUST implement that structure.
- Lower priority: `layout_notes_full` (detail reference). You may adjust exact sizes, spacing, alignment, decorations for beauty and readability.
- `page_png` is only inspiration; pixel-perfect replication is NOT required.

Hard constraints (MUST follow):
- No absolute positioning: do NOT use `position:absolute/top/left`. Do NOT output any inline style containing `top:` or `left:`.
  Do NOT reuse the extracted `<p style="top:...;left:...">` structure.
- You may use inline style on a small number of containers for NON-positioning styles such as `grid-template-columns/rows`, `width/height`, `max-width`, etc.
  But `position:absolute/top/left` are strictly forbidden.
- No new CSS: do NOT output `<style>`, and do NOT reference or generate any new CSS files.
- Images MUST use `<img src="...">`: do NOT use `background-image` / `url()` for images. If you need a “background image effect”, use a grid overlay with an `<img>` as the bottom layer.
- Text MUST be used verbatim: each paragraph's zh/en must be copied character-for-character into the page.
  You may split a paragraph across multiple lines/tags, but you must not rewrite the content.
- No extra text:
  - The page may contain ONLY the text provided by `meta_json.text_paragraphs`. Do NOT add/rewrite/extend any other text.
  - Especially forbidden: do NOT read/transcribe/translate any text from `page_png` or from images/imageN (including images[].description_cn).
    (No OCR of image text into the page.)
  - If some information exists only in the image (e.g. names list, table/chart labels), keep it as image content. Do NOT retype it.
- Bilingual rule: Chinese first, English immediately below. Use font size to differentiate, but do NOT use a lighter text color for English.
  - Body English >= 80% of Chinese body size (not smaller)
  - Title English >= 50% of Chinese title size (not smaller)
- If you implement “two columns” for text, implement it via grid/flex (two containers), NOT via CSS multi-column properties (`columns`, `column-count`, `column-width`, etc.).
- Naming / references:
  - In layout_notes and HTML, you may ONLY refer to text as “第N段” and images as “imageN”.
  - Do NOT output any internal IDs (e.g. p1_i0 / p1_c2 / floating:top_left).
- Coverage requirement: every name in `required_refs.all` MUST appear once in the page, and MUST appear as a `data-ref` attribute value:
  - Each text paragraph must correspond to one container element: `data-ref="第N段"`
  - Each image must correspond to one container or `<img>` element: `data-ref="imageN"`
  - Do NOT use any `data-ref` values outside `required_refs.all`. Do NOT duplicate `data-ref` on the same element.
- No scrolling/clipping: do NOT use `overflow:auto/scroll` (including overflow-x/y). All content must be visible within one page.
  For long text, use columns / smaller font / tighter spacing—but never hide content.
"""


_FENCE_RE = re.compile(r"```(?:html)?\s*([\s\S]*?)\s*```", flags=re.IGNORECASE)

_WHITE_BG_RE = re.compile(
    r"(?i)\bbackground(?:-color)?\s*:\s*(?:#fff(?:fff)?|rgb\(\s*255\s*,\s*255\s*,\s*255\s*\)|rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*1(?:\.0+)?\s*\))\s*;?"
)


def _normalize_model_output(raw: str) -> str:
    t = (raw or "").strip()
    if not t:
        return ""
    m = _FENCE_RE.search(t)
    if m:
        return (m.group(1) or "").strip()
    return t


def _normalize_page_background_vars(page_block: str) -> str:
    """
    If the page root sets a literal white background, normalize it to var(--paper)
    so the CSS library tokens remain the single source of truth.
    """
    t = page_block or ""
    m = re.search(r"(?is)<div\b[^>]*\bclass\s*=\s*\"[^\"]*\bpage\b[^\"]*\"[^>]*>", t)
    if not m:
        return t
    tag = m.group(0)
    sm = re.search(r"(?is)\bstyle\s*=\s*\"([^\"]*)\"", tag)
    if not sm:
        return t
    style0 = sm.group(1) or ""
    style1 = _WHITE_BG_RE.sub("background: var(--paper);", style0)
    if style1 == style0:
        return t
    new_tag = tag[: sm.start(1)] + style1 + tag[sm.end(1) :]
    return t.replace(tag, new_tag, 1)


def _import_google_genai():
    """
    Import google-genai SDK.

    - Prefer installed package (`pip install google-genai`).
    - Fallback to local source checkout under repo root `python-genai/`.
    """
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        from google.genai import errors  # type: ignore

        return genai, types, errors
    except Exception:
        repo_root = Path(__file__).resolve().parents[1]
        local_src = repo_root / "python-genai"
        if local_src.exists():
            ps = str(local_src)
            if ps not in sys.path:
                sys.path.insert(0, ps)
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
            from google.genai import errors  # type: ignore

            return genai, types, errors
        raise


def _get_api_key(args: argparse.Namespace) -> str:
    if args.api_key:
        return args.api_key
    for env_name in args.api_key_env:
        v = os.getenv(env_name, "").strip()
        if v:
            return v
    raise SystemExit("Missing API key. Provide --api-key or set one of env vars: " + ", ".join(args.api_key_env))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _extract_page_markup(raw_html: str) -> str | None:
    """
    Extract the single page markup from model output.
    Returns the full marker block:
      <!-- PAGE_START --> ... <!-- PAGE_END -->
    """
    t = _normalize_model_output(raw_html)

    starts = list(re.finditer(r"<!--\s*PAGE_START\s*-->", t, flags=re.I))
    ends = list(re.finditer(r"<!--\s*PAGE_END\s*-->", t, flags=re.I))
    if not starts or not ends:
        return None

    best_inner: str | None = None
    best_score = -10_000.0
    for sm in starts:
        for em in ends:
            if em.start() <= sm.end():
                continue
            inner = (t[sm.end() : em.start()] or "").strip()
            if not inner:
                continue

            # Prefer the block that actually contains the page root.
            score = 0.0
            if re.search(r"(?is)<div\b", inner):
                score += 1.0
            if re.search(r'(?is)\bclass\s*=\s*["\']page\b', inner):
                score += 5.0
            if re.search(r'(?is)\bid\s*=\s*["\']page', inner):
                score += 1.0
            # Prefer larger blocks (real HTML) over tiny marker mentions in prose.
            score += min(4.0, float(len(inner)) / 800.0)

            if score > best_score:
                best_score = score
                best_inner = inner

    if not best_inner:
        return None
    # Guard: if the best match still doesn't look like HTML, treat as missing.
    if not re.search(r'(?is)\bclass\s*=\s*["\']page\b', best_inner):
        return None
    page_block = "<!-- PAGE_START -->\n" + best_inner + "\n<!-- PAGE_END -->\n"
    return _normalize_page_background_vars(page_block)


def _wrap_chunk_html(*, page_block: str, base_href: str, title: str) -> str:
    # Ensure trailing slash for base href.
    bh = (base_href or "").strip()
    if not bh:
        bh = "./"
    if not bh.endswith("/"):
        bh = bh + "/"
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh">',
            "<head>",
            f'<base href="{bh}">',
            '<meta charset="utf-8" />',
            '<meta name="viewport" content="width=device-width, initial-scale=1" />',
            f"<title>{title}</title>",
            '<link rel="stylesheet" href="css_library.css" />',
            "</head>",
            "<body>",
            page_block.strip(),
            "</body>",
            "</html>",
            "",
        ]
    )


def _extract_page_div_from_chunk_html(chunk_html: str) -> str | None:
    """
    Extract the <div class="page" ...>...</div> from a chunk HTML.
    Returns the page root markup (without PAGE_START/PAGE_END markers).
    """
    t = chunk_html or ""
    m = re.search(r"(?is)<!--\s*PAGE_START\s*-->\s*(.*?)\s*<!--\s*PAGE_END\s*-->", t)
    if not m:
        return None
    inner = (m.group(1) or "").strip()
    if not inner:
        return None
    if not re.search(r'(?is)\bclass\s*=\s*["\']page\b', inner):
        return None
    return inner


def _assemble_preview_html(*, out_bundle_dir: Path, title: str) -> Path:
    """
    Assemble all generated chunk page-divs into a single previewable HTML.
    This file is deterministic and is not produced by the model.
    """
    out_bundle_dir = out_bundle_dir.expanduser().resolve()
    chunks_dir = out_bundle_dir / "chunks"
    chunk_files = sorted(chunks_dir.glob("chunk_*.html"))
    pages: list[str] = []
    for cf in chunk_files:
        txt = cf.read_text(encoding="utf-8", errors="replace")
        page_div = _extract_page_div_from_chunk_html(txt)
        if page_div:
            pages.append(page_div)

    html_txt = "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh">',
            "<head>",
            '<base href="./">',
            '<meta charset="utf-8" />',
            '<meta name="viewport" content="width=device-width, initial-scale=1" />',
            f"<title>{title}</title>",
            '<link rel="stylesheet" href="css_library.css" />',
            "</head>",
            "<body>",
            "\n\n".join(pages),
            "</body>",
            "</html>",
            "",
        ]
    )
    out_path = out_bundle_dir / "index.html"
    out_path.write_text(html_txt, encoding="utf-8")
    return out_path


def _rel_base_href(*, from_dir: Path, to_dir: Path) -> str:
    rel = os.path.relpath(str(to_dir), str(from_dir))
    rel = rel.replace("\\", "/")
    if rel == ".":
        return "./"
    return rel


def _validate_page_block(*, meta: dict[str, Any], page_block: str) -> list[str]:
    warnings: list[str] = []
    t = page_block

    # Required refs: must appear as data-ref="...".
    required_all = (((meta.get("required_refs") or {}).get("all") or []) if isinstance(meta.get("required_refs"), dict) else [])
    if not isinstance(required_all, list):
        required_all = []
    required_set = {r for r in required_all if isinstance(r, str) and r}
    missing = [r for r in required_all if isinstance(r, str) and r and f'data-ref="{r}"' not in t]
    if missing:
        warnings.append(f"missing_data_ref: {missing[:30]}{' ...' if len(missing) > 30 else ''}")

    # Any data-ref value must come from required_refs.all.
    data_refs = re.findall(r'data-ref\s*=\s*"([^"]+)"', t)
    unknown_refs = sorted({r for r in data_refs if r and (r not in required_set)})
    if unknown_refs:
        warnings.append(f"unknown_data_ref: {unknown_refs[:30]}{' ...' if len(unknown_refs) > 30 else ''}")

    # Forbidden internal IDs.
    forbidden = sorted(set(re.findall(r"\b(?:p\d+_[A-Za-z_]+\d*|floating:[A-Za-z_]+)\b", t)))
    if forbidden:
        warnings.append(f"forbidden_internal_ids: {forbidden[:30]}{' ...' if len(forbidden) > 30 else ''}")

    # No absolute positioning.
    if re.search(r"position\s*:\s*absolute", t, flags=re.I):
        warnings.append("forbidden_css: position:absolute")
    # Avoid false positives like margin-top/margin-left by excluding preceding '-'.
    if re.search(r"(?<!-)\btop\s*:", t, flags=re.I) or re.search(r"(?<!-)\bleft\s*:", t, flags=re.I):
        warnings.append("forbidden_css: top/left")

    # No style blocks / external css links inside the page block.
    if re.search(r"(?is)<style\b", t):
        warnings.append("forbidden_tag: style")
    if re.search(r"(?is)<link\b[^>]*rel=[\"']stylesheet[\"']", t):
        warnings.append("forbidden_tag: link[rel=stylesheet] inside page block")

    # Images must be <img src="..."> (not background-image/url()).
    if re.search(r"(?i)url\s*\(", t) or re.search(r"(?i)background-image\s*:", t):
        warnings.append("forbidden_css: url()/background-image (use <img> instead)")

    # No scrollbars / hidden overflow.
    if re.search(r"(?i)overflow(?:-x|-y)?\s*:\s*(auto|scroll)\b", t):
        warnings.append("forbidden_css: overflow auto/scroll")

    return warnings


def _call_gemini(
    *,
    client: Any,
    model: str,
    max_output_tokens: int,
    temperature: float,
    user_prompt: str,
    page_png_bytes: bytes | None = None,
    thinking_budget: int | None = None,
) -> str:
    genai, types, _errors = _import_google_genai()
    parts: list[Any] = []
    if page_png_bytes:
        parts.append(types.Part.from_bytes(data=page_png_bytes, mime_type="image/png"))
    parts.append(types.Part.from_text(text=user_prompt))

    thinking_cfg = None
    if thinking_budget is not None:
        thinking_cfg = types.ThinkingConfig(thinking_budget=int(thinking_budget))

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=float(temperature),
            max_output_tokens=int(max_output_tokens),
            thinking_config=thinking_cfg,
        ),
    )
    return response.text or ""


def _build_format_repair_prompt(*, user_prompt: str, last_raw: str) -> str:
    # Keep the repair wrapper short and unambiguous.
    last_raw = (last_raw or "").strip()
    if len(last_raw) > 1500:
        last_raw = last_raw[:1500] + "\n... (truncated)\n"
    return (
        "Your previous output format is invalid (missing PAGE_START/PAGE_END, or not HTML).\n"
        "Please output again, strictly HTML only, and satisfy:\n"
        "1) First line MUST be `<!-- PAGE_START -->`\n"
        "2) Last line MUST be `<!-- PAGE_END -->`\n"
        "3) Do not output any text outside the markers. Do not output ``` fences.\n"
        "\n"
        "Here is the original input (do NOT rewrite the text_paragraphs content):\n\n"
        + user_prompt
        + "\n\n"
        + ("Previous invalid output (for reference only):\n" + last_raw + "\n" if last_raw else "")
    )


def _condense_layout_notes(layout_notes: str) -> tuple[str, str]:
    """
    Return (brief, full) versions of layout notes.
    Brief: keep only the most actionable, high-level structure guidance.
    """
    full = (layout_notes or "").strip()
    if not full:
        return "", ""

    lines = [ln.rstrip() for ln in full.splitlines()]
    keep_sections = {
        # English headings (expected from generate_layout_notes.py)
        "Content checklist (by reference name)",
        "What the page is conveying (one sentence)",
        "Layout intent (skeleton-level, actionable; MUST reference 第N段 / imageN)",
        "Layout intent (skeleton-level, actionable; MUST reference 第N段)",
        "Layout contract",
        "Decorations / parameters",
        # Chinese headings (legacy / robustness)
        "内容清单（按名称/编号）",
        "页面要表达什么（一句话）",
        "排版意图（可执行，必须引用 第N段 / imageN）",
        "排版意图（可执行，必须引用 第N段）",
        "版式契约",
        "装饰与参数",
    }
    out: list[str] = []
    cur_heading: str | None = None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        is_heading = (not s.startswith("-")) and ("：" not in s) and (len(s) <= 60)
        if is_heading:
            cur_heading = s
            if cur_heading in keep_sections:
                out.append(cur_heading)
            continue
        if cur_heading not in keep_sections:
            continue
        # In "Layout intent" keep only lines that mention concrete refs or structure keywords.
        if cur_heading and (cur_heading.startswith("Layout intent") or cur_heading.startswith("排版意图")):
            has_ref = re.search(r"\bimage\d+\b", s) or re.search(r"第\d+段", s)
            has_kw = re.search(
                r"(?i)\b(left|right|top|bottom|center|two[- ]column|two[- ]col|column|columns|grid|card|band|title|header|whitespace|align|stack|row|column)\b",
                s,
            ) or re.search(r"[左右上下]|两栏|两列|分栏|网格|标题|色带", s)
            has_bilingual = re.search(r"(?i)\b(bilingual|english|chinese|80%|50%)\b", s) is not None
            if not (has_ref or has_kw or has_bilingual):
                continue
        out.append(s)

    # Hard cap to keep it compact. If headings didn't match, fall back to top lines.
    brief = "\n".join(out[:60]).strip()
    if not brief:
        brief = "\n".join([ln for ln in lines if ln.strip()][:60]).strip()
    return brief, full


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate per-page chunk HTML (Gemini/Gemma via google-genai).")
    p.add_argument("--bundle-dir", type=str, required=True, help="Original bundle dir (has images/).")
    p.add_argument(
        "--layout-notes-dir",
        type=str,
        required=True,
        help="Directory produced by generate_layout_notes.py (has page_XXX/request.json + layout_notes.txt).",
    )
    p.add_argument("--page-start", type=int, default=1, help="1-based inclusive start page (default: 1)")
    p.add_argument("--page-end", type=int, default=10, help="1-based inclusive end page (default: 10)")
    p.add_argument("--out-bundle-dir", type=str, default="", help="Output bundle dir")
    p.add_argument("--dry-run", action="store_true", help="Do not call model; only write request JSONs.")
    p.add_argument("--print-prompt", action="store_true", help="Print system+user prompt for page-start then exit.")
    p.add_argument(
        "--layout-notes-mode",
        type=str,
        default="brief",
        choices=["brief", "full"],
        help="How much layout_notes to pass into the model prompt (default: brief).",
    )

    p.add_argument("--model", type=str, default="gemini-3.1-flash-lite", help="Model name")
    p.add_argument("--max-tokens", type=int, default=8192, help="Max output tokens (default: 8192)")
    p.add_argument("--temperature", type=float, default=0.0, help="Temperature (default: 0)")
    p.add_argument(
        "--thinking-budget",
        type=int,
        default=-1,
        help="Thinking budget. Use 0 to disable thinking. Negative means not set (default: -1).",
    )
    p.add_argument("--retries", type=int, default=6, help="Retry count on transient 5xx/429 (default: 6)")
    p.add_argument("--retry-base-seconds", type=float, default=2.0, help="Base sleep seconds (default: 2.0)")
    p.add_argument("--retry-max-seconds", type=float, default=60.0, help="Max sleep seconds (default: 60.0)")
    p.add_argument("--retry-backoff", type=float, default=2.0, help="Backoff multiplier (default: 2.0)")

    p.add_argument("--api-key", type=str, default="", help="API key (avoid; prefer env var).")
    p.add_argument(
        "--api-key-env",
        type=str,
        nargs="+",
        default=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        help="Env var names to check for API key (default: GEMINI_API_KEY GOOGLE_API_KEY)",
    )
    p.add_argument("--save-raw", action="store_true", help="Save raw model output per page.")

    p.add_argument("--title", type=str, default="PPTAgent", help="HTML <title> text (default: PPTAgent)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    bundle_dir = Path(args.bundle_dir).expanduser().resolve()
    if not bundle_dir.exists():
        raise SystemExit(f"bundle-dir not found: {bundle_dir}")
    layout_dir = Path(args.layout_notes_dir).expanduser().resolve()
    if not layout_dir.exists():
        raise SystemExit(f"layout-notes-dir not found: {layout_dir}")

    out_bundle_dir = Path(args.out_bundle_dir).expanduser().resolve() if args.out_bundle_dir else (bundle_dir.parent / "html_bundle_out")
    chunks_dir = out_bundle_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # Ensure css_library.css is present in the output bundle root for local viewing.
    css_lib_src = Path(__file__).resolve().parent / "css_library.css"
    if not css_lib_src.exists():
        raise SystemExit(f"css_library.css not found: {css_lib_src}")
    css_lib_dst = out_bundle_dir / "css_library.css"
    if (not css_lib_dst.exists()) or (
        css_lib_dst.read_text(encoding="utf-8", errors="replace") != css_lib_src.read_text(encoding="utf-8", errors="replace")
    ):
        css_lib_dst.write_text(css_lib_src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")

    # Link images directory into output bundle for convenience (no copy).
    src_images = bundle_dir / "images"
    if src_images.exists():
        dst_images = out_bundle_dir / "images"
        if not dst_images.exists():
            try:
                dst_images.symlink_to(src_images, target_is_directory=True)
            except Exception:
                # Fallback: do nothing; chunk base can still point back to bundle_dir.
                pass

    page_start = int(args.page_start)
    page_end = int(args.page_end)
    if page_start <= 0 or page_end <= 0 or page_end < page_start:
        raise SystemExit("--page-start/--page-end must be positive and page_end >= page_start")

    thinking_budget: int | None = int(args.thinking_budget) if int(args.thinking_budget) >= 0 else None

    css_doc_path = Path(__file__).resolve().parent / "css_library.md"
    if not css_doc_path.exists():
        raise SystemExit(f"css_library doc not found: {css_doc_path}")
    css_doc = css_doc_path.read_text(encoding="utf-8", errors="replace").strip()

    # Prepare first page prompt for preview.
    page0_dir = layout_dir / f"page_{page_start:03d}"
    meta0_path = page0_dir / "request.json"
    notes0_path = page0_dir / "layout_notes.txt"
    if not meta0_path.exists() or not notes0_path.exists():
        raise SystemExit(f"missing inputs for page {page_start}: {meta0_path} / {notes0_path}")
    meta0 = _read_json(meta0_path)
    if not isinstance(meta0, dict):
        raise SystemExit(f"invalid meta json: {meta0_path}")
    page_png0_path = meta0.get("page_png_path")
    if not isinstance(page_png0_path, str) or not page_png0_path.strip():
        raise SystemExit(f"missing meta_json.page_png_path for page {page_start}: {meta0_path}")
    page_png0 = Path(page_png0_path).expanduser()
    if not page_png0.exists():
        raise SystemExit(f"page_png not found: {page_png0} (page {page_start})")
    layout_notes0 = notes0_path.read_text(encoding="utf-8", errors="replace").strip()
    notes0_brief, notes0_full = _condense_layout_notes(layout_notes0)
    notes0_for_model = notes0_brief if str(args.layout_notes_mode) == "brief" else notes0_full
    user_prompt0 = (
        "meta_json:\n"
        + json.dumps(meta0, ensure_ascii=False, indent=2)
        + "\n\ncss_library_doc:\n"
        + (css_doc or "")
        + "\n\nlayout_notes_brief:\n"
        + (notes0_brief or "")
        + "\n\nlayout_notes_full:\n"
        + (notes0_full or "")
        + "\n\nlayout_notes_for_generation (follow this, prefer beauty & readability):\n"
        + (notes0_for_model or "")
        + "\n"
    )

    if args.print_prompt:
        print("===== SYSTEM PROMPT =====")
        print(SYSTEM_PROMPT)
        print("\n===== USER PROMPT (page-start) =====")
        print(user_prompt0)
        return 0

    # Always write per-page request bundles (inspectable).
    for pno in range(page_start, page_end + 1):
        pdir = layout_dir / f"page_{pno:03d}"
        meta_path = pdir / "request.json"
        notes_path = pdir / "layout_notes.txt"
        if not meta_path.exists() or not notes_path.exists():
            continue
        meta = _read_json(meta_path)
        if not isinstance(meta, dict):
            continue
        notes = notes_path.read_text(encoding="utf-8", errors="replace").strip()
        out_page_dir = out_bundle_dir / f"page_{pno:03d}"
        out_page_dir.mkdir(parents=True, exist_ok=True)
        _write_json(out_page_dir / "request.json", {"meta_json": meta, "layout_notes": notes})

    if args.dry_run:
        print(str(out_bundle_dir))
        return 0

    api_key = _get_api_key(args)
    genai, _types, genai_errors = _import_google_genai()

    with genai.Client(api_key=api_key) as client:
        for pno in range(page_start, page_end + 1):
            out_page_dir = out_bundle_dir / f"page_{pno:03d}"
            out_page_dir.mkdir(parents=True, exist_ok=True)

            pdir = layout_dir / f"page_{pno:03d}"
            meta_path = pdir / "request.json"
            notes_path = pdir / "layout_notes.txt"
            if not meta_path.exists() or not notes_path.exists():
                continue
            meta = _read_json(meta_path)
            if not isinstance(meta, dict):
                continue
            page_png_path = meta.get("page_png_path")
            if not isinstance(page_png_path, str) or not page_png_path.strip():
                _write_text(out_page_dir / "error.txt", f"missing meta_json.page_png_path for page {pno}\n")
                continue
            page_png = Path(page_png_path).expanduser()
            if not page_png.exists():
                _write_text(out_page_dir / "error.txt", f"page_png not found for page {pno}: {page_png}\n")
                continue
            page_png_bytes = page_png.read_bytes()
            layout_notes = notes_path.read_text(encoding="utf-8", errors="replace").strip()
            notes_brief, notes_full = _condense_layout_notes(layout_notes)
            notes_for_model = notes_brief if str(args.layout_notes_mode) == "brief" else notes_full
            user_prompt = (
                "meta_json:\n"
                + json.dumps(meta, ensure_ascii=False, indent=2)
                + "\n\ncss_library_doc:\n"
                + (css_doc or "")
                + "\n\nlayout_notes_brief:\n"
                + (notes_brief or "")
                + "\n\nlayout_notes_full:\n"
                + (notes_full or "")
                + "\n\nlayout_notes_for_generation (follow this, prefer beauty & readability):\n"
                + (notes_for_model or "")
                + "\n"
            )

            raw: str = ""
            last_err: Exception | None = None
            for attempt in range(max(0, int(args.retries)) + 1):
                try:
                    raw = _call_gemini(
                        client=client,
                        model=str(args.model),
                        max_output_tokens=int(args.max_tokens),
                        temperature=float(args.temperature),
                        user_prompt=user_prompt,
                        page_png_bytes=page_png_bytes,
                        thinking_budget=thinking_budget,
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    retryable = False
                    code = None
                    if isinstance(e, getattr(genai_errors, "APIError", Exception)):
                        try:
                            code = int(getattr(e, "code", 0) or 0)
                        except Exception:
                            code = None
                        retryable = code in {429, 500, 502, 503, 504}
                    if isinstance(e, (TimeoutError, ConnectionError)):
                        retryable = True
                    if (not retryable) or attempt >= int(args.retries):
                        break
                    sleep_s = min(
                        float(args.retry_max_seconds),
                        float(args.retry_base_seconds) * (float(args.retry_backoff) ** float(attempt)),
                    )
                    time.sleep(max(0.0, float(sleep_s)))

            if last_err is not None:
                _write_text(out_page_dir / "error.txt", f"Model call failed after retries. error={type(last_err).__name__}: {last_err}\n")
                if args.save_raw and raw:
                    _write_text(out_page_dir / "raw.txt", raw)
                continue

            page_block = _extract_page_markup(raw)
            if not page_block:
                # One deterministic "format repair" attempt to coerce proper HTML markers.
                repair_prompt = _build_format_repair_prompt(user_prompt=user_prompt, last_raw=raw)
                raw2 = _call_gemini(
                    client=client,
                    model=str(args.model),
                    max_output_tokens=int(args.max_tokens),
                    temperature=float(args.temperature),
                    user_prompt=repair_prompt,
                    page_png_bytes=page_png_bytes,
                    thinking_budget=thinking_budget,
                )
                if args.save_raw and raw2:
                    _write_text(out_page_dir / "raw_repair.txt", raw2)
                page_block = _extract_page_markup(raw2)

            if not page_block:
                _write_text(out_page_dir / "error.txt", "Model output missing PAGE_START/PAGE_END markers.\n\nRAW:\n" + (raw or "") + "\n")
                if args.save_raw and raw:
                    _write_text(out_page_dir / "raw.txt", raw)
                continue

            warnings = _validate_page_block(meta=meta, page_block=page_block)
            if warnings:
                _write_json(out_page_dir / "warnings.json", {"warnings": warnings})

            # Point relative URLs to the output bundle root (which symlinks images/ and contains css_library.css).
            base_href = _rel_base_href(from_dir=chunks_dir, to_dir=out_bundle_dir)
            chunk_html = _wrap_chunk_html(page_block=page_block, base_href=base_href, title=str(args.title))
            chunk_path = chunks_dir / f"chunk_{pno:03d}_{pno:03d}.html"
            _write_text(chunk_path, chunk_html)

            # Also save extracted page block for inspection.
            _write_text(out_page_dir / "page_block.html", page_block)
            if args.save_raw:
                _write_text(out_page_dir / "raw.txt", raw)

    # Assemble a single previewable HTML for the bundle.
    _assemble_preview_html(out_bundle_dir=out_bundle_dir, title=str(args.title))

    print(str(out_bundle_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

