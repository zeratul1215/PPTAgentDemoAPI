from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse


PipelineMode = Literal["single_model", "mixed_models"]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


REPO_ROOT = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("PPTAGENT_DATA_DIR", str(REPO_ROOT / "result"))).expanduser().resolve()
JOBS_DIR = DATA_DIR / "jobs"

API_TOKEN = (os.getenv("PPTAGENT_API_TOKEN") or "").strip()
MAX_UPLOAD_MB = int(os.getenv("PPTAGENT_MAX_UPLOAD_MB", "50"))
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("PPTAGENT_MAX_CONCURRENT_JOBS", "1")))

DEFAULT_PIPELINE_MODE: PipelineMode = (
    "mixed_models"
    if (os.getenv("PPTAGENT_DEFAULT_PIPELINE_MODE") or "").strip().lower() == "mixed_models"
    else "single_model"
)

# single_model defaults
DEFAULT_MODEL = (os.getenv("PPTAGENT_DEFAULT_MODEL") or "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"
DEFAULT_THINKING_BUDGET = int(os.getenv("PPTAGENT_DEFAULT_THINKING_BUDGET", "0"))

# mixed_models defaults
DEFAULT_MODEL_TRANSLATE = (
    (os.getenv("PPTAGENT_DEFAULT_MODEL_TRANSLATE") or "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"
)
DEFAULT_MODEL_IMAGE_DESC = (
    (os.getenv("PPTAGENT_DEFAULT_MODEL_IMAGE_DESC") or "gemma-4-31b-it").strip() or "gemma-4-31b-it"
)
DEFAULT_MODEL_REST = (os.getenv("PPTAGENT_DEFAULT_MODEL_REST") or "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"
DEFAULT_THINKING_BUDGET_REST = int(os.getenv("PPTAGENT_DEFAULT_THINKING_BUDGET_REST", "0"))


def _require_auth(authorization: str | None) -> None:
    if not API_TOKEN:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing Authorization: Bearer <token>")
    token = authorization[7:].strip()
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


def _require_auth_with_query(authorization: str | None, token_q: str | None) -> None:
    """
    Same auth as _require_auth, but allow passing the token via query param.

    This is useful for loading HTML/CSS/img assets in a browser, because <img>/<link>
    requests cannot attach Authorization headers.
    """
    if not API_TOKEN:
        return
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token == API_TOKEN:
            return
    if (token_q or "").strip() == API_TOKEN:
        return
    raise HTTPException(
        status_code=401,
        detail="missing/invalid token (use Authorization header or ?token=...)",
    )


def _normalize_pipeline_mode(raw: str | None) -> PipelineMode:
    v = (raw or "").strip().lower()
    if not v:
        return DEFAULT_PIPELINE_MODE
    if v in ("single_model", "mixed_models"):
        return v  # type: ignore[return-value]
    raise HTTPException(status_code=400, detail="invalid pipeline_mode (use single_model|mixed_models)")


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "status.json"


def _logs_path(job_id: str) -> Path:
    return _job_dir(job_id) / "logs.txt"


def _read_status(job_id: str) -> dict[str, Any]:
    p = _status_path(job_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="job not found")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        raise HTTPException(status_code=500, detail="job status corrupted")


def _write_status(job_id: str, data: dict[str, Any]) -> None:
    p = _status_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_status(job_id: str, **patch: Any) -> None:
    data = _read_status(job_id)
    data.update(patch)
    data["updated_at"] = _now_iso()
    _write_status(job_id, data)


def _append_log(job_id: str, line: str) -> None:
    lp = _logs_path(job_id)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with lp.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _tail_text(path: Path, max_lines: int) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_lines <= 0:
        return text
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]) + ("\n" if lines else "")


def _progress_hint_from_line(line: str) -> tuple[int | None, str | None]:
    l = line.strip()
    if "render_pages_png.py" in l:
        return 5, "render pages_png"
    if re.search(r"/extract_ref_html\.py(\s|$)", l):
        return 15, "extract reference html"
    if re.search(r"/build_plan\.py(\s|$)", l):
        return 25, "build plan.json"
    if re.search(r"/translate_text\.py(\s|$)", l):
        return 40, "translate text"
    if re.search(r"/describe_images\.py(\s|$)", l):
        return 50, "image descriptions"
    if re.search(r"/generate_layout_notes\.py(\s|$)", l):
        return 60, "layout notes"
    if re.search(r"/generate_html\.py(\s|$)", l):
        return 75, "html generation"
    if re.search(r"/qa_repair_export\.py(\s|$)", l):
        return 90, "QA + repair + export"
    return None, None


async def _run_pipeline_job(job_id: str) -> None:
    async with _semaphore:
        status = _read_status(job_id)
        job_dir = _job_dir(job_id)

        input_pdf = Path(str(status.get("input_pdf") or "")).expanduser().resolve()
        if not input_pdf.exists():
            raise RuntimeError(f"missing input_pdf: {input_pdf}")

        mode: PipelineMode = _normalize_pipeline_mode(status.get("pipeline_mode"))

        page_start = int(status.get("page_start") or 1)
        page_end = int(status.get("page_end") or 0)
        dpi = float(status.get("dpi") or 150.0)

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"

        if mode == "mixed_models":
            script = REPO_ROOT / "script" / "run_pipeline_mixed_models.py"
            cmd = [
                sys.executable,
                str(script),
                str(input_pdf),
                "--out-root",
                str(job_dir),
                "--page-start",
                str(page_start),
                "--page-end",
                str(page_end),
                "--dpi",
                str(dpi),
                "--model-translate",
                str(status.get("model_translate") or DEFAULT_MODEL_TRANSLATE),
                "--model-image-desc",
                str(status.get("model_image_desc") or DEFAULT_MODEL_IMAGE_DESC),
                "--model-rest",
                str(status.get("model_rest") or DEFAULT_MODEL_REST),
                "--thinking-budget-rest",
                str(int(status.get("thinking_budget_rest") or DEFAULT_THINKING_BUDGET_REST)),
            ]
        else:
            script = REPO_ROOT / "script" / "run_pipeline_from_pdf.py"
            cmd = [
                sys.executable,
                str(script),
                str(input_pdf),
                "--out-root",
                str(job_dir),
                "--page-start",
                str(page_start),
                "--page-end",
                str(page_end),
                "--dpi",
                str(dpi),
                "--model",
                str(status.get("model") or DEFAULT_MODEL),
                "--thinking-budget",
                str(int(status.get("thinking_budget") or DEFAULT_THINKING_BUDGET)),
            ]

        if not script.exists():
            raise RuntimeError(f"pipeline script not found: {script}")

        _update_status(job_id, status="running", message="running", progress=1, started_at=_now_iso())
        _append_log(job_id, f"[{_now_iso()}] job_dir={job_dir}")
        _append_log(job_id, f"[{_now_iso()}] $ " + " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None

        result_pdf: str | None = None

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line:
                _append_log(job_id, line)

            prog, msg = _progress_hint_from_line(line)
            if prog is not None:
                cur = int(_read_status(job_id).get("progress") or 0)
                _update_status(job_id, progress=max(cur, prog), message=msg or "running")

            if line.startswith("run_dir:"):
                run_dir = line.split("run_dir:", 1)[1].strip()
                if run_dir:
                    _update_status(job_id, run_dir=run_dir)
            if line.startswith("pdf:"):
                result_pdf = line.split("pdf:", 1)[1].strip()
                if result_pdf:
                    _update_status(job_id, result_path=result_pdf)

        code = await proc.wait()
        if code != 0:
            raise RuntimeError(f"pipeline failed (exit={code})")

        # Best-effort: infer newest out_repaired.pdf
        if not result_pdf:
            candidates = sorted(job_dir.rglob("out_repaired.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                result_pdf = str(candidates[0].resolve())
                _update_status(job_id, result_path=result_pdf)

        if result_pdf and Path(result_pdf).exists():
            _update_status(job_id, status="done", message="done", progress=100, finished_at=_now_iso())
            return

        _update_status(
            job_id,
            status="error",
            message="finished but missing out_repaired.pdf",
            progress=99,
            finished_at=_now_iso(),
        )


async def _run_job(job_id: str) -> None:
    try:
        await _run_pipeline_job(job_id)
    except Exception as e:
        _append_log(job_id, f"[{_now_iso()}] ERROR: {e}")
        _update_status(job_id, status="error", message="error", error=str(e), finished_at=_now_iso())


app = FastAPI(title="PPTAgent FastAPI (Render)")

# Allow browser-based local UI (and other frontends) to call the API.
# Note: CORS only affects browsers; it is not an authentication mechanism.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
_active_tasks: dict[str, asyncio.Task[None]] = {}


@app.on_event("startup")
async def _startup() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def root():
    return {
        "ok": True,
        "repo_root": str(REPO_ROOT),
        "data_dir": str(DATA_DIR),
        "jobs_dir": str(JOBS_DIR),
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "auth_required": bool(API_TOKEN),
        "default_pipeline_mode": DEFAULT_PIPELINE_MODE,
        "endpoints": {"create_job": "POST /jobs", "get_job": "GET /jobs/{job_id}"},
    }


@app.post("/jobs")
async def create_job(
    file: UploadFile = File(...),
    pipeline_mode: str | None = Query(default=None, description="single_model|mixed_models (optional override)"),
    page_start: int = Query(default=1, ge=1),
    page_end: int = Query(default=0, ge=0),
    dpi: float = Query(default=150.0),
    # single_model
    model: str | None = Query(default=None),
    thinking_budget: int | None = Query(default=None, ge=0),
    # mixed_models
    model_translate: str | None = Query(default=None),
    model_image_desc: str | None = Query(default=None),
    model_rest: str | None = Query(default=None),
    thinking_budget_rest: int | None = Query(default=None, ge=0),
    # auth
    authorization: str | None = Header(default=None),
):
    _require_auth(authorization)

    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="only .pdf uploads are supported")

    mode = _normalize_pipeline_mode(pipeline_mode)

    job_id = uuid.uuid4().hex
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=False)

    input_path = job_dir / "input.pdf"

    size = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    try:
        with input_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"file too large (>{MAX_UPLOAD_MB}MB)")
                out.write(chunk)
    finally:
        await file.close()

    status: dict[str, Any] = {
        "job_id": job_id,
        "status": "queued",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "message": "queued",
        "progress": 0,
        "pipeline_mode": mode,
        "page_start": int(page_start),
        "page_end": int(page_end),
        "dpi": float(dpi),
        "input_pdf": str(input_path),
        "original_filename": str(file.filename),
        "input_bytes": int(size),
        "run_dir": None,
        "result_path": None,
        "error": None,
    }

    if mode == "mixed_models":
        status["model_translate"] = (model_translate or DEFAULT_MODEL_TRANSLATE).strip()
        status["model_image_desc"] = (model_image_desc or DEFAULT_MODEL_IMAGE_DESC).strip()
        status["model_rest"] = (model_rest or DEFAULT_MODEL_REST).strip()
        status["thinking_budget_rest"] = (
            int(thinking_budget_rest) if thinking_budget_rest is not None else DEFAULT_THINKING_BUDGET_REST
        )
    else:
        status["model"] = (model or DEFAULT_MODEL).strip()
        status["thinking_budget"] = int(thinking_budget) if thinking_budget is not None else DEFAULT_THINKING_BUDGET

    _write_status(job_id, status)
    _append_log(job_id, f"[{_now_iso()}] queued pipeline_mode={mode} bytes={size} filename={file.filename}")

    task = asyncio.create_task(_run_job(job_id))
    _active_tasks[job_id] = task
    task.add_done_callback(lambda _: _active_tasks.pop(job_id, None))

    return JSONResponse({"job_id": job_id})


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    download: int = Query(default=0, ge=0, le=1),
    logs_tail: int = Query(default=0, ge=0, le=2000),
    authorization: str | None = Header(default=None),
):
    _require_auth(authorization)
    status = _read_status(job_id)

    if download == 1:
        if status.get("status") not in ("done", "error"):
            raise HTTPException(status_code=409, detail="job not finished")
        rp = status.get("result_path")
        if not isinstance(rp, str) or not rp.strip():
            raise HTTPException(status_code=404, detail="result not available")
        p = Path(rp).expanduser().resolve()
        if not p.exists():
            raise HTTPException(status_code=404, detail="result file missing on disk")
        return FileResponse(path=str(p), filename=f"{job_id}.pdf", media_type="application/pdf")

    if logs_tail > 0:
        status = dict(status)
        status["logs_tail"] = _tail_text(_logs_path(job_id), logs_tail)
    return JSONResponse(status)


def _bundle_dir_for_job(job_id: str, status: dict[str, Any]) -> Path:
    """
    Resolve the generated HTML bundle directory for a job.
    Expected output structure:
      <run_dir>/html_outcome/index.html
    """
    job_dir = _job_dir(job_id)

    candidates: list[Path] = []
    run_dir = status.get("run_dir")
    if isinstance(run_dir, str) and run_dir.strip():
        candidates.append(Path(run_dir).expanduser().resolve() / "html_outcome")

    result_path = status.get("result_path")
    if isinstance(result_path, str) and result_path.strip():
        rp = Path(result_path).expanduser().resolve()
        candidates.append(rp.parent / "html_outcome")

    candidates.append(job_dir / "html_outcome")

    for cand in candidates:
        if (cand / "index.html").exists():
            return cand

    # Best-effort fallback: locate the newest html_outcome/index.html under the job dir.
    if job_dir.exists():
        matches = sorted(
            job_dir.rglob("html_outcome/index.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0].parent

    raise HTTPException(status_code=404, detail="html outcome not available for this job")


@app.get("/jobs/{job_id}/html")
async def get_job_html_index(
    job_id: str,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    _require_auth_with_query(authorization, token)
    status = _read_status(job_id)
    bundle_dir = _bundle_dir_for_job(job_id, status)
    p = bundle_dir / "index.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="index.html not found for this job")
    return FileResponse(path=str(p), media_type="text/html")


@app.get("/jobs/{job_id}/html/{asset_path:path}")
async def get_job_html_asset(
    job_id: str,
    asset_path: str,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    _require_auth_with_query(authorization, token)
    status = _read_status(job_id)
    bundle_dir = _bundle_dir_for_job(job_id, status).expanduser().resolve()

    asset_path = (asset_path or "").lstrip("/")
    if not asset_path:
        asset_path = "index.html"
    # Prevent path traversal like ../plan.json
    if ".." in Path(asset_path).parts:
        raise HTTPException(status_code=403, detail="invalid asset path")

    run_dir = bundle_dir.parent.expanduser().resolve()
    requested = (bundle_dir / asset_path)
    p = requested.resolve()

    def _is_under(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except Exception:
            return False

    # Allow assets under:
    # - bundle_dir (html_outcome/...)
    # - run_dir (to support symlinked images -> ref_html/images/...)
    if not (_is_under(p, bundle_dir) or _is_under(p, run_dir)):
        raise HTTPException(status_code=403, detail="invalid asset path")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path=str(p))

