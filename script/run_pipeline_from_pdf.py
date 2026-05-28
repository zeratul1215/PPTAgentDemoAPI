from __future__ import annotations

"""
Run the full PPTAgent PDF -> HTML -> QA/repair -> PDF pipeline (single model).

Assumption: no legacy data / consumers need compatibility.
"""

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    pages_png_dir: Path
    ref_html_dir: Path
    plan_dir: Path
    plan_json: Path
    bilingual_text_dir: Path
    image_desc_dir: Path
    layout_notes_dir: Path
    html_out_dir: Path
    out_pdf: Path


def _require_api_key_env() -> None:
    if (os.getenv("GEMINI_API_KEY") or "").strip():
        return
    if (os.getenv("GOOGLE_API_KEY") or "").strip():
        return
    raise SystemExit("Missing API key. Set GEMINI_API_KEY (or GOOGLE_API_KEY) in env.")


def _run(cmd: list[str]) -> None:
    print("\n$ " + " ".join([subprocess.list2cmdline([c]) if (" " in c) else c for c in cmd]), flush=True)
    subprocess.run(cmd, check=True)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _build_paths(*, out_root: Path, pdf_stem: str, stamp: str) -> RunPaths:
    run_dir = out_root / f"{pdf_stem}_run_{stamp}"
    pages_png_dir = run_dir / "pages_png"
    ref_html_dir = run_dir / "ref_html"
    plan_dir = run_dir / "plan_v3"
    plan_json = plan_dir / "plan.json"
    bilingual_text_dir = run_dir / "bilingual_text"
    image_desc_dir = run_dir / "image_descriptions"
    layout_notes_dir = run_dir / "layout_notes_brief"
    html_out_dir = run_dir / "html_outcome"
    out_pdf = run_dir / "out_repaired.pdf"
    return RunPaths(
        run_dir=run_dir,
        pages_png_dir=pages_png_dir,
        ref_html_dir=ref_html_dir,
        plan_dir=plan_dir,
        plan_json=plan_json,
        bilingual_text_dir=bilingual_text_dir,
        image_desc_dir=image_desc_dir,
        layout_notes_dir=layout_notes_dir,
        html_out_dir=html_out_dir,
        out_pdf=out_pdf,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run full pipeline from a PDF: render -> A..F -> QA/repair -> export PDF.")
    p.add_argument("pdf", type=str, help="Input PDF path")
    p.add_argument(
        "--out-root",
        type=str,
        default="",
        help="Output root directory (default: ./result)",
    )
    p.add_argument("--page-start", type=int, default=1, help="1-based inclusive start page (default: 1)")
    p.add_argument(
        "--page-end",
        type=int,
        default=0,
        help="1-based inclusive end page. 0 means last page (default: 0).",
    )
    p.add_argument("--dpi", type=float, default=150.0, help="DPI for page PNG rendering (default: 150)")

    p.add_argument("--model", type=str, default="gemini-3.1-flash-lite", help="Model name")
    p.add_argument(
        "--thinking-budget",
        type=int,
        default=0,
        help="Thinking budget for HTML generation/repair. 0 disables thinking (default: 0).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    _require_api_key_env()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    repo_root = Path(__file__).resolve().parents[1]
    out_root = Path(args.out_root).expanduser().resolve() if str(args.out_root).strip() else (repo_root / "result")
    out_root.mkdir(parents=True, exist_ok=True)

    stamp = _timestamp()
    paths = _build_paths(out_root=out_root, pdf_stem=str(pdf_path.stem), stamp=stamp)
    paths.run_dir.mkdir(parents=True, exist_ok=False)
    paths.plan_dir.mkdir(parents=True, exist_ok=True)

    script_dir = Path(__file__).resolve().parent
    py = sys.executable

    page_start = int(args.page_start)
    page_end = int(args.page_end)

    # 0) Whole-page PNGs (vision input).
    _run(
        [
            py,
            str(script_dir / "render_pages_png.py"),
            str(pdf_path),
            "--out",
            str(paths.pages_png_dir),
            "--dpi",
            str(float(args.dpi)),
        ]
    )

    # A) PDF -> reference HTML bundle.
    pdf_to_html_cmd = [
        py,
        str(script_dir / "extract_ref_html.py"),
        str(pdf_path),
        "--out",
        str(paths.ref_html_dir),
        "--no-ai-copy",
        "--no-assets",
    ]
    if page_start > 1:
        pdf_to_html_cmd += ["--page-start", str(page_start)]
    if page_end > 0:
        pdf_to_html_cmd += ["--page-end", str(page_end)]
    _run(pdf_to_html_cmd)

    # B) reference bundle -> plan.json.
    _run(
        [
            py,
            str(script_dir / "build_plan.py"),
            str(paths.ref_html_dir),
            "--out",
            str(paths.plan_json),
        ]
    )

    # Infer end page from plan.json for later stages.
    if page_end <= 0:
        try:
            obj = __import__("json").loads(paths.plan_json.read_text(encoding="utf-8", errors="replace"))
            pages = obj.get("pages") if isinstance(obj, dict) else None
            if isinstance(pages, list) and pages:
                page_end = len(pages)
        except Exception:
            pass
    if page_end <= 0:
        raise SystemExit("Unable to infer page-end; pass --page-end explicitly.")

    # Text recompose + translate.
    _run(
        [
            py,
            str(script_dir / "translate_text.py"),
            "--plan",
            str(paths.plan_json),
            "--page-start",
            str(page_start),
            "--page-end",
            str(page_end),
            "--out-dir",
            str(paths.bilingual_text_dir),
            "--model",
            str(args.model),
            "--max-tokens",
            "4096",
            "--temperature",
            "0",
            "--save-raw",
        ]
    )

    # Images: per-page multimodal descriptions.
    _run(
        [
            py,
            str(script_dir / "describe_images.py"),
            "--bundle-dir",
            str(paths.ref_html_dir),
            "--pages-png-dir",
            str(paths.pages_png_dir),
            "--page-start",
            str(page_start),
            "--page-end",
            str(page_end),
            "--out-dir",
            str(paths.image_desc_dir),
            "--model",
            str(args.model),
            "--max-tokens",
            "4096",
            "--temperature",
            "0",
            "--save-raw",
        ]
    )

    # Layout notes (brief-only).
    _run(
        [
            py,
            str(script_dir / "generate_layout_notes.py"),
            "--plan",
            str(paths.plan_json),
            "--pages-png-dir",
            str(paths.pages_png_dir),
            "--image-desc-dir",
            str(paths.image_desc_dir),
            "--bilingual-text-dir",
            str(paths.bilingual_text_dir),
            "--page-start",
            str(page_start),
            "--page-end",
            str(page_end),
            "--out-dir",
            str(paths.layout_notes_dir),
            "--model",
            str(args.model),
            "--max-tokens",
            "4096",
            "--temperature",
            "0",
            "--save-raw",
        ]
    )

    # Generate HTML chunks + index.html.
    _run(
        [
            py,
            str(script_dir / "generate_html.py"),
            "--bundle-dir",
            str(paths.ref_html_dir),
            "--layout-notes-dir",
            str(paths.layout_notes_dir),
            "--page-start",
            str(page_start),
            "--page-end",
            str(page_end),
            "--out-bundle-dir",
            str(paths.html_out_dir),
            "--layout-notes-mode",
            "brief",
            "--model",
            str(args.model),
            "--max-tokens",
            "8192",
            "--temperature",
            "0",
            "--thinking-budget",
            str(int(args.thinking_budget)),
            "--retries",
            "6",
            "--save-raw",
        ]
    )

    # QA + targeted repair + export.
    _run(
        [
            py,
            str(script_dir / "qa_repair_export.py"),
            str(paths.html_out_dir),
            "--out-pdf",
            str(paths.out_pdf),
            "--layout-notes-dir",
            str(paths.layout_notes_dir),
            "--model",
            str(args.model),
            "--thinking-budget",
            str(int(args.thinking_budget)),
            "--save-raw",
        ]
    )

    print("\n=== DONE ===")
    print(f"run_dir: {paths.run_dir}")
    print(f"pdf:     {paths.out_pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

