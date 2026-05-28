from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TEXT_RECOMPOSE_SYSTEM_PROMPT = """You are a “single-page HTML text recomposer + bilingual translator + paragraphizer”.

Background:
- You are processing the text content that should appear on ONE page. The input `texts[]` are text fragments extracted from that page
  (title / heading / subtitle / body / bullet items).
- `texts[]` are mostly Chinese fragments, and may be line-broken, over-split, and/or in a suboptimal order.

Your task has two steps:
1) **Chinese recomposition + paragraphing**: reorder (and when necessary, merge) the Chinese fragments in `texts[]` into natural Chinese paragraphs,
   output as `zh_paragraphs[]`.
2) **Per-paragraph translation**: translate each paragraph in `zh_paragraphs[]` into English, output as `en_paragraphs[]`.

Output requirements:
- `zh_paragraphs[]`: an array of Chinese paragraphs (you may merge multiple fragments into one paragraph; keep title vs body clearly separated).
- `en_paragraphs[]`: an array of English paragraphs, MUST have the same length as `zh_paragraphs[]`, aligned by index.
- `sources[]`: same length as paragraphs; `sources[i]` is the list of input `texts[].id` used by paragraph i (for completeness checking).

Hard constraints (MUST follow):
- No hallucination: do NOT add any new facts or sentences not present in the input fragments.
  - If a Chinese sentence is split in the middle across fragments, first concatenate the existing characters in the correct Chinese order (Step 1),
    then paragraphize and translate. When concatenating, you may ONLY use characters already present in the input (no new words).
- Do not rewrite Chinese: `zh_paragraphs` must be composed only by concatenation / reordering of the input fragments.
  You may do minimal punctuation/whitespace normalization (e.g. remove extra spaces), but do NOT paraphrase or replace synonyms.
- Each input `texts[].id` must be used EXACTLY ONCE: no missing, no duplicates. (Even if you think a fragment is noise, keep it as its own paragraph.)
- Do not merge titles into body: title/heading/subheading/label must be separated from body paragraphs.
  Do NOT force multiple title lines into one sentence (you may keep them as multiple title paragraphs).
- Avoid meaningless paragraph splits: do not split a body paragraph in the middle of a sentence.
  If two body paragraphs are clearly a continuation (previous paragraph does not end with sentence-ending punctuation and the next continues it), merge them.

Output format (VERY IMPORTANT):
- Output JSON only. No explanation. No markdown code fences.
- JSON schema MUST be exactly:
  {
    "zh_paragraphs": ["...", "..."],
    "en_paragraphs": ["...", "..."],
    "sources": [["t0","t3"], ["t1"], ...]
  }
- `zh_paragraphs.length == en_paragraphs.length == sources.length`
"""


_RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["zh_paragraphs", "en_paragraphs", "sources"],
    "properties": {
        "zh_paragraphs": {"type": "array", "items": {"type": "string"}},
        "en_paragraphs": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
        },
    },
}


@dataclass(frozen=True)
class TextFrag:
    id: str
    kind: str
    text: str
    group_id: str
    group_type: str  # groups | floating_groups
    placement: str


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _iter_page_text_frags(plan_page_json: dict[str, Any]) -> list[TextFrag]:
    """
    Extract a flat list of text fragments from a single plan page.

    We keep group boundaries as metadata (group_id/group_type/placement) but do not enforce
    original ordering beyond a stable deterministic extraction order.
    """

    out: list[TextFrag] = []
    next_id = 0

    def emit(*, kind: str, text: str, group_id: str, group_type: str, placement: str) -> None:
        nonlocal next_id
        t = (text or "").strip()
        if not t:
            return
        out.append(
            TextFrag(
                id=f"t{next_id}",
                kind=str(kind),
                text=t,
                group_id=str(group_id),
                group_type=str(group_type),
                placement=str(placement),
            )
        )
        next_id += 1

    def blocks_from_group(group: dict[str, Any], *, group_type: str) -> Iterable[tuple[str, str]]:
        for _blk_idx, blk in enumerate(group.get("blocks") or []):
            if not isinstance(blk, dict):
                continue
            kind = str(blk.get("kind") or "body")
            if kind == "bullets":
                for _item_idx, it in enumerate(blk.get("items") or []):
                    if not isinstance(it, dict):
                        continue
                    txt = str(it.get("text") or "")
                    yield ("bullet_item", txt)
            else:
                txt = str(blk.get("text") or "")
                yield (kind, txt)

    # Deterministic order: groups then floating_groups, each in file order.
    for g in plan_page_json.get("groups") or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id") or "")
        plc = str(g.get("placement") or "")
        for kind, txt in blocks_from_group(g, group_type="groups"):
            emit(kind=kind, text=txt, group_id=gid, group_type="groups", placement=plc)

    for g in plan_page_json.get("floating_groups") or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id") or "")
        plc = str(g.get("placement") or "")
        for kind, txt in blocks_from_group(g, group_type="floating_groups"):
            emit(kind=kind, text=txt, group_id=gid, group_type="floating_groups", placement=plc)

    return out


def _build_request(*, page_1based: int, plan_page_json: dict[str, Any]) -> dict[str, Any]:
    frags = _iter_page_text_frags(plan_page_json)
    return {
        "page_1based": int(page_1based),
        "texts": [
            {
                "id": f.id,
                "kind": f.kind,
                "text": f.text,
                "group_id": f.group_id,
                "group_type": f.group_type,
                "placement": f.placement,
            }
            for f in frags
        ],
    }


def _get_api_key(args: argparse.Namespace) -> str:
    if args.api_key:
        return args.api_key
    for env_name in args.api_key_env:
        v = os.getenv(env_name, "").strip()
        if v:
            return v
    raise SystemExit("Missing API key. Provide --api-key or set one of env vars: " + ", ".join(args.api_key_env))


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

        return genai, types
    except Exception:
        repo_root = Path(__file__).resolve().parents[1]
        local_src = repo_root / "python-genai"
        if local_src.exists():
            ps = str(local_src)
            if ps not in sys.path:
                sys.path.insert(0, ps)
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore

            return genai, types
        raise


def _call_gemini(
    *,
    client: Any,
    model: str,
    max_output_tokens: int,
    temperature: float,
    user_prompt: str,
) -> tuple[str, dict[str, Any] | None]:
    genai, types = _import_google_genai()

    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=TEXT_RECOMPOSE_SYSTEM_PROMPT,
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
    # Fallback: try parsing raw text as JSON.
    try:
        norm = _normalize_model_output(raw_text)
        obj = json.loads(norm)
        return raw_text, obj if isinstance(obj, dict) else None
    except Exception:
        return raw_text, None


def _validate_output(*, req: dict[str, Any], out_obj: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    zh = out_obj.get("zh_paragraphs")
    en = out_obj.get("en_paragraphs")
    src = out_obj.get("sources")
    if not isinstance(zh, list) or not isinstance(en, list) or not isinstance(src, list):
        raise ValueError("output JSON must contain arrays: zh_paragraphs, en_paragraphs, sources")
    if not (len(zh) == len(en) == len(src)):
        raise ValueError("length mismatch: zh_paragraphs, en_paragraphs, sources must have same length")

    in_ids = [t.get("id") for t in (req.get("texts") or []) if isinstance(t, dict)]
    in_ids = [i for i in in_ids if isinstance(i, str)]
    used: list[str] = []
    for s in src:
        if not isinstance(s, list):
            raise ValueError("sources must be an array of arrays")
        for x in s:
            if isinstance(x, str):
                used.append(x)
            else:
                raise ValueError("sources entries must be strings (text ids)")

    missing = [i for i in in_ids if i not in used]
    dup = sorted({i for i in used if used.count(i) > 1})
    extra = [i for i in used if i not in in_ids]

    if missing:
        warnings.append(f"missing_ids: {missing[:20]}{' ...' if len(missing) > 20 else ''}")
    if dup:
        warnings.append(f"duplicate_ids: {dup[:20]}{' ...' if len(dup) > 20 else ''}")
    if extra:
        warnings.append(f"unknown_ids: {extra[:20]}{' ...' if len(extra) > 20 else ''}")
    return warnings


def _build_user_prompt(*, request_obj: dict[str, Any]) -> str:
    return "Input JSON:\n" + json.dumps(request_obj, ensure_ascii=False, indent=2) + "\n"


def _build_validation_repair_prompt(
    *,
    request_obj: dict[str, Any],
    last_raw: str,
    validation_error: str,
) -> str:
    """
    Ask the model to re-emit JSON that strictly satisfies the schema + key invariants.
    This is primarily to make non-Gemini models (e.g. Gemma) robust in structured output mode.
    """
    req_txt = json.dumps(request_obj, ensure_ascii=False, indent=2)
    last_txt = (last_raw or "").strip()
    if len(last_txt) > 6000:
        last_txt = last_txt[:6000] + "\n...(truncated)...\n"
    return (
        "Your previous output failed validation. Please re-emit the JSON.\n"
        f"Validation error: {validation_error}\n\n"
        "Strictly output JSON ONLY (no explanation, no markdown, no ``` fences).\n"
        "You MUST satisfy all of the following:\n"
        "- Output contains and ONLY contains fields: zh_paragraphs / en_paragraphs / sources.\n"
        "- zh_paragraphs.length == en_paragraphs.length == sources.length.\n"
        "- sources[i] must be an array of strings, each string MUST be one of the input texts[].id.\n"
        "- Each input texts[].id MUST be used exactly once (no missing, no duplicates, no new ids).\n"
        "- zh_paragraphs Chinese text can ONLY be formed by concatenating/reordering the input fragments "
        "(no paraphrasing; only minimal punctuation/whitespace normalization).\n"
        "- en_paragraphs must be a per-paragraph translation aligned by index.\n\n"
        "Previous output (for reference; you may ignore it):\n"
        + last_txt
        + "\n\nInput JSON (authoritative; you MUST follow this):\n"
        + req_txt
        + "\n"
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Recompose Chinese fragments into paragraphs, then translate to English (Gemini/Gemma via google-genai).")
    p.add_argument("--plan", type=str, required=True, help="Path to plan.json")
    p.add_argument("--page-start", type=int, default=1, help="1-based inclusive start page (default: 1)")
    p.add_argument("--page-end", type=int, default=10, help="1-based inclusive end page (default: 10)")
    p.add_argument("--out-dir", type=str, default="", help="Directory to write requests/results (default: <plan_dir>/bilingual_text)")
    p.add_argument("--dry-run", action="store_true", help="Do not call model; only write request JSON files.")
    p.add_argument("--print-prompt", action="store_true", help="Print the system+user prompt for page-start then exit.")

    p.add_argument("--model", type=str, default="gemini-3.1-flash-lite", help="Model name (default: gemini-3.1-flash-lite)")
    p.add_argument("--max-tokens", type=int, default=4096, help="Max output tokens (default: 4096)")
    p.add_argument("--temperature", type=float, default=0.0, help="Temperature (default: 0)")

    p.add_argument("--api-key", type=str, default="", help="API key (avoid; prefer env var).")
    p.add_argument(
        "--api-key-env",
        type=str,
        nargs="+",
        default=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        help="Env var names to check for API key (default: GEMINI_API_KEY GOOGLE_API_KEY)",
    )
    p.add_argument("--save-raw", action="store_true", help="Save raw model output next to the parsed JSON.")
    p.add_argument(
        "--max-repairs",
        type=int,
        default=2,
        help="Max repair attempts per page when output fails validation (default: 2)",
    )
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

    page_start = int(args.page_start)
    page_end = int(args.page_end)
    if page_start <= 0 or page_end <= 0 or page_end < page_start:
        raise SystemExit("--page-start/--page-end must be positive and page_end >= page_start")

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else (plan_path.parent / "bilingual_text")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Prepare one page request for prompt preview / dry-run generation.
    first_idx0 = page_start - 1
    if first_idx0 >= len(pages):
        raise SystemExit(f"page-start out of range: {page_start} (pages={len(pages)})")
    req0 = _build_request(page_1based=page_start, plan_page_json=pages[first_idx0])
    user_prompt0 = _build_user_prompt(request_obj=req0)

    if args.print_prompt:
        print("===== SYSTEM PROMPT =====")
        print(TEXT_RECOMPOSE_SYSTEM_PROMPT)
        print("\n===== USER PROMPT (page-start) =====")
        print(user_prompt0)
        return 0

    # Always write request JSONs so you can inspect exactly what was sent.
    for pno in range(page_start, page_end + 1):
        idx0 = pno - 1
        if idx0 >= len(pages):
            break
        req = _build_request(page_1based=pno, plan_page_json=pages[idx0])
        _write_json(out_dir / f"page_{pno:03d}.request.json", req)

    if args.dry_run:
        print(str(out_dir))
        return 0

    api_key = _get_api_key(args)
    genai, _types = _import_google_genai()

    with genai.Client(api_key=api_key) as client:
        for pno in range(page_start, page_end + 1):
            idx0 = pno - 1
            if idx0 >= len(pages):
                break
            req = _build_request(page_1based=pno, plan_page_json=pages[idx0])
            user_prompt = _build_user_prompt(request_obj=req)

            last_raw = ""
            last_obj: dict[str, Any] | None = None
            last_validation_err: str | None = None

            max_repairs = max(0, int(args.max_repairs))
            for attempt in range(0, 1 + max_repairs):
                raw, out_obj = _call_gemini(
                    client=client,
                    model=str(args.model),
                    max_output_tokens=int(args.max_tokens),
                    temperature=float(args.temperature),
                    user_prompt=user_prompt,
                )
                last_raw = raw or ""
                last_obj = out_obj if isinstance(out_obj, dict) else None

                if args.save_raw:
                    suffix = "" if attempt == 0 else f"_repair{attempt}"
                    _write_text(out_dir / f"page_{pno:03d}.raw{suffix}.txt", last_raw)

                if not isinstance(last_obj, dict):
                    last_validation_err = "output is not a JSON object"
                else:
                    try:
                        warnings = _validate_output(req=req, out_obj=last_obj)
                        _write_json(out_dir / f"page_{pno:03d}.result.json", last_obj)
                        if warnings:
                            _write_json(out_dir / f"page_{pno:03d}.warnings.json", {"warnings": warnings})
                        last_validation_err = None
                        break
                    except Exception as e:
                        last_validation_err = f"{type(e).__name__}: {e}"

                if attempt < max_repairs:
                    user_prompt = _build_validation_repair_prompt(
                        request_obj=req,
                        last_raw=last_raw,
                        validation_error=str(last_validation_err),
                    )
                    continue

            if last_validation_err is not None:
                _write_text(
                    out_dir / f"page_{pno:03d}.error.txt",
                    "Model output failed validation.\n" + f"error={last_validation_err}\n\nRAW:\n" + (last_raw or "") + "\n",
                )
                raise SystemExit(f"page {pno}: model output failed validation: {last_validation_err}")

    print(str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

