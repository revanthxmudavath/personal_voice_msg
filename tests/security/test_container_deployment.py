from __future__ import annotations

import json
import subprocess

import pytest

pytestmark = pytest.mark.security

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


@pytest.fixture(scope="module", autouse=True)
def _compose_stack(tmp_path_factory: pytest.TempPathFactory):
    secret_root = tmp_path_factory.mktemp("t18-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    env = {"SECRET_ROOT": str(secret_root)}
    subprocess.run(["docker", "compose", "build"], check=True, capture_output=True)
    subprocess.run(
        COMPOSE + ["up", "-d"], check=True, capture_output=True, env=_full_env(env)
    )
    yield
    subprocess.run(COMPOSE + ["down", "-v"], capture_output=True)


def _full_env(overrides: dict[str, str]) -> dict[str, str]:
    import os
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


@pytest.mark.parametrize("service", ["app", "discovery"])
def test_service_has_no_docker_socket_mount(service: str) -> None:
    info = _inspect(service)
    mounts = [m["Source"] for m in info["Mounts"]]
    assert not any("docker.sock" in source for source in mounts)


def test_discovery_network_has_ipv6_disabled() -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", "personal_voice_msg_discovery_net"],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)[0]
    assert info["EnableIPv6"] is False


def test_discovery_service_has_no_secret_bind_mount() -> None:
    info = _inspect("discovery")
    mounts = [m.get("Source", "") for m in info["Mounts"]]
    assert not any("secret" in source.lower() for source in mounts)
