# Deploying to a real VPS

Run once, on a fresh VPS, by the owner (payment/provisioning and live
external verification are outside what an agent session can do -- see
`docs/superpowers/specs/2026-08-24-t18-cloud-container-hardening-design.md`).

1. `apt-get install wireguard nftables`
2. Copy `infra/ssh/sshd_config.d/10-hardening.conf` to
   `/etc/ssh/sshd_config.d/` on the VPS, then `systemctl restart sshd`
   -- **from a session already using key-based auth**, to avoid locking
   yourself out.
3. `wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey`
   on the VPS; on your own admin device, generate a client keypair the
   same way. Fill both into a real copy of
   `infra/wireguard/wg0.conf.template` at `/etc/wireguard/wg0.conf`.
   `systemctl enable --now wg-quick@wg0`.
4. `nft -f infra/firewall/rules.nft` on the VPS, then
   `nft list ruleset > /etc/nftables.conf` and enable the `nftables`
   systemd unit so it survives reboot.
5. From a **different** machine (not the VPS, not over the VPN), run
   `nmap -Pn <public-ip>` and confirm only `51820/udp` (or nothing, since
   nmap's default scan is TCP) is reported open. This is the literal
   "external port scan finds no application ports" verification the plan
   requires -- it can only be run against a real public IP, which is why
   it's here and not in this repo's test suite. (This repo's own test,
   `tests/security/test_firewall_rules.py`, proves the same ruleset file
   is selective -- only WireGuard's port is reachable, every other probed
   port is silently dropped -- against a real throwaway Linux container
   standing in for a host, since no VPS exists yet to scan for real.)
6. `docker compose up -d` on the VPS, using a real `SECRET_ROOT` outside
   the checked-out repository per `config.py`'s existing enforcement. The
   `discovery` service's default command is a bounded placeholder
   (`sleep 300`) -- no real SearXNG deployment exists yet (a pre-existing
   gap, not something this task solves; see `docker-compose.yml`'s comment
   on the `discovery` service). Once a real SearXNG target is decided, run
   the actual verification harness manually: `docker compose exec
   discovery python scripts/run_discovery_worker.py --searxng-base-url
   <real-url> --budget-seconds 60`.
