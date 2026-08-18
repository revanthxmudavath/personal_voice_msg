# Next delivery platform — research note

**Date:** 2026-08-16
**Status:** Informational research, not a task-log entry. No code or config changed as a result of
this note. Not part of `IMPLEMENTATION_PLAN.md`; does not supersede `AGENTS.md`'s confirmed stack
until a design is approved and a plan amendment lands.

**Trigger:** `docs/research/waha-alternatives.md` concluded the owner's WhatsApp account is blocked
at WhatsApp's own server-side device-linking layer — 5 real linking attempts across 4
architecturally distinct client libraries, all refused identically. That path is closed. This note
evaluates what `personal_voice_msg` delivers through next, using five parallel research agents (one
per candidate), each doing real web search, GitHub issue tracker research, and official docs —
matching the sourcing discipline of the WAHA note.

**Decision priorities set by the owner for this evaluation, in order:**
1. Lowest cost/effort to stand up and operate.
2. Free/zero ongoing cost strongly preferred — a paid tier is a fallback only if free options are
   structurally disqualified.
3. Recipient reachability is currently unknown for every non-WhatsApp platform — treated as open,
   to be confirmed with the recipient once the field narrows to 1-2 finalists, not before.

## Current stack (for reference)

`src/personal_voice_msg/sender.py` (373 lines): HMAC-signed local-trigger auth, a replay-nonce
table (T15), then a `POST /api/sendVoice` to WAHA, with `SenderRejected` (safe to retry) vs.
`SenderAmbiguous` (must reconcile) outcomes. Because WAHA gives no client-supplied idempotency key
and no reliable synchronous "did this send" signal, T16 built a whole second subsystem —
`reconcile_delivery`/`_find_matching_provider_id`/`_no_match_outcome` — that scrapes the recipient's
chat history with bounded polling and a grace period to resolve ambiguous submissions before any
retry. That reconciliation subsystem is over half the file and exists *only* because WAHA is opaque
about its own outcome.

## Options evaluated

| Option | Cost | Proactive send? | Audio/voice capability | Idempotency / delivery-guarantee story | Ban/policy risk | T16-equivalent design impact | Verdict |
|---|---|---|---|---|---|---|---|
| **Telegram Bot API** | Free | Yes, after a one-time recipient `/start` — light, non-recurring ask, no 24h-window gate afterward | `sendVoice` takes OGG/Opus directly — exact match to T14's existing FFmpeg output, no re-encoding needed. 50MB cap, far beyond a voice note. | No client idempotency key exists, and there is **no** chat-history-read method for bots at all — so WAHA-style reconciliation-by-scraping isn't just unneeded, it's structurally impossible. But most real failures (bad chat, blocked bot, oversized file, bad token, rate limit) come back as fast, definite, synchronous errors (400/401/403/429). Only a true connection-drop-mid-response stays genuinely ambiguous, and nothing exists to poll for it. | Official API, so no device-fingerprint-ban risk by construction. Real but soft: Bot ToS bars "unsolicited"/"spam" messaging; a recipient who opted in via `/start`, at 1 msg/day to one person, is a defensible but not pre-cleared case. | **Delete, don't port**, the entire reconciliation subsystem (~50%+ of `sender.py`) — nothing to scrape. Keep the HMAC/replay-nonce local-trigger auth (platform-agnostic). `SenderRejected`/`SenderAmbiguous` split stays but shifts hard toward `Rejected`, since most failures are now definite. | **Best fit against the owner's stated priorities** — free, lowest effort, format-compatible audio pipeline, real T16 simplification. Weakest point: no proactive STOP query (block is only inferred from a failed-send 403), so an explicit in-band STOP-keyword convention is still needed layered on top, same as today. |
| **A different WhatsApp number, same WAHA/VPS stack** | ~$0–10 (spare SIM) if it worked | N/A — same automation model | Same as today (unchanged) | Same as today (unchanged) | Evidence strongly implicates a **datacenter/VPS IP-level block**, not an account-level one: a controlled residential-vs-datacenter A/B test on the identical account/library ([Baileys #2705](https://github.com/WhiskeySockets/Baileys/issues/2705)) failed only from the datacenter IP; the identical symptom reproduced across three unrelated cloud providers ([openclaw #9882](https://github.com/openclaw/openclaw/issues/9882)); a separate maintainer built anti-ban tooling around exactly this theory ([vesta PR #921](https://github.com/elyxlz/vesta/pull/921)). This project's own evidence (4 different libraries, same VPS, identical refusal) already pointed the same direction before this research. | Zero code change if it worked | **Low expected value** — changes the variable (phone number) least supported by the evidence as the actual cause (network origin). A fifth same-VPS attempt is very likely a sixth identical refusal, not a fix. |
| **Signal via `signal-cli`** | Low (spare SIM; software itself free) | Yes, but lands as a "message request" the recipient must accept once | Generic audio attachment (no protocol-level voice-note distinction) | No synchronous delivery guarantee — server-accepted ≠ delivered; true delivery/read confirmation is async, disableable client-side. **T16's reconciliation design would need to be reused nearly as-is**, just against `signal-cli`'s receipt-notification stream instead of WAHA's chat scraping — not a simplification. | Real and non-trivial: Signal's own ToS explicitly bars "unauthorized or automated" account creation and "bulk messaging, auto-messaging, and auto-dialing" — closer textual match to this project's literal behavior than anything found in WhatsApp's terms. Signal also has a documented, active anti-automation detection system, and a March 2026 protocol-enforcement change (SPQR) mass-deregistered every pre-update `signal-cli` account with no advance warning — a different-shaped but comparably serious version of the exact "the platform silently broke our automated client" failure this whole evaluation exists to escape. | **Deprioritized** — not clearly cheaper or lower-effort than Telegram, weaker delivery-guarantee story, and carries a real echo of the WhatsApp problem in a new form. |
| **Discord bot DM** | Free | **No** in the WhatsApp/Telegram sense — Discord's API mechanically requires a mutual server before a bot can open a DM at all; a cold DM to someone who's never interacted with the bot isn't just discouraged, the API blocks it | Confirmed working: `IS_VOICE_MESSAGE` flag + OGG/Opus/48kHz attachment — happens to match T14's output exactly. But this is **unofficial/reverse-engineered** behavior per Discord's own docs-repo discussion, not a committed, versioned bot feature — real regression risk. | **Best of everything researched, technically**: synchronous 2xx + message ID, plus a genuine server-enforced `nonce`/`enforce_nonce` dedup primitive WAHA never had. This could shrink T16 even more than Telegram's option. | Discord's Developer Policy explicitly bars "frequently sending unsolicited direct messages" not related to core functionality — a cold first DM is squarely what this targets. | Reconciliation subsystem is deletable, same as Telegram, with an even cleaner replacement (pass the internal idempotency key straight through as Discord's `nonce`). | Technically the strongest option, but the **heaviest onboarding of any candidate** — recipient must join a private server and have DM-from-members enabled before anything can be sent — and a real, explicit policy conflict. Under "lowest cost/effort," this loses to Telegram on the one part that isn't code: getting the recipient set up at all. |
| **SMS/MMS via Twilio** | **~$2–5/month, forever** (number rental + per-message + likely registration fees) | Yes, no opt-in gate | MMS *can* carry audio, but outside iMessage-to-iMessage delivery it renders as a generic tap-to-play/download attachment, not a native voice-note bubble — a real product-concept degradation | No client idempotency key on the actual send endpoint (contrary to the initial assumption — that only exists on Twilio's separate Broadcast API). Real strength is delivery-status observability (`queued`→`sent`→`delivered`/`undelivered`/`failed` with stable error codes), reachable only by outbound polling given this project's no-public-inbound-ports rule (`AGENTS.md` §Network and container rules forbids accepting Twilio's webhook callback). Reconciliation must still be built, same shape as WAHA's, just against better-documented status data. | Low — official, carrier-regulated. Requires either A2P 10DLC campaign registration (recurring fee, EIN) or Toll-Free Verification (business ID + review lag of "days to a week or more") — real paperwork either way, no same-day path. | Moderate rewrite: transport/auth layer replaced, `reconcile_delivery`'s scraping swapped for Message-resource-by-SID polling; the pattern survives even if the code doesn't. | **Fails the free-only preference outright** and gives a degraded voice-note UX. Its one clear win — a real, carrier-enforced STOP convention (closest match of anything researched to `AGENTS.md`'s "exact STOP disables sending durably" requirement) — isn't enough to outrank it under the owner's stated cost/effort priority. Kept only as a documented fallback if every free option turns out to be structurally disqualified. |

## Recommendation

Per the owner's own stated priority ("lowest cost/effort to stand up," free strongly preferred):
**Telegram Bot API is the clear front-runner.** It's free, its `sendVoice` format requirement is
already what T14 produces with zero pipeline changes, its proactive-messaging gate is a one-time,
low-friction ask (`/start`, not a recurring 24-hour window), and — the single most consequential
finding for this project's next design — the fact that Telegram's Bot API has **no chat-history-read
method at all** means WAHA-style reconciliation-by-scraping isn't something to port and simplify,
it's something to delete outright. Most of T16's existing complexity exists to compensate for a
problem Telegram's API doesn't have.

**Discord** is the technically strongest option (a real server-enforced idempotency nonce, better
than Telegram's), but its mandatory mutual-server onboarding and explicit unsolicited-DM policy
conflict make it a worse fit against the stated "lowest cost/effort" priority specifically — it's
a reasonable second choice if the recipient turns out to already be a heavy Discord user, less
attractive otherwise.

**A different WhatsApp number** looks free and zero-effort on paper, but the evidence assembled here
(a controlled residential-vs-datacenter test, three unrelated cloud providers reproducing the
identical symptom, and this project's own four-library/one-VPS pattern) points at an IP-level block,
not an account-level one — meaning the "zero effort" framing is likely illusory: real effort spent
would probably buy a sixth identical refusal, not a fix.

**Signal** and **SMS/MMS** are both real, working options but lose on the owner's stated priorities
specifically: Signal doesn't clearly beat Telegram on cost or effort and reintroduces a differently-
shaped version of the exact "unofficial client, platform can silently break it" risk this evaluation
exists to escape; SMS/MMS costs real money forever and degrades the core "voice note" product
concept.

**Next step recommended:** design a Telegram-based sender as the primary path, with SMS/MMS
documented as the honest fallback if the recipient turns out to be unreachable/unwilling on
Telegram. This is a recommendation for the owner's approval, not a decision — see the brainstorming
session this note supports for the actual design gate.

## Sourcing caveats

Reddit (reddit.com, old.reddit.com, `site:reddit.com` queries) was unreachable to the research
agents' fetch/search tools in this environment, consistent with the same gap noted in
`docs/research/waha-alternatives.md` — any Reddit-only discussion of these platforms (e.g.
`signal-cli` ban reports, Telegram bot-spam enforcement anecdotes) is not represented here. Several
official policy/support pages (Discord's developer-policy page, Signal's block-behavior support
article, one Twilio MMS size-limit support article) returned HTTP 403 to direct automated fetch;
those claims are sourced instead from GitHub-mirrored canonical text or independent secondary
confirmation, flagged inline in each agent's original report. Twilio pricing figures are
point-in-time and should be re-verified against Twilio's live console before any commitment, since
those pages are dynamic and region/number-type-dependent.
