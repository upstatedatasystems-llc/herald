"""
Unit tests for Herald deployment and lifecycle scripts:
- scripts/reset-herald.sh
- scripts/install_acceptance.sh
- setup.sh idempotency, secret safety, and fault propagation
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
RESET_SCRIPT_PATH = SCRIPTS_DIR / "reset-herald.sh"
ACCEPTANCE_SCRIPT_PATH = SCRIPTS_DIR / "install_acceptance.sh"
SETUP_SCRIPT_PATH = Path(__file__).parent.parent.parent / "setup.sh"


def get_bash_executable() -> str | None:
    """Find bash executable across Linux, macOS, and Windows (Git Bash)."""
    b = shutil.which("bash") or shutil.which("bash.exe")
    if b:
        return b
    git_path = shutil.which("git")
    if git_path:
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(git_path)), "bin", "bash.exe"),
            os.path.join(os.path.dirname(os.path.dirname(git_path)), "usr", "bin", "bash.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
    return None


BASH_EXE = get_bash_executable()


def to_posix_path(p: str | Path) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/{s[0].lower()}{s[2:]}"
    return s


def run_script(script_path: Path, args: list = None, env: dict = None, cwd: Path = None, stdin_input: str = None) -> subprocess.CompletedProcess:
    """Helper to run a shell script directly as subprocess."""
    assert BASH_EXE is not None, "Bash executable not found"
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    path_val = (env or {}).get("PATH")
    if path_val:
        first_dir = path_val.split(os.pathsep)[0]
        posix_path = to_posix_path(first_dir)
        bash_cmd = [
            BASH_EXE,
            "-c",
            f'export PATH="{posix_path}:$PATH"; exec "{script_path.as_posix()}" "$@"',
            "bash",
        ] + (args or [])
    else:
        bash_cmd = [BASH_EXE, str(script_path)] + (args or [])

    return subprocess.run(
        bash_cmd,
        input=stdin_input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=run_env,
        cwd=str(cwd) if cwd else None,
    )


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_scripts_syntax_validity():
    """Verify bash syntax of reset-herald.sh, install_acceptance.sh, and setup.sh."""
    for script_path in [RESET_SCRIPT_PATH, ACCEPTANCE_SCRIPT_PATH, SETUP_SCRIPT_PATH]:
        res = subprocess.run([BASH_EXE, "-n", str(script_path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert res.returncode == 0, f"Bash syntax check failed for {script_path.name}: {res.stderr}"


# =========================================================================
# RESET-HERALD.SH TESTS
# =========================================================================

@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_script_requires_mode():
    """Verify reset-herald.sh fails if neither --warm nor --cold is specified."""
    res = run_script(RESET_SCRIPT_PATH)
    assert res.returncode != 0
    assert "Must specify either --warm or --cold mode" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_script_rejects_both_warm_and_cold():
    """Verify reset-herald.sh fails if both --warm and --cold are provided."""
    res = run_script(RESET_SCRIPT_PATH, args=["--warm", "--cold"])
    assert res.returncode != 0
    assert "Cannot specify both --warm and --cold mode simultaneously." in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_script_help_flag():
    """Verify reset-herald.sh --help outputs synopsis."""
    res = run_script(RESET_SCRIPT_PATH, args=["--help"])
    assert res.returncode == 0
    assert "Herald Reset Tool" in res.stdout
    assert "--warm" in res.stdout
    assert "--cold" in res.stdout
    assert "--remove-env" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_fails_when_docker_missing(tmp_path):
    """Verify reset-herald.sh fails if docker command is not available in PATH."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    # Minimal environment without docker in PATH
    res = run_script(RESET_SCRIPT_PATH, args=["--warm", "--yes"], env={"PATH": fake_bin.as_posix()})
    assert res.returncode != 0
    assert "Docker command is missing" in res.stderr
    assert "reset complete" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_fails_when_compose_missing(tmp_path):
    """Verify reset-herald.sh fails if docker compose v2 plugin is not available."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    res = run_script(RESET_SCRIPT_PATH, args=["--warm", "--yes"], env={"PATH": f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"})
    assert res.returncode != 0
    assert "Docker Compose v2 plugin is missing" in res.stderr
    assert "reset complete" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_fails_when_docker_daemon_unavailable(tmp_path):
    """Verify reset-herald.sh fails if docker info returns non-zero (daemon stopped / permission denied)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    res = run_script(RESET_SCRIPT_PATH, args=["--warm", "--yes"], env={"PATH": f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"})
    assert res.returncode != 0
    assert "Docker daemon is unavailable or permission denied" in res.stderr
    assert "reset complete" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_warm_exact_compose_down_and_preserves_env(tmp_path):
    """Verify reset-herald.sh --warm executes docker compose down -v --remove-orphans and preserves .env."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    log_file = tmp_path / "docker_calls.log"

    (fake_bin / "docker").write_text(
        f"#!/usr/bin/env bash\n"
        f"echo \"$*\" >> \"{log_file.as_posix()}\"\n"
        f"exit 0\n",
        encoding="utf-8",
    )

    env_file = tmp_path / ".env"
    env_file.write_text('TELEGRAM_BOT_TOKEN="my_token"\n')

    res = run_script(
        RESET_SCRIPT_PATH,
        args=["--warm", "--yes"],
        env={"PATH": f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode == 0
    assert "Herald warm reset complete." in res.stdout
    calls = log_file.read_text()
    assert "compose down -v --remove-orphans" in calls


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_cold_removes_herald_images_and_verifies_removal(tmp_path):
    """Verify reset-herald.sh --cold captures Herald built images, deletes them with docker rmi, and verifies removal."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    log_file = tmp_path / "docker_calls.log"

    (fake_bin / "docker").write_text(
        f"#!/usr/bin/env bash\n"
        f"echo \"$*\" >> \"{log_file.as_posix()}\"\n"
        f"if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"images\" ]; then\n"
        f"    echo \"sha256:mig123\"\n"
        f"    echo \"sha256:worker456\"\n"
        f"    exit 0\n"
        f"fi\n"
        f"if [ \"$1\" = \"image\" ] && [ \"$2\" = \"inspect\" ]; then\n"
        f"    # Image is gone after rmi\n"
        f"    exit 1\n"
        f"fi\n"
        f"exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        RESET_SCRIPT_PATH,
        args=["--cold", "--yes"],
        env={"PATH": f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode == 0
    assert "Herald cold reset complete." in res.stdout
    calls = log_file.read_text()
    assert "rmi sha256:mig123" in calls
    assert "rmi sha256:worker456" in calls


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_cold_image_enumeration_failure_propagates(tmp_path):
    """Verify reset-herald.sh --cold fails if image enumeration fails."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)

    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"images\" ]; then\n"
        "    echo \"Failed to query Compose API\" >&2\n"
        "    exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        RESET_SCRIPT_PATH,
        args=["--cold", "--yes"],
        env={"PATH": f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode != 0
    assert "Failed to enumerate Herald Docker images" in res.stderr
    assert "reset complete" not in res.stdout


# =========================================================================
# SETUP.SH TESTS
# =========================================================================

@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_existing_env_missing_postgres_password_fails_without_regeneration(tmp_path):
    """Verify setup.sh refuses to regenerate POSTGRES_PASSWORD on existing .env in non-interactive mode."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'HERALD_API_KEY="valid-key-12345"\n'
        'AI_PROVIDER="none"\n'
        'POSTGRES_PASSWORD=""\n'
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "echo '{\"ok\":true, \"result\":{\"username\":\"TestBot\"}}'\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        SETUP_SCRIPT_PATH,
        args=["--non-interactive"],
        env={"PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode != 0
    assert "missing POSTGRES_PASSWORD" in res.stderr
    assert "Cannot regenerate password because the existing PostgreSQL volume requires the original password" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_custom_groq_model_in_real_curl_request(tmp_path):
    """Verify actual setup.sh transmits custom GROQ_MODEL via curl config file without argv secret leakage."""
    curl_log = tmp_path / "curl_calls.log"
    config_captures = tmp_path / "curl_configs.log"

    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_12345"\n'
        'HERALD_API_KEY="custom_api_key_12345"\n'
        'AI_PROVIDER="groq"\n'
        'GROQ_API_KEY="gsk_secret_token_alpha_999"\n'
        'GROQ_MODEL="llama-custom-model-test"\n'
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "curl").write_text(
        f"#!/usr/bin/env bash\n"
        f"echo \"curl argv: $*\" >> \"{curl_log.as_posix()}\"\n"
        f"while [[ $# -gt 0 ]]; do\n"
        f"    if [ \"$1\" = \"-K\" ] && [ -f \"$2\" ]; then\n"
        f"        cat \"$2\" >> \"{config_captures.as_posix()}\"\n"
        f"        echo \"---CONFIG_END---\" >> \"{config_captures.as_posix()}\"\n"
        f"    fi\n"
        f"    shift\n"
        f"done\n"
        f"echo '{{\"ok\":true, \"result\":{{\"username\":\"TestBot\"}}, \"id\":\"llama-custom-model-test\"}}'\n"
        f"exit 0\n",
        encoding="utf-8",
    )

    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ]; then\n"
        "    if [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"up\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"ps\" ]; then\n"
        "        if [ \"$3\" = \"-q\" ]; then echo \"cid123\"; exit 0; fi\n"
        "        if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "        echo \"herald-worker\"\n"
        "        echo \"telegram-bot\"\n"
        "        exit 0\n"
        "    fi\n"
        "    if [ \"$2\" = \"exec\" ]; then echo 'UNPAIRED:987654:30'; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        SETUP_SCRIPT_PATH,
        args=["--non-interactive"],
        env={"PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode == 0
    assert "Groq Cloud connection and model 'llama-custom-model-test' verified." in res.stdout

    # Verify zero secret argv leakage
    curl_argv_text = curl_log.read_text()
    assert "gsk_secret_token_alpha_999" not in curl_argv_text
    assert "--connect-timeout 10" in curl_argv_text
    assert "--max-time 30" in curl_argv_text

    # Verify custom model and secret were in config file
    configs_text = config_captures.read_text()
    assert "models/llama-custom-model-test" in configs_text
    assert "Bearer gsk_secret_token_alpha_999" in configs_text


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_fails_when_migration_fails(tmp_path):
    """Verify setup.sh aborts and exits nonzero if migration container exits with non-zero code."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_12345"\n'
        'HERALD_API_KEY="custom_api_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "echo '{\"ok\":true, \"result\":{\"username\":\"TestBot\"}}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"ps\" ]; then\n"
        "    if [ \"$3\" = \"-q\" ]; then echo \"cid123\"; exit 0; fi\n"
        "    if [ \"$3\" = \"-a\" ]; then echo \"Exited (1)\"; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        SETUP_SCRIPT_PATH,
        args=["--non-interactive"],
        env={"PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode != 0
    assert "Database migration failed" in res.stderr
    assert "Herald Setup Complete!" not in res.stdout


# =========================================================================
# INSTALL_ACCEPTANCE.SH TESTS
# =========================================================================

@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_location_independent_from_outside_repo(tmp_path):
    """Verify install_acceptance.sh executes correctly when invoked from an arbitrary caller working directory."""
    outside_dir = tmp_path / "outside_workspace"
    outside_dir.mkdir(parents=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ]; then\n"
        "    if [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"ps\" ]; then\n"
        "        if [ \"$3\" = \"-q\" ]; then echo \"cid123\"; exit 0; fi\n"
        "        if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "        echo \"herald-worker\"\n"
        "        echo \"telegram-bot\"\n"
        "        exit 0\n"
        "    fi\n"
        "    if [ \"$2\" = \"run\" ]; then echo '014_diag_events (head)'; exit 0; fi\n"
        "    if [ \"$2\" = \"exec\" ]; then echo '014_diag_events'; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    env_file = tmp_path / "custom.env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    res = run_script(
        ACCEPTANCE_SCRIPT_PATH,
        env={
            "HERALD_ENV_FILE": str(env_file),
            "HERALD_TEST_ALLOW_PERMS": "1",
            "PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        },
        cwd=outside_dir,
    )
    assert res.returncode == 0
    assert "Acceptance Validation Passed: All 7 checks succeeded." in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_detects_optional_profile_running_unless_allowed(tmp_path):
    """Verify install_acceptance.sh fails when n8n is running in default profile, but passes with HERALD_ACCEPTANCE_ALLOW_LEGACY_PROFILES=1."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ]; then\n"
        "    if [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"ps\" ]; then\n"
        "        if [ \"$3\" = \"-q\" ]; then echo \"cid123\"; exit 0; fi\n"
        "        if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "        echo \"herald-worker\"\n"
        "        echo \"telegram-bot\"\n"
        "        echo \"n8n\"\n"
        "        exit 0\n"
        "    fi\n"
        "    if [ \"$2\" = \"run\" ]; then echo '014_diag_events'; exit 0; fi\n"
        "    if [ \"$2\" = \"exec\" ]; then echo '014_diag_events'; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    # By default, fails because n8n is running
    res_fail = run_script(
        ACCEPTANCE_SCRIPT_PATH,
        env={
            "HERALD_ENV_FILE": str(env_file),
            "HERALD_TEST_ALLOW_PERMS": "1",
            "PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert res_fail.returncode != 0
    assert "Optional service 'n8n' is running in default installation profile" in res_fail.stderr

    # With bypass flag, passes
    res_pass = run_script(
        ACCEPTANCE_SCRIPT_PATH,
        env={
            "HERALD_ENV_FILE": str(env_file),
            "HERALD_TEST_ALLOW_PERMS": "1",
            "HERALD_ACCEPTANCE_ALLOW_LEGACY_PROFILES": "1",
            "PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert res_pass.returncode == 0
    assert "Optional profile isolation check bypassed" in res_pass.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_fresh_generates_postgres_password_and_api_key(tmp_path):
    """Verify setup.sh on fresh configuration (.env did not exist before setup) generates secure secrets."""
    env_file = tmp_path / ".env"
    # Do NOT create .env beforehand

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "echo '{\"ok\":true, \"result\":{\"username\":\"TestBot\"}}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ]; then\n"
        "    if [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"up\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"ps\" ]; then\n"
        "        if [ \"$3\" = \"-q\" ]; then echo \"cid123\"; exit 0; fi\n"
        "        if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "        echo \"herald-worker\"\n"
        "        echo \"telegram-bot\"\n"
        "        exit 0\n"
        "    fi\n"
        "    if [ \"$2\" = \"exec\" ]; then echo 'UNPAIRED:987654:30'; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    # Run non-interactive with empty env - prompts will fail if not provided, but we can test generation logic
    # In interactive/stdin mode:
    input_str = "123456:BOT-TOKEN\n1\n"  # token, provider=none
    res = run_script(
        SETUP_SCRIPT_PATH,
        stdin_input=input_str,
        cwd=tmp_path,
        env={
            "HERALD_TEST_ALLOW_STDIN": "1",
            "PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert res.returncode == 0
    assert env_file.exists()
    written_env = env_file.read_text()
    assert "POSTGRES_PASSWORD=" in written_env
    assert "HERALD_API_KEY=" in written_env
    assert "POSTGRES_PASSWORD=\"\"" not in written_env


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_kokoro_timeout_propagates(tmp_path):
    """Verify setup.sh exits nonzero if Kokoro TTS container fails to become healthy."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_12345"\n'
        'HERALD_API_KEY="custom_api_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "echo '{\"ok\":true, \"result\":{\"username\":\"TestBot\"}}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"ps\" ]; then\n"
        "    if [ \"$3\" = \"-q\" ] && [ \"$4\" = \"postgres\" ]; then echo \"cid_pg\"; exit 0; fi\n"
        "    if [ \"$3\" = \"-q\" ] && [ \"$4\" = \"kokoro\" ]; then echo \"cid_kokoro\"; exit 0; fi\n"
        "    if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then\n"
        "    if [ \"$3\" = \"cid_pg\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "    if [ \"$3\" = \"cid_kokoro\" ]; then echo '\"unhealthy\"'; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        SETUP_SCRIPT_PATH,
        args=["--non-interactive"],
        env={"PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode != 0
    assert "Kokoro TTS health check timed out" in res.stderr
    assert "Herald Setup Complete!" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_worker_missing_propagates(tmp_path):
    """Verify setup.sh exits nonzero if herald-worker is not in running services."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_12345"\n'
        'HERALD_API_KEY="custom_api_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "echo '{\"ok\":true, \"result\":{\"username\":\"TestBot\"}}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"ps\" ]; then\n"
        "    if [ \"$3\" = \"-q\" ]; then echo \"cid_ok\"; exit 0; fi\n"
        "    if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "    echo \"telegram-bot\"\n"  # herald-worker missing!
        "    exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        SETUP_SCRIPT_PATH,
        args=["--non-interactive"],
        env={"PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode != 0
    assert "Herald Worker container is not running" in res.stderr
    assert "Herald Setup Complete!" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_telegram_bot_missing_propagates(tmp_path):
    """Verify setup.sh exits nonzero if telegram-bot is not in running services."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_12345"\n'
        'HERALD_API_KEY="custom_api_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "echo '{\"ok\":true, \"result\":{\"username\":\"TestBot\"}}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"ps\" ]; then\n"
        "    if [ \"$3\" = \"-q\" ]; then echo \"cid_ok\"; exit 0; fi\n"
        "    if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "    echo \"herald-worker\"\n"  # telegram-bot missing!
        "    exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = run_script(
        SETUP_SCRIPT_PATH,
        args=["--non-interactive"],
        env={"PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        cwd=tmp_path,
    )
    assert res.returncode != 0
    assert "Telegram Bot container is not running" in res.stderr
    assert "Herald Setup Complete!" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_dynamic_alembic_heads_multiple_heads_fails(tmp_path):
    """Verify install_acceptance.sh fails when dynamic alembic heads returns multiple heads."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ]; then\n"
        "    if [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"ps\" ]; then\n"
        "        if [ \"$3\" = \"-q\" ]; then echo \"cid123\"; exit 0; fi\n"
        "        if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "        echo \"herald-worker\"\n"
        "        echo \"telegram-bot\"\n"
        "        exit 0\n"
        "    fi\n"
        "    if [ \"$2\" = \"run\" ]; then\n"
        "        echo '014_head_a (head)'\n"
        "        echo '014_head_b (head)'\n"
        "        exit 0\n"
        "    fi\n"
        "    if [ \"$2\" = \"exec\" ]; then echo '014_head_a'; exit 0; fi\n"
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    res = run_script(
        ACCEPTANCE_SCRIPT_PATH,
        env={
            "HERALD_ENV_FILE": str(env_file),
            "HERALD_TEST_ALLOW_PERMS": "1",
            "PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert res.returncode != 0
    assert "Could not authoritatively determine single Alembic migration head revision" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_dynamic_alembic_heads_revision_mismatch_fails(tmp_path):
    """Verify install_acceptance.sh fails when live DB revision differs from dynamic alembic head."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ]; then\n"
        "    if [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "    if [ \"$2\" = \"ps\" ]; then\n"
        "        if [ \"$3\" = \"-q\" ]; then echo \"cid123\"; exit 0; fi\n"
        "        if [ \"$3\" = \"-a\" ]; then echo \"Exited (0)\"; exit 0; fi\n"
        "        echo \"herald-worker\"\n"
        "        echo \"telegram-bot\"\n"
        "        exit 0\n"
        "    fi\n"
        "    if [ \"$2\" = \"run\" ]; then echo '014_diag_events (head)'; exit 0; fi\n"
        "    if [ \"$2\" = \"exec\" ]; then echo '013_ai_interactions'; exit 0; fi\n"  # Old live revision!
        "fi\n"
        "if [ \"$1\" = \"inspect\" ]; then echo '\"healthy\"'; exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="none"\n'
    )

    res = run_script(
        ACCEPTANCE_SCRIPT_PATH,
        env={
            "HERALD_ENV_FILE": str(env_file),
            "HERALD_TEST_ALLOW_PERMS": "1",
            "PATH": f"{fake_bin.as_posix()}{os.pathsep}{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert res.returncode != 0
    assert "Database schema revision mismatch" in res.stderr
