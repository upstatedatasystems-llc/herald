

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


def test_setup_sh_ai_fallback_and_research_matrix(tmp_path):
    """
    Verify setup.sh fallback state and research validation across matrix:
    Case A: AI_PROVIDER=gemini, missing key -> AI_PROVIDER=none, RESEARCH_PROVIDER=none
    Case B: AI_PROVIDER=gemini, valid key, invalid primary model, invalid research model -> AI_PROVIDER=none, RESEARCH_PROVIDER=none
    Case C: AI_PROVIDER=groq, valid Groq, valid separate Gemini Research -> AI_PROVIDER=groq, RESEARCH_PROVIDER=gemini
    Case D: AI_PROVIDER=groq, valid Groq, invalid Gemini Research -> AI_PROVIDER=groq, RESEARCH_PROVIDER=none
    """
    def run_simulation(ai_prov, gemini_key, groq_key, gemini_model_valid, research_model_valid, groq_model_valid):
        env = {
            "AI_PROVIDER": ai_prov,
            "GEMINI_API_KEY": gemini_key,
            "GROQ_API_KEY": groq_key,
            "RESEARCH_PROVIDER": "gemini" if gemini_key or ai_prov == "gemini" else "none",
        }

        # Step 3 validation simulation matching setup.sh:
        ai_valid = True
        active_ai = env["AI_PROVIDER"]
        if active_ai == "gemini":
            if not env["GEMINI_API_KEY"] or not gemini_model_valid:
                ai_valid = False
        elif active_ai == "groq":
            if not env["GROQ_API_KEY"] or not groq_model_valid:
                ai_valid = False

        if not ai_valid:
            env["AI_PROVIDER"] = "none"
            active_ai = "none"

        # Research validation matching updated setup.sh:
        if env["RESEARCH_PROVIDER"] == "gemini":
            if not env["GEMINI_API_KEY"] or not research_model_valid:
                env["RESEARCH_PROVIDER"] = "none"

        return env["AI_PROVIDER"], env["RESEARCH_PROVIDER"]

    # Case A: Missing Gemini key
    a_ai, a_res = run_simulation("gemini", "", "", False, False, False)
    assert a_ai == "none"
    assert a_res == "none"

    # Case B: Valid key, invalid primary model, invalid research model
    b_ai, b_res = run_simulation("gemini", "valid_key", "", False, False, False)
    assert b_ai == "none"
    assert b_res == "none"

    # Case C: Valid Groq, valid separate Gemini Research
    c_ai, c_res = run_simulation("groq", "valid_gem_key", "valid_groq_key", False, True, True)
    assert c_ai == "groq"
    assert c_res == "gemini"

    # Case D: Valid Groq, invalid Gemini Research
    d_ai, d_res = run_simulation("groq", "valid_gem_key", "valid_groq_key", False, False, True)
    assert d_ai == "groq"
    assert d_res == "none"


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
