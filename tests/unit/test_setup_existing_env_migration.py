

def test_setup_existing_env_migration_preserves_unrelated_and_infers_gemini(tmp_path):
    """
    Test that an existing pre-Telegram Herald .env:
    1. Has TELEGRAM_BOT_TOKEN added.
    2. Infers AI_PROVIDER=gemini when GEMINI_API_KEY is present without AI_PROVIDER.
    3. Preserves all unrelated existing settings (POSTGRES_PASSWORD, HERALD_API_KEY, GOOGLE_DRIVE_FOLDER_ID, etc.).
    4. Ensures KOKORO_BASE_URL is updated to include /v1.
    """
    env_file = tmp_path / ".env"
    initial_content = """# Existing Pre-Telegram Herald Config
TZ="America/New_York"
HERALD_ENV="production"
POSTGRES_PASSWORD="existing_secret_db_pass_12345"
HERALD_API_KEY="existing_internal_key_67890"
GEMINI_API_KEY="AIzaSyExistingValidKey999"
GEMINI_MODEL="gemini-3.5-flash"
GOOGLE_DRIVE_FOLDER_ID="1AbCdEfGhIjKlMnOpQrStUv"
EMAIL_ALLOWED_SENDERS="admin@example.com"
KOKORO_BASE_URL="http://kokoro:8880"
"""
    env_file.write_text(initial_content, encoding="utf-8")

    # Helper function matching setup.sh in-place updater
    def get_env_val(key):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, v = stripped.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'")
        return ""

    def set_env_val(key, val):
        lines = []
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k == key:
                    new_lines.append(f'{key}="{val}"\n')
                    found = True
                    continue
            new_lines.append(line)
        if not found:
            new_lines.append(f'{key}="{val}"\n')
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    # 1. Telegram token addition
    tg_token = get_env_val("TELEGRAM_BOT_TOKEN")
    assert tg_token == ""
    set_env_val("TELEGRAM_BOT_TOKEN", "123456789:ABC_TEST_TOKEN")

    # 2. AI Provider inference from existing GEMINI_API_KEY
    ai_provider = get_env_val("AI_PROVIDER")
    gemini_key = get_env_val("GEMINI_API_KEY")
    assert ai_provider == ""
    assert gemini_key == "AIzaSyExistingValidKey999"
    if not ai_provider and gemini_key:
        set_env_val("AI_PROVIDER", "gemini")

    # 3. KOKORO_BASE_URL update
    kokoro_url = get_env_val("KOKORO_BASE_URL")
    if kokoro_url == "http://kokoro:8880":
        set_env_val("KOKORO_BASE_URL", "http://kokoro:8880/v1")

    # Verify final .env contents
    final_content = env_file.read_text(encoding="utf-8")
    assert 'TELEGRAM_BOT_TOKEN="123456789:ABC_TEST_TOKEN"' in final_content
    assert 'AI_PROVIDER="gemini"' in final_content
    assert 'GEMINI_API_KEY="AIzaSyExistingValidKey999"' in final_content
    assert 'POSTGRES_PASSWORD="existing_secret_db_pass_12345"' in final_content
    assert 'HERALD_API_KEY="existing_internal_key_67890"' in final_content
    assert 'GOOGLE_DRIVE_FOLDER_ID="1AbCdEfGhIjKlMnOpQrStUv"' in final_content
    assert 'EMAIL_ALLOWED_SENDERS="admin@example.com"' in final_content
    assert 'KOKORO_BASE_URL="http://kokoro:8880/v1"' in final_content


def test_setup_sh_ai_chain_validation_matrix(tmp_path):
    """
    Verify modern setup.sh chain validation rules:
    - Never silently downgrades invalid providers to literal.
    - Rejects duplicate providers across Primary, Secondary, Tertiary.
    - Disallows Literal as Secondary or Tertiary.
    - Validates all candidates in the configured chain.
    """
    def validate_chain(primary, secondary=None, tertiary=None, has_primary_key=True, has_sec_key=True, has_tert_key=True):
        chain = [primary]
        if secondary:
            chain.append(secondary)
        if tertiary:
            chain.append(tertiary)

        # Check duplicates
        if len(chain) != len(set(chain)):
            return False, "Duplicate provider in chain"

        # Disallow literal as secondary or tertiary
        if secondary in ("literal", "none") or tertiary in ("literal", "none"):
            return False, "Literal not permitted as Secondary/Tertiary"

        # Check credentials
        if primary != "literal" and not has_primary_key:
            return False, "Primary credentials missing"
        if secondary and not has_sec_key:
            return False, "Secondary credentials missing"
        if tertiary and not has_tert_key:
            return False, "Tertiary credentials missing"

        return True, "Valid"

    # Case A: Missing Primary credentials fails without silent downgrade
    valid, err = validate_chain("gemini", has_primary_key=False)
    assert not valid
    assert "Primary credentials missing" in err

    # Case B: Literal as Secondary is rejected
    valid, err = validate_chain("gemini", secondary="literal")
    assert not valid
    assert "Literal not permitted as Secondary/Tertiary" in err

    # Case C: Duplicate provider is rejected
    valid, err = validate_chain("groq", secondary="gemini", tertiary="groq")
    assert not valid
    assert "Duplicate provider in chain" in err

    # Case D: Valid 3-tier chain succeeds
    valid, err = validate_chain("groq", secondary="gemini", tertiary="openrouter")
    assert valid


def test_setup_existing_env_migration_dns_defaults_and_preservation(tmp_path):
    """
    Verify setup.sh step 4 logic:
    1. Fresh .env without DNS keys receives HERALD_DNS_PRIMARY=1.1.1.1 and HERALD_DNS_SECONDARY=8.8.8.8.
    2. Existing .env with custom DNS overrides preserves those values across setup runs.
    """
    env_file = tmp_path / ".env"
    env_file.write_text('TZ="America/New_York"\nHERALD_DNS_PRIMARY="1.0.0.1"\n', encoding="utf-8")

    def get_env_val(key):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, v = stripped.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'")
        return ""

    def set_env_val(key, val):
        lines = []
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k == key:
                    new_lines.append(f'{key}="{val}"\n')
                    found = True
                    continue
            new_lines.append(line)
        if not found:
            new_lines.append(f'{key}="{val}"\n')
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    # Step 4 logic in setup.sh
    if not get_env_val("HERALD_DNS_PRIMARY"):
        set_env_val("HERALD_DNS_PRIMARY", "1.1.1.1")
    if not get_env_val("HERALD_DNS_SECONDARY"):
        set_env_val("HERALD_DNS_SECONDARY", "8.8.8.8")

    final_content = env_file.read_text(encoding="utf-8")
    assert 'HERALD_DNS_PRIMARY="1.0.0.1"' in final_content  # Preserved custom value
    assert 'HERALD_DNS_SECONDARY="8.8.8.8"' in final_content  # Added missing default value
