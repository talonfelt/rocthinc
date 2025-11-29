import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import threading

# Vercel-safe browser
thread_local = threading.local()
def get_browser():
    if not hasattr(thread_local, "browser"):
        pw = sync_playwright().start()
        thread_local.browser = pw.chromium.launch(headless=True)
    return thread_local.browser

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).parent.joinpath("index.html").read_text(encoding="utf-8")

def get_page(url: str) -> str:
    page = get_browser().new_page()
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_selector("[data-message-author-role]", timeout=20000)
    html = page.content()
    page.close()
    return html

@app.get("/export")
@app.post("/export")
def export(url: str = Query(...)):
    soup = BeautifulSoup(get_page(url), "html.parser")

    messages = []
    for msg in soup.select('[data-message-author-role]'):
        role = "You" if msg["data-message-author-role"] == "user" else "Assistant"
        text = msg.get_text(separator="\n", strip=True)
        messages.append(f"### {role}\n\n{text}\n")

    title = soup.title.get_text(strip=True) if soup.title else "Chat Export"

    md_content = f"# {title}\n\n" + "\n".join(messages)
    tex_content = (
        "\\documentclass{article}\n"
        "\\usepackage[margin=1in]{geometry}\n"
        "\\begin{document}\n"
        f"\\title{{{title}}}\n"
        "\\maketitle\n\n" +
        "\n\n".join([m.replace("### ", "\\section*{").replace("\n\n", "}\n") for m in messages]) +
        "\n\\end{document}"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("conversation.md", md_content)
        z.writestr("conversation.tex", tex_content)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=rocthinc.zip"}
    )