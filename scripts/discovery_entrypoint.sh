#!/bin/sh
# Entrypoint for docker-compose.yml's `discovery` service.
#
# Two jobs, in this order, and the order is the whole point:
#   1. Load infra/firewall/discovery_egress.nft into THIS container's own
#      network namespace, blocking egress to RFC1918/link-local/cloud-
#      metadata address space. This needs CAP_NET_ADMIN and therefore uid 0.
#   2. Permanently drop to uid/gid 10001 (appuser) with no capabilities at
#      all, and exec the real workload.
#
# `set -e` makes this fail closed: if `nft -f` fails for any reason the
# script exits non-zero, PID 1 dies, and the container never runs the
# discovery workload unfiltered.
#
# Capability tradeoff (deliberate, reviewed):
#   - The service still declares `cap_drop: ["ALL"]`; compose adds back
#     exactly NET_ADMIN (to load the ruleset) plus SETUID/SETGID (to drop
#     privileges afterwards -- a root process with no CAP_SETUID cannot
#     call setresuid at all; verified empirically: `setpriv: setresuid
#     failed: Operation not permitted`).
#   - Alternatives that avoid uid 0 entirely were tried and rejected:
#     file capabilities on /usr/sbin/nft are ignored under
#     `no-new-privileges:true` (that is exactly what NO_NEW_PRIVS does),
#     and a separate NET_ADMIN sidecar sharing the netns re-introduces a
#     start-order race in which the workload can run before the rules land
#     (and loses them silently on any restart) -- i.e. it fails *open*.
#   - What actually runs the untrusted-content workload is unchanged from
#     before this ruleset existed: uid 10001, CapEff 0000000000000000,
#     NoNewPrivs 1, read-only root filesystem. Asserted for real by
#     tests/security/test_discovery_egress_filter.py::
#     test_discovery_pid1_runs_unprivileged_after_the_ruleset_is_applied.
set -eu

nft -f /app/infra/firewall/discovery_egress.nft

exec setpriv \
    --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all \
    -- "$@"
