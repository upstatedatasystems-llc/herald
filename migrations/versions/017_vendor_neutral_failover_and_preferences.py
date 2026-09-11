"""Vendor-neutral AI provider failover and preferences

Revision ID: 017_vendor_neutral_failover
Revises: 016_expand_custom_title_text
Create Date: 2026-09-11 08:05:00.000000

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '017_vendor_neutral_failover'
down_revision: Union[str, None] = '016_expand_custom_title_text'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add new columns to podcast_jobs
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.add_column(sa.Column('ai_provider', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('ai_model', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('ai_provider_chain_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('ai_effective_provider', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('ai_effective_model', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('ai_failover_index', sa.Integer(), nullable=False, server_default='0'))

    # 2. Add new columns to telegram_users (leave NULL for users with no explicit preference)
    with op.batch_alter_table('telegram_users') as batch_op:
        batch_op.add_column(sa.Column('ai_provider_chain_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('ai_models_by_provider_json', sa.JSON(), nullable=True))

    # 3. Deterministic Historical Backfill for podcast_jobs
    conn = op.get_bind()
    jobs = conn.execute(sa.text("""
        SELECT id, request_mode, gemini_model, research_model, generation_settings_json
        FROM podcast_jobs
    """)).fetchall()

    for job in jobs:
        job_id = job[0]
        req_mode = (job[1] or "standard").lower().strip()
        gem_model = job[2]
        res_model = job[3]
        gen_settings_raw = job[4]

        gen_settings = {}
        if gen_settings_raw:
            if isinstance(gen_settings_raw, dict):
                gen_settings = gen_settings_raw
            elif isinstance(gen_settings_raw, str):
                try:
                    gen_settings = json.loads(gen_settings_raw)
                except Exception:
                    gen_settings = {}

        # Precedence Rule 1: Authoritative SCRIPT AI interactions evidence
        interaction = conn.execute(sa.text("""
            SELECT provider, model FROM ai_interactions
            WHERE job_id = :jid AND operation IN ('script_generation', 'script') AND success = true
            ORDER BY completed_at DESC, created_at DESC LIMIT 1
        """), {"jid": job_id}).fetchone()

        if not interaction:
            interaction = conn.execute(sa.text("""
                SELECT provider, model FROM ai_interactions
                WHERE job_id = :jid AND operation IN ('script_generation', 'script')
                ORDER BY created_at DESC LIMIT 1
            """), {"jid": job_id}).fetchone()

        provider = None
        model = None

        if interaction and interaction[0] and interaction[1]:
            provider = interaction[0]
            model = interaction[1]
        elif gen_settings.get("ai_provider") and gen_settings.get("ai_model"):
            # Precedence Rule 2: Explicit generation snapshot provider and model (explicit ai_model wins over gem_model)
            provider = gen_settings["ai_provider"]
            model = gen_settings["ai_model"]
        elif gen_settings.get("ai_provider"):
            provider = gen_settings["ai_provider"]
            model = gen_settings.get("ai_model") or gem_model or ""
        elif req_mode == "literal":
            provider = "literal"
            model = "none"
        elif req_mode == "research":
            provider = gen_settings.get("research_provider") or "gemini"
            model = res_model or gen_settings.get("research_model") or gem_model or "gemini-3.6-flash"
        elif gem_model:
            model = gem_model
            m_low = gem_model.lower().strip()
            # Precedence Rule 3: Unambiguous provider-prefixed or vendor-exclusive models
            if m_low.startswith("@cf/") or m_low.startswith("cloudflare/"):
                provider = "cloudflare"
            elif m_low.startswith("groq/"):
                provider = "groq"
            elif m_low.startswith("openrouter/"):
                provider = "openrouter"
            elif m_low.startswith("ollama/"):
                provider = "ollama"
            elif m_low.startswith("anthropic/") or m_low.startswith("claude-"):
                provider = "anthropic"
            elif m_low.startswith("mistral/") or m_low.startswith("codestral-"):
                provider = "mistral"
            elif m_low.startswith("gemini-") or m_low.startswith("models/gemini"):
                provider = "gemini"
            else:
                # Ambiguous model family (e.g. llama, gpt) check if any interaction recorded provider on job
                any_inter = conn.execute(sa.text("""
                    SELECT provider FROM ai_interactions WHERE job_id = :jid LIMIT 1
                """), {"jid": job_id}).fetchone()
                if any_inter and any_inter[0]:
                    provider = any_inter[0]
                elif "llama" in m_low or "gpt" in m_low:
                    provider = "ambiguous"
                else:
                    # Legacy fallback
                    provider = "gemini"
        else:
            provider = "gemini"
            model = "gemini-3.5-flash"

        provider_chain = [{"provider": provider, "model": model or ""}]

        conn.execute(sa.text("""
            UPDATE podcast_jobs
            SET ai_provider = :prov,
                ai_model = :mod,
                ai_provider_chain_json = :chain,
                ai_effective_provider = :eff_prov,
                ai_effective_model = :eff_mod,
                ai_failover_index = 0
            WHERE id = :jid
        """), {
            "prov": provider,
            "mod": model or "",
            "chain": json.dumps(provider_chain),
            "eff_prov": provider,
            "eff_mod": model or "",
            "jid": job_id,
        })


def downgrade() -> None:
    with op.batch_alter_table('telegram_users') as batch_op:
        batch_op.drop_column('ai_models_by_provider_json')
        batch_op.drop_column('ai_provider_chain_json')

    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.drop_column('ai_failover_index')
        batch_op.drop_column('ai_effective_model')
        batch_op.drop_column('ai_effective_provider')
        batch_op.drop_column('ai_provider_chain_json')
        batch_op.drop_column('ai_model')
        batch_op.drop_column('ai_provider')
