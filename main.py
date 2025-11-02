from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import hashlib
from datetime import datetime
import re
from typing import List, Optional

app = FastAPI(title="Rock Think")

class Message(BaseModel):
    speaker: str
    content: str

class ExportRequest(BaseModel):
    url: Optional[str] = None
    messages: Optional[List[Message]] = None
    openai_key: Optional[str] = None

def clean_text(text):
    return re.sub(r'\s+', ' ', text.strip())

def parse_web(url, platform):
    selectors = {
        'claude': {'container': 'div[class*="message"]', 'user': 'user'},
        'chatgpt': {'container': 'div[data-message-author-role]', 'user': 'user'},
        'grok': {'container': 'div[class*="conversation-turn"]', 'user': 'human'}
    }[platform]
    
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, 'html.parser')
    msgs = []
    for msg in soup.select(selectors['container']):
        classes = ' '.join(msg.get('class', []))
        speaker = 'user' if selectors['user'] in classes else 'assistant'
        content = clean_text(msg.get_text())
        if content:
            msgs.append({"speaker": speaker, "content": content})
    return {"messages": msgs[:100], "source": platform, "url": url}

@app.post("/export")
async def export(req: ExportRequest):
    if req.messages and req.openai_key:
        data = {"messages": [m.dict() for m in req.messages], "source": "openai-private", "url": "private"}
    elif req.url:
        if "claude.ai" in req.url:
            data = parse_web(req.url, "claude")
        elif "chatgpt.com" in req.url or "chat.openai.com" in req.url:
            data = parse_web(req.url, "chatgpt")
        elif "grok.x.ai" in req.url or "x.com" in req.url:
            data = parse_web(req.url, "grok")
        else:
            raise HTTPException(400, "Bad URL")
    else:
        raise HTTPException(400, "Need url or messages+key")

    lines = [f"# Rock Think Export\n**From silicon to thought to getting defeated by a pair of scissors.**\n**Source:** {data['source']}\n**URL:** {data.get('url','private')}\n**Exported:** {datetime.now().isoformat()}\n---\n"]
    for m in data['messages']:
        lines.append(f"## {m['speaker'].title()}\n{m['content']}\n---")
    content = "\n".join(lines)
    h = hashlib.sha256(content.encode()).hexdigest()
    lines.append(f"\n**Hash:** sha256:{h}")
    md = "\n".join(lines)
    file = f"rock_{h[:8]}.md"
    with open(file, "w") as f:
        f.write(md)
    return FileResponse(file, filename=file)

@app.get("/")
def home():
    return {"msg": "Rock Think LIVE! POST /export"}