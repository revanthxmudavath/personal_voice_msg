"""Real proof that `infra/firewall/rules.nft` -- the nftables ruleset applied
on the real VPS per `infra/RUNBOOK.md` -- actually enforces "only WireGuard
is reachable" when loaded for real, not just that the file parses.

No live VPS exists for this project yet (confirmed by the owner at the start
of T18; see `docs/superpowers/specs/2026-08-24-t18-cloud-container-hardening-
design.md`). This test substitutes a real throwaway Linux container's own
network namespace for "the host": `nft -f` loads the real ruleset file into
that namespace exactly as the runbook's step 4 does on the VPS, and a real
connection attempt is made from outside the container (this test process,
via Docker's published ports) -- a real kernel-level firewall decision, not
a simulation.

Deviation from the task brief's illustrative snippet: that snippet only
ever probed the port the ruleset should *block* (9999/tcp) and asserted
`returncode != 0`. That is not sufficient proof of a *selective* ruleset --
a completely broken ruleset (e.g. a typo'd port number, or `policy drop`
with no accept rules at all) would block 9999 too and make that assertion
pass even though the WireGuard port would then also be unreachable, which
is exactly the failure mode this ruleset exists to prevent. Verified this
gap by hand against the ruleset committed here: temporarily changing
`udp dport 51820 accept` to `udp dport 51821 accept` still makes the
brief's original assertion pass, silently proving nothing about 51820.
Fixed by adding a positive control: a real UDP echo round-trip against
51820 (WireGuard's port) through the same loaded ruleset, so the test only
passes when the ruleset is genuinely selective, not merely restrictive.
The TCP negative-control assertion was also tightened from "curl exited
nonzero" to "curl exited with code 28 (timeout)" specifically, to
distinguish a real silent packet drop (`policy drop`, what the ruleset
declares) from an application-level refusal that would also exit nonzero
for an unrelated reason.
"""

from __future__ import annotations

import socket
import subprocess
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "infra" / "firewall" / "rules.nft"

# Tried first, per the task brief. If it isn't pullable in this environment
# (a registry/network issue, not a ruleset problem), fall back to a tiny
# throwaway image built locally from debian:bookworm-slim. Neither image is
# a project dependency -- both are test-only scaffolding for proving the
# committed ruleset file works, not something the app ships or requires.
NIXERY_IMAGE = "nixery.dev/nftables/iproute2/python3"
FALLBACK_IMAGE = "t18-nft-verify-fallback:local"

UDP_ECHO_SCRIPT = textwrap.dedent(
    """\
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 51820))
    while True:
        data, addr = s.recvfrom(1024)
        s.sendto(b"pong", addr)
    """
)


def _resolve_nft_image() -> str:
    pulled = subprocess.run(
        ["docker", "pull", NIXERY_IMAGE],
        capture_output=True, text=True, timeout=120,
    )
    if pulled.returncode == 0:
        # The pull succeeding is not enough: confirmed empirically that
        # nixery.dev images set no PATH and provide no conventional
        # /bin/sh -- every binary lives under a content-addressed
        # /nix/store/<hash>-<pkg>/bin path instead, so `sh -c "..."` (what
        # this test needs to run the http server, the udp echo listener,
        # and nft in sequence) fails with "exec: sh: executable file not
        # found in $PATH" before the container ever starts. A smoke run
        # proves whether this particular pulled image can actually run our
        # shell script, not just whether the registry served it.
        smoke = subprocess.run(
            ["docker", "run", "--rm", NIXERY_IMAGE, "sh", "-c", "true"],
            capture_output=True, text=True, timeout=30,
        )
        if smoke.returncode == 0:
            return NIXERY_IMAGE

    dockerfile = textwrap.dedent(
        """\
        FROM debian:bookworm-slim
        RUN apt-get update \\
            && apt-get install -y --no-install-recommends nftables python3 iproute2 \\
            && rm -rf /var/lib/apt/lists/*
        """
    )
    with tempfile.TemporaryDirectory() as build_dir:
        (Path(build_dir) / "Dockerfile").write_text(dockerfile)
        subprocess.run(
            ["docker", "build", "-t", FALLBACK_IMAGE, build_dir],
            check=True, capture_output=True, text=True, timeout=300,
        )
    return FALLBACK_IMAGE


def _wait_until_running(container: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip() == "true":
            return
        time.sleep(0.3)
    raise RuntimeError(
        f"container {container} never reached a running state -- see "
        f"`docker logs {container}` for why (e.g. nft -f failing would "
        f"exit the container's PID 1 before the sleep 30 keeps it alive)"
    )


def test_only_wireguard_udp_port_is_reachable_under_the_real_ruleset(
    tmp_path: Path,
) -> None:
    image = _resolve_nft_image()
    echo_script = tmp_path / "udp_echo.py"
    echo_script.write_text(UDP_ECHO_SCRIPT)

    container = f"t18-nft-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", container,
                "--cap-add", "NET_ADMIN",
                "-v", f"{RULES_PATH}:/rules.nft:ro",
                "-v", f"{echo_script}:/udp_echo.py:ro",
                "-p", "51820:51820/udp",
                "-p", "9999:9999/tcp",
                image,
                "sh", "-c",
                "python3 -m http.server 9999 >/dev/null 2>&1 & "
                "python3 /udp_echo.py >/dev/null 2>&1 & "
                "nft -f /rules.nft && sleep 30",
            ],
            check=True, capture_output=True, text=True, timeout=60,
        )
        _wait_until_running(container)
        # Let http.server/udp_echo.py bind and nft -f finish loading before
        # probing from outside.
        time.sleep(3)

        # Negative control: an arbitrary application port (9999, standing
        # in for "anything that isn't WireGuard") must be genuinely
        # unreachable under the ruleset's default-drop input policy. curl
        # exit code 28 specifically means "operation timeout" -- the SYN
        # was never answered at all (silently dropped) -- as opposed to
        # exit 7 "connection refused" (which would mean the packet reached
        # the container and got an active RST, not what `policy drop`
        # produces). Asserting the exact code, not just "nonzero", rules
        # out a coincidental/unrelated curl failure passing for the wrong
        # reason.
        blocked = subprocess.run(
            ["curl", "-sf", "--max-time", "2", "http://127.0.0.1:9999/"],
            capture_output=True, timeout=10,
        )
        assert blocked.returncode == 28, (
            "expected curl exit 28 (timeout -- the packet was silently "
            "dropped by the ruleset's default-drop input policy); got "
            f"returncode={blocked.returncode} instead, which would not "
            "prove the firewall (rather than something else) caused the "
            f"failure. stdout={blocked.stdout!r} stderr={blocked.stderr!r}"
        )

        # Positive control (the fix over the brief's illustrative
        # snippet -- see module docstring): a real UDP datagram sent to
        # 51820 must get a real echo back, proving the WireGuard port is
        # genuinely reachable *through this same loaded ruleset*, not that
        # every port happens to be dropped (which would make the assertion
        # above pass even for a broken ruleset).
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        try:
            sock.sendto(b"ping", ("127.0.0.1", 51820))
            reply, _ = sock.recvfrom(1024)
        finally:
            sock.close()
        assert reply == b"pong", (
            "expected the UDP echo listener on 51820 (WireGuard's port) "
            "to be reachable through the loaded ruleset and echo back "
            f"b'pong'; got {reply!r} instead"
        )
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
