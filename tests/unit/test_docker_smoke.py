from pathlib import Path


def test_dockerfiles_structure():
    """Verify Dockerfile.api and Dockerfile.worker copy correct directory structure and do not reference packages/."""
    root_dir = Path(__file__).parent.parent.parent
    docker_dir = root_dir / "docker"
    compose_file = root_dir / "compose.yaml"

    assert docker_dir.exists()
    assert (docker_dir / "Dockerfile.api").exists()
    assert (docker_dir / "Dockerfile.worker").exists()
    assert compose_file.exists()

    api_content = (docker_dir / "Dockerfile.api").read_text(encoding="utf-8")
    worker_content = (docker_dir / "Dockerfile.worker").read_text(encoding="utf-8")
    compose_content = compose_file.read_text(encoding="utf-8")

    assert "packages/" not in api_content
    assert "packages/" not in worker_content
    assert "packages/" not in compose_content

    assert "COPY herald/ ./herald/" in api_content
    assert "COPY herald/ ./herald/" in worker_content
    assert "COPY apps/ ./apps/" in api_content
    assert "COPY apps/ ./apps/" in worker_content
