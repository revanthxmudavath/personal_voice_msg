# Deploying to a real VPS

Run once, on a fresh VPS, by the owner (payment/provisioning and live
external verification are outside what an agent session can do -- see
`docs/superpowers/specs/2026-08-24-t18-cloud-container-hardening-design.md`).

> **Never run this repository's test suite on the production host.**
> Several tests bring the Compose stack up and tear it down with
> `docker compose down -v`, which destroys named volumes -- including
> `db_data`, the real SQLite state. They are pinned to a *different*
> Compose project name (`personal_voice_msg_test`) precisely so they cannot
> touch the production project (`personal_voice_msg`), but do not rely on
> that alone: the VPS is a deployment target, not a development box. Run
> `pytest` on a development machine or in CI only.

1. `apt-get install wireguard nftables`
2. Copy `infra/ssh/sshd_config.d/10-hardening.conf` to
   `/etc/ssh/sshd_config.d/` on the VPS, then `systemctl restart sshd`
   -- **from a session already using key-based auth**, to avoid locking
   yourself out.
3. `wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey`
   on the VPS; on your own admin device, generate a client keypair the
   same way. Fill both into a real copy of
   `infra/wireguard/wg0.conf.template` at `/etc/wireguard/wg0.conf`.
   `systemctl enable --now wg-quick@wg0`. The interface must be named
   `wg0` -- the firewall ruleset in the next step accepts administrative
   traffic by interface name (`iifname "wg0"`).
4. `nft -f infra/firewall/rules.nft` on the VPS, then
   **before you close the SSH session you just ran that from**:
   1. Bring the WireGuard tunnel up on your admin device.
   2. Open a **second, independent SSH session over the VPN** (to the
      VPS's WireGuard address, not its public IP) and confirm it
      connects and gives you a shell.
   3. Only once that second session works, close the first one.

   This is not ceremony. The ruleset's input chain is `policy drop`; if
   the `iifname "wg0" accept` rule is ever removed, renamed, or shadowed,
   the tunnel still comes up and UDP/51820 still answers, but *everything
   inside the tunnel is silently dropped* -- including your SSH. The
   original T18 ruleset had exactly that bug (whole-branch review finding
   C3) and would have locked the owner out of a fresh VPS on first apply.
   Recovering needs the provider's out-of-band console.

   Then persist it: `nft list ruleset > /etc/nftables.conf` and enable the
   `nftables` systemd unit so it survives reboot. Reboot once and repeat
   the second-session check.
5. From a **different** machine (not the VPS, not over the VPN), run
   `nmap -Pn <public-ip>` and confirm only `51820/udp` (or nothing, since
   nmap's default scan is TCP) is reported open. This is the literal
   "external port scan finds no application ports" verification the plan
   requires -- it can only be run against a real public IP, which is why
   it's here and not in this repo's test suite. (This repo's own test,
   `tests/security/test_firewall_rules.py`, proves the same ruleset file
   is selective -- only WireGuard's port is reachable, every other probed
   port is silently dropped, and traffic arriving on an interface named
   `wg0` is accepted -- against a real throwaway Linux container standing
   in for a host, since no VPS exists yet to scan for real.)

## 6. Place the secrets and the configuration file

The application needs **two separate directories**, and they must be
separate -- this is enforced by `config.py`, not a preference:

| Env var | Mounted at | Holds |
|---|---|---|
| `SECRET_ROOT` | `/secrets` (read-only) | the four secret files |
| `APP_CONFIG_DIR` | `/conf` (read-only) | `app.toml` |

`config.py`'s `secret_root()` rejects, for any non-`development` profile, a
secret root at or below the config file's own resolved "project root" --
and that root falls back to the config file's *own directory* when nothing
above it holds a `pyproject.toml` or `.git`, which is the case inside the
container. So `app.toml` inside `/secrets` makes **every** run fail with
`ConfigurationError: deployed secret root must be outside the project
directory`. (That was the shipped state until T18's whole-branch review
finding C2; `tests/security/test_container_config_loads.py` now runs the
crontab's literal `--config` argument inside the real image so it cannot
regress.)

Pick two directories outside the checked-out repository, e.g.:

```bash
export SECRET_ROOT=/srv/personal-voice-msg/secrets
export APP_CONFIG_DIR=/srv/personal-voice-msg/conf
mkdir -p "$SECRET_ROOT" "$APP_CONFIG_DIR"
```

Put the four secret files in `$SECRET_ROOT`:

- `telegram_chat_id.json` -- `{"profile": "production", "telegram_chat_id": <id>}`
  (the `profile` value must equal `app.toml`'s `profile`)
- `telegram-token.txt` -- the bot token, nothing else
- `voice.embedding` -- the enrolled voice embedding
- `sender-auth-key.txt` -- the sender auth key

Then **fix their ownership and mode**, which the container's own config
loader enforces and fails closed on (T18 Task 1). The container runs as
uid/gid 10001, and the check requires each secret file to be owned by the
running identity with no group or other permission bits at all:

```bash
chown -R 10001:10001 "$SECRET_ROOT"
chmod 700 "$SECRET_ROOT"
chmod 600 "$SECRET_ROOT"/*
```

`0640`, `0644`, or a file owned by `root` all raise
`ConfigurationError: <setting> is not owned by the running service identity`
/ `... has an insecure permission mode` on every tick. That is deliberate;
do not work around it by loosening the check.

Write `$APP_CONFIG_DIR/app.toml` with exactly these six keys (no more, no
fewer -- `read_toml` rejects unknown and missing keys):

```toml
profile = "production"
secret_root = "/secrets"
telegram_chat_id_file = "telegram_chat_id.json"
telegram_bot_token_file = "telegram-token.txt"
voice_embedding_file = "voice.embedding"
sender_auth_key_file = "sender-auth-key.txt"
```

Note `secret_root = "/secrets"` -- the path **inside** the container, not
the host path. The file paths are relative to it.

```bash
chown 10001:10001 "$APP_CONFIG_DIR/app.toml"
chmod 600 "$APP_CONFIG_DIR/app.toml"
```

Both variables are declared in `docker-compose.yml` with Compose's
required-variable syntax (`${SECRET_ROOT:?...}`), so leaving either unset
now aborts `docker compose` with a named error instead of silently
bind-mounting the current working directory -- i.e. this whole repository
checkout -- at `/secrets`.

## 7. Build the image and scan it

```bash
docker compose build
uv run python scripts/repository_policy.py image --image personal-voice-msg:t18
```

The second command is the built-image secret scan (`check_image_secrets`):
it exports the real image's filesystem and runs the same credential and
sensitive-filename detectors the repository scan uses. It cannot run under
`repository_policy.py all` because it needs an explicit `--image`, so it
has to be invoked here (CI's `t18-container-security` job runs it too). It
prints nothing and exits 0 when clean.

## 8. Start the stack

```bash
docker compose up -d
```

`app` runs supercronic as PID 1, firing
`scripts/run_daily_entrypoint.py --config /conf/app.toml --database
/data/app.db` every minute; the entrypoint returns immediately outside the
07:00-07:05 Pacific `DAILY_SEND` window. Confirm the first tick is healthy:

```bash
docker compose logs app | tail -20   # expect "job succeeded" and "not due, skipped"
```

A `ConfigurationError` here means step 6 is wrong -- fix it before walking
away, because nothing alerts on a failing tick yet (T19's scope).

## 9. Discovery

The `discovery` service's default command is a bounded placeholder
(`sleep 300`) -- no real SearXNG deployment exists yet (a pre-existing
gap, not something this task solves; see `docker-compose.yml`'s comment
on the `discovery` service). Once a real SearXNG target is decided, run
the actual verification harness manually:

```bash
docker compose exec --user 10001 discovery \
  python scripts/run_discovery_worker.py \
  --searxng-base-url <real-url> --budget-seconds 60
```

`--user 10001` because `discovery`'s compose entry runs its entrypoint as
uid 0 (only to load `infra/firewall/discovery_egress.nft` into its own
network namespace, after which it permanently drops to 10001), so an
unqualified `exec` would land you as root.

That ruleset blocks discovery -- the untrusted-web-content trust boundary
-- from reaching RFC1918, link-local and cloud instance-metadata address
space at the network level. **If the SearXNG instance you point it at is on
a private address**, discovery cannot reach it: add an explicit `accept`
for that exact host above the drop rules in
`infra/firewall/discovery_egress.nft` and re-run
`tests/security/test_discovery_egress_filter.py`. Do not delete the drop
rules.
