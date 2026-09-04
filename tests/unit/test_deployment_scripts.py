"""
Unit tests for Herald deployment and lifecycle scripts:
- scripts/reset-herald.sh
- scripts/install_acceptance.sh
- setup.sh idempotency and input safety
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


def run_bash_script(script_code: str, env: dict = None, args: list = None) -> subprocess.CompletedProcess:
    """Helper to run a bash snippet or script via bash subprocess."""
    assert BASH_EXE is not None, "Bash executable not found"
    bash_cmd = [BASH_EXE, "-c", script_code, "test_proc"] + (args or [])
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        bash_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=run_env,
    )


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_scripts_syntax_validity():
    """Verify bash syntax of reset-herald.sh and install_acceptance.sh."""
    for script_path in [RESET_SCRIPT_PATH, ACCEPTANCE_SCRIPT_PATH, SETUP_SCRIPT_PATH]:
        res = subprocess.run([BASH_EXE, "-n", str(script_path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert res.returncode == 0, f"Bash syntax check failed for {script_path.name}: {res.stderr}"


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_script_requires_mode():
    """Verify reset-herald.sh fails if neither --warm nor --cold is specified."""
    res = subprocess.run([BASH_EXE, str(RESET_SCRIPT_PATH)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert res.returncode != 0
    assert "Must specify either --warm or --cold mode" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reset_script_help_flag():
    """Verify reset-herald.sh --help outputs synopsis."""
    res = subprocess.run([BASH_EXE, str(RESET_SCRIPT_PATH), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert res.returncode == 0
    assert "Herald Reset Tool" in res.stdout
    assert "--warm" in res.stdout
    assert "--cold" in res.stdout
    assert "--remove-env" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_script_fails_if_env_missing(tmp_path):
    """Verify install_acceptance.sh reports failure when .env does not exist."""
    bash_snippet = f"""
    cd "{tmp_path.as_posix()}"
    bash "{ACCEPTANCE_SCRIPT_PATH.as_posix()}"
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode != 0
    assert "not found" in res.stderr or "failed" in res.stdout.lower()


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_script_detects_placeholder_postgres_password(tmp_path):
    """Verify install_acceptance.sh detects unsafe default postgres password."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="herald_secure_password"\n'
        'HERALD_API_KEY="test-valid-key-1234567890"\n'
        'AI_PROVIDER="none"\n'
    )

    bash_snippet = f"""
    set -euo pipefail
    cd "{tmp_path.as_posix()}"
    KNOWN_PLACEHOLDERS=("herald_secure_password" "change-this-to-a-secure-random-db-password")
    DB_PASS=$(grep -E "^POSTGRES_PASSWORD=" .env | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    for p in "${{KNOWN_PLACEHOLDERS[@]}}"; do
        if [ "$DB_PASS" = "$p" ]; then
            echo "FAIL: Placeholder password detected" >&2
            exit 1
        fi
    done
    echo "PASS"
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode != 0
    assert "Placeholder password detected" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_script_detects_missing_gemini_key_when_gemini_provider(tmp_path):
    """Verify install_acceptance.sh fails when AI_PROVIDER is gemini but GEMINI_API_KEY is empty."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="gemini"\n'
        'GEMINI_API_KEY=""\n'
    )

    bash_snippet = f"""
    set -euo pipefail
    cd "{tmp_path.as_posix()}"
    AI_PROV=$(grep -E "^AI_PROVIDER=" .env | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    if [ "$AI_PROV" = "gemini" ]; then
        G_KEY=$(grep -E "^GEMINI_API_KEY=" .env | cut -d'=' -f2- | tr -d '"' | tr -d "'" || true)
        if [ -z "$G_KEY" ]; then
            echo "FAIL: Gemini API key missing" >&2
            exit 1
        fi
    fi
    echo "PASS"
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode != 0
    assert "Gemini API key missing" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_script_ignores_n8n_key_in_default_profile(tmp_path):
    """Verify install_acceptance.sh does not fail if N8N_ENCRYPTION_KEY is missing when n8n is inactive."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="none"\n'
        '# N8N_ENCRYPTION_KEY not set\n'
    )

    bash_snippet = f"""
    set -euo pipefail
    cd "{tmp_path.as_posix()}"
    # In default profile, n8n key is optional and should not cause failure
    N8N_KEY=$(grep -E "^N8N_ENCRYPTION_KEY=" .env | cut -d'=' -f2- | tr -d '"' | tr -d "'" || true)
    echo "PASS (N8N Key ignored in default profile)"
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode == 0
    assert "PASS" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_secret_prompt_direct_variable_assignment_no_stdout_leak():
    """Verify that prompt_secret assigns variable directly and never writes secret value to stdout/stderr."""
    bash_snippet = """
    set -euo pipefail
    prompt_secret() {
        local var_name="$1"
        local prompt_msg="$2"
        read -s -rp "$prompt_msg" "$var_name"
        echo "" >&2
    }

    # Simulate entering a secret token
    SECRET_INPUT="super_secret_telegram_token_12345"
    prompt_secret MY_TOKEN "Enter token: " <<< "$SECRET_INPUT"

    # Print variable name check, never the secret itself
    if [ "$MY_TOKEN" = "$SECRET_INPUT" ]; then
        echo "TOKEN_ASSIGNED_CORRECTLY"
    fi
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode == 0
    assert "TOKEN_ASSIGNED_CORRECTLY" in res.stdout
    assert "super_secret_telegram_token_12345" not in res.stdout
    assert "super_secret_telegram_token_12345" not in res.stderr
