def rehydrate_workflow_credentials(
    workflow_data: dict,
    installed_credentials: dict[str, dict[str, str]],
) -> dict:
    """
    Preserve and rehydrate target environment credential references during workflow deployment/updates.
    Prevents hardcoded or default credential IDs from overwriting installed production OAuth credentials.

    :param workflow_data: Raw n8n workflow JSON dictionary.
    :param installed_credentials: Map of credential type -> {'id': ..., 'name': ...}
    :return: Updated workflow JSON dictionary with environment credential IDs rehydrated.
    """
    updated_wf = dict(workflow_data)
    nodes = updated_wf.get("nodes", [])

    for node in nodes:
        node_creds = node.get("credentials")
        if not node_creds or not isinstance(node_creds, dict):
            continue

        for cred_type, cred_info in node_creds.items():
            if cred_type in installed_credentials:
                env_cred = installed_credentials[cred_type]
                if isinstance(cred_info, dict):
                    cred_info["id"] = env_cred.get("id", cred_info.get("id"))
                    if "name" in env_cred:
                        cred_info["name"] = env_cred["name"]

    return updated_wf


def validate_workflow_credentials(workflow_data: dict) -> bool:
    """
    Validate that all nodes in the workflow requiring credentials have valid non-empty credential IDs.
    Returns True if valid, False otherwise.
    """
    nodes = workflow_data.get("nodes", [])
    for node in nodes:
        node_creds = node.get("credentials")
        if node_creds and isinstance(node_creds, dict):
            for cred_type, cred_info in node_creds.items():
                if isinstance(cred_info, dict):
                    cid = str(cred_info.get("id", "")).strip()
                    if not cid:
                        return False
    return True
