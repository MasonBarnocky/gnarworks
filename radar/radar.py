#!/usr/bin/env python3
"""
Gnarworks Radar: finds Reddit threads where your products would actually help,
scores them, and drafts a reply in your voice. It NEVER posts. You read, edit, and post yourself.

  python radar.py                      # run once (use cron / Task Scheduler for daily)
  python radar.py --fixture sample.json  # test with saved posts, no Reddit calls
  python radar.py --reset              # forget which posts were already seen

Needs Python 3.11+, no pip installs. Secrets go in radar/.env (see .env.example).
"""
import argparse, base64, html, json, os, re, sqlite3, sys, time, tomllib
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTENT = re.compile(r"\?|\b(any (app|tool|software)|recommend|suggestion|how do (you|i)|what do you use|"
                    r"is there (a|an)|looking for|struggl|frustrat|help)\b", re.I)


# ---------------------------------------------------------------- setup
def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http(url, data=None, headers=None, method=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read().decode("utf-8")


# ---------------------------------------------------------------- reddit (read-only)
class Reddit:
    """App-only OAuth (client_credentials): read-only, no Reddit password needed."""

    def __init__(self, client_id, secret, username):
        self.ua = f"gnarworks-radar/0.1 (personal read-only research; by u/{username})"
        auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        _, _, body = http("https://www.reddit.com/api/v1/access_token",
                          data=b"grant_type=client_credentials",
                          headers={"Authorization": f"Basic {auth}", "User-Agent": self.ua},
                          method="POST")
        self.token = json.loads(body)["access_token"]
        self.calls = 0

    def get(self, path, **params):
        params.setdefault("raw_json", 1)
        url = f"https://oauth.reddit.com{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(3):
            try:
                _, hdr, body = http(url, headers={"Authorization": f"Bearer {self.token}", "User-Agent": self.ua})
                self.calls += 1
                remaining = float(hdr.get("x-ratelimit-remaining", 50))
                if remaining < 5:
                    time.sleep(float(hdr.get("x-ratelimit-reset", 60)) + 1)
                time.sleep(0.7)  # stay well under 100 req/min
                return json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    print(f"  ! skipped {path} ({e.code}: private, banned, or doesn't exist)")
                    return None
                if e.code == 429:
                    time.sleep(30 * (attempt + 1))
                    continue
                raise
        return None

    def posts(self, path, **params):
        data = self.get(path, **params)
        return [c["data"] for c in (data or {}).get("data", {}).get("children", []) if c.get("kind") == "t3"]

    def rules(self, sub):
        data = self.get(f"/r/{sub}/about/rules")
        return [f"{r.get('short_name','')}: {r.get('description','')[:200]}" for r in (data or {}).get("rules", [])]


# ---------------------------------------------------------------- storage
def db_open(path):
    db = sqlite3.connect(path)
    db.execute("create table if not exists seen (id text primary key, seen_at real, fit int, product text)")
    db.execute("create table if not exists rules (sub text primary key, fetched_at real, rules text)")
    return db


def sub_rules(db, reddit, sub):
    row = db.execute("select fetched_at, rules from rules where sub=?", (sub.lower(),)).fetchone()
    if row and time.time() - row[0] < 7 * 86400:
        return json.loads(row[1])
    rules = reddit.rules(sub) if reddit else []
    db.execute("insert or replace into rules values (?,?,?)", (sub.lower(), time.time(), json.dumps(rules)))
    db.commit()
    return rules


# ---------------------------------------------------------------- matching
def keyword_hits(text, keywords):
    t = text.lower()
    return [k for k in keywords if re.search(r"(?<![a-z])" + re.escape(k.lower()) + r"(?![a-z])", t)]


def collect(cfg, reddit, fixture, db, lookback_h):
    """Return candidate posts matched to products, newest first, not seen before."""
    raw = {}
    if fixture:
        for p in json.loads(Path(fixture).read_text()):
            raw[p["id"]] = p
    else:
        subs = sorted({s for p in cfg["products"] for s in p.get("subreddits", [])}, key=str.lower)
        for s in subs:
            print(f"  scanning r/{s}")
            for p in reddit.posts(f"/r/{s}/new", limit=100):
                raw[p["id"]] = p
        for prod in cfg["products"]:
            if prod.get("search_all_reddit"):
                for k in prod["keywords"]:
                    for p in reddit.posts("/search", q=f'"{k}"', sort="new", t="week", limit=25):
                        raw[p["id"]] = p

    cutoff = time.time() - lookback_h * 3600
    out = []
    for p in raw.values():
        if p.get("created_utc", 0) < cutoff and not fixture:
            continue
        if p.get("locked") or p.get("archived") or p.get("over_18") or p.get("stickied"):
            continue
        if db.execute("select 1 from seen where id=?", (p["id"],)).fetchone():
            continue
        text = f"{p.get('title','')}\n{p.get('selftext','')}"
        matches = []
        for prod in cfg["products"]:
            hits = keyword_hits(text, prod["keywords"])
            if hits:
                matches.append((prod["name"], hits))
        if not matches:
            continue
        intent = bool(INTENT.search(text))
        best = max(matches, key=lambda m: len(m[1]))
        heur = min(10, 3 + 2 * len(best[1]) + (2 if intent else 0))
        out.append({"post": p, "products": [m[0] for m in matches], "hits": best[1], "heur": heur})
    out.sort(key=lambda c: (-c["heur"], -c["post"].get("created_utc", 0)))
    return out


# ---------------------------------------------------------------- AI scoring + drafts
def build_prompt(cfg, batch, rules_by_sub):
    s = cfg["settings"]
    prods = "\n".join(f"- {p['name']} ({p['link']}): {p['pitch']}" for p in cfg["products"])
    posts = []
    for c in batch:
        p = c["post"]
        sub = p.get("subreddit", "")
        rules = "\n    ".join(rules_by_sub.get(sub.lower(), [])[:8]) or "(no rules fetched)"
        posts.append(
            f"<post id=\"{p['id']}\" subreddit=\"r/{sub}\">\nTITLE: {p.get('title','')}\n"
            f"BODY: {p.get('selftext','')[:1500]}\nSUBREDDIT RULES:\n    {rules}\n</post>")
    return f"""You help a solo founder find Reddit threads where he can genuinely help, and draft replies HE will review and post himself under his own name.

His products:
{prods}

His voice: {s.get('voice','')}

For EACH post below, return a JSON object with:
- "id": the post id
- "product": which product fits, or null if none really does
- "fit": 0-10. 8-10 = they're directly asking for this kind of tool or describing the exact pain. 5-7 = related pain, a helpful answer could mention it. 0-4 = keyword coincidence, skip.
- "intent": one of "asking_for_tool", "describing_pain", "asking_how", "discussion", "irrelevant"
- "why": one short sentence on why it fits or doesn't
- "link_ok": true ONLY if the subreddit rules don't forbid self-promotion AND the person is asking for tools/recommendations. Otherwise false.
- "reply": a draft reply (2-5 sentences) that actually answers their question with useful advice FIRST, even if they never use the product. If link_ok is true, mention the product at the end with an honest disclosure like "I built a small tool for this" plus the link. If link_ok is false, no link and no product name, just be helpful. Never invent experiences, customers, numbers, or results. Never pretend to be a neutral third party. Empty string if fit < 5.

Return ONLY a JSON array, no other text.

{chr(10).join(posts)}"""


def ai_score(cfg, batch, rules_by_sub):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {}
    body = json.dumps({
        "model": os.environ.get("RADAR_MODEL", "claude-sonnet-4-5"),
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": build_prompt(cfg, batch, rules_by_sub)}],
    }).encode()
    _, _, resp = http("https://api.anthropic.com/v1/messages", data=body, method="POST", timeout=120, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    return parse_ai(json.loads(resp)["content"][0]["text"])


def parse_ai(text):
    text = text[text.find("["): text.rfind("]") + 1]
    try:
        return {str(d["id"]): d for d in json.loads(text)}
    except (ValueError, KeyError, TypeError):
        print("  ! couldn't parse AI response; falling back to keyword scores")
        return {}


# ---------------------------------------------------------------- output
def age(p):
    h = (time.time() - p.get("created_utc", time.time())) / 3600
    return f"{h:.0f}h ago" if h < 48 else f"{h/24:.0f}d ago"


def write_digest(items, out_dir, stats):
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    cards = []
    for it in items:
        p = it["post"]
        url = "https://www.reddit.com" + p.get("permalink", "")
        badge = '<span class="b ok">link OK</span>' if it.get("link_ok") else '<span class="b no">no link: just help</span>'
        reply = html.escape(it.get("reply") or "(add ANTHROPIC_API_KEY to get drafts)")
        cards.append(f"""<div class="card"><div class="meta"><b class="fit">{it['fit']}/10</b> · r/{html.escape(p.get('subreddit',''))} · {age(p)} · {p.get('num_comments',0)} comments · {html.escape(it['product'] or '-')} {badge}</div>
<a class="t" href="{html.escape(url)}" target="_blank">{html.escape(p.get('title',''))}</a>
<p class="why">{html.escape(it.get('why',''))}</p>
<textarea rows="5">{reply}</textarea>
<div class="row"><button onclick="navigator.clipboard.writeText(this.parentNode.previousElementSibling.value);this.textContent='Copied'">Copy reply</button><a href="{html.escape(url)}" target="_blank">Open thread →</a></div></div>""")
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar {stamp}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#f2f0e9;color:#16181d;margin:0}}.w{{max-width:760px;margin:auto;padding:24px 16px}}
.card{{background:#fbfaf6;border:1px solid #dcd8cc;border-radius:14px;padding:16px;margin:12px 0}}.meta{{font-size:13px;color:#5f636b}}.fit{{color:#e2552b}}
.t{{display:block;font-weight:700;font-size:17px;color:inherit;margin:6px 0}}.why{{margin:4px 0 10px;color:#5f636b}}textarea{{width:100%;box-sizing:border-box;font:inherit;padding:10px;border-radius:10px;border:1px solid #dcd8cc}}
.row{{display:flex;gap:12px;align-items:center;margin-top:8px}}button{{font:inherit;font-weight:600;border:0;border-radius:8px;padding:6px 12px;background:#16181d;color:#fff;cursor:pointer}}
.b{{font-size:11px;font-weight:700;padding:1px 7px;border-radius:99px;margin-left:4px}}.ok{{background:#e6f4ea;color:#1a7f37}}.no{{background:#fff4d6;color:#9a6700}}</style>
<div class="w"><h1>Gnarworks Radar</h1><p>{stamp} · {stats}</p><p><b>Rules:</b> read the thread first, edit the draft so it's yours, post by hand. Skip anything that doesn't feel right.</p>
{''.join(cards) or '<p>Nothing worth replying to this run.</p>'}</div>"""
    path = out_dir / f"digest-{stamp}.html"
    path.write_text(page, encoding="utf-8")
    return path


def telegram(items):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat) or not items:
        return
    for it in items[:8]:
        p = it["post"]
        msg = (f"Radar {it['fit']}/10 · r/{p.get('subreddit')} · {age(p)} · {it['product'] or '-'}"
               f"{' · link OK' if it.get('link_ok') else ' · no link'}\n{p.get('title','')}\n"
               f"https://www.reddit.com{p.get('permalink','')}\n\nWhy: {it.get('why','')}\n\nDraft:\n{it.get('reply','')}")[:4000]
        http(f"https://api.telegram.org/bot{tok}/sendMessage", method="POST",
             data=urllib.parse.urlencode({"chat_id": chat, "text": msg, "disable_web_page_preview": "true"}).encode())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--fixture", help="JSON list of reddit post objects to use instead of the API")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    load_env(HERE / ".env")
    cfg = tomllib.loads(Path(a.config).read_text())
    s = cfg["settings"]
    db = db_open(HERE / "radar.db")
    if a.reset:
        db.execute("delete from seen"); db.commit(); print("Seen list cleared."); return

    reddit = None
    if not a.fixture:
        need = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME"]
        missing = [k for k in need if not os.environ.get(k)]
        if missing:
            sys.exit(f"Missing in radar/.env: {', '.join(missing)}")
        reddit = Reddit(os.environ["REDDIT_CLIENT_ID"], os.environ["REDDIT_CLIENT_SECRET"], os.environ["REDDIT_USERNAME"])

    print("Collecting posts…")
    cands = collect(cfg, reddit, a.fixture, db, s.get("lookback_hours", 24))
    cands = cands[: s.get("max_ai_posts", 40)]
    print(f"  {len(cands)} new keyword matches")

    rules_by_sub = {}
    for c in cands:
        sub = c["post"].get("subreddit", "")
        if sub.lower() not in rules_by_sub:
            rules_by_sub[sub.lower()] = sub_rules(db, reddit, sub) if reddit else []

    ai = {}
    for i in range(0, len(cands), 8):
        try:
            ai.update(ai_score(cfg, cands[i:i + 8], rules_by_sub))
        except Exception as e:  # keep going on API hiccups
            print(f"  ! AI scoring failed for a batch: {e}")

    items = []
    for c in cands:
        p, d = c["post"], ai.get(c["post"]["id"])
        it = {"post": p}
        if d:
            it.update(fit=int(d.get("fit", 0)), product=d.get("product"), why=d.get("why", ""),
                      link_ok=bool(d.get("link_ok")), reply=d.get("reply", ""))
        else:
            it.update(fit=c["heur"], product=c["products"][0], link_ok=False, reply="",
                      why=f"keyword match: {', '.join(c['hits'])}")
        db.execute("insert or replace into seen values (?,?,?,?)", (p["id"], time.time(), it["fit"], it["product"]))
        if it["fit"] >= s.get("min_fit", 6):
            items.append(it)
    db.commit()
    items.sort(key=lambda x: -x["fit"])

    stats = f"{len(cands)} matches checked, {len(items)} worth a look" + \
            (f", {reddit.calls} Reddit calls" if reddit else " (fixture)") + \
            ("" if os.environ.get("ANTHROPIC_API_KEY") else ", AI drafts OFF (no ANTHROPIC_API_KEY)")
    path = write_digest(items, HERE / "out", stats)
    telegram(items)
    print(f"Done: {stats}\nDigest: {path}")


if __name__ == "__main__":
    main()
