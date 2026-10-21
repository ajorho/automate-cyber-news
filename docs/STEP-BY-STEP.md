# Step-by-Step — Building the Vigie Cyber News Portal

The complete, chronological record of how this project was designed, built,
deployed and verified. Every command is reproducible; lab-specific values are
sanitized to placeholders:

| Placeholder | Meaning |
|---|---|
| `<LAB-HOST>` | the small lab server (LXC container) that already runs Docker |
| `<LAN-IP>` | its static-by-DHCP LAN address |
| `<SSH-USER>` | unprivileged host user allowed `sudo -n` |
| `<SSH-KEY>` | path to the ed25519 private key in the build sandbox |
| `<RECIPIENT>` | email address receiving the daily digest |
| `<TOKEN-FILE>` | root-owned OAuth2 token JSON on the host |

---

## Phase 0 — Requirements

The owner asked for:

1. A web portal on the same lab host as an existing service, **on a different
   port** (8090 was taken → choose **8091**).
2. A deep web-search-based weekly report (in French) plus **live RSS feeds**.
3. Feeds grouped so each content tab stays clean: **one dedicated tab for all
   feeds**, showing **only articles less than 24 hours old**.
4. A **dedicated ICS/OT section** (news + recent breaches) — the owner works in
   GRC with an IEC 62443 / OT focus.
5. A **daily automated job** that refreshes the portal and **emails a
   notification**.

Derived constraints: the build sandbox has Python but no `curl`/`scp`; the
target host has Python 3.12 but **no `requests`** and no feedparser — so
everything server-side must be **stdlib-only**.

## Phase 1 — Feed selection (verify before you trust)

Candidate feeds were **tested from the build sandbox first** — vendor blog feeds
die often, and a dead feed would show as a permanent error box.

```python
# quick probe, run per candidate
import urllib.request, socket
socket.setdefaulttimeout(12)
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (vigie-cyber)"})
data = urllib.request.urlopen(req).read(20000)
ok = b"<item" in data or b"<entry" in data
```

Results that shaped the final list:

| Candidate | Verdict |
|---|---|
| The Hacker News, KrebsOnSecurity, BleepingComputer, Infosecurity, Security Affairs, Dark Reading, Schneier, CISA all-advisories | ✅ kept (cyber) |
| Ars Technica, The Verge, TechCrunch, Engadget | ✅ kept (tech) |
| The Decoder, MarkTechPost, Simon Willison, Google AI Blog, Hugging Face | ✅ kept (AI) |
| IndustrialCyber, SecurityWeek ICS/OT category, Tenable blog | ✅ kept (OT) |
| VentureBeat AI | ❌ HTTP 429 (rate-limited) |
| IEEE Spectrum, cyber.gc.ca RSS, Dragos/Claroty/Nozomi/WaterISAC/SANS blogs | ❌ 404 / refused |

**Lesson:** probe every feed; prefer category-specific feeds (`securityweek.com/
category/ics-ot/feed/`) over generic ones, and re-probe periodically.

## Phase 2 — The update script (`rss_update.py`)

Design decisions, in the order they were made:

1. **stdlib only.** `urllib.request` for fetching, `xml.etree.ElementTree` for
   RSS 2.0 *and* Atom (branch on `item` vs `entry`), `email.utils.parsedate_to_datetime`
   with an ISO-8601 fallback for dates (feeds mix RFC-822 and ISO formats).
2. **XXE / billion-laughs mitigation** without defusedxml: reject any payload
   whose first 4 KiB contains `<!DOCTYPE` or `<!ENTITY` — no legitimate RSS
   feed ships a DTD.
3. **HTML-escaping invariant.** The page injects the fragment with `innerHTML`,
   so the generator escapes `& < > " '` on every title and every attribute, and
   only passes links matching `^https?://` (single quotes → `%27`, quotes →
   `%22`). This is what makes the innerHTML usage safe; the client-side comment
   in `index.html` documents the contract.
4. **24-hour filter applied *before* bookkeeping.** Items older than
   `MAX_AGE_S = 86400` — or with no parseable date — are neither displayed nor
   recorded, so they can never resurface as "new".
5. **GUID dedupe** in `state.json` (`{"seen": {guid: epoch}}`), pruned to the
   4 000 most recent GUIDs so the file cannot grow forever.
6. **Atomic writes** (`write .tmp` then `os.replace`) for the fragment and the
   state file — the web container serves the directory read-only and the cron
   job may run while someone is reading the page.
7. **A feed that fails** renders as `indisponible…` instead of disappearing —
   visible degradation beats silent omission. An up feed with nothing < 24 h
   renders `aucun article de moins de 24 h`.
8. **`--no-email` flag** so the feed pipeline can be tested without touching
   the mail path.

## Phase 3 — The email path (the hardest part)

1. **Token refresh with stdlib**: read `<TOKEN-FILE>`, POST
   `grant_type=refresh_token` + client id/secret to the stored `token_uri`,
   use the returned access token as a Bearer header. The file also stores
   `client_id`/`client_secret`, so nothing else is needed.
2. **Endpoint choice**: `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send`
   with `{"raw": base64url(mime)}`. The *upload multipart* variant returned
   HTTP 400 — use the plain JSON endpoint.
3. **Header encoding**: a subject containing an em-dash broke the request
   (400) until headers were wrapped in `email.header.Header(subject, "utf-8")`.
   An empty `From:` header (the token file's `account` field was empty) also
   caused a 400 — omit the header instead of sending it empty.
4. **Message**: `MIMEMultipart("alternative")` with plain-text and HTML parts,
   new items capped at 14, each tagged `[CYBER] / [TECH] / [IA] / [OT]`.
5. **The structural gotcha**: with the OAuth consent app in **Testing** mode,
   Google expires the refresh token (~7 days, `invalid_grant`). For an
   unattended daily job this is a recurring failure — re-authorize when it
   happens, or switch to SMTP with a Gmail **App Password** (no expiry). This
   project still uses OAuth; the re-auth procedure is:
   - copy the Google-workspace skill's `setup.py` + `_hermes_home.py` onto a
     machine with the client secret,
   - run a 3-line driver that imports `setup`, overrides `SCOPES` to
     `["https://www.googleapis.com/auth/gmail.send"]` (send-only — the job
     never needs to read a mailbox) and calls `get_auth_url()`,
   - the owner approves in a browser and pastes the `localhost:1` redirect,
   - exchange the code, then deploy the fresh token file to the host.

## Phase 4 — The portal page (`index.html`)

1. **Tabs, not one long page.** Vanilla JS tab bar; the active tab is stored in
   the URL hash (`#ot`, `#flux`) so links are shareable and reload-safe.
2. **Content tabs carry no feeds** (owner's explicit preference after a first
   version mixed them): Cybersecurity / IT Technology / AI & Agentic AI /
   ICS/OT hold hand-written French summaries; a single **Flux** tab holds the
   live fragment.
3. **Fragment, not API.** The page `fetch()`es a server-rendered `feeds.html`
   and injects it — works with the dumbest possible web server, no JSON/JS
   rendering layer, and works offline-ish (last fragment stays until the next
   cron run).
4. Feed boxes carry a coloured category tag (CYBER / TECH / IA / OT) so the
   mixed grid stays scannable.
5. Dark theme, system fonts, CSS grid with `auto-fit minmax(280px, 1fr)` — no
   framework, no external assets (the portal must render even with no
   Internet, only the fragment depends on feeds being fetched server-side).

## Phase 5 — Deployment

The build sandbox cannot `scp`; the standard pattern is tar-over-ssh:

```bash
tar czf - -C src index.html rss_update.py | \
ssh -i <SSH-KEY> -o StrictHostKeyChecking=no <SSH-USER>@<LAN-IP> \
  'sudo -n bash -c "mkdir -p /opt/cyber-news && tar xzf - -C /opt/cyber-news \
   && chmod 644 /opt/cyber-news/* && python3 /opt/cyber-news/rss_update.py --no-email"'
```

1. **Container choice**: the host already had `python:3.12-slim` but not nginx —
   rather than pulling an image, the site is served by the stdlib itself:
   ```bash
   docker run -d --name cyber-news --restart unless-stopped \
     -p <LAN-IP>:8091:8091 -v /opt/cyber-news:/srv:ro \
     python:3.12-slim python -m http.server 8091 --directory /srv
   ```
   Site files are mounted **read-only**; the cron job writes beside them.
2. **Verification after every deploy**: `docker ps` status, `docker port`
   (port programming present — never infer it from the command), and
   `curl -s -o /dev/null -w "%{http_code} %{size_download}"` on `/` and
   `/feeds.html` **from the host itself**, then a final render check from a
   desktop browser against the LAN URL (an HTTP 200 is not proof the page works).
3. **A stuck container incident**: one `docker run` hung and left the
   container in `Created` state (port programmed, nothing listening, HTTP 000).
   Diagnosis: `docker ps -a` shows `Created` + a live `docker run` process.
   Fix: `docker rm -f` and re-run — it came up instantly. Do not spend an hour
   suspecting the app when the state is `Created`.

## Phase 6 — Scheduling & boot resilience

1. **Cron** (root, because it must read the root-owned token file):
   ```
   30 7 * * * /usr/bin/python3 /opt/cyber-news/rss_update.py >> /opt/cyber-news/update.log 2>&1 # vigie-cyber
   ```
   Installed idempotently: `crontab -l | grep -v vigie-cyber | ... | crontab -`
   (never blind-overwrite a crontab).
2. **The DHCP race.** The host gets its LAN address by DHCP; at boot Docker can
   start before the address exists, so publishing `<LAN-IP>:8091` fails with
   *cannot assign requested address* — and worse, a plain `docker start` after
   a daemon restart can succeed while **silently skipping the port
   programming** (container "healthy", `docker port` empty, everything
   refused). Fix: a oneshot systemd unit that waits for the address, then
   `docker rm -f` + fresh `docker run`:
   ```ini
   [Unit]
   Description=Vigie Cyber portal (static, port 8091)
   After=docker.service
   Requires=docker.service
   [Service]
   Type=oneshot
   ExecStart=/usr/bin/bash -c 'for i in $(seq 1 60); do ip -4 addr show eth0 | \
     grep -q " inet <LAN-IP>/" && break; sleep 2; done; \
     docker rm -f cyber-news 2>/dev/null; docker run -d --name cyber-news ...'
   RemainAfterExit=yes
   [Install]
   WantedBy=multi-user.target
   ```
3. **Test the unit, not the enable message**: `systemctl restart
   cyber-news.service`, then `systemctl is-active`, `docker port`, and a curl.
   The first test exposed the stuck-`Created` incident above.
4. **pkill footgun**: `pkill -f "docker run ... cyber-news"` matches the SSH
   command line carrying that same string and kills the session. Never pkill a
   pattern that appears in your own remote command.

## Phase 7 — Iterations driven by the owner

1. *v1 — single page, feeds inline* → owner: "didn't like it".
2. *v2 — feeds removed from content tabs, one dedicated Flux tab, 24 h filter* →
   shipped; fragment shrank 92 KB → 19 KB.
3. *v3 — ICS/OT tab added* (owner works in OT security): curated breach/campaign
   summaries + a 4th feed category with an orange OT tag; Flux subtitle updated
   to 20 feeds.
4. Every iteration: edit in the staging copy → tar-over-ssh → run with
   `--no-email` → curl checks → browser render check → copy back to the
   persistent master clone.

## Phase 8 — Publishing this repository

- Master copies live in the build sandbox (`/workspace/…`, which survives
  rebuilds); the repo is a sanitized mirror: **no LAN addresses, hostnames,
  usernames, recipient addresses or token paths** in any committed file.
- `src/` holds the two artefacts; `README.md` is the generic overview; this
  document is the build log. MIT license, matching the owner's other repos.
- Pushed with the GitHub CLI already authenticated in the sandbox:
  ```bash
  git init && git add -A && git commit -m "Initial release"
  gh repo create <OWNER>/automate-cyber-news --public --source . --push
  ```

## Final state (verified)

- Portal serving 200 on `/` and `/feeds.html`; 20/20 feeds fetch OK;
  render check of every tab performed in a real browser preview.
- Cron job in place (07:30 local, logs to `update.log`); systemd unit enabled
  and restart-tested; container auto-recreates on boot.
- Email digest: implemented and code-reviewed end-to-end; activation pending a
  fresh OAuth authorization (Testing-mode expiry, see Phase 3).
