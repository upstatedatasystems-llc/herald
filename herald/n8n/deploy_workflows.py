import argparse
import json
import logging
import os
import sys
from pathlib import Path

from herald.n8n.credential_rehydrator import (
    rehydrate_workflow_credentials,
    validate_workflow_for_deployment,
)

logger = logging.getLogger("herald.n8n.deploy")


def load_installed_credentials_from_env() -> dict[str, dict[str, str]]:
    """
    Build credential mapping from environment variables if present.
    """
    creds = {}
    gmail_id = os.getenv("GMAIL_CREDENTIAL_ID")
    if gmail_id:
        creds["gmailOAuth2"] = {"id": gmail_id, "name": os.getenv("GMAIL_CREDENTIAL_NAME", "Herald Gmail Account")}

    drive_id = os.getenv("GOOGLE_DRIVE_CREDENTIAL_ID")
    if drive_id:
        creds["googleDriveOAuth2Api"] = {"id": drive_id, "name": os.getenv("GOOGLE_DRIVE_CREDENTIAL_NAME", "Herald Google Drive Account")}
        creds["googleDriveOAuth2"] = {"id": drive_id, "name": os.getenv("GOOGLE_DRIVE_CREDENTIAL_NAME", "Herald Google Drive Account")}

    return creds


def prepare_deployment_workflows(
    workflows_dir: Path | str,
    output_dir: Path | str,
    installed_credentials: dict[str, dict[str, str]],
    workflow_id_map: dict[str, str] | None = None,
) -> list[Path]:
    """
    Loads raw repo workflow JSON files, rehydrates installed credentials and errorWorkflow IDs,
    strictly validates that no placeholder IDs remain, and outputs import-ready JSON files.

    Fails closed by raising ValueError if any required credential or error workflow reference cannot be resolved.
    """
    wf_path = Path(workflows_dir)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if not wf_path.exists():
        raise FileNotFoundError(f"Workflows directory not found: {wf_path}")

    json_files = list(wf_path.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No workflow JSON files found in {wf_path}")

    prepared_files = []

    for file in json_files:
        with open(file, "r", encoding="utf-8") as f:
            raw_wf = json.load(f)

        rehydrated = rehydrate_workflow_credentials(raw_wf, installed_credentials, workflow_id_map)
        
        # Validate rehydrated workflow - raises ValueError if invalid
        validate_workflow_for_deployment(rehydrated)

        out_file = out_path / file.name
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(rehydrated, f, indent=2)

        prepared_files.append(out_file)
        logger.info(f"Successfully prepared import-ready workflow: {out_file}")

    return prepared_files


def main():
    parser = argparse.ArgumentParser(description="Herald n8n Workflow Deployment & Credential Rehydrator")
    parser.add_argument("--workflows-dir", default="n8n/workflows", help="Directory containing raw repo workflow JSONs")
    parser.add_argument("--output-dir", default="n8n/build", help="Directory to output import-ready rehydrated JSONs")
    parser.add_argument("--credentials-json", help="Path to JSON file containing installed credentials mapping")
    parser.add_argument("--error-handler-id", help="Installed workflow ID for Herald - System Error Handler")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    installed_creds = load_installed_credentials_from_env()

    if args.credentials_json and Path(args.credentials_json).exists():
        with open(args.credentials_json, "r", encoding="utf-8") as f:
            file_creds = json.load(f)
            installed_creds.update(file_creds)

    wf_map = {}
    err_id = args.error_handler_id or os.getenv("HERALD_ERROR_WORKFLOW_ID")
    if err_id:
        wf_map["Herald - System Error Handler"] = err_id

    try:
        files = prepare_deployment_workflows(args.workflows_dir, args.output_dir, installed_creds, wf_map)
        print(f"Deployment preparation succeeded. {len(files)} import-ready workflow(s) written to '{args.output_dir}'.")
    except Exception as e:
        print(f"DEPLOYMENT PREPARATION FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
