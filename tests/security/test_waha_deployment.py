from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from urllib.error import HTTPError

import pytest

pytestmark = pytest.mark.security

CONTAINER_ENV = "T15_WAHA_CONTAINER"
BASE_URL_ENV = "T15_WAHA_SETTINGS_BASE_URL"

if CONTAINER_ENV not in os.environ:
    pytestmark = [
        pytest.mark.security,
        pytest.mark.integration,
        pytest.mark.skip(
            reason=(
                "requires the real WAHA container from docker-compose.yml; "
                f"set {CONTAINER_ENV} to the container name "
                "(docs/task-logs/T15.md)"
            )
        ),
    ]


def container_name() -> str:
    return os.environ[CONTAINER_ENV]


def base_url() -> str:
    return os.environ.get(BASE_URL_ENV, "http://127.0.0.1:3000")


def test_waha_container_publishes_no_public_port() -> None:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            container_name(),
            "--format",
            "{{json .NetworkSettings.Ports}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    ports = json.loads(result.stdout)

    bound_hosts = {
        binding["HostIp"]
        for bindings in ports.values()
        if bindings
        for binding in bindings
    }
    assert bound_hosts, "WAHA container published no ports at all"
    assert bound_hosts == {"127.0.0.1"}, (
        f"WAHA published a non-loopback host binding: {bound_hosts}"
    )


@pytest.mark.parametrize("path", ["/api/sessions", "/dashboard"])
def test_waha_rejects_unauthenticated_requests(path: str) -> None:
    request = urllib.request.Request(f"{base_url()}{path}")

    with pytest.raises(HTTPError) as raised:
        urllib.request.urlopen(request, timeout=10)

    assert raised.value.code == 401
