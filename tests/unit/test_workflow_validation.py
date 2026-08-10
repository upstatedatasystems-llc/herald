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

        if wf_file.name == "completion-dispatcher.json":
            nodes_by_name = {n["name"]: n for n in data["nodes"]}
            assert "Schedule Trigger - 1 Min Poll" in nodes_by_name
            assert "Webhook Trigger - Audio Ready Nudge" in nodes_by_name
            assert "Validate Nudge Header Auth" in nodes_by_name

            assert "Check Needs Details Upload" in nodes_by_name
            assert "Read Local Details MD File" in nodes_by_name
            assert "Upload Details to Google Drive" in nodes_by_name
            assert "Record Details Drive Metadata" in nodes_by_name

            assert "Fetch Final Completion Email" in nodes_by_name
            fetch_node = nodes_by_name["Fetch Final Completion Email"]
            assert "/completion-email" in fetch_node["parameters"]["url"]

            assert "Send Link-Only Reply" in nodes_by_name
            reply_node = nodes_by_name["Send Link-Only Reply"]
            assert reply_node["parameters"].get("emailType") == "html"
            assert "html" in reply_node["parameters"]["message"]

            assert "Record Delivery Complete" in nodes_by_name
            assert "Read Final Details MD File" in nodes_by_name
            assert "Update Details in Google Drive" in nodes_by_name
            assert nodes_by_name["Update Details in Google Drive"]["parameters"].get("operation") == "update"
            assert "Record Details Finalized" in nodes_by_name

        if wf_file.name == "email-intake.json":
            nodes_by_name = {n["name"]: n for n in data["nodes"]}
            trigger_node = nodes_by_name["Gmail Trigger - Herald Intake"]
            assert "-from:upstatedatasystems@gmail.com" in trigger_node["parameters"]["filters"]["q"]

            ack_node = nodes_by_name["Send Submission Acknowledgment"]
            assert ack_node["parameters"].get("emailType") == "html"
            assert "acknowledgment_email_html" in ack_node["parameters"]["message"]
