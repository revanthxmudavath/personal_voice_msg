"""Real fault-injection proof that Docker's `--pids-limit`/`--memory` flags
actually terminate a runaway process inside the shared `t18` image -- not
just that `docker-compose.yml` *declares* `pids_limit`/`mem_limit` for the
`discovery` service (that declared-config check already exists in
`test_container_deployment.py::test_service_runs_non_root_with_dropped_capabilities`,
which only asserts `HostConfig.PidsLimit` is set, not that it's enforced).

This uses plain `docker run` directly against `personal-voice-msg:t18`
(not `docker compose`), so it does not need the compose stack running --
it is exercising Docker's own cgroup enforcement, which applies identically
whether the container was started by `docker run` or by Compose using the
same flags (`pids_limit: 64` / `mem_limit: 1g` on the `discovery` service
in `docker-compose.yml`). Lower limits are used here (10 pids / 64m) purely
to make the fault trigger fast and deterministically, not because the
enforced values differ from production.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.security, pytest.mark.docker]

IMAGE = "personal-voice-msg:t18"


@pytest.fixture(scope="module", autouse=True)
def _built_image() -> None:
    # Built explicitly rather than relying on whichever other module's
    # fixture happens to be collected first. Layers are cached, so this is
    # nearly free when the image is already current.
    here = Path(__file__).resolve().parent
    built = subprocess.run(
        ["docker", "compose", "-p", "personal_voice_msg_test",
         "-f", "docker-compose.yml", "build"],
        capture_output=True, text=True, timeout=1800,
        env={**os.environ, "SECRET_ROOT": str(here), "APP_CONFIG_DIR": str(here)},
    )
    assert built.returncode == 0, built.stderr


def test_pids_limit_blocks_fork_bombing_the_discovery_worker() -> None:
    # A real fork bomb: os.fork() with no exec/wait means every forked
    # child resumes the very same loop, so each successful iteration
    # doubles the live process count (1, 2, 4, 8, ...) -- the same
    # unbounded runaway-process pattern a compromised or buggy discovery
    # worker could produce.
    #
    # Deviation from the task brief's illustrative snippet: the brief
    # wrapped this in `sh -c` with 15 backgrounded `: &` no-ops prefixed
    # before the python3 call. Verified empirically (see task-6-report.md)
    # that those 15 no-op background jobs *by themselves*, with no
    # python3/fork loop at all, already exhaust `--pids-limit 10` (the
    # shell itself fails with `sh: 0: Cannot fork`) -- so the brief's
    # version would still report nonzero and "pass" even if the fork-bomb
    # code were broken or deleted entirely. That is exactly the
    # "superficially similar command that fails for an unrelated reason"
    # pitfall the brief warned against. Invoking python3 directly, with no
    # shell wrapper and no padding, makes the failure trace unambiguously
    # to `os.fork()` hitting the pids ceiling.
    fork_bomb = "import os\nfor _ in range(200):\n os.fork()"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--pids-limit", "10",
            IMAGE, "python3", "-c", fork_bomb,
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    # A nonzero exit alone doesn't prove pids-limit caused it -- assert the
    # real EAGAIN signature os.fork() raises once Docker's pids cgroup
    # controller refuses to hand out another task.
    assert "BlockingIOError" in result.stderr and "Errno 11" in result.stderr, (
        "expected os.fork() to fail with BlockingIOError: [Errno 11] "
        "Resource temporarily unavailable (the real EAGAIN Docker's pids "
        "cgroup controller returns once --pids-limit is hit); got a "
        "different failure instead, which would not prove pids-limit was "
        f"the actual cause. stderr was:\n{result.stderr}"
    )


def test_memory_limit_kills_a_deliberately_oversized_allocation() -> None:
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--memory", "64m",
            IMAGE, "python3", "-c", "x = bytearray(500 * 1024 * 1024)",
        ],
        capture_output=True, text=True, timeout=30,
    )
    # 137 = 128 + SIGKILL(9): Docker's own exit-code convention for a
    # container terminated by a signal. This is the real cgroup OOM killer
    # ending the process for exceeding --memory 64m -- not e.g. Python
    # raising MemoryError on its own (which would exit 1) or the container
    # runtime rejecting the flag outright (which would fail before the
    # container ever started).
    assert result.returncode == 137, (
        "expected the cgroup OOM killer to SIGKILL the process (docker's "
        "own exit code 137) once --memory 64m was exceeded by a 500MB "
        f"allocation; got returncode={result.returncode} instead. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
