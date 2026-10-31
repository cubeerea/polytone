"""scripts/rotate-neo4j-password.sh with a fake `docker` on PATH (never the real container)."""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rotate-neo4j-password.sh"

FAKE_DOCKER = """#!/bin/sh
# records argv, the NEO4J_PASSWORD it was given and the statement on stdin; exits $FAKE_EXIT
{ printf 'ARGS:'; printf ' %s' "$@"; printf '\\nPW:%s\\nSTDIN:' "$NEO4J_PASSWORD"; cat; } >> "$FAKE_LOG"
[ -n "$FAKE_MSG" ] && echo "$FAKE_MSG" >&2
exit "${FAKE_EXIT:-0}"
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    envfile = tmp_path / ".env"
    envfile.write_text("NEO4J_URI=bolt://localhost:7687\nNEO4J_PASSWORD=polytone-local\nPOLYTONE_LLM=\n")
    envfile.chmod(0o644)
    e = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "POLYTONE_ENV_FILE": str(envfile),
        "POLYTONE_COMPOSE_DIR": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "docker.log"),
    }
    return e, envfile, tmp_path / "docker.log"


def _run(e, **extra):
    return subprocess.run(["bash", str(SCRIPT)], env={**e, **extra}, capture_output=True, text=True, timeout=30, check=False)


def _pw(envfile):
    return re.findall(r"^NEO4J_PASSWORD=(.*)$", envfile.read_text(), re.MULTILINE)


def test_rotates_and_rewrites_env(env):
    e, envfile, log = env
    r = _run(e)
    assert r.returncode == 0, r.stderr
    (new,) = _pw(envfile)
    assert re.fullmatch(r"[A-Za-z0-9]{32}", new) and new != "polytone-local"
    text = envfile.read_text()
    assert "NEO4J_URI=bolt://localhost:7687" in text and "POLYTONE_LLM=" in text  # other lines kept
    assert stat.S_IMODE(envfile.stat().st_mode) == 0o600
    called = log.read_text()
    assert "ARGS: compose exec -T -e NEO4J_PASSWORD neo4j cypher-shell -u neo4j -d system" in called
    assert "PW:polytone-local" in called
    assert f"ALTER CURRENT USER SET PASSWORD FROM 'polytone-local' TO '{new}';" in called
    assert new not in called.split("STDIN:")[0]  # never on the command line
    assert not list(envfile.parent.glob(".env.*"))  # no temp file left behind
    # a second rotation starts from the new password
    r = _run(e)
    assert r.returncode == 0
    (newer,) = _pw(envfile)
    assert newer != new and f"FROM '{new}' TO '{newer}'" in log.read_text()


def test_failure_leaves_env_untouched(env):
    e, envfile, _ = env
    before = envfile.read_text()
    r = _run(e, FAKE_EXIT="1", FAKE_MSG="The client is unauthorized due to authentication failure.")
    assert r.returncode == 1
    assert envfile.read_text() == before and stat.S_IMODE(envfile.stat().st_mode) == 0o644
    assert ".env was NOT modified" in r.stderr and "does not match" in r.stderr


def test_container_down_message(env):
    e, envfile, _ = env
    r = _run(e, FAKE_EXIT="1", FAKE_MSG='service "neo4j" is not running')
    assert r.returncode == 1 and "docker compose up" in r.stderr
    assert _pw(envfile) == ["polytone-local"]


def test_appends_when_missing_and_handles_quotes(env):
    e, envfile, log = env
    envfile.write_text("NEO4J_URI=bolt://localhost:7687")  # no trailing newline, no password line
    assert _run(e).returncode == 0
    lines = envfile.read_text().splitlines()
    assert lines[0] == "NEO4J_URI=bolt://localhost:7687" and lines[1].startswith("NEO4J_PASSWORD=")
    assert "FROM 'polytone-local'" in log.read_text()  # the compose default
    envfile.write_text('NEO4J_PASSWORD="quoted-old"\n')
    assert _run(e).returncode == 0
    assert "PW:quoted-old" in log.read_text()


def test_missing_env_file(env):
    e, envfile, _ = env
    os.remove(envfile)
    r = _run(e)
    assert r.returncode == 1 and "not found" in r.stderr
