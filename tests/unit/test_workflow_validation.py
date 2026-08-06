import json
from pathlib import Path


def test_validate_all_n8n_workflows():
    """
    Validate that all 7 n8n workflow files exist, contain valid JSON,
    and have valid nodes and connections structure.
    """
    workflows_dir = Path(__file__).parent.parent.parent / "n8n" / "workflows"
    assert workflows_dir.exists(), "n8n/workflows directory must exist"

    workflow_files = list(workflows_dir.glob("*.json"))
    assert len(workflow_files) >= 7, f"Expected at least 7 workflow files, found {len(workflow_files)}"

    required_workflows = [
        "completion-dispatcher.json",
        "daily-cleanup.json",
        "daily-health-report.json",
        "email-intake.json",
        "error-handler.json",
        "stale-job-recovery.json",
        "weekly-maintenance.json",
    ]

    found_names = [f.name for f in workflow_files]
    for req in required_workflows:
        assert req in found_names, f"Missing required workflow: {req}"

    for wf_file in workflow_files:
        content = wf_file.read_text(encoding="utf-8")
        data = json.loads(content)

        assert "name" in data, f"{wf_file.name} missing 'name' property"
        assert "nodes" in data, f"{wf_file.name} missing 'nodes' array"
        assert "connections" in data, f"{wf_file.name} missing 'connections' object"
        assert isinstance(data["nodes"], list), f"{wf_file.name} 'nodes' must be a list"
        assert len(data["nodes"]) > 0, f"{wf_file.name} must have at least 1 node"
