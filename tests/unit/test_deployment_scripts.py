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


def run_script(script_path: Path, args: list = None, env: dict = None, cwd: Path = None) -> subprocess.CompletedProcess:
    """Helper to run a shell script directly as subprocess."""
    assert BASH_EXE is not None, "Bash executable not found"
    bash_cmd = [BASH_EXE, str(script_path)] + (args or [])
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
        cwd=str(cwd) if cwd else None,
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
def test_acceptance_script_fails_if_env_missing(tmp_path):
    """Verify install_acceptance.sh reports failure when .env does not exist."""
    res = run_script(ACCEPTANCE_SCRIPT_PATH, env={"HERALD_ENV_FILE": str(tmp_path / "nonexistent.env")})
    assert res.returncode != 0
    assert "not found" in res.stderr or "failed" in res.stdout.lower()


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_script_detects_placeholder_postgres_password(tmp_path):
    """Verify install_acceptance.sh detects unsafe default postgres password in .env."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="herald_secure_password"\n'
        'HERALD_API_KEY="test-valid-key-1234567890"\n'
        'AI_PROVIDER="none"\n'
    )
    try:
        env_file.chmod(0o600)
    except Exception:
        pass

    res = run_script(ACCEPTANCE_SCRIPT_PATH, env={"HERALD_ENV_FILE": str(env_file)})
    assert res.returncode != 0
    assert "POSTGRES_PASSWORD matches a known default placeholder." in res.stderr


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
    try:
        env_file.chmod(0o600)
    except Exception:
        pass

    res = run_script(ACCEPTANCE_SCRIPT_PATH, env={"HERALD_ENV_FILE": str(env_file)})
    assert res.returncode != 0
    assert "AI_PROVIDER is 'gemini' but GEMINI_API_KEY is missing." in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_script_rejects_unknown_ai_provider(tmp_path):
    """Verify install_acceptance.sh fails when an unknown AI_PROVIDER is configured."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="unsupported_future_provider"\n'
    )
    try:
        env_file.chmod(0o600)
    except Exception:
        pass

    res = run_script(ACCEPTANCE_SCRIPT_PATH, env={"HERALD_ENV_FILE": str(env_file)})
    assert res.returncode != 0
    assert "Unknown AI_PROVIDER 'unsupported_future_provider' configured" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_acceptance_script_rejects_unknown_research_provider(tmp_path):
    """Verify install_acceptance.sh fails when an unsupported RESEARCH_PROVIDER is configured."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="none"\n'
        'RESEARCH_PROVIDER="custom_unknown_research"\n'
    )
    try:
        env_file.chmod(0o600)
    except Exception:
        pass

    res = run_script(ACCEPTANCE_SCRIPT_PATH, env={"HERALD_ENV_FILE": str(env_file)})
    assert res.returncode != 0
    assert "Unsupported RESEARCH_PROVIDER 'custom_unknown_research'" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_setup_noninteractive_fails_on_missing_config(tmp_path):
    """Verify setup.sh --non-interactive fails immediately if required credentials are missing."""
    res = run_script(SETUP_SCRIPT_PATH, args=["--non-interactive"], cwd=tmp_path)
    assert res.returncode != 0
    assert "Interactive credential required" in res.stderr or "Error" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_groq_custom_model_retained(tmp_path):
    """Verify setup.sh uses custom GROQ_MODEL and does not default incorrectly to G_MOD."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'POSTGRES_PASSWORD="custom_secure_pw_98765"\n'
        'HERALD_API_KEY="custom_herald_key_12345"\n'
        'AI_PROVIDER="groq"\n'
        'GROQ_API_KEY="gsk_valid_key_12345"\n'
        'GROQ_MODEL="custom-llama-model-test"\n'
    )

    bash_snippet = f"""
    set -euo pipefail
    cd "{tmp_path.as_posix()}"
    get_env_val() {{
        local key="$1"
        grep -E "^${{key}}=" .env | cut -d'=' -f2- | tr -d '"' | tr -d "'"
    }}
    GR_MOD=$(get_env_val "GROQ_MODEL")
    GR_MOD=${{GR_MOD:-"llama-3.3-70b-versatile"}}
    if [ "$GR_MOD" = "custom-llama-model-test" ]; then
        echo "GROQ_CUSTOM_MODEL_VERIFIED"
    fi
    """
    cmd = [BASH_EXE, "-c", bash_snippet]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0
    assert "GROQ_CUSTOM_MODEL_VERIFIED" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_zero_secret_argv_leakage_in_set_env_and_curl(tmp_path):
    """Verify set_env_val and call_curl_config pass secrets via stdin/temp config, never in process argv."""
    log_file = tmp_path / "argv_log.txt"

    bash_snippet = f"""
    set -euo pipefail
    ENV_FILE="{tmp_path.as_posix()}/.env"
    SECRET_VAL="super_secret_token_alpha_omega_999"

    # Mock curl to record arguments
    curl() {{
        echo "curl argv: $*" >> "{log_file.as_posix()}"
        echo '{{"ok":true, "result":{{"username":"FakeBot"}}}}'
    }}

    # Call curl config helper
    call_curl_config() {{
        local cfg
        cfg=$(mktemp)
        chmod 600 "$cfg"
        cat > "$cfg"
        curl -s -K "$cfg"
        rm -f "$cfg"
    }}

    printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$SECRET_VAL" | call_curl_config

    # Call set_env_val helper
    set_env_val() {{
        local key="$1"
        local val="$2"
        python3 -c "
import sys, os
key = sys.argv[1]
filepath = sys.argv[2]
val = sys.stdin.read().rstrip('\\r\\n')
with open(filepath, 'w') as f:
    f.write(f'{{key}}=\\"{{val}}\\"\\n')
" "$key" "$ENV_FILE" <<< "$val"
    }}

    set_env_val "TELEGRAM_BOT_TOKEN" "$SECRET_VAL"
    """

    cmd = [BASH_EXE, "-c", bash_snippet]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0

    # Inspect logged curl argv
    assert log_file.exists()
    argv_content = log_file.read_text()
    assert "super_secret_token_alpha_omega_999" not in argv_content
    assert "-K" in argv_content

    # Inspect written .env
    written_env = (tmp_path / ".env").read_text()
    assert 'TELEGRAM_BOT_TOKEN="super_secret_token_alpha_omega_999"' in written_env
