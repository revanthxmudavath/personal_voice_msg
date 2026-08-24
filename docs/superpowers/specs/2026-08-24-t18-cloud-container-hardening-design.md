# T18 — Cloud and container hardening — design

Status: approved (topology confirmed by owner 2026-08-24 in
`docs/task-logs/pre-t18-reverification.md` / `IMPLEMENTATION_PLAN.md`'s T18
section; the infra-as-code-vs-live-VPS scope decision below confirmed by the
owner at the start of this task), ready for `writing-plans`.

## Context

`IMPLEMENTATION_PLAN.md`'s T18 section already reconciled the plan's original
WAHA-era wording against the current architecture (2026-08-24, before this
task started): **one app container** (generation, judging, voice/Pocket TTS,
sender/delivery, scheduler — in-process, matching T15's own precedent) plus
**one discovery container/network** (the actual trust boundary: untrusted web
content, no access to secrets/voice/sender). That reconciliation is not
redone here — this spec starts from it.

Two things that reconciliation left open, resolved here:

1. **No cloud VPS exists for this project.** T18's own red tests (external
   port scan, WireGuard-only public firewall) only mean something against a
   real internet-facing host, and provisioning one requires payment the
   agent cannot make. Confirmed with the owner via `AskUserQuestion` at the
   start of this task: **build infra-as-code, verify everything Docker
   Desktop can prove locally for real, defer the literal external-scan/
   live-firewall verification to a documented owner-run runbook step** —
   the same split this project already used for T15's WAHA pairing and
   T17b's live STOP/send tests (agent builds and locally verifies the real
   mechanism; the owner runs the parts that need real external
   infrastructure or their own phone).
2. **The weekly-discovery production pipeline (search → card → generate →
   judge → queue) has no caller anywhere in the codebase.** This is a
   pre-existing gap, not something T18 created. `docs/superpowers/specs/
   2026-08-20-t17b-daily-send-entrypoint-design.md`'s own "Explicitly out of
   scope" section already found and named this gap and explicitly deferred
   it ("likely folded into T20's... prerequisite, or split out"), not to
   T18. T18 does not build it either — see "Explicitly out of scope" below.

## Explicitly out of scope

- **Weekly discovery pipeline wiring** (as above — a pre-existing, already-
  named gap that predates T18 and was explicitly assigned elsewhere).
  T18's discovery container needs a real bounded process to harden and
  fault-inject against; it gets one (see "Discovery worker verification
  entrypoint" below), but that process is explicitly a **verification
  harness reusing T07's existing tested code**, not new production
  candidate-generation wiring. It does not call generation, judging, or
  `queue_refill.refill_queue()`.
- **Actual VPS provisioning, DNS, TLS termination for anything public.**
  Nothing in this system is a public HTTP service, so there is nothing to
  terminate TLS for. WireGuard is a UDP tunnel, not HTTP.
- **Splitting `pyproject.toml` into per-service dependency groups.** The
  discovery/app isolation this task builds is enforced by secret-mount
  scoping and network policy (see "Container image" below), which is what
  the plan's own implementation bullet names ("expose each only to its
  required service") — not by shipping two different Python environments
  for what is architecturally still one package (`IMPLEMENTATION_PLAN.md`
  §8's target layout has always been one `src/personal_voice_msg/`).
- **Structured logging, alerting, backup/restore.** T19's scope.

## Component design

### 1. Secret file ownership and mode validation (`config.py`)

`secret_file()` (T02) already resolves each secret path and confirms it is
inside `secret_root` and a real file. It does not yet check *who owns it* or
*what its mode is*. New: for non-development profiles, `secret_file()` calls
a new `_validate_secret_file_permissions(path)` that uses `path.stat()` to
require:

- `st_uid == os.geteuid()` — the file is owned by whatever identity is
  actually running the service ("the service administrator" — this project
  has no separate admin-vs-service-account concept, so identity match with
  the running process is the concrete, testable meaning of that
  requirement).
- Mode has no group or other bits set at all (`st_mode & 0o077 == 0`) —
  i.e. `0600` or stricter. `0640`/`0644`/world-readable all fail closed.

Raises `ConfigurationError` (existing exception, fail-closed, matches every
other check in this function) on either violation. Development profile is
exempt (matches the existing exemption for the repo-external secret-root
rule already in `secret_root()`) since local dev secrets are throwaway test
fixtures, not deployed credentials.

Windows has no POSIX mode bits, so this can only be tested for real inside a
Linux environment — Docker Desktop is running (confirmed, `AGENTS.md`), so
the test spins up a real minimal Linux container, `chmod`s/`chown`s files
inside it via `docker exec`, and calls the real `personal_voice_msg.config`
functions inside that same container (via `docker exec python -c ...`) —
same "real fault injection via containers" pattern this project already
uses for WAHA/Telegram fault injection, just applied to file permissions
instead of network calls.

### 2. Container image

**One shared image**, not two — the app and discovery services are the same
Python package with the same dependency set (`pyproject.toml` is not being
split, see "Explicitly out of scope"). One `Dockerfile`:

- Pinned base digest: `python:3.12-slim@sha256:<resolved digest>` (resolved
  and recorded at implementation time, re-verified before the image is
  built).
- Multi-stage: a `builder` stage runs `uv sync --locked --no-dev` into a
  venv; a `runtime` stage copies only the synced venv and `src/` — no
  build toolchain, no `uv` binary, no dev dependencies in the final image.
- Creates a non-root user (`appuser`, fixed UID) and runs as that user.
- No shell utilities beyond what the base image already has are installed;
  nothing extra is added "for debugging."
- FFmpeg: `audio_pipeline.py` requires it. Installed in the runtime stage
  from Debian's pinned package repo (the base image is Debian-slim), version
  pinned via `apt-get install ffmpeg=<pinned-version>` resolved from the base
  image's actual available package at build time and recorded in the
  Dockerfile comment — Pocket TTS's own model weights are fetched and cached
  through the existing `pocket-tts` dependency path already qualified in
  T13/T14, unchanged here.

The **same image** is used for both compose services; what differs between
them is entirely compose-level: which secrets are bind-mounted, which
network they're attached to, resource limits, and the container `command`.
This is the concrete mechanism behind the plan's "expose each only to its
required service" line — the discovery container's compose service mounts
zero secret files (it needs none: no bot token, no sender auth key, no
voice embedding), the app container's mounts all four.

### 3. `docker-compose.yml` (repo root — none exists today; T16b deleted the
last one along with WAHA)

Two services, `app` and `discovery`:

```yaml
services:
  app:
    build: .
    image: personal-voice-msg:app
    user: "appuser"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: ["ALL"]
    security_opt: ["no-new-privileges:true"]
    networks: [app_net]
    volumes:
      - db_data:/data
      - type: bind
        source: ${SECRET_ROOT}
        target: /secrets
        read_only: true
    deploy:
      resources:
        limits:
          cpus: "4"
          memory: 8g
        # pids_limit is a plain Compose v2 key, not under `deploy`
    pids_limit: 512
    restart: unless-stopped

  discovery:
    build: .
    image: personal-voice-msg:app  # same image, different command/network
    # No `searxng` service exists yet -- like the weekly-discovery-pipeline
    # gap (see "Explicitly out of scope"), "what SearXNG instance does
    # production discovery search against" is a separate, pre-existing open
    # question this task does not solve. This container's job here is to
    # exist as a correctly networked, correctly bounded, correctly isolated
    # container for the hardening tests below -- not to run real discovery.
    # `scripts/run_discovery_worker.py` (component 4) is invoked manually
    # against a real SearXNG target once one is decided (infra/RUNBOOK.md),
    # not as this container's default command.
    command: ["sleep", "300"]
    user: "appuser"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: ["ALL"]
    security_opt: ["no-new-privileges:true"]
    networks: [discovery_net]
    # No secret bind mount at all -- discovery needs no credentials.
    deploy:
      resources:
        limits:
          cpus: "1"
          memory: 1g
    pids_limit: 64
    restart: "no"  # bounded worker: runs, exits, is not respawned in a loop

networks:
  app_net:
    enable_ipv6: false
  discovery_net:
    enable_ipv6: false   # T18's NAT64 handling: disable IPv6 (and therefore
                          # any NAT64 path) on the discovery network entirely,
                          # rather than trusting a deployment-specific Pref64
                          # we don't have yet (no VPS provisioned -- see
                          # "Explicitly out of scope"). discovery/web.py's
                          # `is_public_address()` already rejects the two
                          # well-known NAT64 prefixes at the DNS-resolution
                          # boundary as defense in depth if IPv6 is ever
                          # re-enabled on a real deployment's network; if a
                          # real VPS's actual Pref64 becomes known later, add
                          # it to `NAT64_PREFIXES` and re-test rather than
                          # relying on `enable_ipv6: false` alone.

volumes:
  db_data:
```

Neither service publishes any host port (satisfies "external port scan
finds no application ports" for both containers directly, not just via
firewall — the containers never ask Docker to publish anything). Neither
mounts `/var/run/docker.sock` (satisfies "containers cannot access the
Docker socket" — this is enforced by *absence*, verified by a real test
that `docker exec`s into each container and confirms no socket file and no
`docker` binary is reachable).

`app`'s egress: allowlisted to the pinned Gemini API host and
`api.telegram.org` only, enforced by an iptables/nftables egress rule
applied to `app_net` at compose-up time via a small wrapper script (Docker
Compose alone cannot express DNS-name-based egress allowlisting; the rule
matches destination IPs resolved from the two pinned hostnames at rule-apply
time, re-resolved on each `compose up`). `discovery`'s egress: allowlisted
to the SearXNG host only, on `discovery_net` — nothing in `app_net` is
reachable from `discovery_net` because they are separate Docker networks
with no shared network and no explicit link between them.

### 4. Discovery worker verification entrypoint (`scripts/run_discovery_worker.py`)

A **verification harness**, not the production weekly pipeline (see
"Explicitly out of scope"). Its only job: give the discovery container a
real, bounded, resource-limited process to run and fault-inject against,
using 100% existing tested code (`discovery.baseline.DeterministicDiscovery`,
`discovery.web.DiscoveryWebSession`, both already real and already tested by
T06/T07). It runs the existing bounded `search_web`/`analyze_result` calls
against `DISCOVERY_QUERIES` once, with a wall-clock budget, and exits —
mirroring T17b's own "short-lived script, not a daemon" entrypoint shape.
It intentionally stops at `DiscoveryRecord` — it does not build an
`InspirationCard`, does not call generation or judging, and does not touch
the database, since wiring that full pipeline is the pre-existing,
already-elsewhere-assigned gap this task does not solve.

### 5. Cron timer for the daily-send entrypoint (`app` container)

T17b built `run_daily_entrypoint`/`scripts/run_daily_entrypoint.py` and
explicitly left "the container's cron/systemd configuration" to T18. The
`app` image installs a minimal cron daemon and a crontab entry invoking
`scripts/run_daily_entrypoint.py --config /secrets/app.toml --database
/data/app.db` every minute (matching T17b's documented "every 1-2 minutes"
target). Cron runs as `appuser`, not root, inside the container (this is
why the base image needs a cron package that supports non-root operation —
`cron`/`supercronic`; `supercronic` is preferred, it's a single static Go
binary designed exactly for this container use case and needs no root
daemon setup). Verified with a real test: start the `app` container with a
fake local Telegram-shaped HTTP server as `api_base`, wait past a full
minute boundary, confirm the entrypoint actually fired (fake server
received a request) without any test-side invocation.

### 6. Reboot/restart state test

Real fault injection: `docker compose restart app`, then confirm `/data`
(the `db_data` named volume — SQLite) survived, while anything written to
`/tmp` (the `tmpfs` mount) did not. This directly matches the plan's
"reboot preserves only intended state (SQLite data, not transient secrets
or session artifacts)".

### 7. Git-history and built-image secret scan (`scripts/repository_policy.py`)

Two new checks added to the existing `CHECKS` registry (same pattern as
`check_mocks`/`check_lockfile`/`check_secrets`/`check_workflows`):

- `check_git_history(root)`: walks every blob reachable from any ref
  (`git rev-list --all --objects`), not just the working tree — closes the
  gap that `check_secrets` only ever scanned files currently on disk, so a
  secret added and later deleted in a subsequent commit would never be
  caught. Reuses the exact same detection functions (`GITHUB_TOKEN`,
  `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `PRIVATE_KEY`,
  `SENSITIVE_ARTIFACT_NAMES`/`SUFFIXES`) against each blob's historical path
  and content via `git cat-file -p <sha>`.
- `check_image_secrets(image)`: `docker create` a container from the given
  image tag (never started), `docker export | tar` it to a temp directory,
  and run the same detectors over every extracted file. `docker rm`s the
  container afterward regardless of outcome.

Both are real, no-mock tests: `check_git_history` runs against a real
throwaway git repo fixture (a temp dir, `git init`, a commit containing a
planted fake token, a following commit that deletes it — proving history
scanning catches what working-tree scanning would miss) and
`check_image_secrets` runs against a real minimal Docker image built in the
test with a planted fake secret file baked into a layer.

### 8. Firewall / WireGuard / SSH hardening (`infra/`)

Infra-as-code, not applied to a live host this session:

- `infra/firewall/rules.nft` — an nftables ruleset: default-deny inbound,
  allow only UDP/51820 (WireGuard) plus established/related, allow all
  outbound (egress is controlled per-container by Docker/iptables rules
  from component 3, not by the host firewall).
- `infra/wireguard/wg0.conf.template` — a WireGuard server config template
  with placeholder keys (never real keys committed) and instructions for
  generating real ones on the actual VPS.
- `infra/ssh/sshd_config.d/10-hardening.conf` — `PasswordAuthentication no`,
  `PermitRootLogin no`, `PubkeyAuthentication yes`.
- `infra/RUNBOOK.md` — the exact steps the owner runs on a real VPS: apply
  the SSH hardening, install WireGuard and generate real keys, apply the
  nftables ruleset, verify from an external host that only the WireGuard
  port answers (`nmap -Pn <public-ip>`), then `docker compose up -d`.

Real local verification (not a live VPS, but real, not simulated): a
throwaway Linux container started with `--cap-add=NET_ADMIN` loads
`rules.nft` for real via `nft -f`, and a real test from outside that
container's network namespace confirms only the WireGuard port accepts a
connection and every other probed port is refused/dropped.

### 9. Container digest pinning and Gemini requalification

`docker-compose.yml`'s `image:` fields are resolved to `@sha256:` digests
before merge (not `latest`, matching T15's own established precedent for
WAHA). Gemini's provider/model ID/API contract/client version are already
pinned since T10 (`gemini-3.6-flash`, hand-rolled `aiohttp` client;
`AGENTS.md` §Confirmed stack) — T18 adds no new pin, it adds a one-line
assertion test that the pinned model string in `generation/` still matches
the value `AGENTS.md` documents, so an accidental future edit to that
constant fails CI rather than silently drifting, and a code comment at the
pin site pointing back to this requirement (any real change still requires
the full T10/T11 requalification runs, unchanged).

## Testing plan (no-mock, matching every prior task)

- **security**: secret-permission Linux-container tests, discovery/app
  network-egress-isolation tests, Docker-socket-inaccessibility test,
  git-history/image secret-scan tests, firewall port-reachability test.
- **integration**: compose-up smoke test (both services start, healthy,
  publish no ports), cron-fires-the-entrypoint test, reboot/restart state
  test.
- **security** (resource limits): real `docker run --memory`/`--pids-limit`
  against the discovery worker script forcing it to hit the limit and be
  OOM-killed/blocked, proving termination rather than degradation or a
  silent hang.

## Live verification — folded in per `AGENTS.md`'s "Immediate next step"

Two items carried over from T17/T17b, closed as an explicit final task
within this branch, run by the owner (not this sandboxed session — same
reason T17b's own live verification had to be owner-run: this sandbox
cannot reach `api.telegram.org` over genuine TLS):

1. `tests/integration/test_consent_integration.py::test_a_real_exact_stop_from_the_enrolled_chat_disables_sending_durably`
   — a real `STOP` from the enrolled chat.
2. One real `scripts/run_daily_entrypoint.py` invocation during a genuine,
   unmodified 07:00-07:05 Pacific window (now delivered via the cron timer
   built in component 5, so this closes to "confirm cron actually fired
   during a real window and the owner received the note," not a manual
   script run).

This task produces the exact commands/env-vars for both and records the
owner's real results in `docs/task-logs/T18.md`, same policy as every prior
live-verification item in this project.

## Independent review

T18 is on `AGENTS.md`'s fixed mandatory-review list. Full whole-branch
review before merge, same discipline as T15/T16/T16b/T17/T17b.

## What does NOT change

- `discovery/`, `generation/`, `judging/`, `sender.py`, `consent.py`,
  `delivery.py`, `daily_send_entrypoint.py`, `config.py`'s existing checks,
  `database.py` — T18 adds container/network/firewall/secret-permission
  infrastructure around this code; it does not modify application logic
  except the one additive `config.py` permission check in component 1.
- The existing AST source-boundary test
  (`tests/security/test_voice_enrollment_boundaries.py`) — T18's network/
  secret-mount isolation is deployment-level enforcement of the same
  boundary that test already enforces at the source level; both stay.
