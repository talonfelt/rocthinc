import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def root():
    return Path("index.html").read_text(encoding="utf-8")

def get_html(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector("[data-message-author-role]")
        html = page.content()
        browser.close()
    return html

@app.get("/export")
@app.post("/export")
def export(url: str = Query(...)):
    soup = BeautifulSoup(get_html(url), "html.parser")
    messages = []
    for m in soup.select("[data-message-author-role]"):
        role = "You" if m["data-message-author-role"] == "user" else "Assistant"
        text = m.get_text(separator="\n", strip=True)
        messages.append(f"### {role}\n{text}\n\n")

    title = soup.title.string.strip() if soup.title else "Chat"

    md = f"# {title}\n\n" + "".join(messages)
    tex = f"\\documentclass{{article}}\n\\begin{{document}}\n\\title{{{title}}}\n\\maketitle\n\n" + "".join(messages) + "\\end{{document}}"

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("conversation.md", md)
        z.writestr("conversation.tex", tex)
    buf.seek(0)

    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=rocthinc.zip"})