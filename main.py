import time
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional, List, Literal

import requests
from bs4 import BeautifulSoup  # only used for robustness if you want to extend later
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

# -------------------------------------------------------------
# App bootstrap
# -------------------------------------------------------------

app = FastAPI(
    title="rocthinc",
    version="1.0.0",
    description="Any web page → Markdown + LaTeX (zipped)."
)

ExportFormat = Literal["md", "tex", "pdf"]


class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None


# -------------------------------------------------------------
# Utility helpers
# -------------------------------------------------------------

def escape_latex(text: str) -> str:
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def strip_html_to_text(html: str) -> str:
    """
    Generic HTML → plain text:
    - remove <script> and <style>
    - strip all tags
    - collapse whitespace
    """
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html).strip()
    return html


# -------------------------------------------------------------
# Fetch + parse any page (no ChatGPT special-casing)
# -------------------------------------------------------------

def fetch_html_or_explain(url: str) -> str:
    """
    Fetch HTML and raise a clear HTTPException only for real errors.
    """
    try:
        resp = requests.get(url, timeout=20)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "rocthinc could not reach that URL. "
                "Check that it’s correct and publicly reachable. "
                f"(Details: {e})"
            ),
        )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The URL you pasted returned HTTP {resp.status_code}. "
                "rocthinc can only export pages that load normally in a browser."
            ),
        )

    return resp.text


def parse_conversation(url: str) -> dict:
    """
    Generic pipeline:
      URL → HTML → plain text → single 'assistant' message.
    No AI-site special handling at all.
    """
    html = fetch_html_or_explain(url)
    text = strip_html_to_text(html)

    max_len = 20000
    if len(text) > max_len:
        text = text[:max_len] + " … [truncated]"

    messages = [
        {"speaker": "assistant", "content": text},
    ]
    return {
        "source": "web",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }


# -------------------------------------------------------------
# MARKDOWN + LATEX
# -------------------------------------------------------------

def to_markdown(conv: dict) -> str:
    lines = []
    lines.append("# Page Export")
    lines.append("")
    lines.append(f"**Source:** {conv['source']}")
    lines.append(f"**URL:** {conv['url']}")
    lines.append(f"**Exported at:** {conv['created_at']}")
    lines.append("")
    for m in conv["messages"]:
        role = m["speaker"].capitalize()
        lines.append(f"**{role}:** {m['content']}")
        lines.append("")
    return "\n".join(lines)


def to_latex(conv: dict) -> str:
    parts = [
        r"\documentclass{article}",
        r"\usepackage[margin=1in]{geometry}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage[utf8]{inputenc}",
        r"\begin{document}",
        r"\section*{Page Export}",
        "",
        r"\textbf{Source:} " + escape_latex(conv["source"]) + r"\\",
        r"\textbf{URL:} " + escape_latex(conv["url"]) + r"\\",
        r"\textbf{Exported at:} " + escape_latex(conv["created_at"]) + r"\\[1em]",
    ]
    for m in conv["messages"]:
        role = escape_latex(m["speaker"].capitalize())
        content = escape_latex(m["content"])
        parts.append(r"\textbf{" + role + r":} " + content + r"\\[0.75em]")
    parts.append(r"\end{document}")
    return "\n".join(parts)


# -------------------------------------------------------------
# ZIP OUTPUT
# -------------------------------------------------------------

def make_zip_response(url: str, formats: List[ExportFormat]):
    conv = parse_conversation(url)

    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            z.writestr("page.md", to_markdown(conv))
        if "tex" in formats:
            z.writestr("page.tex", to_latex(conv))
        if "pdf" in formats:
            z.writestr(
                "README_PDF.txt",
                "PDF export not implemented yet. "
                "Use page.tex to compile a PDF locally.",
            )

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename=\"page_export.zip\"'
        },
    )


# -------------------------------------------------------------
# ROUTES
# -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def web_ui():
    """
    Serve the static index.html that contains your rocthinc UI.
    """
    html_path = Path(__file__).parent / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>rocthinc</h1><p>index.html not found on server.</p>",
            status_code=500,
        )
    return html_path.read_text(encoding="utf-8")


@app.post("/export")
def export_post(req: ExportRequest):
    """
    JSON API used by your web UI and Shortcuts:
      { "url": "...", "formats": ["md","tex"] }
    """
    formats: List[ExportFormat] = req.formats or ["md", "tex"]
    return make_zip_response(req.url, formats)


@app.get("/export")
def export_get(
    url: str = Query(..., description="Any web page URL"),
    formats: Optional[str] = Query(
        None,
        description="Comma-separated formats, e.g. md,tex or md,tex,pdf",
    ),
):
    """
    GET API for manual calls:
      /export?url=...&formats=md,tex
    """
    if formats:
        fmts = [f.strip() for f in formats.split(",") if f.strip()]
        fmts = [f for f in fmts if f in ("md", "tex", "pdf")]
        if not fmts:
            fmts = ["md", "tex"]
    else:
        fmts = ["md", "tex"]
    return make_zip_response(url, fmts)

# -------------------------------------------------------------
# END OF FILE
# -------------------------------------------------------------