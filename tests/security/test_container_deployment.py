from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = [pytest.mark.security, pytest.mark.docker]

# `-p personal_voice_msg_test`: never share the production Compose project
# (and therefore the production `db_data` volume) with a test whose teardown
# runs `down -v`. See test_network_isolation.py's comment and
# infra/RUNBOOK.md's "never run the test suite on the production host".
PROJECT = "personal_voice_msg_test"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml"]


@pytest.fixture(scope="module", autouse=True)
def _compose_stack(tmp_path_factory: pytest.TempPathFactory):
    secret_root = tmp_path_factory.mktemp("t18-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    config_dir = tmp_path_factory.mktemp("t18-conf")
    # Exported into this process's own environment, not just passed to the
    # two calls below: docker-compose.yml now declares SECRET_ROOT and
    # APP_CONFIG_DIR with Compose's required-variable syntax, and *every*
    # compose subcommand interpolates the whole file -- including the bare
    # `docker compose ps -q <service>` that `_inspect` runs. Threading an
    # env= through each call site is the same thing with more places to
    # forget one.
    env = _full_env({
        "SECRET_ROOT": str(secret_root),
        "APP_CONFIG_DIR": str(config_dir),
    })
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


def _inspect(service: str) -> dict:
    result = subprocess.run(
        COMPOSE + ["ps", "-q", service], capture_output=True, text=True, check=True,
    )
    container_id = result.stdout.strip()
    inspected = subprocess.run(
        ["docker", "inspect", container_id], capture_output=True, text=True, check=True,
    )
    return json.loads(inspected.stdout)[0]


@pytest.mark.parametrize("service", ["app", "discovery"])
def test_service_publishes_no_ports(service: str) -> None:
    info = _inspect(service)
    assert info["NetworkSettings"]["Ports"] in ({}, None) or all(
        v is None for v in info["NetworkSettings"]["Ports"].values()
    )


@pytest.mark.parametrize("service", ["app", "discovery"])
def test_service_runs_non_root_with_dropped_capabilities(service: str) -> None:
    info = _inspect(service)
    host_config = info["HostConfig"]
    assert host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["PidsLimit"] not in (0, None, -1)


@pytest.mark.parametrize(
    ("service", "expected_cap_add"),
    [
        # `app` holds every secret and must never regain a capability.
        ("app", []),
        # `discovery` gets back exactly three, and only because its
        # entrypoint loads infra/firewall/discovery_egress.nft into its own
        # netns (NET_ADMIN) and then permanently drops to uid 10001
        # (SETUID/SETGID). Pinned exactly so a future edit cannot quietly
        # widen the grant -- e.g. to SYS_ADMIN -- without failing here.
        ("discovery", ["NET_ADMIN", "SETUID", "SETGID"]),
    ],
)
def test_service_adds_back_only_the_expected_capabilities(
    service: str, expected_cap_add: list[str]
) -> None:
    info = _inspect(service)
    # Docker normalises CapAdd to CAP_-prefixed names and does not preserve
    # the compose file's ordering, so compare as an unprefixed set.
    granted = {
        capability.removeprefix("CAP_")
        for capability in (info["HostConfig"]["CapAdd"] or [])
    }
    assert granted == set(expected_cap_add), (
        f"{service} must add back exactly {sorted(expected_cap_add)}; "
        f"got {sorted(granted)}"
    )


@pytest.mark.parametrize("service", ["app", "discovery"])
def test_service_has_no_docker_socket_mount(service: str) -> None:
    info = _inspect(service)
    mounts = [m["Source"] for m in info["Mounts"]]
    assert not any("docker.sock" in source for source in mounts)


def test_discovery_network_has_ipv6_disabled() -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", f"{PROJECT}_discovery_net"],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)[0]
    assert info["EnableIPv6"] is False


def test_discovery_service_has_no_secret_bind_mount() -> None:
    info = _inspect("discovery")
    mounts = [m.get("Source", "") for m in info["Mounts"]]
    assert not any("secret" in source.lower() for source in mounts)
