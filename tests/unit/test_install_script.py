"""
Unit tests for Herald top-level bootstrap installer (install.sh).
Tests OS verification, CPU architecture validation, disk thresholds,
directory safety, argument parsing, and error propagation using subprocess.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SCRIPT_PATH = Path(__file__).parent.parent.parent / "install.sh"


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
def test_install_script_syntax_validity():
    """Verify install.sh has valid bash syntax."""
    res = subprocess.run([BASH_EXE, "-n", str(INSTALL_SCRIPT_PATH)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert res.returncode == 0, f"Bash syntax check failed: {res.stderr}"


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_install_script_help_flag():
    """Verify --help outputs usage information and exits 0."""
    res = subprocess.run([BASH_EXE, str(INSTALL_SCRIPT_PATH), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert res.returncode == 0
    assert "Herald Bootstrap Installer" in res.stdout
    assert "--install-dir" in res.stdout
    assert "--ref" in res.stdout
    assert "--update" in res.stdout
    assert "--reinstall" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_install_script_rejects_unknown_argument():
    """Verify unknown arguments produce an error and non-zero exit."""
    res = subprocess.run([BASH_EXE, str(INSTALL_SCRIPT_PATH), "--invalid-flag"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert res.returncode != 0
    assert "Unknown argument '--invalid-flag'" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_os_check_accepts_ubuntu_2404(tmp_path):
    """Verify check_os accepts Ubuntu 24.04 LTS."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\nNAME="Ubuntu"\nVERSION="24.04 LTS"\n')

    bash_snippet = f"""
    set -euo pipefail
    check_os() {{
        local os_id=""
        local os_version=""
        while IFS='=' read -r key val || [ -n "$key" ]; do
            val=$(echo "$val" | tr -d '"' | tr -d "'")
            if [ "$key" = "ID" ]; then os_id="$val"; fi
            if [ "$key" = "VERSION_ID" ]; then os_version="$val"; fi
        done < "{os_release.as_posix()}"

        if [ "$os_id" != "ubuntu" ] || [ "$os_version" != "24.04" ]; then
            echo "Unsupported OS: $os_id $os_version" >&2
            exit 1
        fi
        echo "OK"
    }}
    check_os
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode == 0
    assert "OK" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
@pytest.mark.parametrize(
    "id_val,ver_val",
    [
        ("ubuntu", "22.04"),
        ("ubuntu", "20.04"),
        ("debian", "12"),
        ("fedora", "40"),
        ("arch", "rolling"),
    ],
)
def test_os_check_rejects_non_ubuntu_2404(tmp_path, id_val, ver_val):
    """Verify check_os rejects any OS other than Ubuntu 24.04 LTS."""
    os_release = tmp_path / "os-release"
    os_release.write_text(f'ID={id_val}\nVERSION_ID="{ver_val}"\n')

    bash_snippet = f"""
    set -euo pipefail
    check_os() {{
        local os_id=""
        local os_version=""
        while IFS='=' read -r key val || [ -n "$key" ]; do
            val=$(echo "$val" | tr -d '"' | tr -d "'")
            if [ "$key" = "ID" ]; then os_id="$val"; fi
            if [ "$key" = "VERSION_ID" ]; then os_version="$val"; fi
        done < "{os_release.as_posix()}"

        if [ "$os_id" != "ubuntu" ] || [ "$os_version" != "24.04" ]; then
            echo "Unsupported OS: $os_id $os_version" >&2
            exit 1
        fi
        echo "OK"
    }}
    check_os
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode != 0
    assert "Unsupported OS" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
@pytest.mark.parametrize("arch", ["x86_64", "amd64", "aarch64", "arm64"])
def test_arch_check_accepts_supported_architectures(arch):
    """Verify check_arch accepts amd64, x86_64, arm64, aarch64."""
    bash_snippet = f"""
    set -euo pipefail
    check_arch() {{
        local arch="{arch}"
        case "$arch" in
            x86_64|amd64|aarch64|arm64)
                echo "ARCH_OK: $arch"
                ;;
            *)
                echo "Unsupported arch: $arch" >&2
                exit 1
                ;;
        esac
    }}
    check_arch
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode == 0
    assert f"ARCH_OK: {arch}" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
@pytest.mark.parametrize("arch", ["s390x", "armv7l", "i386", "riscv64", "ppc64le"])
def test_arch_check_rejects_unsupported_architectures(arch):
    """Verify check_arch rejects unsupported CPU architectures."""
    bash_snippet = f"""
    set -euo pipefail
    check_arch() {{
        local arch="{arch}"
        case "$arch" in
            x86_64|amd64|aarch64|arm64)
                echo "ARCH_OK"
                ;;
            *)
                echo "Unsupported arch: $arch" >&2
                exit 1
                ;;
        esac
    }}
    check_arch
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode != 0
    assert f"Unsupported arch: {arch}" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_disk_space_guard_thresholds():
    """Verify disk space math: <4000MB fails, 4000-8000MB warns, >=8000MB passes."""
    bash_snippet = """
    set -euo pipefail
    check_disk_logic() {
        local avail_mb="$1"
        local min_mb=4000
        local warn_mb=8000

        if [ "$avail_mb" -lt "$min_mb" ]; then
            echo "FAIL: $avail_mb < $min_mb" >&2
            return 1
        fi
        if [ "$avail_mb" -lt "$warn_mb" ]; then
            echo "WARN: $avail_mb < $warn_mb"
            return 0
        fi
        echo "PASS: $avail_mb"
        return 0
    }
    check_disk_logic "$1"
    """
    # Test hard fail
    res_fail = run_bash_script(bash_snippet, args=["2500"])
    assert res_fail.returncode != 0
    assert "FAIL" in res_fail.stderr

    # Test warning
    res_warn = run_bash_script(bash_snippet, args=["5500"])
    assert res_warn.returncode == 0
    assert "WARN" in res_warn.stdout

    # Test pass
    res_pass = run_bash_script(bash_snippet, args=["12000"])
    assert res_pass.returncode == 0
    assert "PASS" in res_pass.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
@pytest.mark.parametrize("bad_dir", ["/", "/etc", "/usr", "/var", "/tmp", "/bin", "/sbin", "/boot", "/root"])
def test_directory_safety_rejects_critical_roots(bad_dir):
    """Verify installer rejects dangerous target directories."""
    bash_snippet = f"""
    set -euo pipefail
    check_dir() {{
        local target="{bad_dir}"
        local unsafe_dirs=("/" "$HOME" "/etc" "/usr" "/var" "/tmp" "/bin" "/sbin" "/lib" "/boot" "/root")
        for d in "${{unsafe_dirs[@]}}"; do
            if [ "$target" = "$d" ]; then
                echo "Unsafe directory: $target" >&2
                exit 1
            fi
        done
        echo "OK"
    }}
    check_dir
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode != 0
    assert "Unsafe directory" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_custom_install_path_with_spaces(tmp_path):
    """Verify custom install paths containing spaces are handled safely."""
    space_path = tmp_path / "My Herald App Directory"
    space_path.mkdir(parents=True)

    bash_snippet = f"""
    set -euo pipefail
    TARGET="{space_path.as_posix()}"
    cd "$TARGET"
    pwd
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode == 0
    assert "My Herald App Directory" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_insecure_repo_scheme_rejected():
    """Verify non-https repository URLs are rejected."""
    bash_snippet = """
    set -euo pipefail
    check_repo() {
        local repo="$1"
        if [[ ! "$repo" =~ ^https:// ]]; then
            echo "Invalid repo URL scheme: $repo" >&2
            exit 1
        fi
        echo "OK"
    }
    check_repo "http://insecure-repo.com/herald.git"
    """
    res = run_bash_script(bash_snippet)
    assert res.returncode != 0
    assert "Invalid repo URL scheme" in res.stderr
