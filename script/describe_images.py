from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lxml import html


SYSTEM_PROMPT = """You are a “single-page image understanding + layout role analysis” assistant.

You will receive:
1) `page_png`: a full-page render (PNG) of the original PDF page. Use it to understand the overall composition and where images sit on the page.
2) `asset_pngs`: the extracted raw image files (PNG) from that page. Use them to see the content of each image clearly.
   They are provided in the same order as `meta_json.images`.
3) `meta_json`: page size info, and for each image: its (pt) size and a coarse deterministic placement label (e.g. top_left / middle_right).

Your output:
- For EACH image in `asset_pngs` (corresponding to `meta_json.images`), output ONE English description string.
- Do NOT describe the `page_png` itself as an output item; only describe the per-image `asset_pngs`.
- Each image description should include:
  - What is in the image (NO OCR; do NOT transcribe or restate any text inside the image; ignore image text).
  - The role of the image on the page (hero visual / supporting illustration / evidence photo / icon / background / chart, etc.).
  - A relative placement suggestion for the new layout (rough position, approximate size, what text it should be near). Relative guidance only—no coordinates.

Important:
- You MUST cover every `meta_json.images[].id`: output exactly one record per id, no missing, no merging.
- If there are many images on the page (e.g. > 8), keep each description concise (prefer <= 120 characters).
- Output MUST be strict JSON only (no extra text, no markdown fences).
"""


_RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["images"],
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "text"],
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
    },
}

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", flags=re.IGNORECASE)
_STRAY_FENCE_LINE_RE = re.compile(r"(?im)^\s*```(?:json)?\s*$")


def _normalize_model_output(raw: str) -> str:
    t = (raw or "").strip()
    if not t:
        return ""
    m = _FENCE_RE.search(t)
    if m:
        return (m.group(1) or "").strip()
    # Some models occasionally append a stray closing fence (```), or emit fences without a matching pair.
    # Strip standalone fence lines and edge fences so JSON parsing remains robust.
    t = _STRAY_FENCE_LINE_RE.sub("", t).strip()
    t = re.sub(r"(?is)^\s*```(?:json)?", "", t).strip()
    t = re.sub(r"(?is)```\s*$", "", t).strip()
    return t


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


def _parse_page_size(css_text: str) -> dict[str, float]:
    m = re.search(r"@page\s*\{\s*size:\s*([0-9.]+)pt\s+([0-9.]+)pt\s*;", css_text)
    if not m:
        return {"w": 959.76, "h": 540.0}
    return {"w": float(m.group(1)), "h": float(m.group(2))}


def _placement_3x3(*, x: float, y: float, w: float, h: float, pw: float, ph: float) -> str:
    cx = (x + w / 2) / pw if pw else 0.5
    cy = (y + h / 2) / ph if ph else 0.5
    horiz = "left" if cx < 0.38 else ("right" if cx > 0.62 else "center")
    vert = "top" if cy < 0.38 else ("bottom" if cy > 0.62 else "middle")
    return f"{vert}_{horiz}"


@dataclass(frozen=True)
class ImageMeta:
    page_1based: int
    page_id: str
    image_id: str
    src: str
    x_pt: float
    y_pt: float
    w_pt: float
    h_pt: float
    page_w_pt: float
    page_h_pt: float
    placement: str


def _iter_page_images(*, bundle_dir: Path, page_div, page_1based: int, page_w_pt: float, page_h_pt: float) -> list[ImageMeta]:
    pid = str(page_div.get("id") or f"page{page_1based-1}")
    out: list[ImageMeta] = []
    for im in page_div.xpath(
        ".//div[contains(concat(' ', normalize-space(@class), ' '), ' image-layer ')]"
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' image ')]"
    ):
        iid = str(im.get("id") or "").strip()
        if not iid:
            continue
        src = str((im.get("data-src") or "")).strip()
        if not src:
            continue
        try:
            x = float(im.get("data-x") or 0.0)
            y = float(im.get("data-y") or 0.0)
            w = float(im.get("data-w") or 0.0)
            h = float(im.get("data-h") or 0.0)
        except Exception:
            continue
        placement = _placement_3x3(x=x, y=y, w=w, h=h, pw=page_w_pt, ph=page_h_pt)
        out.append(
            ImageMeta(
                page_1based=int(page_1based),
                page_id=pid,
                image_id=iid,
                src=src,
                x_pt=float(x),
                y_pt=float(y),
                w_pt=float(w),
                h_pt=float(h),
                page_w_pt=float(page_w_pt),
                page_h_pt=float(page_h_pt),
                placement=str(placement),
            )
        )
    return out


def _call_gemini_describe_page_images(
    *,
    client: Any,
    model: str,
    max_output_tokens: int,
    temperature: float,
    page_png_bytes: bytes,
    asset_pngs: list[tuple[str, bytes]],
    meta: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    genai, types, _errors = _import_google_genai()

    user_text = (
        "meta_json:\n"
        + json.dumps(meta, ensure_ascii=False, indent=2)
        + "\n\n"
        "Below, the page's asset_pngs will be provided in the SAME order as meta_json.images.\n"
        "Output JSON only, schema:\n"
        "{\n"
        '  "images": [\n'
        '    {"id": "p1_i0", "text": "one English description..."},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "Requirements: images.length MUST equal meta_json.images.length, and MUST include every image.id exactly once.\n"
    )

    parts: list[Any] = [
        types.Part.from_bytes(data=page_png_bytes, mime_type="image/png"),
        types.Part.from_text(text=user_text),
    ]
    # Add per-asset labels to reduce ambiguity.
    for image_id, img_bytes in asset_pngs:
        parts.append(types.Part.from_text(text=f"asset_png_for_image_id: {image_id}"))
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Content(
                role="user",
                parts=parts,
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=float(temperature),
            max_output_tokens=int(max_output_tokens),
            response_mime_type="application/json",
            response_json_schema=_RESPONSE_JSON_SCHEMA,
        ),
    )

    raw_text = response.text or ""
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return raw_text, parsed
    try:
        obj = json.loads(_normalize_model_output(raw_text))
        return raw_text, obj if isinstance(obj, dict) else None
    except Exception:
        return raw_text, None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Describe per-page images (multimodal).")
    p.add_argument("--bundle-dir", type=str, required=True, help="Bundle dir with index.html/styles.css/images/")
    p.add_argument(
        "--pages-png-dir",
        type=str,
        required=True,
        help="Directory containing full-page PNGs: page_001.png ... (from render_pages_png.py)",
    )
    p.add_argument("--page-start", type=int, default=1, help="1-based inclusive start page (default: 1)")
    p.add_argument("--page-end", type=int, default=0, help="1-based inclusive end page (default: last)")
    p.add_argument("--out-dir", type=str, default="", help="Output directory (default: <bundle_dir>/image_descriptions)")
    p.add_argument("--dry-run", action="store_true", help="Do not call model; only write request JSONs.")

    p.add_argument("--model", type=str, default="gemini-3.1-flash-lite", help="Model name")
    p.add_argument("--max-tokens", type=int, default=4096, help="Max output tokens (default: 4096)")
    p.add_argument("--temperature", type=float, default=0.0, help="Temperature (default: 0)")
    p.add_argument("--retries", type=int, default=8, help="Retry count on transient 5xx/429 (default: 8)")
    p.add_argument("--retry-base-seconds", type=float, default=2.0, help="Base sleep seconds for retry backoff (default: 2.0)")
    p.add_argument("--retry-max-seconds", type=float, default=60.0, help="Max sleep seconds per retry (default: 60.0)")
    p.add_argument("--retry-backoff", type=float, default=2.0, help="Exponential backoff multiplier (default: 2.0)")

    p.add_argument("--api-key", type=str, default="", help="API key (avoid; prefer env var).")
    p.add_argument(
        "--api-key-env",
        type=str,
        nargs="+",
        default=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        help="Env var names to check for API key (default: GEMINI_API_KEY GOOGLE_API_KEY)",
    )
    p.add_argument("--save-raw", action="store_true", help="Save raw model output next to result JSON.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    bundle_dir = Path(args.bundle_dir).expanduser().resolve()
    pages_png_dir = Path(args.pages_png_dir).expanduser().resolve()
    if not bundle_dir.exists():
        raise SystemExit(f"bundle-dir not found: {bundle_dir}")
    if not pages_png_dir.exists():
        raise SystemExit(f"pages-png-dir not found: {pages_png_dir}")

    index_path = bundle_dir / "index.html"
    css_path = bundle_dir / "styles.css"
    if not index_path.exists():
        raise SystemExit(f"index.html not found: {index_path}")
    if not css_path.exists():
        raise SystemExit(f"styles.css not found: {css_path}")

    css_text = css_path.read_text(encoding="utf-8", errors="replace")
    page_size = _parse_page_size(css_text)
    pw, ph = float(page_size["w"]), float(page_size["h"])

    doc = html.fromstring(index_path.read_text(encoding="utf-8", errors="replace"))
    pages = doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' page ')]")
    if not pages:
        raise SystemExit("No .page divs found in index.html")

    page_start = max(1, int(args.page_start))
    page_end = int(args.page_end) if int(args.page_end or 0) > 0 else len(pages)
    page_end = min(page_end, len(pages))
    if page_end < page_start:
        raise SystemExit("page-end must be >= page-start")

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else (bundle_dir / "image_descriptions")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build per-page requests first (always written) so you can inspect inputs.
    page_requests: list[tuple[int, list[ImageMeta], dict[str, Any]]] = []
    for page_1based in range(page_start, page_end + 1):
        page_div = pages[page_1based - 1]
        metas = _iter_page_images(bundle_dir=bundle_dir, page_div=page_div, page_1based=page_1based, page_w_pt=pw, page_h_pt=ph)
        if not metas:
            # Skip pages with no embedded asset images.
            continue

        page_png_path = pages_png_dir / f"page_{page_1based:03d}.png"
        images_meta = [
            {
                "id": m.image_id,
                "src": m.src,
                "bbox_pt": {"x": m.x_pt, "y": m.y_pt, "w": m.w_pt, "h": m.h_pt},
                "size_pt": {"w": m.w_pt, "h": m.h_pt},
                "placement": m.placement,
                "area_ratio": (m.w_pt * m.h_pt) / (m.page_w_pt * m.page_h_pt) if (m.page_w_pt and m.page_h_pt) else None,
            }
            for m in metas
        ]
        req = {
            "page_1based": int(page_1based),
            "page_id": str(metas[0].page_id) if metas else f"page{page_1based-1}",
            "page_size_pt": {"w": float(pw), "h": float(ph)},
            "images": images_meta,
            "inputs": {
                "page_png": str(page_png_path),
                "asset_pngs": [str(bundle_dir / m.src) for m in metas],
            },
        }
        page_requests.append((page_1based, metas, req))

        page_out_dir = out_dir / f"page_{page_1based:03d}"
        page_out_dir.mkdir(parents=True, exist_ok=True)
        (page_out_dir / "request.json").write_text(json.dumps(req, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.dry_run:
        print(str(out_dir))
        return 0

    api_key = _get_api_key(args)
    genai, _types, genai_errors = _import_google_genai()

    with genai.Client(api_key=api_key) as client:
        for page_1based, metas, req in page_requests:
            page_png_path = Path(req["inputs"]["page_png"])
            asset_paths = [Path(p) for p in (req["inputs"].get("asset_pngs") or [])]
            if not page_png_path.exists():
                raise SystemExit(f"missing page_png: {page_png_path}")
            missing_assets = [p for p in asset_paths if not p.exists()]
            if missing_assets:
                raise SystemExit(f"missing asset_png(s): {missing_assets[:5]}{' ...' if len(missing_assets) > 5 else ''}")

            asset_pngs: list[tuple[str, bytes]] = []
            for m in metas:
                asset_path = bundle_dir / m.src
                asset_pngs.append((m.image_id, asset_path.read_bytes()))

            raw: str = ""
            out_obj: dict[str, Any] | None = None
            last_err: Exception | None = None
            for attempt in range(max(0, int(args.retries)) + 1):
                try:
                    raw, out_obj = _call_gemini_describe_page_images(
                        client=client,
                        model=str(args.model),
                        max_output_tokens=int(args.max_tokens),
                        temperature=float(args.temperature),
                        page_png_bytes=page_png_path.read_bytes(),
                        asset_pngs=asset_pngs,
                        meta=req,
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
                    # Some transient network errors may not be APIError.
                    if isinstance(e, (TimeoutError, ConnectionError)):
                        retryable = True

                    if (not retryable) or attempt >= int(args.retries):
                        break

                    sleep_s = min(
                        float(args.retry_max_seconds),
                        float(args.retry_base_seconds) * (float(args.retry_backoff) ** float(attempt)),
                    )
                    time.sleep(max(0.0, float(sleep_s)))

            page_out_dir = out_dir / f"page_{page_1based:03d}"
            page_out_dir.mkdir(parents=True, exist_ok=True)

            if last_err is not None:
                (page_out_dir / "error.txt").write_text(
                    f"Model call failed after retries. error={type(last_err).__name__}: {last_err}\n",
                    encoding="utf-8",
                )
                if args.save_raw and raw:
                    (page_out_dir / "raw.txt").write_text(raw or "", encoding="utf-8")
                continue

            if not isinstance(out_obj, dict) or not isinstance(out_obj.get("images"), list):
                (page_out_dir / "error.txt").write_text(
                    "Model output is not valid JSON with field 'images' (array).\n\nRAW:\n" + (raw or "") + "\n",
                    encoding="utf-8",
                )
                continue

            (page_out_dir / "result.json").write_text(json.dumps(out_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # Fan out per-image text files.
            by_id: dict[str, str] = {}
            for it in out_obj.get("images") or []:
                if isinstance(it, dict) and isinstance(it.get("id"), str) and isinstance(it.get("text"), str):
                    by_id[it["id"]] = it["text"].strip()
            for m in metas:
                txt = by_id.get(m.image_id)
                if txt:
                    (page_out_dir / f"{m.image_id}.text.txt").write_text(txt + "\n", encoding="utf-8")
                else:
                    (page_out_dir / f"{m.image_id}.missing.txt").write_text("missing description for this image id\n", encoding="utf-8")
            if args.save_raw:
                (page_out_dir / "raw.txt").write_text(raw or "", encoding="utf-8")

    print(str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

