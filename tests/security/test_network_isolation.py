from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = pytest.mark.security

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


@pytest.fixture(scope="module", autouse=True)
def _compose_stack(tmp_path_factory: pytest.TempPathFactory):
    secret_root = tmp_path_factory.mktemp("t18-net-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    env = _full_env({"SECRET_ROOT": str(secret_root)})
    subprocess.run(["docker", "compose", "build"], check=True, capture_output=True)
    subprocess.run(COMPOSE + ["up", "-d"], check=True, capture_output=True, env=env)
    yield
    subprocess.run(COMPOSE + ["down", "-v"], capture_output=True)


def _full_env(overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(overrides)
    return merged


def _exec(service: str, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        COMPOSE + ["exec", "-T", service, *cmd],
        capture_output=True, text=True, timeout=15,
    )


def test_discovery_cannot_reach_the_app_container_at_all() -> None:
    result = _exec("discovery", "python3", "-c",
        "import socket; socket.create_connection(('app', 80), timeout=3)")
    assert result.returncode != 0
    _assert_dns_isolation_failure(result, hostname="app")


def test_discovery_cannot_reach_a_private_network_address() -> None:
    result = _exec("discovery", "python3", "-c",
        "import socket; socket.create_connection(('192.168.1.1', 80), timeout=3)")
    assert result.returncode != 0


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
