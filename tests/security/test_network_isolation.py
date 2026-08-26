"""Container-to-container isolation between `app` and `discovery`.

Egress *out of* `discovery` towards private/RFC1918/link-local/cloud-
metadata address space is covered separately and for real by
tests/security/test_discovery_egress_filter.py. A weak
`test_discovery_cannot_reach_a_private_network_address` used to live here
and asserted only `returncode != 0` against 192.168.1.1 -- an address
nothing answers at on a typical developer host, so it timed out and
"passed" whether or not anything was blocking it. It was removed rather
than patched: proving that block needs the nftables counter readout the
dedicated module does, not another exec probe against this stack.
"""

from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = [pytest.mark.security, pytest.mark.docker]

# `-p personal_voice_msg_test`: docker-compose.yml pins
# `name: personal_voice_msg` for the real deployment, and the teardown
# below runs `down -v`. Sharing that project name would mean running this
# repo's test suite on the production host destroys the real `db_data`
# volume. The distinct project name makes that impossible; infra/RUNBOOK.md
# additionally tells the owner never to run pytest there at all.
COMPOSE = [
    "docker", "compose", "-p", "personal_voice_msg_test",
    "-f", "docker-compose.yml",
]


@pytest.fixture(scope="module", autouse=True)
def _compose_stack(tmp_path_factory: pytest.TempPathFactory):
    secret_root = tmp_path_factory.mktemp("t18-net-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    config_dir = tmp_path_factory.mktemp("t18-net-conf")
    env = _full_env({
        "SECRET_ROOT": str(secret_root),
        "APP_CONFIG_DIR": str(config_dir),
    })
    # Exported into this process's environment too: docker-compose.yml
    # declares both with Compose's required-variable syntax and *every*
    # compose subcommand interpolates the whole file, including the bare
    # `docker compose exec` calls below.
    os.environ.update(
        {"SECRET_ROOT": env["SECRET_ROOT"], "APP_CONFIG_DIR": env["APP_CONFIG_DIR"]}
    )
    subprocess.run(COMPOSE + ["build"], check=True, capture_output=True, env=env)
    subprocess.run(COMPOSE + ["up", "-d"], check=True, capture_output=True, env=env)
    yield
    subprocess.run(COMPOSE + ["down", "-v"], capture_output=True, env=env)


def _full_env(overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(overrides)
    return merged


def _exec(service: str, *cmd: str) -> subprocess.CompletedProcess[str]:
    # `--user 10001` explicitly: `discovery` no longer declares `user:` in
    # docker-compose.yml (its entrypoint starts as uid 0 to load the egress
    # ruleset, then drops), so an unqualified exec would run as root and
    # probe as an identity the real workload never has.
    return subprocess.run(
        COMPOSE + ["exec", "-T", "--user", "10001", service, *cmd],
        capture_output=True, text=True, timeout=30,
    )


def test_discovery_cannot_reach_the_app_container_at_all() -> None:
    result = _exec("discovery", "python3", "-c",
        "import socket; socket.create_connection(('app', 80), timeout=3)")
    assert result.returncode != 0
    _assert_dns_isolation_failure(result, hostname="app")


def test_app_cannot_reach_the_discovery_container() -> None:
    result = _exec("app", "python3", "-c",
        "import socket; socket.create_connection(('discovery', 80), timeout=3)")
    assert result.returncode != 0
    _assert_dns_isolation_failure(result, hostname="discovery")


def _assert_dns_isolation_failure(
    result: subprocess.CompletedProcess[str], *, hostname: str
) -> None:
    # A nonzero exit alone is not proof of network isolation: if the two
    # containers shared a network (the exact "stray shared network" gap
    # Task 4's own precedent test worried about), the hostname would
    # resolve, the connect would reach a real host, and nothing listening
    # on port 80 would raise ConnectionRefusedError -- which also exits
    # nonzero, giving a false pass. Isolation is only proven when the
    # hostname doesn't even resolve across the boundary, i.e. a
    # socket.gaierror ("Name or service not known"), not a refused
    # connection to a resolved host.
    assert "gaierror" in result.stderr, (
        f"expected socket.gaierror (DNS resolution failure) proving "
        f"'{hostname}' does not resolve across the network boundary; "
        f"got a different failure instead (e.g. ConnectionRefusedError "
        f"would mean the host resolved and was reachable -- NOT "
        f"isolation). stderr was:\n{result.stderr}"
    )
