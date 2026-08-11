import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("herald.n8n")

PLACEHOLDER_CREDENTIAL_IDS = {"1", "default-id", "placeholder-id", ""}
DEFAULT_ERROR_HANDLER_NAME = "Herald - System Error Handler"


def rehydrate_workflow_credentials(
    workflow_data: dict[str, Any],
    installed_credentials: dict[str, dict[str, str]],
    workflow_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Preserve and rehydrate target environment credential references during workflow deployment/updates.
    Prevents hardcoded or default credential IDs from overwriting installed production OAuth credentials.
    Also resolves workflow errorWorkflow settings from workflow names to installed workflow IDs.
    """
    import copy
    updated_wf = copy.deepcopy(workflow_data)
    nodes = updated_wf.get("nodes", [])

    for node in nodes:
        node_creds = node.get("credentials")
        if not node_creds or not isinstance(node_creds, dict):
            continue

        for cred_type, cred_info in node_creds.items():
            if not isinstance(cred_info, dict):
                continue

            target_cred = installed_credentials.get(cred_type)
            if not target_cred:
                # Check normalized credential type fallback (e.g. googleDriveOAuth2 <-> googleDriveOAuth2Api)
                if cred_type == "googleDriveOAuth2Api" and "googleDriveOAuth2" in installed_credentials:
                    target_cred = installed_credentials["googleDriveOAuth2"]
                elif cred_type == "googleDriveOAuth2" and "googleDriveOAuth2Api" in installed_credentials:
                    target_cred = installed_credentials["googleDriveOAuth2Api"]

            if target_cred and isinstance(target_cred, dict):
                if "id" in target_cred and target_cred["id"]:
                    cred_info["id"] = target_cred["id"]
                if "name" in target_cred and target_cred["name"]:
                    cred_info["name"] = target_cred["name"]

    # Rehydrate errorWorkflow setting from workflow name to installed workflow ID
    settings = updated_wf.get("settings")
    if settings and isinstance(settings, dict):
        err_wf = settings.get("errorWorkflow")
        if err_wf and workflow_id_map:
            # If errorWorkflow matches a workflow name in the map, resolve to ID
            if err_wf in workflow_id_map:
                settings["errorWorkflow"] = workflow_id_map[err_wf]
            elif DEFAULT_ERROR_HANDLER_NAME in workflow_id_map:
                settings["errorWorkflow"] = workflow_id_map[DEFAULT_ERROR_HANDLER_NAME]

    # Rehydrate workflow ID if mapped
    wf_name = updated_wf.get("name")
    if wf_name and workflow_id_map and wf_name in workflow_id_map:
        updated_wf["id"] = workflow_id_map[wf_name]

    return updated_wf


def validate_workflow_for_deployment(workflow_data: dict[str, Any]) -> bool:
    """
    Strictly validate that no placeholder credential IDs remain and errorWorkflow settings use workflow IDs.
    Fails closed by raising ValueError if validation checks fail.
    """
    wf_name = workflow_data.get("name", "Unnamed Workflow")
    nodes = workflow_data.get("nodes", [])

    for node in nodes:
        node_name = node.get("name", "Unnamed Node")
        node_creds = node.get("credentials")
        if node_creds and isinstance(node_creds, dict):
            for cred_type, cred_info in node_creds.items():
                if isinstance(cred_info, dict):
                    cid = str(cred_info.get("id", "")).strip()
                    if cid in PLACEHOLDER_CREDENTIAL_IDS:
                        raise ValueError(
                            f"Workflow '{wf_name}' node '{node_name}' requires credential for '{cred_type}' "
                            f"but contains unresolved placeholder credential ID '{cid}'"
                        )

    # Validate errorWorkflow is a resolved ID, not a raw workflow name
    settings = workflow_data.get("settings")
    if settings and isinstance(settings, dict):
        err_wf = settings.get("errorWorkflow")
        if err_wf == DEFAULT_ERROR_HANDLER_NAME:
            raise ValueError(
                f"Workflow '{wf_name}' settings.errorWorkflow contains unresolved workflow name '{err_wf}' instead of an installed workflow ID"
            )

    return True


def validate_workflow_credentials(workflow_data: dict[str, Any]) -> bool:
    """
    Legacy helper function. Returns True if workflow credentials pass validation, False otherwise.
    """
    try:
        return validate_workflow_for_deployment(workflow_data)
    except ValueError:
        return False
