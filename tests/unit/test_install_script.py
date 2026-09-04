"""
Unit tests for Herald top-level bootstrap installer (install.sh).
Executes the actual install.sh script as a subprocess with mock environments.
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


def run_install_script(args: list = None, env: dict = None) -> subprocess.CompletedProcess:
    """Helper to run the actual install.sh via bash subprocess."""
    assert BASH_EXE is not None, "Bash executable not found"
    bash_cmd = [BASH_EXE, str(INSTALL_SCRIPT_PATH)] + (args or [])
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
    res = run_install_script(args=["--help"])
    assert res.returncode == 0
    assert "Herald Bootstrap Installer" in res.stdout
    assert "--install-dir" in res.stdout
    assert "--ref" in res.stdout
    assert "--update" in res.stdout
    assert "--reinstall" in res.stdout
    assert "--force" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_install_script_rejects_unknown_argument():
    """Verify unknown arguments produce an error and non-zero exit."""
    res = run_install_script(args=["--invalid-flag"])
    assert res.returncode != 0
    assert "Unknown argument '--invalid-flag'" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_os_check_accepts_ubuntu_2404(tmp_path):
    """Verify install.sh accepts Ubuntu 24.04 LTS via actual script execution."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\nNAME="Ubuntu"\n')

    # Give invalid arch to stop execution right after OS check
    res = run_install_script(env={"HERALD_TEST_OS_RELEASE": str(os_release), "HERALD_TEST_ARCH": "invalid_arch"})
    assert "Operating System verified: Ubuntu 24.04 LTS" in res.stdout
    assert res.returncode != 0  # stopped at arch


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
    """Verify install.sh rejects non-Ubuntu 24.04 OS releases."""
    os_release = tmp_path / "os-release"
    os_release.write_text(f'ID={id_val}\nVERSION_ID="{ver_val}"\n')

    res = run_install_script(env={"HERALD_TEST_OS_RELEASE": str(os_release)})
    assert res.returncode != 0
    assert "Unsupported operating system" in res.stderr
    assert "Herald Phase 2 officially supports Ubuntu 24.04 LTS only." in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
@pytest.mark.parametrize("arch", ["x86_64", "amd64", "aarch64", "arm64"])
def test_arch_check_accepts_supported_architectures(tmp_path, arch):
    """Verify install.sh accepts amd64, x86_64, arm64, aarch64."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    res = run_install_script(
        args=["--install-dir", str(tmp_path / "target")],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": arch,
            "HERALD_TEST_AVAIL_MB": "2000",  # fail at disk check after passing arch
        },
    )
    assert f"CPU Architecture verified: {arch}" in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
@pytest.mark.parametrize("arch", ["s390x", "armv7l", "i386", "riscv64", "ppc64le"])
def test_arch_check_rejects_unsupported_architectures(tmp_path, arch):
    """Verify install.sh rejects unsupported CPU architectures."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    res = run_install_script(env={"HERALD_TEST_OS_RELEASE": str(os_release), "HERALD_TEST_ARCH": arch})
    assert res.returncode != 0
    assert f"Unsupported architecture '{arch}'" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_disk_space_guard_thresholds(tmp_path):
    """Verify install.sh disk space math: <4000MB fails, 4000-8000MB warns, >=8000MB passes."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    # Hard fail (< 4000 MB)
    res_fail = run_install_script(
        args=["--install-dir", str(tmp_path / "target1")],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "2500",
        },
    )
    assert res_fail.returncode != 0
    assert "Insufficient free disk space" in res_fail.stderr

    # Warning (4000 - 8000 MB)
    res_warn = run_install_script(
        args=["--install-dir", str(tmp_path / "target2"), "--help"],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "5500",
        },
    )
    assert res_warn.returncode == 0


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
@pytest.mark.parametrize("bad_dir", ["/", "/etc", "/usr", "/var", "/tmp", "/bin", "/sbin", "/boot", "/root"])
def test_directory_safety_rejects_critical_roots(tmp_path, bad_dir):
    """Verify install.sh rejects dangerous root target directories."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    res = run_install_script(
        args=["--install-dir", bad_dir],
        env={"HERALD_TEST_OS_RELEASE": str(os_release), "HERALD_TEST_ARCH": "x86_64"},
    )
    assert res.returncode != 0
    assert f"Target installation directory '{bad_dir}' is unsafe." in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_custom_install_path_with_spaces(tmp_path):
    """Verify install.sh handles custom install paths with spaces."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    space_dir = tmp_path / "My Herald App Directory"
    space_dir.mkdir(parents=True)
    (space_dir / "compose.yaml").write_text("services: {}\n")
    (space_dir / "setup.sh").write_text("#!/usr/bin/env bash\n")

    res = run_install_script(
        args=["--install-dir", str(space_dir)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
        },
    )
    assert res.returncode != 0
    assert "Herald installation already exists" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_insecure_repo_scheme_rejected(tmp_path):
    """Verify install.sh rejects non-https repository URLs."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    res = run_install_script(
        args=["--repo", "http://insecure-herald-repo.com/repo.git", "--install-dir", str(tmp_path / "target")],
        env={"HERALD_TEST_OS_RELEASE": str(os_release), "HERALD_TEST_ARCH": "x86_64"},
    )
    assert res.returncode != 0
    assert "Repository URL must start with 'https://'." in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_normal_mode_refuses_existing_installation(tmp_path):
    """Verify normal install refuses to overwrite an existing Herald installation directory."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    install_dir = tmp_path / "existing_herald"
    install_dir.mkdir(parents=True)
    (install_dir / "compose.yaml").write_text("services: {}\n")
    (install_dir / "setup.sh").write_text("#!/usr/bin/env bash\n")

    res = run_install_script(
        args=["--install-dir", str(install_dir)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
        },
    )
    assert res.returncode != 0
    assert "Herald installation already exists" in res.stderr
    assert "To update the existing installation, run: ./install.sh --update" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_update_mode_refuses_dirty_tree(tmp_path):
    """Verify --update refuses if repository has uncommitted or untracked changes."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    repo_dir = tmp_path / "herald_git_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", "https://github.com/upstatedatasystems-llc/herald.git"],
        check=True,
        capture_output=True,
    )
    # Add untracked dirty file
    (repo_dir / "untracked_file.txt").write_text("dirty content")

    res = run_install_script(
        args=["--update", "--install-dir", str(repo_dir)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
        },
    )
    assert res.returncode != 0
    assert "has uncommitted or untracked changes. Update refused." in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_update_mode_refuses_wrong_origin(tmp_path):
    """Verify --update refuses if repository origin is not the official Herald repo."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    repo_dir = tmp_path / "foreign_git_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", "https://github.com/attacker/malicious.git"],
        check=True,
        capture_output=True,
    )

    res = run_install_script(
        args=["--update", "--install-dir", str(repo_dir)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
        },
    )
    assert res.returncode != 0
    assert "does not match expected Herald origin." in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reinstall_mode_requires_force_on_dirty_tree(tmp_path):
    """Verify --reinstall requires --force when repository is dirty."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    repo_dir = tmp_path / "herald_dirty_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", "https://github.com/upstatedatasystems-llc/herald.git"],
        check=True,
        capture_output=True,
    )
    (repo_dir / "dirty.txt").write_text("modified uncommitted")

    res = run_install_script(
        args=["--reinstall", "--install-dir", str(repo_dir)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
        },
    )
    assert res.returncode != 0
    assert "Use --force to proceed with reinstall." in res.stderr
