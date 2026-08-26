"""Real fault-injection proof that `discovery` -- this project's untrusted-
web-content trust boundary -- cannot reach private/RFC1918/link-local/cloud-
metadata address space at the *network* level.

Background (T18 whole-branch review finding C1): before this, `discovery`
could reach anything its Docker network could route to, including the host
via its own bridge gateway (verified: a connect to the gateway's :22 raised
ConnectionRefusedError, i.e. packets were being delivered to the host's own
stack). `discovery/web.py`'s `is_public_address()` blocks that at the
application layer for the one HTTP client that uses it; this ruleset blocks
it for the whole container, including any future code path that bypasses
`DiscoveryWebSession`. The two are defense in depth, not substitutes.

Why the counter assertions matter: "the probe timed out" alone proves
nothing -- an unrouted address times out too, which is exactly the
"test that cannot fail" bug this branch fixed in
test_network_isolation.py, test_firewall_rules.py and
test_discovery_resource_limits.py already. Every negative assertion here is
paired with the nftables rule's own packet counter, read out of the running
kernel before and after the probe, so the test only passes when the
firewall genuinely matched and dropped those specific packets. And every
negative is paired with a real positive control (a live TCP connection to
1.1.1.1:443 and a real DNS resolution) so the suite cannot pass just
because all networking in the container is broken.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess

import pytest

pytestmark = [pytest.mark.security, pytest.mark.docker]

# `-p`: never share a Compose project (and therefore never share the
# `db_data` volume) with a real deployment. Every fixture in this repo tears
# down with `down -v`, which would destroy production data if it ever ran
# against the production project name pinned in docker-compose.yml.
COMPOSE = [
    "docker", "compose", "-p", "personal_voice_msg_test",
    "-f", "docker-compose.yml",
]

GATEWAY_RULE_COMMENT = (
    "RFC1918 172.16/12 (incl. this container's own Docker gateway)"
)
METADATA_RULE_COMMENT = "cloud instance metadata service"


@pytest.fixture(scope="module", autouse=True)
def _discovery_up(tmp_path_factory: pytest.TempPathFactory):
    secret_root = tmp_path_factory.mktemp("t18-egress-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    config_dir = tmp_path_factory.mktemp("t18-egress-conf")
    # Exported into this process's environment, not just passed below:
    # docker-compose.yml declares both with Compose's required-variable
    # syntax and *every* compose subcommand interpolates the whole file,
    # including the bare `docker compose exec` calls the probes use.
    os.environ["SECRET_ROOT"] = str(secret_root)
    os.environ["APP_CONFIG_DIR"] = str(config_dir)
    env = dict(os.environ)
    subprocess.run(COMPOSE + ["build"], check=True, capture_output=True, env=env)
    subprocess.run(
        COMPOSE + ["up", "-d", "discovery"],
        check=True, capture_output=True, env=env,
    )
    yield
    subprocess.run(COMPOSE + ["down", "-v"], capture_output=True, env=env)


def _exec(*cmd: str, user: str = "10001") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        COMPOSE + ["exec", "-T", "--user", user, "discovery", *cmd],
        capture_output=True, text=True, timeout=60,
    )


def _rule_counters() -> dict[str, int]:
    """Packet counters, keyed by rule comment, read live out of the running
    container's own network namespace.

    `--privileged` on `exec` (not on the service) is what makes this
    readable: nftables state is only visible to a process with
    CAP_NET_ADMIN in that netns, and the *workload* deliberately has no
    capabilities at all. This raises the privilege of the read-only probe,
    never of the container being probed.
    """

    result = subprocess.run(
        COMPOSE + [
            "exec", "-T", "--privileged", "--user", "0", "discovery",
            "nft", "-j", "list", "table", "inet", "discovery_egress",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "could not read the discovery_egress nftables table out of the "
        "running container -- the entrypoint should have loaded it before "
        f"starting the workload. stderr:\n{result.stderr}"
    )
    counters: dict[str, int] = {}
    for item in json.loads(result.stdout)["nftables"]:
        rule = item.get("rule")
        if not rule or "comment" not in rule:
            continue
        for expression in rule["expr"]:
            if "counter" in expression:
                counters[rule["comment"]] = expression["counter"]["packets"]
    return counters


def _gateway_address() -> str:
    # /proc/net/route rather than `ip route`: iproute2 is deliberately not
    # installed in the runtime image. Column 2 is the little-endian hex
    # gateway of the 0.0.0.0/0 default route.
    result = _exec("cat", "/proc/net/route")
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            packed = bytes.fromhex(fields[2])[::-1]
            return socket.inet_ntoa(packed)
    raise AssertionError(
        f"no default route found in the container:\n{result.stdout}"
    )


_PROBE = (
    "import socket, sys\n"
    "try:\n"
    "    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=6)\n"
    "    print('CONNECTED')\n"
    "except OSError as exc:\n"
    "    print(type(exc).__name__)\n"
)


def _probe(address: str, port: int) -> str:
    result = _exec("python3", "-c", _PROBE, address, str(port))
    assert result.returncode == 0, (
        f"the probe helper itself failed to run: {result.stderr}"
    )
    return result.stdout.strip()


def test_the_egress_ruleset_is_actually_loaded_in_the_containers_namespace() -> None:
    counters = _rule_counters()
    for comment in (
        METADATA_RULE_COMMENT,
        "IPv4 link-local",
        "RFC1918 10/8",
        GATEWAY_RULE_COMMENT,
        "RFC1918 192.168/16",
    ):
        assert comment in counters, (
            f"expected a counted drop rule commented {comment!r} in the "
            f"loaded ruleset; got {sorted(counters)}"
        )


def test_discovery_cannot_reach_its_own_docker_gateway_and_the_firewall_blocks_it(
) -> None:
    gateway = _gateway_address()
    assert gateway.startswith("172."), (
        f"expected the discovery bridge gateway to be in RFC1918 172.16/12 "
        f"(Docker's default address pool); got {gateway!r} -- if Docker's "
        f"pool has been reconfigured, point this probe at whichever private "
        f"range the gateway now lives in and at that range's drop rule"
    )
    before = _rule_counters()[GATEWAY_RULE_COMMENT]

    # Port 22 specifically: the review's original finding was that this
    # exact probe returned ConnectionRefusedError, proving packets reached
    # the *host's* TCP stack -- i.e. any host-listening port was reachable
    # from the untrusted-content container.
    outcome = _probe(gateway, 22)

    after = _rule_counters()[GATEWAY_RULE_COMMENT]
    assert after > before, (
        f"the {GATEWAY_RULE_COMMENT!r} drop rule's packet counter did not "
        f"move ({before} -> {after}) during the probe. Without this the "
        f"test proves nothing: a probe can 'fail' merely because nothing "
        f"answered. A rising counter is the kernel confirming it matched "
        f"and dropped these packets."
    )
    assert outcome == "TimeoutError", (
        f"expected the connect to be silently dropped (socket.timeout / "
        f"TimeoutError); got {outcome!r}. 'CONNECTED' means the block is "
        f"gone entirely; 'ConnectionRefusedError' means packets are still "
        f"reaching the host's stack, which is the exact finding this "
        f"ruleset exists to close."
    )


def test_discovery_cannot_reach_the_cloud_metadata_address() -> None:
    before = _rule_counters()[METADATA_RULE_COMMENT]
    outcome = _probe("169.254.169.254", 80)
    after = _rule_counters()[METADATA_RULE_COMMENT]
    assert after > before, (
        f"the {METADATA_RULE_COMMENT!r} drop rule's counter did not move "
        f"({before} -> {after}); on a real cloud VPS this address answers, "
        f"so a timeout alone would not prove it is blocked here"
    )
    assert outcome == "TimeoutError", outcome


def test_discovery_can_still_reach_the_public_internet_and_resolve_dns() -> None:
    """Positive control. Without this, every assertion above would also pass
    on a container whose networking was simply broken."""

    before = _rule_counters()

    assert _probe("1.1.1.1", 443) == "CONNECTED", (
        "expected a real TCP connection to 1.1.1.1:443 to succeed through "
        "the egress ruleset -- the ruleset must block private space only, "
        "not the default route to the public internet"
    )

    resolved = _exec(
        "python3", "-c",
        "import socket; print(socket.gethostbyname('one.one.one.one'))",
    )
    assert resolved.returncode == 0 and resolved.stdout.strip(), (
        "expected DNS to still work: Docker's embedded resolver lives at "
        "127.0.0.11 inside the container, which the ruleset explicitly "
        f"accepts. stdout={resolved.stdout!r} stderr={resolved.stderr!r}"
    )

    after = _rule_counters()
    assert after == before, (
        f"public-internet traffic must not match any private-range drop "
        f"rule (the default route's next hop is a 172.16/12 gateway, but "
        f"netfilter matches the layer-3 destination, not the next hop). "
        f"Counters moved: {before} -> {after}"
    )


def test_discovery_pid1_runs_unprivileged_after_the_ruleset_is_applied() -> None:
    """The entrypoint starts as uid 0 to load the ruleset. This asserts it
    really does drop back afterwards, so the CAP_NET_ADMIN/SETUID/SETGID
    grant does not leak into the workload that handles untrusted content."""

    status = _exec("cat", "/proc/1/status", user="0")
    assert status.returncode == 0, status.stderr
    fields = {
        line.split(":", 1)[0]: line.split(":", 1)[1].split()
        for line in status.stdout.splitlines()
        if ":" in line
    }
    assert fields["Uid"] == ["10001"] * 4, (
        f"expected PID 1 to have dropped to uid 10001 (real, effective, "
        f"saved, filesystem); got {fields['Uid']}"
    )
    assert fields["Gid"] == ["10001"] * 4, fields["Gid"]
    assert fields["CapEff"] == ["0000000000000000"], (
        f"expected the workload to hold no effective capabilities at all "
        f"after the entrypoint dropped privileges; got {fields['CapEff']}"
    )
    assert fields["NoNewPrivs"] == ["1"], fields["NoNewPrivs"]
