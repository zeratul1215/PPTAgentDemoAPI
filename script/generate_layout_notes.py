from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


_ZH_CHARS_PER_LINE_EST = 28  # Approx for ~12-14pt body in a single column.
_EN_CHARS_PER_LINE_EST = 55  # Approx for ~80% font size English in a single column.
_LONG_TEXT_LINES_THRESHOLD = 30.0  # Conservative: only trigger for obviously text-heavy pages.
_TEXT_AREA_RATIO_MIN = 0.35  # Avoid extreme amplification when images cover most of the page.


SYSTEM_PROMPT_WITH_IMAGES = """You are a “single-page HTML layout plan (layout_notes_brief) generator”.

Background:
- You are generating a **short** layout plan (layout_notes_brief) for ONE page, for downstream automatic HTML generation that must NOT use absolute positioning.
- You will receive `page_png`: a full-page render of this page. It is only for layout inspiration and composition reference; you do NOT need to replicate it.
  OCR is forbidden. Do NOT read text from images to add content.
- You will receive `meta_json`: includes `text_paragraphs` (第N段, bilingual zh/en paragraphs),
  `images` (imageN, with src + reference sizes + image descriptions), and `required_refs` (the ONLY allowed reference names).

Your task:
- Output `layout_notes_brief` for this page. Write only skeleton-level info (structure / roles / relative positions),
  so the downstream HTML generator can freely refine details while keeping the page beautiful.

Hard constraints (MUST follow):
- No hallucination: do NOT add any new facts or text points beyond `meta_json`. Do NOT guess any image text from `page_png`.
- Naming / references:
  - To refer to text, you may ONLY use “第N段”.
  - To refer to images, you may ONLY use “imageN”.
  - Do NOT output any internal IDs (e.g. p1_i0 / p1_c2 / floating:top_left).
- Coverage: every name listed in `meta_json.required_refs.all` MUST appear at least once in your output.
- Relative layout only, no coordinates: use “left/right/top/bottom/center/two-column/grid/cards/band/whitespace/alignment”, etc.
  Do NOT mention top/left/x/y/pixels/pt coordinates.
- Bilingual rule: just state “English follows Chinese; differentiate by font size but do NOT fade the color
  (body ≥ 80%, titles ≥ 50%)”. Do NOT give specific font size numbers.
- If there are many images (`meta_json.heuristics.images_count > 5`): only state “images are shown as a wall/grid + a title area”;
  do NOT provide per-image fine-grained instructions.

Soft suggestion (recommended):
- Readability / contrast: remind downstream HTML generation to ensure strong contrast between text and backgrounds
  (page background or text container background). Dark background → light text; light background → dark text.
  Avoid low-contrast combinations.

Output format (VERY IMPORTANT):
- Output plain text only (no JSON, no explanation, no markdown code fences).
- Only 3–6 short paragraphs / bullet items. Avoid overly detailed parameters.

Recommended structure (you may omit sections if unnecessary):
1) Content checklist (by reference name)
2) What the page is conveying (one sentence)
3) Layout intent (skeleton-level, actionable; MUST reference 第N段 / imageN)
4) Layout contract (role allocation + global alignment/repetition rules)
"""


SYSTEM_PROMPT_NO_IMAGES = """You are a “single-page layout plan (layout_notes_brief) generator”.

Background:
- You are generating a layout plan (layout_notes_brief) for ONE page, for downstream automatic HTML generation that must NOT use absolute positioning.
- You will receive `page_png`: a full-page render of this page. It is only for layout inspiration and composition reference
  (e.g. rhythm, whitespace, title placement tendency). The final layout does NOT need to be identical to `page_png`.
  Follow `meta_json` content and constraints as the source of truth. OCR is forbidden. Do NOT read text from images.
- You will receive `meta_json`: page meta (page_id / page_size_pt / palette.primary), `text_paragraphs` (第N段, bilingual zh/en paragraphs),
  and `required_refs` (the ONLY allowed reference names).

Your task:
- Output `layout_notes_brief` for this page. Write only skeleton-level info (structure / roles / relative positions),
  so the downstream HTML generator can freely refine details while keeping the page beautiful.

Hard constraints (MUST follow):
- No hallucination: do NOT add any new facts or text points beyond `meta_json`. Do NOT guess or fill in any missing text.
- Naming / references: you may ONLY refer to text as “第N段” (from `meta_json.text_paragraphs[].name`).
  Do NOT output any internal IDs (e.g. p1_c2 / floating:top_left).
- Coverage: every name listed in `meta_json.required_refs.all` MUST appear at least once (best: list them explicitly in “Content checklist”).
- Relative layout only, no coordinates: use “more top / more right / centered / two-column / grid / cards / band / whitespace / alignment”.
  Do NOT mention top/left/x/y/pixels/pt coordinates.
- Bilingual rule: Chinese + English must both be included for the same content (takes more space).
  Describe their relative position (English immediately follows Chinese), differentiate by font size but **do NOT fade the color**.
  - Body: English may be reduced to at most **80%** of the Chinese body font size (not smaller).
  - Titles / big titles: English may be reduced to at most **50%** of the Chinese title font size.
- Do NOT propose “new CSS file / new class names”: layout_notes should stay abstract and must not invent file names or class names.

Soft suggestion (recommended):
- Readability / contrast: remind downstream HTML generation to ensure strong contrast between text and backgrounds
  (page background or text container background). Dark background → light text; light background → dark text.
  Avoid low-contrast combinations.

Output format (VERY IMPORTANT):
- Output plain text only (no JSON, no explanation, no markdown code fences).
- Only 3–6 short paragraphs / bullet items. Avoid overly detailed parameters.

Recommended structure (you may omit sections if unnecessary):
1) Content checklist (by reference name)
2) What the page is conveying (one sentence)
3) Layout intent (skeleton-level, actionable; MUST reference 第N段)
4) Layout contract (role allocation + global alignment/repetition rules)

Example (demonstrates “human-like layout notes” and “how to reference IDs”; do NOT copy content verbatim):

Content checklist (by reference name)
- Text: 第1段 (main title/subtitle), 第2段 (body or bullets), 第3段 (supplement)

What the page is conveying (one sentence)
- Use a clear title to introduce the theme, then use a bullet list to explain the points with a crisp rhythm.

Layout intent (actionable; MUST reference 第N段)
- Put 第1段 at the top as the title area, highest visual weight, acting as the anchor.
- Put 第2段 below the title area as the information area; if there are many bullet points, use two columns / cards to reduce vertical stacking.
- Pair zh+en together (Chinese first, English immediately below), differentiate by font size (body ≥ 80%, title ≥ 50%), do NOT fade color.

Decorations / parameters
- Use palette.primary sparingly for emphasis (small band / light background blocks / thin dividers), background-only, never covering text.

Layout contract
- Roles: 第1段 = main title; 第2段 = explanation/bullets; 第3段 = supplement.
- Alignment & repetition: consistent left edge for title/body; consistent line/paragraph spacing; consistent zh/en spacing for readability.
"""


def _system_prompt_for_meta(meta: dict[str, Any]) -> str:
    images_count = 0
    heur = meta.get("heuristics")
    if isinstance(heur, dict):
        try:
            images_count = int(heur.get("images_count") or 0)
        except Exception:
            images_count = 0
    if images_count > 0:
        return SYSTEM_PROMPT_WITH_IMAGES
    return SYSTEM_PROMPT_NO_IMAGES


_RESPONSE_JSON_SCHEMA: dict[str, Any] = {}


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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", flags=re.IGNORECASE)


def _normalize_model_output(raw: str) -> str:
    t = (raw or "").strip()
    if not t:
        return ""
    m = _FENCE_RE.search(t)
    if m:
        return (m.group(1) or "").strip()
    return t


def _get_api_key(args: argparse.Namespace) -> str:
    if args.api_key:
        return args.api_key
    for env_name in args.api_key_env:
        v = os.getenv(env_name, "").strip()
        if v:
            return v
    raise SystemExit("Missing API key. Provide --api-key or set one of env vars: " + ", ".join(args.api_key_env))


def _page_png_path(pages_png_dir: Path, page_1based: int) -> Path:
    return pages_png_dir / f"page_{page_1based:03d}.png"


def _load_bilingual_text(*, bilingual_text_dir: Path, page_1based: int) -> dict[str, Any] | None:
    req_path = bilingual_text_dir / f"page_{page_1based:03d}.request.json"
    res_path = bilingual_text_dir / f"page_{page_1based:03d}.result.json"
    if not req_path.exists() or not res_path.exists():
        return None
    req = _read_json(req_path)
    res = _read_json(res_path)
    if not isinstance(req, dict) or not isinstance(res, dict):
        return None

    zh = res.get("zh_paragraphs")
    en = res.get("en_paragraphs")
    sources = res.get("sources")
    texts = req.get("texts")
    if not isinstance(zh, list) or not isinstance(en, list) or len(zh) != len(en):
        return None

    t_meta: dict[str, dict[str, Any]] = {}
    if isinstance(texts, list):
        for it in texts:
            if not isinstance(it, dict):
                continue
            tid = it.get("id")
            if isinstance(tid, str):
                t_meta[tid] = {
                    "id": tid,
                    "kind": it.get("kind"),
                    "text": it.get("text"),
                    "placement": it.get("placement"),
                }

    paragraphs: list[dict[str, Any]] = []
    for i in range(len(zh)):
        zh_i = str(zh[i])
        en_i = str(en[i])
        item: dict[str, Any] = {"name": f"第{i+1}段", "idx": int(i + 1), "zh": zh_i, "en": en_i}

        src_tids: list[str] = []
        kind_hints: list[str] = []
        placement_hints: list[str] = []
        if isinstance(sources, list) and i < len(sources) and isinstance(sources[i], list):
            src_tids = [x for x in sources[i] if isinstance(x, str)]
            for tid in src_tids:
                km = t_meta.get(tid) or {}
                k = km.get("kind")
                if isinstance(k, str) and k:
                    kind_hints.append(k)
                plc = km.get("placement")
                if isinstance(plc, str) and plc and plc not in placement_hints:
                    placement_hints.append(plc)

        # Derive a coarse kind hint to help layout planning.
        priority = ["title", "heading", "subheading", "label", "bullet_item", "body"]
        kind_hint = "body"
        for k in priority:
            if k in kind_hints:
                kind_hint = k
                break
        item["kind_hint"] = kind_hint
        if placement_hints:
            item["placement_hints"] = placement_hints
        paragraphs.append(item)

    zh_chars = sum(len((it.get("zh") or "").strip()) for it in paragraphs)
    en_chars = sum(len((it.get("en") or "").strip()) for it in paragraphs)
    return {
        "zh_paragraphs": [str(x) for x in zh],
        "en_paragraphs": [str(x) for x in en],
        "paragraphs": paragraphs,
        "summary": {
            "para_count": len(paragraphs),
            "zh_chars": int(zh_chars),
            "en_chars": int(en_chars),
        },
    }


def _load_image_descriptions(*, image_desc_dir: Path, page_1based: int) -> dict[str, str]:
    """
    Read image descriptions for a page from describe_images.py outputs.
    Returns mapping: image_id -> description_text
    """
    page_dir = image_desc_dir / f"page_{page_1based:03d}"
    result_path = page_dir / "result.json"
    out: dict[str, str] = {}
    if result_path.exists():
        try:
            obj = _read_json(result_path)
            images = obj.get("images") if isinstance(obj, dict) else None
            if isinstance(images, list):
                for it in images:
                    if not isinstance(it, dict):
                        continue
                    iid = it.get("id")
                    txt = it.get("text")
                    if isinstance(iid, str) and isinstance(txt, str):
                        t = txt.strip()
                        if t:
                            out[iid] = t
        except Exception:
            pass
    # Fallback: per-image text files
    if page_dir.exists() and not out:
        for p in page_dir.glob("*.text.txt"):
            iid = p.name.replace(".text.txt", "")
            try:
                out[iid] = p.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
    return out


def _load_image_descriptions_ordered_by_src(*, image_desc_dir: Path, page_1based: int) -> list[dict[str, str]]:
    """
    Read image descriptions in a stable "folder order" defined by
    describe_images.py's per-page request.json (meta_json.images order).

    Returns list items: {"src": "...", "description_cn": "..."} in that order.
    """
    page_dir = image_desc_dir / f"page_{page_1based:03d}"
    req_path = page_dir / "request.json"
    if not req_path.exists():
        return []
    try:
        req = _read_json(req_path)
    except Exception:
        return []
    if not isinstance(req, dict):
        return []
    req_images = req.get("images")
    if not isinstance(req_images, list):
        return []

    ordered: list[tuple[str, str]] = []
    for it in req_images:
        if not isinstance(it, dict):
            continue
        iid = it.get("id")
        src = it.get("src")
        if not isinstance(src, str) or not src:
            continue
        ordered.append((str(iid or ""), src))

    id_to_text: dict[str, str] = {}
    res_path = page_dir / "result.json"
    if res_path.exists():
        try:
            obj = _read_json(res_path)
            images = obj.get("images") if isinstance(obj, dict) else None
            if isinstance(images, list):
                for it in images:
                    if not isinstance(it, dict):
                        continue
                    iid = it.get("id")
                    txt = it.get("text")
                    if isinstance(iid, str) and isinstance(txt, str):
                        t = txt.strip()
                        if t:
                            id_to_text[iid] = t
        except Exception:
            pass

    out: list[dict[str, str]] = []
    for iid, src in ordered:
        txt = ""
        if iid and iid in id_to_text:
            txt = id_to_text[iid]
        elif iid:
            p = page_dir / f"{iid}.text.txt"
            if p.exists():
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:
                    txt = ""
        out.append({"src": src, "description_cn": txt})
    return out


def _simplify_group_for_model(group: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "group_id": group.get("group_id"),
        "placement": group.get("placement"),
        "size": group.get("size"),
    }
    if "fill" in group:
        out["fill"] = group.get("fill")
    blocks_out: list[dict[str, Any]] = []
    for blk in group.get("blocks") or []:
        if not isinstance(blk, dict):
            continue
        kind = blk.get("kind") or "body"
        if kind == "bullets":
            items = []
            for it in blk.get("items") or []:
                if isinstance(it, dict) and isinstance(it.get("text"), str):
                    items.append({"text": it["text"]})
            blocks_out.append({"kind": "bullets", "items": items, "placement": blk.get("placement")})
        else:
            blocks_out.append({"kind": str(kind), "text": blk.get("text") or "", "placement": blk.get("placement")})
    out["blocks"] = blocks_out
    return out


def _build_meta_json(
    *,
    plan: dict[str, Any],
    page_obj: dict[str, Any],
    page_1based: int,
    page_png_path: Path,
    bilingual_text_dir: Path,
    image_desc_dir: Path,
) -> dict[str, Any]:
    page_size = (plan.get("constraints") or {}).get("page_size_fixed_pt") if isinstance(plan.get("constraints"), dict) else None
    if not isinstance(page_size, dict):
        page_size = {"w": None, "h": None}

    desc_ordered = _load_image_descriptions_ordered_by_src(image_desc_dir=image_desc_dir, page_1based=page_1based)
    desc_by_src: dict[str, str] = {}
    ordered_srcs: list[str] = []
    for it in desc_ordered:
        if not isinstance(it, dict):
            continue
        src = it.get("src")
        if not isinstance(src, str) or not src:
            continue
        ordered_srcs.append(src)
        txt = it.get("description_cn")
        if isinstance(txt, str) and txt.strip():
            desc_by_src[src] = txt.strip()

    bilingual = _load_bilingual_text(bilingual_text_dir=bilingual_text_dir, page_1based=page_1based)

    # Text paragraphs (prefer translation output; refer by "第N段").
    text_paragraphs: list[dict[str, Any]] = []
    if isinstance(bilingual, dict):
        paras = bilingual.get("paragraphs")
        if isinstance(paras, list):
            for it in paras:
                if not isinstance(it, dict):
                    continue
                name = it.get("name")
                idx = it.get("idx")
                zh_t = it.get("zh")
                en_t = it.get("en")
                if not isinstance(name, str) or not name:
                    continue
                if not isinstance(zh_t, str) or not isinstance(en_t, str):
                    continue
                out_p: dict[str, Any] = {"name": name, "idx": idx, "zh": zh_t, "en": en_t}
                kind_hint = it.get("kind_hint")
                if isinstance(kind_hint, str) and kind_hint:
                    out_p["kind_hint"] = kind_hint
                placement_hints = it.get("placement_hints")
                if isinstance(placement_hints, list) and placement_hints:
                    out_p["placement_hints"] = placement_hints
                text_paragraphs.append(out_p)

    # Plan image metadata (we will rename images as image1/image2... for the model).
    plan_srcs: list[str] = []
    plan_info_by_src: dict[str, dict[str, Any]] = {}
    images_area = 0.0
    for im in page_obj.get("images") or []:
        if not isinstance(im, dict):
            continue
        src = str(im.get("src") or "")
        if not src:
            continue
        display_scale = float(im.get("display_scale") or 0.0)
        display_w = float(im.get("display_w_pt") or 0.0)
        display_h = float(im.get("display_h_pt") or 0.0)
        # "Original" reference size in the source HTML is approximated by inverting display_scale.
        # This is deterministic and matches how plan.json encodes a fixed scale (typically 0.85).
        ref_w = (display_w / display_scale) if display_scale else None
        ref_h = (display_h / display_scale) if display_scale else None
        area = display_w * display_h
        images_area += max(0.0, float(area))
        plan_srcs.append(src)
        plan_info_by_src[src] = {
            "src": src,
            "placement": im.get("placement"),
            "size": im.get("size"),
            "display_w_pt": im.get("display_w_pt"),
            "display_h_pt": im.get("display_h_pt"),
            "ref_w_pt": (None if ref_w is None else round(float(ref_w), 2)),
            "ref_h_pt": (None if ref_h is None else round(float(ref_h), 2)),
        }

    # Determine image ordering: prefer the image_descriptions folder order (request.json order),
    # otherwise fall back to plan.json order.
    src_order: list[str] = [s for s in ordered_srcs if s in plan_info_by_src]
    if not src_order:
        src_order = list(plan_srcs)
    for s in plan_srcs:
        if s not in src_order:
            src_order.append(s)

    images_out: list[dict[str, Any]] = []
    max_area = -1.0
    featured_default_name: str | None = None
    for idx, src in enumerate(src_order):
        info = plan_info_by_src.get(src)
        if not isinstance(info, dict):
            continue
        name = f"image{idx+1}"
        try:
            dw = float(info.get("display_w_pt") or 0.0)
            dh = float(info.get("display_h_pt") or 0.0)
        except Exception:
            dw, dh = 0.0, 0.0
        area = dw * dh
        if area > max_area:
            max_area = area
            featured_default_name = name
        images_out.append(
            {
                "name": name,
                "src": src,
                "placement": info.get("placement"),
                "size": info.get("size"),
                "display_w_pt": info.get("display_w_pt"),
                "display_h_pt": info.get("display_h_pt"),
                "ref_w_pt": info.get("ref_w_pt"),
                "ref_h_pt": info.get("ref_h_pt"),
                "description_cn": desc_by_src.get(src, ""),
            }
        )

    palette = page_obj.get("palette") if isinstance(page_obj.get("palette"), dict) else {}
    primary = palette.get("primary") if isinstance(palette, dict) else None

    required_images = [x.get("name") for x in images_out if isinstance(x.get("name"), str) and x.get("name")]
    required_paras = [x.get("name") for x in text_paragraphs if isinstance(x.get("name"), str) and x.get("name")]
    required_all = list(required_images) + list(required_paras)

    meta: dict[str, Any] = {
        "page_1based": int(page_1based),
        "page_id": page_obj.get("page_id"),
        "page_png_path": str(page_png_path),
        "page_size_pt": {"w": page_size.get("w"), "h": page_size.get("h")},
        "palette": {"primary": primary},
        "required_refs": {
            "images": required_images,
            "text_paragraphs": required_paras,
            "all": required_all,
        },
        "heuristics": {
            "images_count": len(required_images),
            "featured_image_default_name": featured_default_name,
        },
        "images": images_out,
        "text_paragraphs": text_paragraphs,
    }
    if bilingual is not None:
        if isinstance(bilingual, dict):
            if isinstance(bilingual.get("zh_paragraphs"), list):
                meta["zh_paragraphs"] = bilingual.get("zh_paragraphs")
            if isinstance(bilingual.get("en_paragraphs"), list):
                meta["en_paragraphs"] = bilingual.get("en_paragraphs")

    # Text heaviness heuristic (used to conditionally request compaction/columns strategy).
    try:
        pw = float(page_size.get("w") or 0.0)
        ph = float(page_size.get("h") or 0.0)
    except Exception:
        pw, ph = 0.0, 0.0
    page_area = max(1.0, pw * ph)
    images_area_ratio = max(0.0, min(0.85, float(images_area) / float(page_area)))
    text_area_ratio = max(_TEXT_AREA_RATIO_MIN, 1.0 - images_area_ratio)
    zh_chars = int(((bilingual or {}).get("summary") or {}).get("zh_chars") or 0) if isinstance(bilingual, dict) else 0
    en_chars = int(((bilingual or {}).get("summary") or {}).get("en_chars") or 0) if isinstance(bilingual, dict) else 0
    para_count = int(((bilingual or {}).get("summary") or {}).get("para_count") or 0) if isinstance(bilingual, dict) else 0
    est_lines_1col = (float(zh_chars) / float(_ZH_CHARS_PER_LINE_EST)) + (float(en_chars) / float(_EN_CHARS_PER_LINE_EST))
    est_lines_adjusted = est_lines_1col / float(text_area_ratio) if text_area_ratio > 0 else est_lines_1col
    is_long_text = bool(est_lines_adjusted >= float(_LONG_TEXT_LINES_THRESHOLD))
    meta["text_stats"] = {
        "zh_chars": zh_chars,
        "en_chars": en_chars,
        "para_count": para_count,
        "images_area_ratio": round(float(images_area_ratio), 4),
        "text_area_ratio": round(float(text_area_ratio), 4),
        "estimated_lines_1col": round(float(est_lines_1col), 2),
        "estimated_lines_adjusted": round(float(est_lines_adjusted), 2),
        "is_long_text": is_long_text,
    }
    return meta


def _call_gemini_layout_notes(
    *,
    client: Any,
    model: str,
    max_output_tokens: int,
    temperature: float,
    page_png_bytes: bytes,
    meta: dict[str, Any],
) -> tuple[str, str]:
    genai, types, _errors = _import_google_genai()

    required_all = (((meta.get("required_refs") or {}).get("all") or []) if isinstance(meta.get("required_refs"), dict) else [])
    if not isinstance(required_all, list):
        required_all = []

    user_text = (
        "meta_json:\n"
        + json.dumps(meta, ensure_ascii=False, indent=2)
        + "\n\n"
        "Output plain-text layout_notes_brief only (no JSON, no explanation, no markdown).\n"
        "Requirement: you MUST mention every name in required_refs.all at least once, and you may ONLY use these names to refer to images/text.\n"
        "Keep it short: skeleton-level structure only; avoid detailed parameters.\n"
        "required_refs.all list (copy into your Content checklist section to avoid missing any):\n"
        + json.dumps(required_all, ensure_ascii=False, indent=2)
        + "\n"
    )

    parts: list[Any] = [
        types.Part.from_bytes(data=page_png_bytes, mime_type="image/png"),
        types.Part.from_text(text=user_text),
    ]

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Content(
                role="user",
                parts=parts,
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=_system_prompt_for_meta(meta),
            temperature=float(temperature),
            max_output_tokens=int(max_output_tokens),
        ),
    )

    raw_text = response.text or ""
    notes = _normalize_model_output(raw_text).strip()
    # Ensure required refs always appear (deterministic safety net).
    missing = [x for x in required_all if isinstance(x, str) and x and x not in notes]
    if missing:
        notes = (notes + "\n\n" if notes else "") + "Coverage checklist (for validation):\n" + json.dumps(
            required_all, ensure_ascii=False, indent=2
        )
    return raw_text, notes


def _validate_layout_notes(*, meta: dict[str, Any], layout_notes: str) -> list[str]:
    warnings: list[str] = []
    notes = (layout_notes or "").strip()
    if not notes:
        return ["layout_notes_missing_or_empty"]
    required_all = (((meta.get("required_refs") or {}).get("all") or []) if isinstance(meta.get("required_refs"), dict) else [])
    required_set = {x for x in required_all if isinstance(x, str) and x}
    missing = [x for x in required_all if isinstance(x, str) and x and x not in notes]
    if missing:
        warnings.append(f"missing_required_refs: {missing[:30]}{' ...' if len(missing) > 30 else ''}")

    # Forbidden internal IDs must not appear.
    forbidden = sorted(set(re.findall(r"\b(?:p\d+_[A-Za-z_]+\d*|floating:[A-Za-z_]+)\b", notes)))
    if forbidden:
        warnings.append(f"forbidden_internal_ids: {forbidden[:30]}{' ...' if len(forbidden) > 30 else ''}")

    # Unknown references (e.g., image9 when only image1..image3 exist).
    mentioned_images = sorted(set(re.findall(r"\bimage\d+\b", notes)))
    unknown_images = [x for x in mentioned_images if x not in required_set]
    if unknown_images:
        warnings.append(f"unknown_image_refs: {unknown_images[:30]}{' ...' if len(unknown_images) > 30 else ''}")
    mentioned_paras = sorted(set(re.findall(r"第\d+段", notes)))
    unknown_paras = [x for x in mentioned_paras if x not in required_set]
    if unknown_paras:
        warnings.append(f"unknown_paragraph_refs: {unknown_paras[:30]}{' ...' if len(unknown_paras) > 30 else ''}")
    return warnings


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate per-page layout_notes_brief (multimodal).")
    p.add_argument("--plan", type=str, required=True, help="Path to plan.json")
    p.add_argument("--pages-png-dir", type=str, required=True, help="Directory containing full-page PNGs: page_001.png ...")
    p.add_argument(
        "--image-desc-dir",
        type=str,
        required=True,
        help="Directory containing image_descriptions/page_XXX/result.json from describe_images.py",
    )
    p.add_argument(
        "--bilingual-text-dir",
        type=str,
        default="",
        help="Directory containing translate_text.py outputs (default: <plan_dir>/bilingual_text)",
    )
    p.add_argument("--page-start", type=int, default=1, help="1-based inclusive start page (default: 1)")
    p.add_argument("--page-end", type=int, default=10, help="1-based inclusive end page (default: 10)")
    p.add_argument("--out-dir", type=str, default="", help="Output directory (default: <plan_dir>/layout_notes_out)")
    p.add_argument("--dry-run", action="store_true", help="Do not call model; only write request JSONs.")
    p.add_argument("--print-prompt", action="store_true", help="Print the system+user prompt for page-start then exit.")
    p.add_argument("--write-plan", action="store_true", help="Write updated plan JSON with layout_notes to <out_dir>/plan.with_layout_notes.json")

    p.add_argument("--model", type=str, default="gemini-3.1-flash-lite", help="Model name")
    p.add_argument("--max-tokens", type=int, default=4096, help="Max output tokens (default: 4096)")
    p.add_argument("--temperature", type=float, default=0.0, help="Temperature (default: 0)")
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
    p.add_argument("--save-raw", action="store_true", help="Save raw model output next to result.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    plan_path = Path(args.plan).expanduser().resolve()
    if not plan_path.exists():
        raise SystemExit(f"plan not found: {plan_path}")
    plan = _read_json(plan_path)
    if not isinstance(plan, dict) or "pages" not in plan:
        raise SystemExit("plan must be a dict with key 'pages'")
    pages = plan.get("pages") or []
    if not isinstance(pages, list):
        raise SystemExit("plan['pages'] must be a list")

    pages_png_dir = Path(args.pages_png_dir).expanduser().resolve()
    if not pages_png_dir.exists():
        raise SystemExit(f"pages-png-dir not found: {pages_png_dir}")
    image_desc_dir = Path(args.image_desc_dir).expanduser().resolve()
    if not image_desc_dir.exists():
        raise SystemExit(f"image-desc-dir not found: {image_desc_dir}")

    bilingual_text_dir = (
        Path(args.bilingual_text_dir).expanduser().resolve() if args.bilingual_text_dir else (plan_path.parent / "bilingual_text")
    )

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else (plan_path.parent / "layout_notes_out")
    out_dir.mkdir(parents=True, exist_ok=True)

    page_start = int(args.page_start)
    page_end = int(args.page_end)
    if page_start <= 0 or page_end <= 0 or page_end < page_start:
        raise SystemExit("--page-start/--page-end must be positive and page_end >= page_start")

    # Prepare first page meta for prompt preview.
    idx0 = page_start - 1
    if idx0 >= len(pages):
        raise SystemExit(f"page-start out of range: {page_start} (pages={len(pages)})")
    page_png0 = _page_png_path(pages_png_dir, page_start)
    if not page_png0.exists():
        raise SystemExit(f"missing page_png: {page_png0}")
    first_page_obj = pages[idx0]
    if not isinstance(first_page_obj, dict):
        raise SystemExit(f"plan.pages[{idx0}] is not an object")
    meta0 = _build_meta_json(
        plan=plan,
        page_obj=first_page_obj,
        page_1based=page_start,
        page_png_path=page_png0,
        bilingual_text_dir=bilingual_text_dir,
        image_desc_dir=image_desc_dir,
    )

    user_prompt0 = (
        "meta_json:\n"
        + json.dumps(meta0, ensure_ascii=False, indent=2)
        + "\n\n"
        + "Output plain-text layout_notes_brief only (no JSON, no explanation, no markdown).\n"
    )

    if args.print_prompt:
        sys_prompt0 = _system_prompt_for_meta(meta0)
        print("===== SYSTEM PROMPT =====")
        print(sys_prompt0)
        print("\n===== USER PROMPT (page-start) =====")
        print(user_prompt0)
        return 0

    # Always write per-page request JSONs first (deterministic, inspectable).
    for pno in range(page_start, page_end + 1):
        idx = pno - 1
        if idx >= len(pages):
            break
        page_obj = pages[idx]
        if not isinstance(page_obj, dict):
            continue
        page_png = _page_png_path(pages_png_dir, pno)
        if not page_png.exists():
            raise SystemExit(f"missing page_png: {page_png}")
        meta = _build_meta_json(
            plan=plan,
            page_obj=page_obj,
            page_1based=pno,
            page_png_path=page_png,
            bilingual_text_dir=bilingual_text_dir,
            image_desc_dir=image_desc_dir,
        )
        page_out_dir = out_dir / f"page_{pno:03d}"
        page_out_dir.mkdir(parents=True, exist_ok=True)
        _write_json(page_out_dir / "request.json", meta)

    if args.dry_run:
        print(str(out_dir))
        return 0

    api_key = _get_api_key(args)
    genai, _types, genai_errors = _import_google_genai()

    updated_plan = json.loads(json.dumps(plan))

    with genai.Client(api_key=api_key) as client:
        for pno in range(page_start, page_end + 1):
            idx = pno - 1
            if idx >= len(pages):
                break
            page_obj = pages[idx]
            if not isinstance(page_obj, dict):
                continue

            page_out_dir = out_dir / f"page_{pno:03d}"
            page_out_dir.mkdir(parents=True, exist_ok=True)

            page_png = _page_png_path(pages_png_dir, pno)
            meta = _read_json(page_out_dir / "request.json")
            if not isinstance(meta, dict):
                (page_out_dir / "error.txt").write_text("invalid request.json\n", encoding="utf-8")
                continue

            raw: str = ""
            layout_notes: str = ""
            last_err: Exception | None = None
            for attempt in range(max(0, int(args.retries)) + 1):
                try:
                    raw, layout_notes = _call_gemini_layout_notes(
                        client=client,
                        model=str(args.model),
                        max_output_tokens=int(args.max_tokens),
                        temperature=float(args.temperature),
                        page_png_bytes=page_png.read_bytes(),
                        meta=meta,
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
                (page_out_dir / "error.txt").write_text(
                    f"Model call failed after retries. error={type(last_err).__name__}: {last_err}\n",
                    encoding="utf-8",
                )
                if args.save_raw and raw:
                    (page_out_dir / "raw.txt").write_text(raw or "", encoding="utf-8")
                continue

            if not isinstance(layout_notes, str) or not layout_notes.strip():
                (page_out_dir / "error.txt").write_text(
                    "Model output is empty/invalid layout_notes text.\n\nRAW:\n" + (raw or "") + "\n",
                    encoding="utf-8",
                )
                if args.save_raw:
                    (page_out_dir / "raw.txt").write_text(raw or "", encoding="utf-8")
                continue

            (page_out_dir / "layout_notes.txt").write_text(layout_notes.strip() + "\n", encoding="utf-8")
            warnings = _validate_layout_notes(meta=meta, layout_notes=layout_notes)
            if warnings:
                _write_json(page_out_dir / "warnings.json", {"warnings": warnings})
            if args.save_raw:
                (page_out_dir / "raw.txt").write_text(raw or "", encoding="utf-8")

            # Update plan copy in memory.
            try:
                updated_plan["pages"][idx]["layout_notes"] = str(layout_notes or "")
            except Exception:
                pass

    if args.write_plan:
        _write_json(out_dir / "plan.with_layout_notes.json", updated_plan)

    print(str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

