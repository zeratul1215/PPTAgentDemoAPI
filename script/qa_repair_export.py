from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

"""
Auto QA + targeted repair for generated chunk HTML, then export a final PDF.

Workflow:
1) For each chunk_XXX_XXX.html, load in Chromium, apply autoshrink, then run QA:
   - page overflow: .page scrollHeight/Width must not exceed clientHeight/Width
   - clipped text: any overflow!=visible container with text must not clip its text
2) If a page still fails QA after autoshrink, ask the model to repair that page only.
3) Re-QA only repaired pages, loop up to 3 rounds total (initial + 2 repairs/page).
4) Always export a PDF at the end (autoshrink applied), even if some pages still fail QA.

Assumption: no legacy chunk formats / consumers need compatibility.
"""


CHUNK_RE = re.compile(r"chunk_(\d+)_(\d+)\.html$", flags=re.I)

_FENCE_RE = re.compile(r"```(?:html)?\s*([\s\S]*?)\s*```", flags=re.IGNORECASE)

OFFICE_FRIENDLY_FONTS_CSS = r"""
/* Office/PPT-friendly font override (macOS target). */
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


AUTO_SHRINK_JS = r"""
async (opts) => {
  const minFontPt = Number(opts?.minFontPt ?? 11.0);
  const maxIters = Math.max(1, Math.min(12, Number(opts?.maxIters ?? 8)));
  const minScale = Math.max(0.2, Math.min(1.0, Number(opts?.minScale ?? 0.6)));
  const PT_TO_PX = 96 / 72;
  const minPx = minFontPt * PT_TO_PX;

  function nextFrame() {
    return new Promise((r) => requestAnimationFrame(() => r()));
  }

  function hasDirectText(el) {
    const nodes = el.childNodes || [];
    for (const n of nodes) {
      if (n.nodeType === Node.TEXT_NODE && (n.textContent || "").trim()) return true;
    }
    return false;
  }

  function isNonEmptyTextElement(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "noscript") return false;
    if (!hasDirectText(el)) return false;
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    return true;
  }

  function collectLeaves(root) {
    const out = [];
    const nodes = root.querySelectorAll("*");
    for (const el of nodes) {
      if (isNonEmptyTextElement(el)) out.push(el);
    }
    return out;
  }

  function applyScale(leaves, origSizes, scale) {
    for (let i = 0; i < leaves.length; i++) {
      const el = leaves[i];
      const orig = origSizes[i];
      if (!orig || !Number.isFinite(orig) || orig <= 0) continue;
      const px = Math.max(minPx, orig * scale);
      const finalPx = Math.min(orig, px); // never enlarge
      el.style.fontSize = `${finalPx}px`;
    }
  }

  function pageOverflows(pg) {
    return pg.scrollHeight > pg.clientHeight + 1 || pg.scrollWidth > pg.clientWidth + 1;
  }

  function hasNonEmptyText(el) {
    const txt = (el.innerText || "").trim();
    return txt.length > 0;
  }

  function isClippedCandidate(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "noscript") return false;
    if (!hasNonEmptyText(el)) return false;
    const cs = getComputedStyle(el);
    const ovX = cs.overflowX;
    const ovY = cs.overflowY;
    const clips = (ovX && ovX !== "visible") || (ovY && ovY !== "visible");
    if (!clips) return false;
    if (el.clientHeight <= 0 || el.clientWidth <= 0) return false;
    return true;
  }

  function collectClippedCandidates(pg) {
    const out = [];
    const nodes = pg.querySelectorAll("*");
    for (const el of nodes) {
      if (isClippedCandidate(el)) out.push(el);
    }
    return out;
  }

  function hasClippedText(candidates) {
    for (const el of candidates) {
      const overH = el.scrollHeight > el.clientHeight + 1;
      const overW = el.scrollWidth > el.clientWidth + 1;
      if (overH || overW) return true;
    }
    return false;
  }

  const pages = Array.from(document.querySelectorAll(".page"));
  if (!pages.length) return { pages: 0, adjusted: 0 };

  let adjusted = 0;
  await nextFrame();

  for (const pg of pages) {
    const clippedCandidates = collectClippedCandidates(pg);
    const needsShrink = pageOverflows(pg) || hasClippedText(clippedCandidates);
    if (!needsShrink) continue;

    const leaves = collectLeaves(pg);
    if (!leaves.length) continue;

    const origSizes = leaves.map((el) => {
      const fs = parseFloat(getComputedStyle(el).fontSize || "0");
      return Number.isFinite(fs) ? fs : 0;
    });

    let lo = minScale;
    let hi = 1.0;
    let best = minScale;

    for (let it = 0; it < maxIters; it++) {
      const mid = (lo + hi) / 2;
      applyScale(leaves, origSizes, mid);
      await nextFrame();
      const fits = !pageOverflows(pg) && !hasClippedText(clippedCandidates);
      if (fits) {
        best = mid;
        lo = mid;
      } else {
        hi = mid;
      }
    }

    applyScale(leaves, origSizes, best);
    await nextFrame();
    if (best < 0.999) adjusted += 1;
  }

  return { pages: pages.length, adjusted };
}
"""

PAGE_OVERFLOW_QA_JS = r"""
() => {
  const pages = Array.from(document.querySelectorAll(".page"));
  const bad = [];
  for (let idx = 0; idx < pages.length; idx++) {
    const pg = pages[idx];
    const overH = pg.scrollHeight > pg.clientHeight + 1;
    const overW = pg.scrollWidth > pg.clientWidth + 1;
    if (!overH && !overW) continue;
    bad.push({
      idx,
      id: pg.id || "",
      scrollHeight: pg.scrollHeight,
      clientHeight: pg.clientHeight,
      scrollWidth: pg.scrollWidth,
      clientWidth: pg.clientWidth,
    });
  }
  return { pages: pages.length, bad };
}
"""

CLIPPED_TEXT_QA_JS = r"""
() => {
  function hasNonEmptyText(el) {
    const txt = (el.innerText || "").trim();
    return txt.length > 0;
  }

  function isCandidate(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === "html" || tag === "body" || tag === "script" || tag === "style" || tag === "noscript") return false;
    if (!hasNonEmptyText(el)) return false;
    const cs = getComputedStyle(el);
    const ovX = cs.overflowX;
    const ovY = cs.overflowY;
    const clips = (ovX && ovX !== "visible") || (ovY && ovY !== "visible");
    if (!clips) return false;
    if (el.clientHeight <= 0 || el.clientWidth <= 0) return false;
    return true;
  }

  function brief(el) {
    const ref = el.getAttribute("data-ref") || "";
    const cls = (el.className || "").toString();
    const txt = (el.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 90);
    return {
      tag: el.tagName.toLowerCase(),
      ref,
      cls,
      overflowX: getComputedStyle(el).overflowX,
      overflowY: getComputedStyle(el).overflowY,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
      text: txt,
    };
  }

  const pages = Array.from(document.querySelectorAll(".page"));
  const bad = [];
  for (let pageIdx = 0; pageIdx < pages.length; pageIdx++) {
    const pg = pages[pageIdx];
    const els = Array.from(pg.querySelectorAll("*"));
    for (const el of els) {
      if (!isCandidate(el)) continue;
      const overH = el.scrollHeight > el.clientHeight + 1;
      const overW = el.scrollWidth > el.clientWidth + 1;
      if (!overH && !overW) continue;
      bad.push({ pageIdx, ...brief(el) });
    }
  }
  return { pages: pages.length, bad };
}
"""


def _load_module_from_path(*, name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if not spec or not spec.loader:
        raise SystemExit(f"Failed to load module spec: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _extract_page_block_from_chunk_html(chunk_html: str) -> str | None:
    t = chunk_html or ""
    m = re.search(r"(?is)<!--\s*PAGE_START\s*-->\s*(.*?)\s*<!--\s*PAGE_END\s*-->", t)
    if not m:
        return None
    inner = (m.group(1) or "").strip()
    if not inner:
        return None
    if not re.search(r'(?is)\bclass\s*=\s*["\']page\b', inner):
        return None
    return "<!-- PAGE_START -->\n" + inner + "\n<!-- PAGE_END -->\n"


def _replace_page_block_in_chunk_html(*, chunk_html: str, new_page_block: str) -> str:
    t = chunk_html or ""
    if not re.search(r"(?is)<!--\s*PAGE_START\s*-->", t):
        raise ValueError("PAGE_START not found in chunk")
    if not re.search(r"(?is)<!--\s*PAGE_END\s*-->", t):
        raise ValueError("PAGE_END not found in chunk")
    return re.sub(
        r"(?is)<!--\s*PAGE_START\s*-->.*?<!--\s*PAGE_END\s*-->",
        new_page_block.strip(),
        t,
        count=1,
    )


def _strip_fences(raw: str) -> str:
    t = (raw or "").strip()
    if not t:
        return ""
    m = _FENCE_RE.search(t)
    if m:
        return (m.group(1) or "").strip()
    return t


def _chunk_page_no(chunk_path: Path) -> int:
    m = CHUNK_RE.search(chunk_path.name)
    if not m:
        raise SystemExit(f"Unrecognized chunk filename: {chunk_path.name}")
    a, b = int(m.group(1)), int(m.group(2))
    if a != b:
        raise SystemExit(f"Only single-page chunks are supported (got {chunk_path.name})")
    return a


_WS_RE = re.compile(r"\s+")
_LEADING_BULLET_RE = re.compile(r"^[\s•·●⚫\-–—]+")
_TRIVIAL_TEXT_RE = re.compile(r"^[\s\d\W]+$", flags=re.UNICODE)


def _norm_text(s: str) -> str:
    t = (s or "").replace("\u00a0", " ")
    t = _WS_RE.sub(" ", t)
    return t.strip()


def _strip_ws(s: str) -> str:
    return _WS_RE.sub("", (s or ""))


class _PageTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._page_depth = 0
        self._skip_depth = 0
        self.texts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = (tag or "").lower()
        if t in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t == "div":
            cls = ""
            for k, v in attrs or []:
                if (k or "").lower() == "class" and isinstance(v, str):
                    cls = v
                    break
            classes = {x for x in (cls or "").split() if x}
            if self._page_depth == 0 and "page" in classes:
                self._page_depth = 1
                return
            if self._page_depth > 0:
                self._page_depth += 1

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t in {"script", "style", "noscript"}:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t == "div" and self._page_depth > 0:
            self._page_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or self._page_depth <= 0:
            return
        if not data or not data.strip():
            return
        self.texts.append(data)

    def handle_comment(self, data: str) -> None:
        # Comments are never rendered; ignore them.
        return


def _extract_page_text_fragments_from_chunk_html(*, chunk_html: str) -> list[str]:
    t = chunk_html or ""
    m = re.search(r"(?is)<!--\s*PAGE_START\s*-->(.*?)<!--\s*PAGE_END\s*-->", t)
    snippet = (m.group(1) if m else t) or ""
    p = _PageTextExtractor()
    try:
        p.feed(snippet)
    except Exception:
        # Best-effort extraction; if parsing fails, return empty to avoid false positives.
        return []
    out: list[str] = []
    for x in p.texts:
        nx = _norm_text(x)
        if nx:
            out.append(nx)
    return out


def _allowed_text_pool_from_meta(*, meta_json: dict[str, Any]) -> tuple[str, str]:
    meta = meta_json if isinstance(meta_json, dict) else {}
    parts: list[str] = []
    for it in (meta.get("text_paragraphs") or []):
        if not isinstance(it, dict):
            continue
        zh = it.get("zh")
        en = it.get("en")
        if isinstance(zh, str) and zh.strip():
            parts.append(_norm_text(zh))
        if isinstance(en, str) and en.strip():
            parts.append(_norm_text(en))
    allowed_concat = "\n".join([p for p in parts if p])
    allowed_no_ws_lower = _strip_ws(allowed_concat).lower()
    return allowed_concat, allowed_no_ws_lower


def _fragment_allowed(*, fragment: str, allowed_concat: str, allowed_no_ws_lower: str) -> bool:
    t = _norm_text(fragment)
    if not t:
        return True
    # Ignore trivial punctuation-only / digit-only bits.
    if len(t) <= 2 and _TRIVIAL_TEXT_RE.fullmatch(t):
        return True

    t_no_ws_lower = _strip_ws(t).lower()
    if t in allowed_concat:
        return True
    if t_no_ws_lower and t_no_ws_lower in allowed_no_ws_lower:
        return True

    # Allow leading bullets/dashes added by formatting.
    t2 = _LEADING_BULLET_RE.sub("", t)
    if t2 and t2 != t:
        t2_no_ws_lower = _strip_ws(t2).lower()
        if t2 in allowed_concat:
            return True
        if t2_no_ws_lower and t2_no_ws_lower in allowed_no_ws_lower:
            return True

    # If model concatenated zh/en into one node, accept if both sides are individually allowed.
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", t))
    has_latin = bool(re.search(r"[A-Za-z]", t))
    if has_cjk and has_latin:
        # Try common separators where left is zh and right is en.
        for sep in ["：", ":", "/", "／", "|", "｜"]:
            if sep not in t:
                continue
            left, right = t.split(sep, 1)
            left = left.strip()
            right = right.strip()
            if not left or not right:
                continue
            left2 = left + sep if sep in {"：", ":"} else left
            if _fragment_allowed(fragment=left2, allowed_concat=allowed_concat, allowed_no_ws_lower=allowed_no_ws_lower) and _fragment_allowed(
                fragment=right, allowed_concat=allowed_concat, allowed_no_ws_lower=allowed_no_ws_lower
            ):
                return True

    return False


def _detect_extra_text(
    *,
    chunk_html: str,
    meta_json: dict[str, Any] | None,
    max_items: int = 40,
) -> list[str]:
    if not isinstance(meta_json, dict):
        return []
    allowed_concat, allowed_no_ws_lower = _allowed_text_pool_from_meta(meta_json=meta_json)
    if not allowed_concat.strip():
        return []

    fragments = _extract_page_text_fragments_from_chunk_html(chunk_html=chunk_html)
    extras: list[str] = []
    seen: set[str] = set()
    for f in fragments:
        if _fragment_allowed(fragment=f, allowed_concat=allowed_concat, allowed_no_ws_lower=allowed_no_ws_lower):
            continue
        if f in seen:
            continue
        seen.add(f)
        extras.append(f)
        if len(extras) >= max_items:
            break
    return extras


def _qa_one_chunk(
    *,
    pw_page: Any,
    chunk_path: Path,
    autoshrink_opts: dict[str, Any],
    meta_json: dict[str, Any] | None,
) -> dict[str, Any]:
    pw_page.goto(chunk_path.as_uri(), wait_until="load")
    pw_page.evaluate("() => (document.fonts ? document.fonts.ready : true)")
    pw_page.evaluate(AUTO_SHRINK_JS, autoshrink_opts)

    ov = pw_page.evaluate(PAGE_OVERFLOW_QA_JS) or {}
    cl = pw_page.evaluate(CLIPPED_TEXT_QA_JS) or {}
    overflow_bad = (ov.get("bad") or []) if isinstance(ov, dict) else []
    clipped_bad = (cl.get("bad") or []) if isinstance(cl, dict) else []
    extra_text_bad: list[str] = []
    try:
        chunk_html = chunk_path.read_text(encoding="utf-8", errors="replace")
        extra_text_bad = _detect_extra_text(chunk_html=chunk_html, meta_json=meta_json)
    except Exception:
        extra_text_bad = []

    # Normalize to plain lists.
    if not isinstance(overflow_bad, list):
        overflow_bad = []
    if not isinstance(clipped_bad, list):
        clipped_bad = []

    ok = (len(overflow_bad) == 0) and (len(clipped_bad) == 0) and (len(extra_text_bad) == 0)
    return {
        "ok": ok,
        "overflow_bad": overflow_bad,
        "clipped_bad": clipped_bad,
        "extra_text_bad": extra_text_bad,
    }


def _qa_summary(*, page_no: int, qa: dict[str, Any]) -> str:
    parts: list[str] = []
    overflow_bad = qa.get("overflow_bad") or []
    clipped_bad = qa.get("clipped_bad") or []
    extra_text_bad = qa.get("extra_text_bad") or []
    if isinstance(overflow_bad, list) and overflow_bad:
        x = overflow_bad[0] if isinstance(overflow_bad[0], dict) else {}
        parts.append(f"p{page_no} overflow: h {x.get('scrollHeight')}/{x.get('clientHeight')}, w {x.get('scrollWidth')}/{x.get('clientWidth')}")
    if isinstance(clipped_bad, list) and clipped_bad:
        # Largest height deltas first.
        items = [x for x in clipped_bad if isinstance(x, dict)]
        items.sort(key=lambda z: float(z.get("scrollHeight") or 0) - float(z.get("clientHeight") or 0), reverse=True)
        for x in items[:3]:
            cls = (x.get("cls") or "").strip()
            cls_short = "." + ".".join(cls.split()[:2]) if cls else ""
            ref = (x.get("ref") or "").strip()
            ref_part = f' ref="{ref}"' if ref else ""
            parts.append(f"p{page_no} clipped: {x.get('tag')}{cls_short}{ref_part} h {x.get('scrollHeight')}/{x.get('clientHeight')}")
    if isinstance(extra_text_bad, list) and extra_text_bad:
        parts.append(f"p{page_no} extra_text: {len(extra_text_bad)}")
    return " | ".join(parts).strip()


def _build_repair_prompt(
    *,
    meta_json: dict[str, Any],
    css_doc: str,
    layout_notes_brief: str,
    current_page_block: str,
    qa: dict[str, Any],
    autoshrink_opts: dict[str, Any],
    attempt_no: int,
) -> str:
    page_no = int(meta_json.get("page_1based") or 0) or 0
    overflow_bad = qa.get("overflow_bad") or []
    clipped_bad = qa.get("clipped_bad") or []
    extra_text_bad = qa.get("extra_text_bad") or []

    failures: list[str] = []
    if isinstance(overflow_bad, list) and overflow_bad:
        x = overflow_bad[0] if isinstance(overflow_bad[0], dict) else {}
        failures.append(f"- Page overflow: h {x.get('scrollHeight')}/{x.get('clientHeight')}, w {x.get('scrollWidth')}/{x.get('clientWidth')}")
    if isinstance(clipped_bad, list) and clipped_bad:
        items = [x for x in clipped_bad if isinstance(x, dict)]
        items.sort(key=lambda z: float(z.get("scrollHeight") or 0) - float(z.get("clientHeight") or 0), reverse=True)
        for x in items[:6]:
            failures.append(
                "- Clipped/overflowing text inside container: "
                + f"tag={x.get('tag')} class={x.get('cls')} data-ref={x.get('ref')} "
                + f"h {x.get('scrollHeight')}/{x.get('clientHeight')}, w {x.get('scrollWidth')}/{x.get('clientWidth')} "
                + (f'text="{x.get("text")}"' if x.get("text") else "")
            )
    if isinstance(extra_text_bad, list) and extra_text_bad:
        for x in [t for t in extra_text_bad if isinstance(t, str)][:12]:
            failures.append(
                f"- Detected extra text NOT in meta_json.text_paragraphs (forbidden: OCR/transcribing image text into the page): {x!r}"
            )
    failures_txt = "\n".join(failures).strip()

    # Embed current page block for iterative repair. Keep it short if huge.
    cur = (current_page_block or "").strip()
    if len(cur) > 18_000:
        cur = cur[:18_000] + "\n...(truncated)...\n"

    qa_json = {
        "overflow_bad": overflow_bad if isinstance(overflow_bad, list) else [],
        "clipped_bad": clipped_bad if isinstance(clipped_bad, list) else [],
        "extra_text_bad": extra_text_bad if isinstance(extra_text_bad, list) else [],
    }
    qa_json_txt = json.dumps(qa_json, ensure_ascii=False, indent=2)

    return (
        "[CURRENT PAGE HTML (edit based on this)]\n"
        + cur
        + "\n\n[QA RESULT JSON (measured after autoshrink)]\n"
        + qa_json_txt
        + "\n\n[ISSUE SUMMARY]\n"
        + (failures_txt or "(none?)")
        + "\n\n[WHAT YOU NEED TO DO]\n"
        + "- Starting from the CURRENT PAGE HTML above, make adjustments (prefer small targeted edits: layout structure/spacing/columns/listification, etc.) to fix overflow/clipped/extra-text issues.\n"
        + "- Goal: after autoshrink, this page passes QA (no page overflow; no clipped text; do NOT rely on overflow:hidden to hide text).\n"
        + f"- For reference: autoshrink params minFontPt={autoshrink_opts.get('minFontPt')}, minScale={autoshrink_opts.get('minScale')}, maxIters={autoshrink_opts.get('maxIters')}.\n"
        + "\nlayout_notes_brief (only layout reference):\n"
        + (layout_notes_brief or "")
        + "\n\nmeta_json (for required_refs / exact source text / image src):\n"
        + json.dumps(meta_json, ensure_ascii=False, indent=2)
        + "\n\ncss_library_doc (available classes):\n"
        + (css_doc or "")
        + "\n\n[OUTPUT FORMAT (MUST follow)]\n"
        + "- Output HTML only. No explanation. No markdown code fences.\n"
        + "- First line MUST be `<!-- PAGE_START -->`, last line MUST be `<!-- PAGE_END -->`.\n"
        + '- Between the markers must contain exactly ONE `<div class="page">...</div>`.\n'
        + "\n[HARD CONSTRAINTS (MUST follow)]\n"
        + "- Text must be verbatim: each zh/en in meta_json.text_paragraphs must be copied character-for-character into the page (you may split into multiple lines/tags, but must not rewrite/delete).\n"
        + "- No extra text: the page may contain ONLY meta_json.text_paragraphs text. Do NOT OCR/read/transcribe any text from page_png/images/image descriptions into HTML.\n"
        + "- Bilingual order: Chinese first, English second (zh then en).\n"
        + "- Do NOT use `overflow:hidden` to hide text; `overflow:hidden` is only allowed for pure image frames (e.g. img-frame).\n"
        + "- Forbidden: overflow:auto/scroll; <style>; new CSS files; background-image/url(); position:absolute/top/left.\n"
        + "- data-ref values must come from required_refs.all, and you MUST cover every item in required_refs.all; do NOT invent new data-ref.\n"
    )


def _extract_page_block_from_model_output(*, html_gen_mod: Any, raw: str) -> str | None:
    # Prefer the shared extractor if available (handles code fences + scoring).
    if hasattr(html_gen_mod, "_extract_page_markup"):
        return html_gen_mod._extract_page_markup(raw)  # type: ignore[attr-defined]

    t = _strip_fences(raw)
    m = re.search(r"(?is)<!--\s*PAGE_START\s*-->\s*(.*?)\s*<!--\s*PAGE_END\s*-->", t)
    if not m:
        return None
    inner = (m.group(1) or "").strip()
    if not inner or not re.search(r'(?is)\bclass\s*=\s*["\']page\b', inner):
        return None
    return "<!-- PAGE_START -->\n" + inner + "\n<!-- PAGE_END -->\n"


def _call_gemini_with_retries(
    *,
    html_gen_mod: Any,
    client: Any,
    genai_errors: Any,
    model: str,
    max_output_tokens: int,
    temperature: float,
    user_prompt: str,
    page_png_bytes: bytes | None,
    thinking_budget: int | None,
    retries: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    retry_backoff: float,
) -> tuple[str, Exception | None]:
    raw: str = ""
    last_err: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        try:
            raw = html_gen_mod._call_gemini(  # type: ignore[attr-defined]
                client=client,
                model=model,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                user_prompt=user_prompt,
                page_png_bytes=page_png_bytes,
                thinking_budget=thinking_budget,
            )
            return raw, None
        except Exception as e:
            last_err = e
            retryable = False
            code = None
            api_err_cls = getattr(genai_errors, "APIError", None)
            if api_err_cls and isinstance(e, api_err_cls):
                try:
                    code = int(getattr(e, "code", 0) or 0)
                except Exception:
                    code = None
                retryable = code in {429, 500, 502, 503, 504}
            if isinstance(e, (TimeoutError, ConnectionError)):
                retryable = True
            if (not retryable) or attempt >= int(retries):
                return raw, last_err
            sleep_s = min(
                float(retry_max_seconds),
                float(retry_base_seconds) * (float(retry_backoff) ** float(attempt)),
            )
            time.sleep(max(0.0, float(sleep_s)))
    return raw, last_err


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Auto QA + model repair for generated chunk HTML, then export PDF.")
    p.add_argument(
        "bundle_dir",
        type=str,
        help="Generated output bundle dir (has chunks/, css_library.css, page_XXX/request.json).",
    )
    p.add_argument(
        "--layout-notes-dir",
        type=str,
        default="",
        help=(
            "Optional layout-notes output dir (e.g. layout_notes_brief/). "
            "If provided, the repair prompt will read per-page layout_notes.txt from it."
        ),
    )
    p.add_argument("--out-pdf", type=str, default="", help="Output PDF path (default: <bundle_dir>/out_repaired.pdf)")

    p.add_argument("--max-rounds", type=int, default=3, help="Max QA rounds total (default: 3 = initial + 2 repairs).")
    p.add_argument(
        "--max-repairs-per-page",
        type=int,
        default=2,
        help="Max repairs per page (default: 2).",
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

    p.add_argument("--title", type=str, default="PPTAgent", help="HTML <title> text (default: PPTAgent)")
    p.add_argument("--save-raw", action="store_true", help="Save raw model output + prompts per repair attempt.")
    p.add_argument("--dry-run", action="store_true", help="Run QA + report only; do not call model; still export PDF.")

    # Autoshrink knobs (must match QA + export).
    p.add_argument("--autoshrink-min-font-pt", type=float, default=11.0, help="Autoshrink minimum font size in pt.")
    p.add_argument("--autoshrink-min-scale", type=float, default=0.6, help="Autoshrink minimum scale factor.")
    p.add_argument("--autoshrink-max-iters", type=int, default=8, help="Autoshrink iterations per page.")

    return p.parse_args()


def main() -> int:
    args = _parse_args()
    bundle_dir = Path(args.bundle_dir).expanduser().resolve()
    if not bundle_dir.exists():
        raise SystemExit(f"bundle_dir not found: {bundle_dir}")

    # Prefer a single source of layout notes for the model (brief-only).
    layout_notes_dir: Path | None = None
    if str(getattr(args, "layout_notes_dir", "") or "").strip():
        cand = Path(str(args.layout_notes_dir)).expanduser().resolve()
        if not cand.exists():
            raise SystemExit(f"layout-notes-dir not found: {cand}")
        layout_notes_dir = cand
    else:
        # New repo default: sibling layout_notes_brief under the run root.
        cand2 = bundle_dir.parent / "layout_notes_brief"
        if cand2.exists() and cand2.is_dir():
            layout_notes_dir = cand2

    chunks_dir = bundle_dir / "chunks"
    if not chunks_dir.exists():
        raise SystemExit(f"chunks/ not found under: {bundle_dir}")

    chunk_files = sorted(chunks_dir.glob("chunk_*.html"))
    if not chunk_files:
        raise SystemExit(f"No chunk_*.html files under: {chunks_dir}")

    # Load shared HTML generator helpers for model call + parsing/validation.
    gen_html_path = Path(__file__).resolve().parent / "generate_html.py"
    if not gen_html_path.exists():
        raise SystemExit(f"generate_html.py not found: {gen_html_path}")
    html_gen_mod = _load_module_from_path(name="generate_html", path=gen_html_path)

    css_doc_path = Path(__file__).resolve().parent / "css_library.md"
    if not css_doc_path.exists():
        raise SystemExit(f"css_library doc not found: {css_doc_path}")
    css_doc = css_doc_path.read_text(encoding="utf-8", errors="replace").strip()

    autoshrink_opts = {
        "minFontPt": float(args.autoshrink_min_font_pt),
        "minScale": float(args.autoshrink_min_scale),
        "maxIters": int(args.autoshrink_max_iters),
    }

    max_rounds = max(1, int(args.max_rounds))
    max_repairs_per_page = max(0, int(args.max_repairs_per_page))
    thinking_budget: int | None = int(args.thinking_budget) if int(args.thinking_budget) >= 0 else None

    # Track per-page repair attempts.
    attempts: dict[int, int] = {}
    report: dict[str, Any] = {
        "bundle_dir": str(bundle_dir),
        "autoshrink": autoshrink_opts,
        "max_rounds": max_rounds,
        "max_repairs_per_page": max_repairs_per_page,
        "pages": {},  # page_no -> info
    }

    # First QA pass on all pages; subsequent passes only on modified pages.
    pending_pages: set[int] = {_chunk_page_no(cf) for cf in chunk_files}
    modified_pages: set[int] = set()

    api_key = ""
    genai = None
    genai_errors = None
    client_ctx = None

    if not bool(args.dry_run):
        api_key = html_gen_mod._get_api_key(args)  # type: ignore[attr-defined]
        genai, _types, genai_errors = html_gen_mod._import_google_genai()  # type: ignore[attr-defined]
        client_ctx = genai.Client(api_key=api_key)  # type: ignore[union-attr]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pw_page = browser.new_page(viewport={"width": 1280, "height": 720})

        # Lazy-open model client so dry-run needs no key.
        client = client_ctx.__enter__() if client_ctx else None  # type: ignore[union-attr]
        try:
            for round_no in range(0, max_rounds):
                # QA only pages that might have changed since last round.
                pages_to_check = sorted(modified_pages) if (round_no > 0 and modified_pages) else sorted(pending_pages)
                modified_pages = set()

                failing: dict[int, dict[str, Any]] = {}
                for page_no in pages_to_check:
                    chunk_path = chunks_dir / f"chunk_{page_no:03d}_{page_no:03d}.html"
                    if not chunk_path.exists():
                        continue
                    meta_json_for_qa: dict[str, Any] | None = None
                    req_path_for_qa = bundle_dir / f"page_{page_no:03d}" / "request.json"
                    if req_path_for_qa.exists():
                        req0 = _read_json(req_path_for_qa)
                        if isinstance(req0, dict):
                            mj0 = req0.get("meta_json")
                            if isinstance(mj0, dict):
                                meta_json_for_qa = mj0

                    qa = _qa_one_chunk(
                        pw_page=pw_page,
                        chunk_path=chunk_path,
                        autoshrink_opts=autoshrink_opts,
                        meta_json=meta_json_for_qa,
                    )
                    info = report["pages"].get(str(page_no), {})
                    info["last_qa"] = qa
                    info["last_qa_summary"] = _qa_summary(page_no=page_no, qa=qa)
                    report["pages"][str(page_no)] = info
                    if not qa.get("ok"):
                        failing[page_no] = qa

                if not failing:
                    report["qa_status"] = "pass"
                    break

                # Stop if dry-run.
                if bool(args.dry_run):
                    report["qa_status"] = "fail_dry_run"
                    break

                # Repair pages that still fail and have remaining budget.
                to_repair = [pno for pno in sorted(failing.keys()) if attempts.get(pno, 0) < max_repairs_per_page]
                if not to_repair:
                    report["qa_status"] = "fail_no_budget"
                    break

                for page_no in to_repair:
                    attempts[page_no] = attempts.get(page_no, 0) + 1
                    attempt_no = attempts[page_no]
                    chunk_path = chunks_dir / f"chunk_{page_no:03d}_{page_no:03d}.html"
                    page_dir = bundle_dir / f"page_{page_no:03d}"
                    req_path = page_dir / "request.json"
                    if not chunk_path.exists():
                        continue

                    # meta_json:
                    # - Prefer the layout-notes dir request.json (it is the meta_json itself)
                    # - Fallback to bundle page_XXX/request.json["meta_json"]
                    meta_json: dict[str, Any] | None = None
                    if layout_notes_dir:
                        meta_path2 = layout_notes_dir / f"page_{page_no:03d}" / "request.json"
                        if meta_path2.exists():
                            meta2 = _read_json(meta_path2)
                            if isinstance(meta2, dict):
                                meta_json = meta2
                    if meta_json is None:
                        if not req_path.exists():
                            continue
                        req = _read_json(req_path)
                        if not isinstance(req, dict):
                            continue
                        mj = req.get("meta_json")
                        if isinstance(mj, dict):
                            meta_json = mj
                    if meta_json is None:
                        continue

                    # layout_notes (brief-only, exact text):
                    # - Prefer layout-notes dir page_XXX/layout_notes.txt
                    # - Fallback to bundle page_XXX/request.json["layout_notes"]
                    layout_notes_brief = ""
                    if layout_notes_dir:
                        notes_path2 = layout_notes_dir / f"page_{page_no:03d}" / "layout_notes.txt"
                        if notes_path2.exists():
                            layout_notes_brief = notes_path2.read_text(encoding="utf-8", errors="replace").strip()
                    if not layout_notes_brief:
                        if req_path.exists():
                            req = _read_json(req_path)
                            if isinstance(req, dict):
                                ln = req.get("layout_notes")
                                if isinstance(ln, str):
                                    layout_notes_brief = ln.strip()
                    if not layout_notes_brief:
                        layout_notes_brief = "(layout_notes missing)"

                    # Load page PNG (multimodal) if present.
                    page_png_bytes = None
                    page_png_path = meta_json.get("page_png_path")
                    if isinstance(page_png_path, str) and page_png_path.strip():
                        ppath = Path(page_png_path).expanduser()
                        if ppath.exists():
                            try:
                                page_png_bytes = ppath.read_bytes()
                            except Exception:
                                page_png_bytes = None

                    chunk_html0 = chunk_path.read_text(encoding="utf-8", errors="replace")
                    current_page_block = _extract_page_block_from_chunk_html(chunk_html0) or ""

                    user_prompt = _build_repair_prompt(
                        meta_json=meta_json,
                        css_doc=css_doc,
                        layout_notes_brief=layout_notes_brief,
                        current_page_block=current_page_block,
                        qa=failing[page_no],
                        autoshrink_opts=autoshrink_opts,
                        attempt_no=attempt_no,
                    )

                    if bool(args.save_raw):
                        _write_text(page_dir / f"repair_round{attempt_no}_prompt.txt", user_prompt)

                    raw, err = _call_gemini_with_retries(
                        html_gen_mod=html_gen_mod,
                        client=client,
                        genai_errors=genai_errors,
                        model=str(args.model),
                        max_output_tokens=int(args.max_tokens),
                        temperature=float(args.temperature),
                        user_prompt=user_prompt,
                        page_png_bytes=page_png_bytes,
                        thinking_budget=thinking_budget,
                        retries=int(args.retries),
                        retry_base_seconds=float(args.retry_base_seconds),
                        retry_max_seconds=float(args.retry_max_seconds),
                        retry_backoff=float(args.retry_backoff),
                    )
                    if err is not None:
                        info = report["pages"].get(str(page_no), {})
                        info.setdefault("repair_errors", []).append(f"{type(err).__name__}: {err}")
                        report["pages"][str(page_no)] = info
                        continue

                    if bool(args.save_raw) and raw:
                        _write_text(page_dir / f"repair_round{attempt_no}_raw.txt", raw)

                    page_block = _extract_page_block_from_model_output(html_gen_mod=html_gen_mod, raw=raw)
                    if not page_block and hasattr(html_gen_mod, "_build_format_repair_prompt"):
                        repair_prompt = html_gen_mod._build_format_repair_prompt(  # type: ignore[attr-defined]
                            user_prompt=user_prompt,
                            last_raw=raw,
                        )
                        raw2, err2 = _call_gemini_with_retries(
                            html_gen_mod=html_gen_mod,
                            client=client,
                            genai_errors=genai_errors,
                            model=str(args.model),
                            max_output_tokens=int(args.max_tokens),
                            temperature=float(args.temperature),
                            user_prompt=repair_prompt,
                            page_png_bytes=page_png_bytes,
                            thinking_budget=thinking_budget,
                            retries=int(args.retries),
                            retry_base_seconds=float(args.retry_base_seconds),
                            retry_max_seconds=float(args.retry_max_seconds),
                            retry_backoff=float(args.retry_backoff),
                        )
                        if bool(args.save_raw) and raw2:
                            _write_text(page_dir / f"repair_round{attempt_no}_raw_format_repair.txt", raw2)
                        if err2 is None:
                            page_block = _extract_page_block_from_model_output(html_gen_mod=html_gen_mod, raw=raw2)

                    if not page_block:
                        info = report["pages"].get(str(page_no), {})
                        info.setdefault("repair_errors", []).append("model_output_missing_PAGE_START_END")
                        report["pages"][str(page_no)] = info
                        continue

                    # Validate refs strictly for safety.
                    fatal_warnings: list[str] = []
                    if hasattr(html_gen_mod, "_validate_page_block"):
                        warnings = html_gen_mod._validate_page_block(meta=meta_json, page_block=page_block)  # type: ignore[attr-defined]
                        if isinstance(warnings, list):
                            for w in warnings:
                                if isinstance(w, str) and (
                                    w.startswith("missing_data_ref:")
                                    or w.startswith("unknown_data_ref:")
                                    or w.startswith("forbidden_css: overflow auto/scroll")
                                ):
                                    fatal_warnings.append(w)

                    if fatal_warnings:
                        # One deterministic validation-repair attempt.
                        repair_prompt2 = (
                            "Your output failed ref/constraint validation (data-ref or forbidden items). Please fix and output again. "
                            "After autoshrink it must still pass QA (no overflow, no clipped text).\n"
                            + "Issues:\n- "
                            + "\n- ".join(fatal_warnings[:8])
                            + "\n\nOriginal input (repair based on this; do NOT rewrite meta_json.text_paragraphs):\n\n"
                            + user_prompt
                        )
                        raw3, err3 = _call_gemini_with_retries(
                            html_gen_mod=html_gen_mod,
                            client=client,
                            genai_errors=genai_errors,
                            model=str(args.model),
                            max_output_tokens=int(args.max_tokens),
                            temperature=float(args.temperature),
                            user_prompt=repair_prompt2,
                            page_png_bytes=page_png_bytes,
                            thinking_budget=thinking_budget,
                            retries=int(args.retries),
                            retry_base_seconds=float(args.retry_base_seconds),
                            retry_max_seconds=float(args.retry_max_seconds),
                            retry_backoff=float(args.retry_backoff),
                        )
                        if bool(args.save_raw) and raw3:
                            _write_text(page_dir / f"repair_round{attempt_no}_raw_validation_repair.txt", raw3)
                        if err3 is None:
                            page_block2 = _extract_page_block_from_model_output(html_gen_mod=html_gen_mod, raw=raw3)
                            if page_block2:
                                page_block = page_block2

                    # Backup current chunk before overwriting.
                    backup_dir = bundle_dir / "_qa_repair_backups" / f"round{round_no + 1}"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup_path = backup_dir / chunk_path.name
                    if not backup_path.exists():
                        backup_path.write_text(chunk_html0, encoding="utf-8")

                    # Write updated chunk + page_block.html for inspection.
                    chunk_html1 = _replace_page_block_in_chunk_html(chunk_html=chunk_html0, new_page_block=page_block)
                    _write_text(chunk_path, chunk_html1)
                    _write_text(page_dir / "page_block.html", page_block)

                    modified_pages.add(page_no)

            # Always rebuild index.html from chunks to reflect latest fixes (deterministic).
            if hasattr(html_gen_mod, "_assemble_preview_html"):
                html_gen_mod._assemble_preview_html(out_bundle_dir=bundle_dir, title=str(args.title))  # type: ignore[attr-defined]
        finally:
            if client_ctx:
                client_ctx.__exit__(None, None, None)  # type: ignore[union-attr]
            browser.close()

    # Final export: always produce a PDF, even if QA still fails.
    out_pdf = Path(args.out_pdf).expanduser().resolve() if args.out_pdf else (bundle_dir / "out_repaired.pdf")
    index_html = bundle_dir / "index.html"
    if not index_html.exists():
        # Last resort: assemble from chunks quickly (no head reuse).
        pages: list[str] = []
        for cf in chunk_files:
            txt = cf.read_text(encoding="utf-8", errors="replace")
            pb = _extract_page_block_from_chunk_html(txt)
            if pb:
                inner = re.sub(r"(?is)<!--\s*PAGE_START\s*-->|<!--\s*PAGE_END\s*-->", "", pb).strip()
                pages.append(inner)
        _write_text(
            index_html,
            "\n".join(
                [
                    "<!doctype html>",
                    '<html lang="zh">',
                    "<head>",
                    '<base href="./">',
                    '<meta charset="utf-8" />',
                    '<meta name="viewport" content="width=device-width, initial-scale=1" />',
                    f"<title>{args.title}</title>",
                    '<link rel="stylesheet" href="css_library.css" />',
                    "</head>",
                    "<body>",
                    "\n\n".join(pages),
                    "</body>",
                    "</html>",
                    "",
                ]
            ),
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(index_html.as_uri(), wait_until="load")
        page.add_style_tag(content=OFFICE_FRIENDLY_FONTS_CSS)
        page.evaluate("() => (document.fonts ? document.fonts.ready : true)")
        page.evaluate(AUTO_SHRINK_JS, autoshrink_opts)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        page.pdf(path=str(out_pdf), print_background=True, prefer_css_page_size=True)
        browser.close()

    report["out_pdf"] = str(out_pdf)
    report_path = bundle_dir / "qa_repair_report.json"
    _write_json(report_path, report)

    print(str(out_pdf))
    print(str(report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

