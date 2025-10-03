#!/usr/bin/env python3
# AlphaForge — Single-file News App + Live Admin (stdlib only)

import os, sys, json, time, sqlite3, datetime, html, re, threading
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "10000"))
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

STATE = {
    "news": {"auto_enabled": True, "interval_sec": 600, "last_run": None},
    "sources": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/marketsNews",
        "https://www.marketwatch.com/feeds/topstories",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.bls.gov/feed/news.rss",
        "https://www.bea.gov/rss.xml"
    ],
    "watchlist": ["SPY","QQQ","DIA","AAPL","MSFT"]
}

DB = "news.db"
UA = "AlphaForgeNews/1.0 (educational; stdlib-only)"
STOP = threading.Event()

DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  source TEXT,
  url TEXT,
  title TEXT,
  summary TEXT,
  published INTEGER
);
CREATE INDEX IF NOT EXISTS ix_articles_pub ON articles(published DESC);
"""

def db_connect():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; return con

def db_init():
    con = db_connect(); cur = con.cursor()
    for stmt in filter(None, DDL.split(";")): cur.execute(stmt)
    con.commit(); con.close()

def db_store(items):
    con = db_connect(); cur = con.cursor(); inserted = 0
    for a in items:
        try:
            cur.execute("INSERT INTO articles (id,source,url,title,summary,published) VALUES (?,?,?,?,?,?)",
                (a["id"],a["source"],a["url"],a["title"],a["summary"],a["published"]))
            inserted += 1
        except sqlite3.IntegrityError: pass
    con.commit(); con.close(); return inserted

def db_latest(limit=50):
    con=db_connect();cur=con.cursor()
    cur.execute("SELECT * FROM articles ORDER BY published DESC LIMIT ?",(int(limit),))
    rows=[dict(r) for r in cur.fetchall()]; con.close(); return rows

def db_delete_all():
    con=db_connect(); cur=con.cursor(); cur.execute("DELETE FROM articles")
    con.commit(); con.close()

def _sha1(s): import hashlib; return hashlib.sha1((s or "").encode()).hexdigest()
def _to_ts(txt):
    if not txt: return int(time.time())
    try: from email.utils import parsedate_to_datetime; return int(parsedate_to_datetime(txt).timestamp())
    except: pass
    try: return int(datetime.datetime.fromisoformat(txt.replace("Z","+00:00")).timestamp())
    except: return int(time.time())

def fetch_feed(url,highlight=None):
    req=Request(url,headers={"User-Agent":UA})
    try: raw=urlopen(req,timeout=20).read().decode("utf-8","ignore")
    except: return []
    try: root=ET.fromstring(raw)
    except: return []
    items=[]
    entries=root.findall(".//{http://www.w3.org/2005/Atom}entry")
    if entries:
        for e in entries:
            title=(e.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link=(e.find("{http://www.w3.org/2005/Atom}link").get("href") if e.find("{http://www.w3.org/2005/Atom}link") is not None else "")
            summ=e.findtext("{http://www.w3.org/2005/Atom}summary") or e.findtext("{http://www.w3.org/2005/Atom}content") or ""
            pub=e.findtext("{http://www.w3.org/2005/Atom}updated") or e.findtext("{http://www.w3.org/2005/Atom}published")
            items.append(_pack(url,link,title,summ,_to_ts(pub)))
    else:
        for it in root.findall(".//item"):
            title=it.findtext("title") or ""
            link=it.findtext("link") or ""
            summ=it.findtext("description") or ""
            pub=it.findtext("pubDate") or ""
            items.append(_pack(url,link,title,summ,_to_ts(pub)))
    return items

def _pack(src,link,title,summ,pub):
    return {"id":_sha1(src+title+link),"source":src,"url":link,"title":title,"summary":summ,"published":pub}

def news_worker():
    db_init()
    while not STOP.is_set():
        try:
            for src in STATE["sources"]:
                try: db_store(fetch_feed(src,STATE["watchlist"]))
                except Exception as e: print("source error:",src,e,flush=True)
            STATE["news"]["last_run"]=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e: print("worker error:",e,flush=True)
        STOP.wait(timeout=STATE["news"]["interval_sec"])

INDEX_HTML="""<!doctype html><html><head><meta charset=utf-8><title>AlphaForge</title></head>
<body style=background:#0b0f14;color:#e5e7eb;font-family:system-ui>
<h2>AlphaForge — Auto News</h2>
<p>Status: <span id=status>Loading…</span></p>
<div id=digest>Loading…</div>
<script>
async function load(){
 let s=await fetch('/api/news/auto'); let j=await s.json();
 document.getElementById('status').innerText='Auto: '+(j.auto_enabled?'ON':'OFF')+' every '+j.interval_sec+'s • last '+(j.last_run||'—');
 let d=await fetch('/api/news/digest'); document.getElementById('digest').innerHTML=await d.text();
}
load(); setInterval(load,60000);
</script>
</body></html>"""

ADMIN_HTML="""<!doctype html><meta charset=utf-8><title>Admin</title>
<textarea id=txt style=width:100%;height:200px>{}</textarea><br>
<input id=k placeholder='API Key'><button onclick=send()>Apply</button>
<pre id=o></pre>
<script>
async function send(){let body=document.getElementById('txt').value;
let key=document.getElementById('k').value.trim();
let r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+key},body});
document.getElementById('o').innerText=await r.text();}
</script>"""

class H(BaseHTTPRequestHandler):
    def _json(self,obj,code=200):
        b=json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _html(self,txt,code=200):
        b=txt.encode(); self.send_response(code)
        self.send_header("Content-Type","text/html"); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _body(self):
        ln=int(self.headers.get("Content-Length","0")); return json.loads(self.rfile.read(ln) or b"{}")
    def do_GET(self):
        if self.path=="/": return self._html(INDEX_HTML)
        if self.path=="/admin": return self._html(ADMIN_HTML)
        if self.path.startswith("/api/news/auto"): return self._json(STATE["news"])
        if self.path.startswith("/api/news/digest"):
            rows=db_latest(30)
            if not rows: return self._html("<p>No news yet</p>")
            return self._html("<br>".join(f"<a href='{r['url']}'>{html.escape(r['title'])}</a>" for r in rows))
        return self._json({"error":"not found"},404)
    def do_POST(self):
        if self.path.startswith("/api/settings"):
            if not (ADMIN_API_KEY and self.headers.get("Authorization","").split(" ")[-1]==ADMIN_API_KEY):
                return self._json({"error":"unauthorized"},401)
            body=self._body()
            if "interval_sec" in body.get("news",{}): STATE["news"]["interval_sec"]=int(body["news"]["interval_sec"])
            if "auto_enabled" in body.get("news",{}): STATE["news"]["auto_enabled"]=bool(body["news"]["auto_enabled"])
            if "add_sources" in body: STATE["sources"]+=body["add_sources"]
            if "remove_sources" in body: STATE["sources"]=[s for s in STATE["sources"] if s not in body["remove_sources"]]
            if "watchlist" in body: STATE["watchlist"]=[w.upper() for w in body["watchlist"]]
            return self._json({"ok":True,"state":STATE})
        return self._json({"error":"not found"},404)

def main():
    db_init()
    if STATE["news"]["auto_enabled"]: threading.Thread(target=news_worker,daemon=True).start()
    server=ThreadingHTTPServer(("0.0.0.0",PORT),H)
    print("AlphaForge running on",PORT,flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: STOP.set(); server.server_close()

if __name__=="__main__": main()
