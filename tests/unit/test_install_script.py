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


def to_posix_path(p: str | Path) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/{s[0].lower()}{s[2:]}"
    return s


def run_install_script(args: list = None, env: dict = None) -> subprocess.CompletedProcess:
    """Helper to run the actual install.sh via bash subprocess."""
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
            f'export PATH="{posix_path}:$PATH"; exec "{INSTALL_SCRIPT_PATH.as_posix()}" "$@"',
            "bash",
        ] + (args or [])
    else:
        bash_cmd = [BASH_EXE, str(INSTALL_SCRIPT_PATH)] + (args or [])

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
@pytest.mark.parametrize(
    "bad_path",
    [
        ".",
        "..",
        "/tmp/foo/..",
    ],
)
def test_directory_safety_canonicalization(tmp_path, bad_path):
    """Verify install.sh canonicalizes paths and rejects unsafe resolved locations."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    res = run_install_script(
        args=["--install-dir", bad_path],
        env={"HERALD_TEST_OS_RELEASE": str(os_release), "HERALD_TEST_ARCH": "x86_64"},
    )
    assert res.returncode != 0
    assert "unsafe location" in res.stderr or "is invalid" in res.stderr or "is unsafe" in res.stderr


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reinstall_mode_dirty_tracked_no_force_fails(tmp_path):
    """Verify --reinstall requires --force when tracked files are modified."""
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
    (repo_dir / "tracked.txt").write_text("initial")
    subprocess.run(["git", "-C", str(repo_dir), "add", "tracked.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial", "--no-gpg-sign"], check=True, capture_output=True)
    (repo_dir / "tracked.txt").write_text("modified uncommitted")

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


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reinstall_mode_dirty_untracked_no_force_fails(tmp_path):
    """Verify --reinstall requires --force when untracked files are present."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    repo_dir = tmp_path / "herald_untracked_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", "https://github.com/upstatedatasystems-llc/herald.git"],
        check=True,
        capture_output=True,
    )
    (repo_dir / "untracked.txt").write_text("untracked junk")

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


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reinstall_forced_cleans_source_and_preserves_env(tmp_path):
    """Verify --reinstall --force discards tracked/untracked changes, preserves .env, and restores clean tree."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    # Create bare upstream repo
    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    # Create local clone
    repo_dir = tmp_path / "herald_local_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)

    (repo_dir / ".gitignore").write_text(".env\n")
    (repo_dir / "compose.yaml").write_text("services: {}\n")
    (repo_dir / "setup.sh").write_text("#!/usr/bin/env bash\necho 'Setup mock'\n")
    (repo_dir / "scripts").mkdir()
    (repo_dir / "scripts" / "install_acceptance.sh").write_text("#!/usr/bin/env bash\necho 'Acceptance mock'\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v1.0.0", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "HEAD:main"], check=True, capture_output=True)
    expected_sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    # Now dirty the local repo
    (repo_dir / "compose.yaml").write_text("services: { dirty: true }\n")
    (repo_dir / "rogue_untracked.txt").write_text("should be deleted")
    env_file = repo_dir / ".env"
    env_file.write_text('TELEGRAM_BOT_TOKEN="preserve-me-secret-123"\n')
    try:
        env_file.chmod(0o600)
    except Exception:
        pass

    # Fake bin for docker info / build
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"build\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    path_env = f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"

    res = run_install_script(
        args=["--reinstall", "--force", "--install-dir", str(repo_dir), "--repo", str(bare_remote)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
            "HERALD_TEST_ALLOW_FILE_REPO": "1",
            "PATH": path_env,
        },
    )

    assert f"Reinstalled clean source at commit {expected_sha}" in res.stdout
    assert (repo_dir / ".env").exists()
    assert 'TELEGRAM_BOT_TOKEN="preserve-me-secret-123"' in (repo_dir / ".env").read_text()
    assert not (repo_dir / "rogue_untracked.txt").exists()
    assert "dirty: true" not in (repo_dir / "compose.yaml").read_text()
    status_out = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert status_out == ""


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_install_normal_clone_success_and_ref_selection(tmp_path):
    """Verify normal install clones repo, checks out branch/tag/sha, builds images, and runs setup/acceptance."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    # Create bare remote
    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    # Seed remote with main and a feature branch
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(seed_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)
    (seed_dir / "compose.yaml").write_text("services: {}\n")
    (seed_dir / "setup.sh").write_text("#!/usr/bin/env bash\necho 'Setup executed'\n")
    (seed_dir / "scripts").mkdir()
    (seed_dir / "scripts" / "install_acceptance.sh").write_text("#!/usr/bin/env bash\necho 'Acceptance executed'\n")
    subprocess.run(["git", "-C", str(seed_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "commit", "-m", "initial", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "push", "origin", "HEAD:main"], check=True, capture_output=True)

    # Create feature branch
    subprocess.run(["git", "-C", str(seed_dir), "checkout", "-b", "feature/test-branch"], check=True, capture_output=True)
    (seed_dir / "feature.txt").write_text("feature content")
    subprocess.run(["git", "-C", str(seed_dir), "add", "feature.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "commit", "-m", "feature commit", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "push", "origin", "HEAD:feature/test-branch"], check=True, capture_output=True)
    feature_sha = subprocess.run(
        ["git", "-C", str(seed_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path_env = f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"

    target_dir = tmp_path / "installed_herald"
    res = run_install_script(
        args=["--install-dir", str(target_dir), "--repo", str(bare_remote), "--ref", "feature/test-branch"],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
            "HERALD_TEST_ALLOW_FILE_REPO": "1",
            "PATH": path_env,
        },
    )
    assert res.returncode == 0
    assert f"Checked out commit {feature_sha}" in res.stdout
    assert "Herald installation and acceptance checks passed!" in res.stdout
    assert (target_dir / "feature.txt").exists()


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_install_setup_failure_propagates(tmp_path):
    """Verify that failure in setup.sh causes install.sh to exit non-zero without claiming success."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(seed_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)
    (seed_dir / "compose.yaml").write_text("services: {}\n")
    # Setup fails deliberately
    (seed_dir / "setup.sh").write_text("#!/usr/bin/env bash\necho 'Setup failed!' >&2\nexit 1\n")
    (seed_dir / "scripts").mkdir()
    (seed_dir / "scripts" / "install_acceptance.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "-C", str(seed_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "commit", "-m", "initial", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "push", "origin", "HEAD:main"], check=True, capture_output=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path_env = f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"

    target_dir = tmp_path / "installed_herald_fail"
    res = run_install_script(
        args=["--install-dir", str(target_dir), "--repo", str(bare_remote)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
            "HERALD_TEST_ALLOW_FILE_REPO": "1",
            "PATH": path_env,
        },
    )
    assert res.returncode != 0
    assert "Herald installation and acceptance checks passed!" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_install_acceptance_failure_propagates(tmp_path):
    """Verify that failure in install_acceptance.sh causes install.sh to exit non-zero."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(seed_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)
    (seed_dir / "compose.yaml").write_text("services: {}\n")
    (seed_dir / "setup.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (seed_dir / "scripts").mkdir()
    # Acceptance fails deliberately
    (seed_dir / "scripts" / "install_acceptance.sh").write_text("#!/usr/bin/env bash\necho 'Acceptance check failed!' >&2\nexit 1\n")
    subprocess.run(["git", "-C", str(seed_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "commit", "-m", "initial", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "push", "origin", "HEAD:main"], check=True, capture_output=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path_env = f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"

    target_dir = tmp_path / "installed_herald_acc_fail"
    res = run_install_script(
        args=["--install-dir", str(target_dir), "--repo", str(bare_remote)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
            "HERALD_TEST_ALLOW_FILE_REPO": "1",
            "PATH": path_env,
        },
    )
    assert res.returncode != 0
    assert "Herald installation and acceptance checks passed!" not in res.stdout


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reinstall_backup_cleaned_up_on_success_and_failure(tmp_path):
    """Verify reinstall temporary .env backup is removed on success and on git operation failure."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    # Create bare upstream repo
    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    # Create local clone
    repo_dir = tmp_path / "herald_reinstall_cleanup_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)

    (repo_dir / ".gitignore").write_text(".env\n")
    (repo_dir / "compose.yaml").write_text("services: {}\n")
    (repo_dir / "setup.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (repo_dir / "scripts").mkdir()
    (repo_dir / "scripts" / "install_acceptance.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v1.0", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "HEAD:main"], check=True, capture_output=True)

    env_file = repo_dir / ".env"
    env_file.write_text('SECRET_TOKEN="very_secret_12345"\n')

    isolated_tmp = tmp_path / "custom_tmp"
    isolated_tmp.mkdir(parents=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"info\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"compose\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path_env = f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"

    # 1. Successful reinstall -> no temp backup remains in TMPDIR, .env preserved
    res = run_install_script(
        args=["--reinstall", "--force", "--install-dir", str(repo_dir), "--repo", str(bare_remote)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
            "HERALD_TEST_ALLOW_FILE_REPO": "1",
            "TMPDIR": isolated_tmp.as_posix(),
            "PATH": path_env,
        },
    )
    assert res.returncode == 0
    assert "Herald installation and acceptance checks passed!" in res.stdout
    assert env_file.exists()
    assert "very_secret_12345" in env_file.read_text()
    assert list(isolated_tmp.iterdir()) == []


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_reinstall_backup_cleaned_up_on_git_reset_or_clean_failure(tmp_path):
    """Verify reinstall temporary .env backup is cleaned up via trap when git reset or clean fails."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    repo_dir = tmp_path / "herald_fail_cleanup_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)

    (repo_dir / ".gitignore").write_text(".env\n")
    (repo_dir / "compose.yaml").write_text("services: {}\n")
    (repo_dir / "setup.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (repo_dir / "scripts").mkdir()
    (repo_dir / "scripts" / "install_acceptance.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v1.0", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "HEAD:main"], check=True, capture_output=True)

    env_file = repo_dir / ".env"
    env_file.write_text('SECRET_TOKEN="secret_before_git_failure"\n')

    isolated_tmp = tmp_path / "custom_tmp_fail"
    isolated_tmp.mkdir(parents=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)

    real_git = shutil.which("git")
    posix_real_git = to_posix_path(real_git) if real_git else "git"
    (fake_bin / "git").write_text(
        f"#!/usr/bin/env bash\n"
        f"if [ \"$1\" = \"reset\" ] && [ \"$2\" = \"--hard\" ]; then\n"
        f"    echo 'Simulated git reset fatal error' >&2\n"
        f"    exit 1\n"
        f"fi\n"
        f"exec \"{posix_real_git}\" \"$@\"\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path_env = f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"

    res = run_install_script(
        args=["--reinstall", "--force", "--install-dir", str(repo_dir), "--repo", str(bare_remote)],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
            "HERALD_TEST_ALLOW_FILE_REPO": "1",
            "TMPDIR": isolated_tmp.as_posix(),
            "PATH": path_env,
        },
    )
    assert res.returncode != 0
    # Trap must have removed the temporary backup file
    assert list(isolated_tmp.iterdir()) == []


@pytest.mark.skipif(BASH_EXE is None, reason="Bash shell not available on host")
def test_install_docker_group_continuation_via_sg(tmp_path):
    """Verify install.sh seamlessly re-execs via sg docker when first invocation gets permission denied,
    and preserves all arguments including ref, non-interactive, and install paths containing spaces.
    """
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    # Upstream bare repo
    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    # Seed source repo
    seed_dir = tmp_path / "seed_repo"
    seed_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(seed_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)

    # Copy actual install.sh into seed repo so on-disk re-execution runs the real script
    shutil.copy2(INSTALL_SCRIPT_PATH, seed_dir / "install.sh")
    (seed_dir / "compose.yaml").write_text("services: {}\n")
    (seed_dir / "setup.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Setup successfully called' >> setup_ran.log\n"
        "exit 0\n"
    )
    (seed_dir / "scripts").mkdir()
    (seed_dir / "scripts" / "install_acceptance.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Acceptance passed' >> acceptance_ran.log\n"
        "exit 0\n"
    )

    subprocess.run(["git", "-C", str(seed_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "commit", "-m", "initial", "--no-gpg-sign"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "push", "origin", "HEAD:main"], check=True, capture_output=True)

    # Target dir containing SPACES
    target_dir = tmp_path / "my install target" / "herald"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    sg_log = tmp_path / "sg_invocations.log"

    # Fake docker: fails permission check unless HERALD_SG_ACTIVE is set
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"compose\" ] && [ \"$2\" = \"version\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"info\" ]; then\n"
        "    if [ \"${HERALD_SG_ACTIVE:-}\" = \"1\" ]; then\n"
        "        exit 0\n"
        "    else\n"
        "        echo 'permission denied while trying to connect to the Docker daemon socket' >&2\n"
        "        exit 1\n"
        "    fi\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    # Fake sg: logs invocation and executes the command string
    (fake_bin / "sg").write_text(
        f"#!/usr/bin/env bash\n"
        f"echo \"$*\" >> \"{sg_log.as_posix()}\"\n"
        f"if [ \"$1\" = \"docker\" ] && [ \"$2\" = \"-c\" ]; then\n"
        f"    shift 2\n"
        f"    eval \"$@\"\n"
        f"fi\n",
        encoding="utf-8",
    )

    # Fake id: reports current user belongs to docker group
    (fake_bin / "id").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-un\" ]; then echo 'heralduser'; exit 0; fi\n"
        "if [ \"$1\" = \"-nG\" ]; then echo 'heralduser docker sudo'; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    path_env = f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}"

    res = run_install_script(
        args=[
            "--install-dir", str(target_dir),
            "--ref", "main",
            "--repo", str(bare_remote),
            "--non-interactive",
        ],
        env={
            "HERALD_TEST_OS_RELEASE": str(os_release),
            "HERALD_TEST_ARCH": "x86_64",
            "HERALD_TEST_AVAIL_MB": "10000",
            "HERALD_TEST_ALLOW_FILE_REPO": "1",
            "PATH": path_env,
        },
    )

    assert res.returncode == 0
    assert "Activating docker group session..." in res.stdout
    assert "Herald installation and acceptance checks passed!" in res.stdout
    assert (target_dir / "setup_ran.log").exists()
    assert (target_dir / "acceptance_ran.log").exists()

    sg_calls = sg_log.read_text()
    assert "docker -c" in sg_calls
    assert "--internal-docker-stage" in sg_calls
    assert "my\\ install\\ target" in sg_calls or "my install target" in sg_calls
    assert "--non-interactive" in sg_calls
