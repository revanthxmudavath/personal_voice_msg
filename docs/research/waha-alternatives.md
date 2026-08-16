# WhatsApp connection alternatives to WAHA — research note

**Date:** 2026-08-16
**Status:** Informational research, not a task-log entry. No code or config changed as a result of
this note. Not part of `IMPLEMENTATION_PLAN.md`; does not supersede AGENTS.md's confirmed stack
(`WAHA Core behind a narrow internal sender`, AGENTS.md §Confirmed stack).

**Trigger:** T16's done-when gate is blocked because the real, owner-paired WAHA session logged out
and is refusing to re-pair (AGENTS.md §Immediate next step, as of the T16 status recorded there).
This note surveys whether a different WhatsApp-connection layer would avoid that failure mode, using
four parallel web-research subagents (Baileys direct-use, Node.js WAHA-style wrappers, the Go/
whatsmeow ecosystem, and the official Meta Cloud API + direct community verdict on WAHA). Full raw
agent output is not reproduced here; this is the synthesized, source-linked conclusion.

## Current stack (for reference)

- `docker-compose.yml`: `devlikeapro/waha:noweb-2026.7.2`, pinned tag + digest, loopback-only
  (`127.0.0.1:3000`), named volume for session data (never a repo bind mount).
- NOWEB engine = WAHA's wrapper around the Node.js library **Baileys**
  (github.com/WhiskeySockets/Baileys), a pure-WebSocket WhatsApp multi-device protocol
  implementation (no browser/Puppeteer).
- `src/personal_voice_msg/sender.py` calls `POST /api/sendVoice` after HMAC/timestamp/replay/audio
  checks; destination is always `settings.recipient`, read server-side (T15's locked sender
  boundary).
- MIT-licensed, no public ports, WAHA/SQLite/model/TTS/application APIs unreachable from outside the
  host (AGENTS.md §Network and container rules).

## The core finding

**The session-logout/re-pair failure is not a WAHA bug — it's a documented, systemic issue in the
Baileys library WAHA-NOWEB wraps**, and it reproduces on WAHA's own tracker under the exact engine
this project runs:

- WAHA/NOWEB: ["session cannot recover by itself... after reconnecting, the same issue happens
  again"](https://github.com/devlikeapro/waha/issues/2081) (#2081); also
  [#904](https://github.com/devlikeapro/waha/issues/904),
  [#884](https://github.com/devlikeapro/waha/issues/884).
- Upstream Baileys itself: [QR pairing fails with a 401 "Unable to link
  device"](https://github.com/WhiskeySockets/Baileys/issues/2381) (#2381); [reconnect establishes a
  socket but WhatsApp mobile rejects the session and forces
  logout](https://github.com/WhiskeySockets/Baileys/issues/2110) (#2110);
  [#1976](https://github.com/WhiskeySockets/Baileys/issues/1976),
  [#1625](https://github.com/WhiskeySockets/Baileys/issues/1625).
- WAHA's own docs institutionalize a manual "Restart → Logout → Start again, rescan QR" recovery
  ritual for this exact scenario (https://waha.devlike.pro/docs/how-to/sessions/).

**Ban/logout risk is architecture-wide, not WAHA-specific.** Every unofficial WhatsApp-Web-automation
project — Baileys, whatsapp-web.js, venom-bot, WPPConnect — impersonates the linked-device protocol
WhatsApp actively fingerprints against. 2025-2026 evidence of tightening enforcement:
[5 bots banned in one week after 3+ stable years](https://github.com/WhiskeySockets/Baileys/issues/1869)
(#1869); a new in-app warning about ["unauthorized"
tools](https://github.com/WhiskeySockets/Baileys/issues/2658) (#2658). **No self-hosted alternative
below eliminates this risk** — each option changes the risk's shape (which detection heuristic, which
maintainer's bugs), not whether it exists.

## Options evaluated

| Option | License / engine | Verdict |
|---|---|---|
| **WAHA's own GOWS engine** (config change, not a migration) | Same WAHA container, third built-in engine option, wraps Go's `whatsmeow` instead of Baileys | Cheapest experiment by far — same `/api/sendVoice` + webhook contract `sender.py` already expects, just an engine setting. `whatsmeow` has a better-regarded session-longevity reputation than Baileys in maintainer/community discussion ([whatsmeow discussion #979](https://github.com/tulir/whatsmeow/discussions/979)), though not formally benchmarked. [WAHA engines docs](https://waha.devlike.pro/docs/engines/) warn API/webhook payload shapes may shift slightly between engines — verify before relying on it. |
| **wuzapi** (Go, `whatsmeow`) | MIT | Closest structural one-to-one WAHA replacement: documented `POST /chat/send/audio` for PTT voice notes, HMAC-signed webhooks, single lightweight Go binary, official Docker image, active commits (days-old at research time). Needs a small adapter for its JSON schema (`Phone`/`Audio` fields vs WAHA's), no architectural rework. |
| **Evolution API** (Node, Baileys) | Apache-2.0 **+ mandatory attribution notice clause** (not a plain permissive license) | Largest community, real `sendWhatsAppAudio` endpoint + webhooks — but built on the **same Baileys engine** implicated in the current failure, and has open pairing/reconnect issues of the identical shape ([#2430](https://github.com/EvolutionAPI/evolution-api/issues/2430): infinite reconnection loop, no QR ever produced). Likely inherits this exact problem rather than fixing it. |
| **WPPConnect Server** (Puppeteer/Chromium) | Apache-2.0 | Genuinely different engine family (real headless browser, not raw WebSocket) — worth trying *because* it doesn't share WAHA-NOWEB's code path. Heavier resource use; has its own reconnect-after-restart bugs ([#1844](https://github.com/wppconnect-team/wppconnect-server/issues/1844), [#2206](https://github.com/wppconnect-team/wppconnect-server/issues/2206)) to read before switching. |
| **GOWA** (Go, `whatsmeow`, more features/UI than wuzapi) | MIT | **Ruled out for this use case**: open bug where sending Opus/PTT audio reports `SUCCESS` with a message ID but the recipient never receives it ([#501](https://github.com/aldinokemal/go-whatsapp-web-multidevice/issues/501)) — a direct dealbreaker for a voice-note sender. |
| Raw Baileys (no wrapper, write a thin custom service) | MIT | Same underlying risk as today (it *is* what WAHA-NOWEB wraps), plus ~1-2 days rebuilding session/reconnect/webhook handling WAHA already provides. Not a fix, just less abstraction to debug through. Also: a 2025-2026 npm supply-chain campaign shipped ~70 malicious packages impersonating Baileys forks (one, "lotusbail," had 56k+ downloads) that stole session credentials — pin only the canonical `@whiskeysockets/baileys` package if pursuing this. |
| venom-bot / whatsapp-web.js used directly | Apache-2.0 | Libraries, not REST/webhook servers — would mean rebuilding the wrapper layer WAHA already provides. Deprioritized. |
| mautrix-whatsapp | AGPL-3.0 | Wrong tool — a Matrix puppeting bridge requiring a full Matrix homeserver (Synapse + DB) to receive/send anything. Excluded on architecture grounds, not stability. |
| **OpenWA** (`rmyndharis/OpenWA`, Node/TypeScript/NestJS) | MIT, no paid tier | Dual-engine, switchable per deployment via `ENGINE_TYPE`: `whatsapp-web.js` (Puppeteer, lower ban-risk, ~300–500MB RAM/session) or `baileys` (WebSocket, higher fingerprint risk, ~30–80MB RAM/session) — project publishes this trade-off table itself. Most explicit PTT contract found: `POST /api/sessions/:sessionId/messages/send-audio` with `ptt: true` → real voice-note bubble, `type: "voice"`. HMAC-signed webhooks. **Verified directly (2026-08-16):** MIT, 12,818 stars, 2,936 forks, 2,055 commits, 53 contributors, repo created 2026-02-02 (~6.5 months old), last push 2026-08-16, only 4 open issues. That star-to-issue ratio is unusual for a 6.5-month-old project — no evidence of impropriety found (real satellite repos, own domain, transparent compliance disclaimer in its own docs steering regulated use toward the official Cloud API instead), but also no multi-year issue trail like WAHA/Baileys/Evolution have, so it hasn't yet surfaced (or not) the session-logout failure class this note is about. Worth a parallel sandbox trial, not a first move for the production send. |
| **Official Meta WhatsApp Business Platform (Cloud API)** | Official, not open source | Ban/restriction risk genuinely near-zero, and setup is cheaper than reputation suggests for one fixed recipient (a free test-number sandbox exists; no forced business verification or app review at this volume). **But structurally cannot do this project's job**: proactive daily messages outside a customer-initiated 24h window must go through a pre-approved template, and WhatsApp template headers do not support an audio/voice-note format (only TEXT/IMAGE/VIDEO/DOCUMENT/LOCATION, confirmed against three separate Meta doc pages). Likely cannot deliver a native voice-note bubble for this use case at all. |

## Recommendation

Per Karpathy "simplest thing that could work": **try switching the existing WAHA container from
NOWEB to its built-in GOWS engine before evaluating any migration.** It changes one engine setting
against infrastructure that's already configured, hardened, and integrated (T15's locked sender
boundary, T18's network rules) — no change to `sender.py`, Docker hardening, or T16 delivery logic.
It swaps out the specific library (Baileys) implicated in the current failure for one with a
better-regarded session-longevity track record, without a migration.

If GOWS still logs out and won't re-pair, that's a real signal to migrate off WAHA entirely, and
**wuzapi** is the best-fit landing spot — closest to what `sender.py` already expects structurally
(audio-send endpoint + webhooks, MIT, single binary, actively maintained). **OpenWA** is a reasonable
parallel sandbox candidate (its per-deployment Baileys/whatsapp-web.js choice is genuinely useful for
isolating whether the failure is engine-specific), but its 6.5-month track record on this exact
failure class is unproven either way — don't make it the first move for the production send.

**Result, real-world (2026-08-16):** GOWS was tried. Same WAHA container, `WHATSAPP_DEFAULT_ENGINE=GOWS`,
confirmed genuinely running (`engine.gows.found/connected: true` in the API response, not just the
image tag), fresh session, reached `SCAN_QR_CODE`. Scanned ~50 minutes later: refused with the
identical "can't link new device" error, session confirmed `FAILED`. This revises the recommendation
above: **wuzapi is now a low-value next step**, not the best-fit landing spot the original reasoning
suggested — it shares GOWS's exact `whatsmeow` engine, so a refusal there would almost certainly
reproduce this same result through a different wrapper around the same already-tested protocol
implementation, not add new information. The one mechanism now worth testing before treating this as
conclusively account-wide is **OpenWA in its `whatsapp-web.js` mode specifically** (not its `baileys`
mode, which shares NOWEB's already-failed engine) — a real headless-browser session automating the
actual WhatsApp Web site is architecturally distinct from both WebSocket-protocol reimplementations
(Baileys, whatsmeow) now tested, and may receive different treatment from whatever is enforcing this
throttle. Two properly-spaced NOWEB attempts (2026-08-12, 2026-08-15) plus this GOWS attempt
(2026-08-16) are now three refusals across two distinct protocol families on the same account — see
`.superpowers/sdd/2026-08-09-t16-exactly-once-delivery/progress.md` in the T16 worktree for the full
attempt-by-attempt record.

**Result, real-world (2026-08-16, same day): both OpenWA and WPPConnect Server were tried, in an
isolated sandbox worktree, never wired into production code.** OpenWA ran with
`ENGINE_TYPE=whatsapp-web.js`, `MCP_ENABLED=false`, no Docker-socket mount (confirmed disabled in its
own boot log). WPPConnect Server ran its own default `whatsapp-web.js`-based engine. Both reached
QR-ready independently; both were scanned and refused with the identical "can't link new device"
error. Confirmed via each tool's own logs, not just the scan report: WPPConnect logged an explicit
`qrReadError` / "Failed to authenticate" / "Auto Close Called" after a 60s window; OpenWA never
reached a connected state and kept silently regenerating a fresh QR roughly every 20–60s for the full
~6 minutes observed — WhatsApp's server never completed the pairing handshake regardless of how many
times the client requested a new code. Both sandboxes were fully torn down (containers, volumes,
images) immediately after, per the experiment's own design.

**This is decisive.** Four architecturally distinct implementations — Baileys (NOWEB), whatsmeow
(GOWS), and whatsapp-web.js via two independent server wrappers (OpenWA, WPPConnect Server) — across
5 total real linking attempts on this account, all refused identically. This exhausts essentially the
entire practical design space of self-hosted "link this account as a WhatsApp Web device" approaches:
every other option in the table above (raw Baileys, venom-bot, Evolution API, GOWA) reuses one of the
two already-tested underlying libraries (Baileys or whatsmeow), so none would add new information.
**The evidence now strongly supports this being WhatsApp's own account-level device-linking
throttle/restriction, not an implementation-specific bug.** Further self-hosted WhatsApp-Web
experiments on this account are not recommended — see this plan's ledger
(`.superpowers/sdd/2026-08-09-t16-exactly-once-delivery/progress.md`) for the full record and next
steps under discussion (a different account/number, or a non-WhatsApp-Web platform such as Telegram).

## Sourcing caveats

Reddit (reddit.com, old.reddit.com, `site:reddit.com` queries) was fully blocked to the research
agents' fetch/search tools in this environment across many query attempts — no genuine Reddit thread
was retrieved despite that being explicitly requested. YouTube page fetches returned only
titles/upload dates, not transcripts, on this niche topic. Sentiment evidence above is drawn instead
from GitHub issue trackers (arguably more concrete for a developer-tool question), official docs, and
named first-person blog/forum posts — flagged inline per-claim in the original agent reports where a
claim could not be sourced. Treat any "no complaints found" for a newer project (e.g. OpenWA,
mentioned but not tabled above) as "insufficient track record," not "more stable."
