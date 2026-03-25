import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
from dotenv import load_dotenv

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

load_dotenv()

COZE_STREAM_RUN_URL = os.getenv("COZE_STREAM_RUN_URL", "https://k2h62g6g38.coze.site/stream_run")
COZE_TOKEN = os.getenv("COZE_TOKEN", "") or os.getenv("UPSTREAM_TOKEN", "")
COZE_SESSION_ID = os.getenv("COZE_SESSION_ID", "")
COZE_PROJECT_ID = os.getenv("COZE_PROJECT_ID", "")
UPSTREAM_CONNECT_TIMEOUT = float(os.getenv("UPSTREAM_CONNECT_TIMEOUT", "15"))
UPSTREAM_READ_TIMEOUT = float(os.getenv("UPSTREAM_READ_TIMEOUT", "180"))
UPSTREAM_WRITE_TIMEOUT = float(os.getenv("UPSTREAM_WRITE_TIMEOUT", "30"))
UPSTREAM_POOL_TIMEOUT = float(os.getenv("UPSTREAM_POOL_TIMEOUT", "30"))
DEBUG_SSE = os.getenv("DEBUG_SSE", "false").lower() == "true"
DEBUG_SSE_MAX_EVENTS = int(os.getenv("DEBUG_SSE_MAX_EVENTS", "20"))

BASE_DIR = Path(__file__).resolve().parent


def _resolve_generated_dir() -> Path:
    """选择可写目录，避免 Serverless 只读文件系统导致启动崩溃。"""
    candidates = [
        Path("/tmp") / "generated_reports",  # Vercel/Linux Serverless
        Path(os.getenv("TMPDIR", "")) / "generated_reports" if os.getenv("TMPDIR") else None,
        BASE_DIR / "generated_reports",  # 本地开发
    ]

    for c in candidates:
        if c is None:
            continue
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return c
        except Exception:
            continue

    # 理论上不应走到这里；兜底抛错供日志定位
    raise RuntimeError("No writable directory available for generated reports")


GENERATED_DIR = _resolve_generated_dir()

app = FastAPI(title="Industry Research Agent API", version="1.5.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class ChatMessage(BaseModel):
    role: str = Field(..., description="system/user/assistant")
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


def _extract_latest_user_text(messages: List[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content.strip():
            return message.content.strip()
    return ""


def _extract_text_from_any(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text:
            return text
        content = value.get("content")
        if isinstance(content, dict):
            ctext = content.get("text")
            if isinstance(ctext, str) and ctext:
                return ctext
        for key in ("answer", "output"):
            v = value.get(key)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                vt = v.get("text")
                if isinstance(vt, str) and vt:
                    return vt
    if isinstance(value, list):
        return "".join(_extract_text_from_any(item) for item in value)
    return ""


def _extract_answer_from_sse_obj(obj: Dict[str, Any]) -> Optional[str]:
    if not isinstance(obj, dict):
        return None

    candidate = obj.get("data", obj)
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError:
            return None

    if not isinstance(candidate, dict):
        return None
    if candidate.get("type") != "answer":
        return None

    text = _extract_text_from_any(candidate.get("content", {}))
    if text:
        return text
    root_text = _extract_text_from_any(candidate)
    return root_text or None


def _extract_first_download_url(text: str) -> Optional[str]:
    if not text:
        return None

    md_matches = re.findall(r"\[[^\]]+\]\((https?://[^)\s]+)\)", text)
    candidates = list(md_matches)
    plain_matches = re.findall(r'https?://[^\s<>"\']+', text)
    candidates.extend(plain_matches)

    for raw in candidates:
        url = raw.strip().rstrip(").,;!?]}")
        if not url:
            continue
        if "coze-coding-project.tos.coze.site" in url or url.lower().endswith(".pdf"):
            return url
    return None


def _extract_file_id(text: str, download_url: Optional[str]) -> Optional[str]:
    m = re.search(r"file[_\s-]?id\s*[:：]\s*([A-Za-z0-9_-]+)", text or "", flags=re.I)
    if m:
        return m.group(1)
    if download_url:
        m2 = re.search(r"coze_storage_([A-Za-z0-9_-]+)", download_url)
        if m2:
            return m2.group(1)
    return None


def _build_report_filename() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"industry_report_{ts}_{uuid4().hex[:8]}.pdf"


def _strip_markdown_inline(s: str) -> str:
    t = s
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t)
    t = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", t)
    return t


def _wrap_text_lines(text: str, max_chars: int = 40) -> List[str]:
    lines: List[str] = []
    p = _strip_markdown_inline(text.strip())
    if not p:
        return [""]
    while len(p) > max_chars:
        lines.append(p[:max_chars])
        p = p[max_chars:]
    lines.append(p)
    return lines


def _write_pdf_from_text(text: str) -> str:
    if not REPORTLAB_AVAILABLE:
        raise HTTPException(status_code=500, detail="reportlab not installed. Please `pip install reportlab`.")

    filename = _build_report_filename()
    filepath = GENERATED_DIR / filename

    c = canvas.Canvas(str(filepath), pagesize=A4)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    page_w, page_h = A4
    left_x = 20 * mm
    y = page_h - 20 * mm
    body_line_h = 7 * mm

    def ensure_space(min_y: float = 20 * mm):
        nonlocal y
        if y < min_y:
            c.showPage()
            y = page_h - 20 * mm

    c.setFont("STSong-Light", 16)
    c.drawString(left_x, y, "行业分析报告")
    y -= 10 * mm

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            y -= 4 * mm
            ensure_space()
            continue

        # 标题层级
        if stripped.startswith("### "):
            ensure_space(28 * mm)
            c.setFont("STSong-Light", 13)
            for seg in _wrap_text_lines(stripped[4:], max_chars=40):
                c.drawString(left_x + 4 * mm, y, seg)
                y -= body_line_h
            y -= 1 * mm
            continue

        if stripped.startswith("## "):
            ensure_space(30 * mm)
            c.setFont("STSong-Light", 14)
            for seg in _wrap_text_lines(stripped[3:], max_chars=38):
                c.drawString(left_x + 2 * mm, y, seg)
                y -= body_line_h
            y -= 1 * mm
            continue

        if stripped.startswith("# "):
            ensure_space(32 * mm)
            c.setFont("STSong-Light", 15)
            for seg in _wrap_text_lines(stripped[2:], max_chars=36):
                c.drawString(left_x, y, seg)
                y -= body_line_h
            y -= 2 * mm
            continue

        # 列表项
        if stripped.startswith("- "):
            c.setFont("STSong-Light", 12)
            bullet_text = "• " + stripped[2:]
            for seg in _wrap_text_lines(bullet_text, max_chars=42):
                ensure_space()
                c.drawString(left_x + 3 * mm, y, seg)
                y -= body_line_h
            continue

        # 普通段落
        c.setFont("STSong-Light", 12)
        for seg in _wrap_text_lines(stripped, max_chars=44):
            ensure_space()
            c.drawString(left_x, y, seg)
            y -= body_line_h

    c.save()
    return filename


@app.post("/api/chat")
async def industry_chat(req: ChatRequest) -> Dict[str, Any]:
    if not COZE_TOKEN:
        raise HTTPException(status_code=500, detail="Missing COZE_TOKEN in .env")
    if not COZE_SESSION_ID:
        raise HTTPException(status_code=500, detail="Missing COZE_SESSION_ID in .env")
    if not COZE_PROJECT_ID:
        raise HTTPException(status_code=500, detail="Missing COZE_PROJECT_ID in .env")

    prompt_text = _extract_latest_user_text(req.messages)
    if not prompt_text:
        raise HTTPException(status_code=400, detail="No user prompt found")

    headers = {
        "Authorization": f"Bearer {COZE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = {
        "content": {
            "query": {
                "prompt": [{"type": "text", "content": {"text": prompt_text}}]
            }
        },
        "type": "query",
        "session_id": COZE_SESSION_ID,
        "project_id": COZE_PROJECT_ID,
    }

    timeout = httpx.Timeout(
        connect=UPSTREAM_CONNECT_TIMEOUT,
        read=UPSTREAM_READ_TIMEOUT,
        write=UPSTREAM_WRITE_TIMEOUT,
        pool=UPSTREAM_POOL_TIMEOUT,
    )

    answer_parts: List[str] = []
    last_piece: Optional[str] = None
    raw_event_count = 0
    debug_events: List[Dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("POST", COZE_STREAM_RUN_URL, headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if not data_text or data_text == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data_text)
                    except json.JSONDecodeError:
                        continue

                    raw_event_count += 1
                    piece = _extract_answer_from_sse_obj(obj)

                    if DEBUG_SSE and len(debug_events) < DEBUG_SSE_MAX_EVENTS:
                        debug_events.append({"event_index": raw_event_count, "raw": obj, "parsed_piece": piece})

                    if piece and piece != last_piece:
                        answer_parts.append(piece)
                        last_piece = piece

    except httpx.HTTPStatusError as exc:
        upstream_status = exc.response.status_code
        upstream_text = (exc.response.text or "").strip()
        if len(upstream_text) > 1200:
            upstream_text = upstream_text[:1200] + "..."
        raise HTTPException(
            status_code=upstream_status,
            detail={
                "message": f"Upstream HTTP error: status_code={upstream_status}",
                "upstream_body": upstream_text,
                "hint": "Check COZE_TOKEN / COZE_PROJECT_ID / COZE_SESSION_ID consistency and permission.",
            },
        ) from exc
    except httpx.ReadTimeout as exc:
        partial = "".join(answer_parts).strip()
        if partial:
            result: Dict[str, Any] = {
                "success": True,
                "answer": partial,
                "warning": "Upstream stream read timeout, returned partial answer.",
            }
            try:
                pdf_file = _write_pdf_from_text(partial)
                result["download_url"] = f"/api/download-report/{pdf_file}"
                result["file_name"] = pdf_file
            except Exception:
                pass
            if DEBUG_SSE:
                result["debug"] = {"events_received": raw_event_count, "events_sample": debug_events}
            return result

        raise HTTPException(
            status_code=500,
            detail=(
                "Upstream request timeout: "
                f"type={exc.__class__.__name__}, repr={repr(exc)}, "
                f"url={COZE_STREAM_RUN_URL}, events_received={raw_event_count}"
            ),
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Upstream request error: "
                f"type={exc.__class__.__name__}, repr={repr(exc)}, url={COZE_STREAM_RUN_URL}"
            ),
        ) from exc

    answer = "".join(answer_parts).strip()
    answer = re.sub(r"(?<=[\u4e00-\u9fff\w])\n(?=[\u4e00-\u9fff\w])", "", answer)
    answer = re.sub(r"\n{3,}", "\n\n", answer)

    if not answer:
        detail = (
            "Upstream returned stream but no answer chunks parsed. "
            f"events_received={raw_event_count}. Please inspect raw SSE payload format."
        )
        if DEBUG_SSE:
            return {
                "success": False,
                "answer": "",
                "detail": detail,
                "debug": {"events_received": raw_event_count, "events_sample": debug_events},
            }
        raise HTTPException(status_code=502, detail=detail)

    result: Dict[str, Any] = {"success": True, "answer": answer}

    try:
        pdf_file = _write_pdf_from_text(answer)
        result["download_url"] = f"/api/download-report/{pdf_file}"
        result["file_name"] = pdf_file
    except Exception:
        pass
    if DEBUG_SSE:
        result["debug"] = {"events_received": raw_event_count, "events_sample": debug_events}

    return result


@app.get("/api/download-report/{file_name}")
async def download_report(file_name: str):
    if "/" in file_name or "\\" in file_name:
        raise HTTPException(status_code=400, detail="invalid file name")

    path = GENERATED_DIR / file_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")

    return FileResponse(
        str(path),
        media_type="application/pdf",
        filename=file_name,
    )


@app.get("/api/validate-download-url")
async def validate_download_url(url: str = Query(..., description="报告下载链接")) -> Dict[str, Any]:
    timeout = httpx.Timeout(connect=UPSTREAM_CONNECT_TIMEOUT, read=20, write=UPSTREAM_WRITE_TIMEOUT, pool=UPSTREAM_POOL_TIMEOUT)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            head_resp = await client.head(url)
            if head_resp.status_code in (405, 403):
                get_resp = await client.get(url)
                return {"ok": get_resp.status_code < 400, "status_code": get_resp.status_code}
            return {"ok": head_resp.status_code < 400, "status_code": head_resp.status_code}
    except Exception as exc:
        return {"ok": False, "status_code": 0, "detail": f"validate error: type={exc.__class__.__name__}, repr={repr(exc)}"}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}
