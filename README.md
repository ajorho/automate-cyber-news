# Vigie Cyber — Automated Cybersecurity News & Feeds Portal

A self-hosted, zero-dependency news portal that aggregates cybersecurity,
technology, AI and ICS/OT RSS feeds into a tabbed dark-themed web page, refreshes
itself daily, and emails a digest of new articles. Runs in a single Docker
container on a small lab host, entirely on Python standard library.

**Live sections:** 🛡️ Cybersecurity · 🚀 IT Technology · 🤖 AI & Agentic AI ·
🏭 ICS/OT · 📰 Live Feeds (last 24 h)

## Features

- **4 curated tabs** (French) with hand-written threat summaries, patch
  priorities and GRC notes — no feed clutter.
- **1 dedicated "Flux" tab** showing every feed item published in the **last
  24 hours only** (undated items are dropped), with colour tags per category.
- **20 RSS/Atom feeds** across 4 categories (cyber, tech, AI, ICS/OT).
- **Daily cron job**: rebuilds the feeds fragment, tracks seen article GUIDs
  in a small JSON state file, and emails a digest of new items via the Gmail
  API (OAuth2, send-only scope).
- **Hardened by design**: every title/link is HTML-escaped before the page
  injects the fragment; only `http(s)` links pass; XML feeds containing a DTD
  are rejected (XXE / billion-laughs mitigation without defusedxml); secrets
  never appear in logs — the OAuth token is refreshed in memory from a
  root-owned file on the host.
- **Boot-resilient**: a systemd oneshot unit waits for the host's LAN address
  before recreating the container (avoids the classic DHCP-before-Docker port
  publishing failure).

## Architecture

```
cron 07:30 ──► rss_update.py ──► fetch 20 feeds (urllib, stdlib)
                    │               ├─ parse RSS 2.0 + Atom (xml.etree)
                    │               ├─ filter: published < 24 h
                    │               ├─ dedupe by GUID (state.json)
                    │               └─ render feeds.html (escaped)
                    ├─► writes feeds.html next to index.html
                    └─► Gmail API send (gmail.send scope) ──► digest email

nginx-free web tier: python:3.12-slim + `python -m http.server 8091`
    container `cyber-news`, restart=unless-stopped, site mounted read-only
index.html ──(fetch)──► feeds.html fragment ──► innerHTML into the Flux tab
```

## Repository layout

```
├── README.md            this file
├── docs/
│   └── STEP-BY-STEP.md  the full build log: every step, command and decision
├── src/
│   ├── index.html       the portal (5 tabs, vanilla JS/CSS, no framework)
│   └── rss_update.py    daily job: feeds → fragment → email (stdlib only)
└── LICENSE
```

## Quick start

```bash
# 1. put index.html and rss_update.py in one directory, edit the constants:
#    NOTIFY_TO, PORTAL_URL, and the FEEDS / CATEGORIES dict to your taste
# 2. test the job without email:
python3 rss_update.py --no-email
# 3. serve it any way you like, e.g.:
docker run -d --name cyber-news --restart unless-stopped \
  -p 8080:8080 -v "$PWD":/srv:ro python:3.12-slim \
  python -m http.server 8080 --directory /srv
# 4. schedule it (host crontab):
30 7 * * * /usr/bin/python3 /opt/cyber-news/rss_update.py >> /opt/cyber-news/update.log 2>&1
```

Email sending requires an OAuth2 token file with `refresh_token`, `client_id`,
`client_secret` and `token_uri` (the standard Google Python client storage
format) at the path set in `TOKEN_FILE`, granted the `gmail.send` scope.

## Design notes & gotchas (detailed in docs/STEP-BY-STEP.md)

- **Why no feedparser/requests?** The target host is a minimal container with
  stdlib-only Python; `urllib` + `xml.etree` cover everything RSS needs.
- **innerHTML is safe here** only because the generator escapes all text and
  whitelists link schemes. Keep that invariant if you modify the fragment.
- **OAuth refresh tokens expire (~7 days) while the consent app is in Testing
  mode** — plan re-auth or switch to an SMTP app password for unattended use.
- **Docker port publishing can silently fail at boot** when the LAN address
  arrives after the daemon starts; a oneshot systemd unit that waits for the
  address and force-recreates the container fixes it. A plain `docker start`
  is *not* enough.

## License

MIT — see [LICENSE](LICENSE).
