#!/usr/bin/env python3
"""Vigie Cyber — job quotidien : flux RSS du portail (3 onglets) + notification courriel.

Exécuté par cron sur CT-Hermes01 : python3 /opt/cyber-news/rss_update.py
Option : --no-email (met à jour les fragments sans envoyer de courriel)
- récupère les fils RSS, régénère feeds.html (tous les fils, articles < 24 h uniquement)
  injecté par l'onglet « Flux » de index.html
- envoie un courriel récapitulatif des nouveaux articles à aziz.jorho@gmail.com
Dépendances : stdlib uniquement (urllib, xml.etree). Aucune valeur de secret n'est affichée.
"""
import base64
import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = "/opt/hermes/.hermes/google_token.json"
STATE_FILE = os.path.join(BASE, "state.json")
NOTIFY_TO = "aziz.jorho@gmail.com"
PORTAL_URL = "http://10.0.0.10:8091"
MAX_PER_FEED = 6
MAX_EMAIL_ITEMS = 14
MAX_AGE_S = 24 * 3600  # on n'affiche que les articles de moins de 24 h
UA = "Mozilla/5.0 (compatible; vigie-cyber-portal/1.0)"

CATEGORIES = {
    "cyber": {
        "title": "Cybersécurité",
        "file": "feeds-cyber.html",
        "feeds": {
            "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
            "KrebsOnSecurity": "https://krebsonsecurity.com/feed/",
            "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
            "Infosecurity Magazine": "https://www.infosecurity-magazine.com/rss/news/",
            "Security Affairs": "https://feeds.feedburner.com/securityaffairs",
            "Dark Reading": "https://www.darkreading.com/rss.xml",
            "Schneier on Security": "https://www.schneier.com/feed/",
            "CISA Advisories": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        },
    },
    "tech": {
        "title": "Nouvelles technologies IT",
        "file": "feeds-tech.html",
        "feeds": {
            "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
            "The Verge": "https://www.theverge.com/rss/index.xml",
            "TechCrunch": "https://techcrunch.com/feed/",
            "Engadget": "https://www.engadget.com/rss.xml",
        },
    },
    "ot": {
        "title": "ICS/OT",
        "file": "unused",
        "feeds": {
            "IndustrialCyber": "https://industrialcyber.co/feed/",
            "SecurityWeek ICS/OT": "https://www.securityweek.com/category/ics-ot/feed/",
            "Tenable Blog": "https://www.tenable.com/blog/feed",
        },
    },
    "ia": {
        "title": "IA & IA agentique",
        "file": "feeds-ia.html",
        "feeds": {
            "The Decoder": "https://www.the-decoder.com/feed/",
            "MarkTechPost": "https://www.marktechpost.com/feed/",
            "Simon Willison": "https://simonwillison.net/atom/everything/",
            "Google AI Blog": "https://blog.google/technology/ai/rss/",
            "Hugging Face Blog": "https://huggingface.co/blog/feed.xml",
        },
    },
}


def esc(s: str) -> str:
    """HTML-escape: les fragments sont injectés via innerHTML, tout doit être inerte."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def safe_url(u: str) -> str:
    u = str(u).strip()
    if re.match(r"^https?://", u, re.I):
        return u.replace('"', "%22").replace("'", "%27")
    return ""


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(2_000_000)


def parse_date(*raws):
    for raw in raws:
        raw = clean(raw)
        if not raw:
            continue
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass
    return 0.0


def parse_feed(name: str, url: str):
    """Retourne [(epoch, title, link, guid)] pour RSS 2.0 et Atom."""
    out = []
    try:
        data = fetch(url)
        # Mitigation XXE / billion-laughs (defusedxml absent sur l'hôte) :
        # les fils RSS légitimes n'embarquent pas de DTD ; on refuse toute déclaration.
        head = data[:4096].lower()
        if b"<!doctype" in head or b"<!entity" in head:
            raise ValueError("DTD refusée (mitigation XXE)")
        root = ET.fromstring(data)
    except Exception as e:
        print(f"[feed] {name}: INDISPONIBLE ({type(e).__name__}: {e})")
        return None
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag == "item":  # RSS
            title = link = guid = ""
            for ch in item:
                t = ch.tag.rsplit("}", 1)[-1]
                if t == "title":
                    title = clean(ch.text)
                elif t == "link":
                    link = clean(ch.text)
                elif t == "guid":
                    guid = clean(ch.text)
            date_raw = next((clean(ch.text) for ch in item
                             if ch.tag.rsplit("}", 1)[-1] == "pubDate"), "")
            if not link:
                link = next((clean(ch.text) for ch in item
                             if ch.tag.rsplit("}", 1)[-1] == "guid"), "")
            out.append((parse_date(date_raw), title, link, guid or link))
        elif tag == "entry":  # Atom
            title = clean(next((el.text or "" for el in item if el.tag.rsplit("}", 1)[-1] == "title"), ""))
            link = ""
            for el in item:
                if el.tag.rsplit("}", 1)[-1] == "link":
                    href = el.get("href", "")
                    if safe_url(href):
                        link = clean(href)
                        break
            updated = next((clean(el.text) for el in item
                            if el.tag.rsplit("}", 1)[-1] in ("updated", "published")), "")
            eid = next((clean(el.text) for el in item if el.tag.rsplit("}", 1)[-1] == "id"), "")
            out.append((parse_date(updated), title, link, eid or link))
    return out


def build_fragment(all_entries, ts):
    """all_entries = [(cat, name, entries|None)] — un seul fragment pour l'onglet Flux."""
    parts = []
    n_ok = 0
    for cat, name, entries in all_entries:
        tag = f'<span class="tag tag-{esc(cat)}">{esc(CAT_LABEL.get(cat, cat))}</span> '
        if entries is None:
            parts.append(
                f'<div class="feedbox"><h3>{tag}{esc(name)}</h3>'
                f'<div class="d">indisponible lors de la dernière mise à jour</div></div>')
            continue
        n_ok += 1
        if not entries:
            parts.append(
                f'<div class="feedbox"><h3>{tag}{esc(name)}</h3>'
                f'<div class="d">aucun article de moins de 24 h</div></div>')
            continue
        lis = []
        for epoch, title, link, _gid in entries:
            d = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%d %b %H:%M") if epoch else ""
            li = f'<li><a href="{esc(link)}" rel="noopener">{esc(title)}</a>' if link else f"<li>{esc(title)}"
            if d:
                li += f' <span class="d">· {esc(d)}</span>'
            lis.append(li + "</li>")
        parts.append(
            f'<div class="feedbox"><h3>{tag}{esc(name)}</h3><ul>{"".join(lis)}</ul>'
            f'<div class="d">maj {ts} UTC</div></div>')
    return "\n".join(parts), n_ok


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def write_atomic(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)


def gmail_send(subject: str, html: str, text: str):
    tok = json.load(open(TOKEN_FILE))
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(tok["token_uri"], data=data), timeout=30) as r:
        access = json.loads(r.read())["access_token"]

    msg = MIMEMultipart("alternative")
    msg["To"] = NOTIFY_TO
    if tok.get("account"):
        msg["From"] = tok["account"]
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = json.dumps({"raw": raw}).encode()
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {access}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


CAT_LABEL = {"cyber": "CYBER", "tech": "TECH", "ia": "IA", "ot": "OT"}


def main():
    send_email = "--no-email" not in sys.argv
    first_run = not os.path.exists(STATE_FILE)
    state = load_state()
    seen = state.get("seen", {})

    new_items, n_ok_total, n_feeds_total = [], 0, 0
    all_entries = []  # [(cat, name, entries|None)] pour le fragment unique
    cutoff = time.time() - MAX_AGE_S
    for cat, cfg in CATEGORIES.items():
        for name, url in cfg["feeds"].items():
            entries = parse_feed(name, url)
            n_feeds_total += 1
            if entries:
                n_ok_total += 1
                entries.sort(key=lambda x: x[0], reverse=True)
                # filtre 24 h : les articles plus vieux ou sans date ne sont ni affichés ni suivis
                entries = [e for e in entries if e[0] and e[0] >= cutoff][:MAX_PER_FEED]
                for epoch, title, link, guid in entries:
                    if guid and guid not in seen and title:
                        new_items.append((epoch, CAT_LABEL[cat], title, link))
                    if guid:
                        seen[guid] = epoch
            all_entries.append((cat, name, entries))

    fragment, n_ok = build_fragment(all_entries,
                                    datetime.now(timezone.utc).strftime("%d %b %H:%M"))
    write_atomic(os.path.join(BASE, "feeds.html"), fragment)
    print(f"[feeds] {n_ok}/{n_feeds_total} fils OK -> feeds.html (articles < 24 h)")

    # limite le dictionnaire "seen" aux 4000 GUID les plus récents
    if len(seen) > 4000:
        keep = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:4000]
        seen = dict(keep)
    save_state({"seen": seen, "last_run": time.time()})
    print(f"[feeds] total {n_ok_total}/{n_feeds_total} fils OK, {len(new_items)} nouveaux articles")

    # ---- courriel ----
    if not send_email:
        print("[email] sauté (--no-email)")
        return
    now_local = datetime.now().strftime("%d %B %Y, %H:%M")
    if first_run:
        subject = "Vigie Cyber — portail activé, notifications quotidiennes en place"
        intro = ("Le portail de veille <a href='%s'>Vigie Cyber &amp; Techno</a> est en ligne "
                 "(CT-Hermes01, port 8091) avec 3 onglets : Cybersécurité, Nouvelles technologies IT, "
                 "IA &amp; IA agentique. Vous recevrez chaque jour à 7h30 (heure de l'Alberta) "
                 "un résumé des nouveaux articles détectés par les flux RSS." % PORTAL_URL)
    else:
        subject = f"Vigie Cyber — {len(new_items)} nouveaux articles aujourd'hui"
        intro = (f"{len(new_items)} nouveaux articles détectés ce matin (cyber, techno, IA). "
                 "Portail complet : <a href='%s'>%s</a>" % (PORTAL_URL, PORTAL_URL))

    new_items.sort(key=lambda x: x[0], reverse=True)
    rows = []
    for epoch, cat, title, link in new_items[:MAX_EMAIL_ITEMS]:
        d = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%d %b") if epoch else ""
        if link and safe_url(link):
            rows.append(f"<li><strong>[{esc(cat)}]</strong> {d} — "
                        f"<a href='{esc(link)}'>{esc(title)}</a></li>")
        else:
            rows.append(f"<li><strong>[{esc(cat)}]</strong> {d} — {esc(title)}</li>")
    more = "" if len(new_items) <= MAX_EMAIL_ITEMS else \
        f"<p>… et {len(new_items) - MAX_EMAIL_ITEMS} autres sur le portail.</p>"

    html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;background:#0d1117;color:#e6edf3;padding:20px;">
<h2 style="color:#58a6ff;margin-top:0;">🛡️ Vigie Cyber &amp; Techno</h2>
<p>{intro}</p>
<ul style="line-height:1.7;">{"".join(rows)}</ul>{more}
<p style="color:#8b949e;font-size:.85em;">Notification automatique du job quotidien (7h30, America/Edmonton) ·
portail : {PORTAL_URL} · générée le {now_local}.</p>
</body></html>"""
    text = intro + "\n\n" + "\n".join(
        f"- [{cat}] {title} {link}".strip() for _e, cat, title, link in new_items[:MAX_EMAIL_ITEMS])

    if not rows:
        print("[email] aucun nouvel article : pas de courriel")
        return
    resp = gmail_send(subject, html, text)
    print(f"[email] envoyé à {NOTIFY_TO} (id={resp.get('id', '?')[:12]}…)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode(errors="replace")[:300]
            except Exception:
                pass
        print(f"[ERREUR] {type(e).__name__}: {e} {body}")
        sys.exit(1)
