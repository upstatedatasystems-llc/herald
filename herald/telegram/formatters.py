"""
Centralized HTML message formatters for Telegram interface.
All dynamic text values MUST be escaped with html.escape when used in HTML parse_mode.
"""

import html
import logging
from datetime import UTC, datetime
from typing import Any

from herald.config import settings
from herald.db.models import JobState, PodcastJob, RequestMode
from herald.services.eta_calculator import calculate_script_duration
from herald.services.settings_fingerprint import (
    are_generation_settings_identical,
    format_settings_display,
    get_job_generation_settings,
)

logger = logging.getLogger(__name__)


def get_job_ai_identity(job: PodcastJob) -> tuple[str | None, str | None]:
    """
    Return truthful (provider_name, model_name) for a job based on its request mode,
    effective provider, interactions, and persisted model evidence.
    Returns (None, None) for Literal mode (no AI used).
    Attribution precedence:
      ai_effective_provider/model -> ai_interactions -> ai_provider/model -> isolated legacy fallback only.
    """
    mode = getattr(job, "request_mode", RequestMode.STANDARD.value)
    if mode == RequestMode.LITERAL.value:
        return None, None

    provider_display_map = {
        "gemini": "Gemini",
        "groq": "Groq",
        "openrouter": "OpenRouter",
        "mistral": "Mistral",
        "cloudflare": "Cloudflare Workers AI",
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "ollama": "Ollama",
        "literal": "Literal",
    }

    # 1. ai_effective_provider / ai_effective_model (highest precedence for completed/active operations)
    eff_prov = getattr(job, "ai_effective_provider", None)
    eff_mod = getattr(job, "ai_effective_model", None)
    if eff_prov:
        p_low = str(eff_prov).lower().strip()
        disp_name = provider_display_map.get(p_low, p_low.capitalize())
        return disp_name, eff_mod

    # 2. ai_interactions for recorded external AI execution evidence
    if hasattr(job, "ai_interactions") and job.ai_interactions:
        first_ai = job.ai_interactions[0]
        p_low = str(first_ai.provider or "").lower().strip()
        disp_name = provider_display_map.get(p_low, p_low.capitalize())
        return disp_name, first_ai.model

    # 3. Snapshotted research_provider / research_model for Research mode jobs
    if mode == RequestMode.RESEARCH.value:
        res_mod = getattr(job, "research_model", None)
        gen_settings = getattr(job, "generation_settings_json", None) or {}
        res_prov = gen_settings.get("research_provider") if isinstance(gen_settings, dict) else None
        if not res_prov and hasattr(job, "ai_provider") and job.ai_provider:
            res_prov = job.ai_provider
        if res_prov or res_mod:
            p_low = str(res_prov or "gemini").lower().strip()
            disp_name = provider_display_map.get(p_low, p_low.capitalize())
            return disp_name, res_mod

    # 4. ai_provider / ai_model (primary configured provider)
    ai_prov = getattr(job, "ai_provider", None)
    ai_mod = getattr(job, "ai_model", None)
    if ai_prov:
        p_low = str(ai_prov).lower().strip()
        disp_name = provider_display_map.get(p_low, p_low.capitalize())
        return disp_name, ai_mod

    # 5. Isolated legacy fallback only (historical jobs with only gemini_model column)
    legacy_model = getattr(job, "gemini_model", None)
    if legacy_model:
        return "Gemini", legacy_model

    # Default configured server provider
    def_prov = getattr(settings, "AI_PROVIDER", "gemini").lower().strip()
    disp_name = provider_display_map.get(def_prov, def_prov.capitalize())
    def_model = getattr(settings, "GEMINI_MODEL", "gemini-3.5-flash") if def_prov == "gemini" else None
    return disp_name, def_model



def format_duration_sec(seconds: int | float | None) -> str:
    """Format seconds into human-readable duration (e.g. '3m 45s' or '45s')."""
    if seconds is None or seconds <= 0:
        return "0s"
    s = int(round(seconds))
    mins, sec = divmod(s, 60)
    if mins > 0:
        return f"{mins}m {sec}s"
    return f"{sec}s"


def format_quickstart(owner_name: str, default_mode: str, ai_provider: str) -> str:
    """Format quick-start onboarding message sent after /pair or authenticated /start."""
    esc_owner = html.escape(owner_name or "Owner")
    esc_mode = html.escape(default_mode or "standard")
    esc_ai = html.escape(ai_provider or "None (Literal only)")

    return (
        f"🎙️ <b>Welcome to Herald!</b>\n\n"
        f"Owner: <b>{esc_owner}</b>\n"
        f"Default Mode: <code>{esc_mode}</code>\n"
        f"AI Provider: <code>{esc_ai}</code>\n\n"
        f"<b>Quick Start:</b>\n"
        f"• Send an article URL for a <code>{esc_mode}</code> podcast.\n"
        f"• Put <code>brief</code> above a URL or text for a short episode.\n"
        f"• Put <code>research high</code> above a URL or text for deep research.\n"
        f"• Put <code>literal</code> above text for zero-AI narration.\n\n"
        f"Use /help to view all available commands and directives."
    )


def format_help() -> str:
    """Format comprehensive help and command reference message."""
    return (
        "📖 <b>Herald Usage Guide</b>\n\n"
        "<b>Ways to generate audio:</b>\n"
        "• Send an article URL (e.g. <code>https://example.com/article</code>)\n"
        "• Paste or forward an article, document, or newsletter\n\n"
        "<b>Modes (top of message):</b>\n"
        "• <code>literal</code> — Local deterministic reading (no AI required)\n"
        "• <code>brief</code> — Concise AI summary\n"
        "• <code>standard</code> — Full AI podcast narration\n"
        "• <code>research high</code> — Deep-dive grounded research podcast (Gemini)\n\n"
        "<b>Directives (top of message):</b>\n"
        "• <code>Voice: af_bella</code> (af_heart, af_bella, af_sarah, am_adam, am_michael)\n"
        "• <code>Speed: 1.1</code> (0.8 to 1.2)\n"
        "• <code>Title: Custom Title</code>\n\n"
        "<b>Commands:</b>\n"
        "/start — Quick-start guide\n"
        "/help — Full usage and directive reference\n"
        "/download — Download latest (or specific) episode MP3 file\n"
        "/diagnostics — View job diagnostics and download support package\n"
        "/status — Live system health, AI status, and queue depth\n"
        "/ai_check — Fresh AI provider connection test (alias: /ai-check)\n"
        "/queue — Pending and processing jobs\n"
        "/settings — Preferences, voice selection, and pre-TTS confirmation toggle\n"
        "/readme — Project documentation"
    )


def format_settings(user_prefs: dict, instance_settings: object = None) -> tuple[str, dict]:
    """
    Format settings message and generate inline keyboard markup for voice selection,
    confirmation toggle, AI provider slots, AI models, speed, and mode.
    Returns:
        (text, reply_markup_dict)
    """
    from herald.ai.registry import get_descriptor, is_provider_configured
    from herald.ai.resolution import resolve_job_settings

    confirm_on = bool(user_prefs.get("confirm_before_tts", False))
    default_voice = html.escape(str(user_prefs.get("default_voice", "af_heart")))
    default_speed = float(user_prefs.get("default_speed", 1.0))
    default_mode = html.escape(str(user_prefs.get("default_mode", "standard")).capitalize())

    confirm_str = "🟢 On" if confirm_on else "⚪ Off"
    button_text = "🔕 Disable Confirm Before TTS" if confirm_on else "🔔 Enable Confirm Before TTS"
    button_callback = "h2:settings:confirm:off" if confirm_on else "h2:settings:confirm:on"

    resolved = resolve_job_settings(request_params={}, user_prefs=user_prefs)
    candidates = resolved.ai_candidates

    chain_lines = []
    slot_names = ["Primary", "Secondary", "Tertiary"]
    for i, slot_name in enumerate(slot_names):
        if i < len(candidates):
            c = candidates[i]
            desc = get_descriptor(c.provider_id)
            p_name = desc.display_name if desc else c.provider_id.capitalize()
            cfg_warn = "" if (c.provider_id == "literal" or is_provider_configured(c.provider_id)) else " ⚠️ <i>(Unconfigured)</i>"
            chain_lines.append(f"  {i+1}. {slot_name}: <b>{html.escape(p_name)}</b> (<code>{html.escape(c.model_id)}</code>){cfg_warn}")
        else:
            chain_lines.append(f"  {i+1}. {slot_name}: <i>None</i>")
    chain_str = "\n".join(chain_lines)

    text = (
        "⚙️ <b>Herald Preferences & Settings</b>\n\n"
        f"• <b>Default Mode:</b> <code>{default_mode}</code>\n"
        f"• <b>Default Voice:</b> <code>{default_voice}</code>\n"
        f"• <b>Default Speed:</b> <code>{default_speed:.1f}x</code>\n"
        f"• <b>Confirm Before TTS:</b> {confirm_str}\n"
        f"• <b>AI Provider Chain:</b>\n{chain_str}\n\n"
        "<i>Tap below to customize your preferences:</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🎙 Set Voice",
                    "callback_data": "h2:settings:voice",
                }
            ],
            [
                {
                    "text": button_text,
                    "callback_data": button_callback,
                }
            ],
            [
                {
                    "text": "🤖 AI Providers",
                    "callback_data": "h3:settings:providers",
                },
                {
                    "text": "🧩 AI Models",
                    "callback_data": "h3:settings:models",
                },
            ],
            [
                {
                    "text": "⚡ Speed",
                    "callback_data": "h3:settings:speed",
                },
                {
                    "text": "🧭 Mode",
                    "callback_data": "h3:settings:mode",
                },
            ],
            [
                {
                    "text": "🔍 Check AI Connections",
                    "callback_data": "h3:settings:aicheck",
                }
            ],
        ]
    }

    return text, reply_markup


def format_ai_providers_menu(user_prefs: dict) -> tuple[str, dict[str, Any]]:
    """Format AI providers chain management menu."""
    from herald.ai.registry import get_descriptor, is_provider_configured
    from herald.ai.resolution import resolve_job_settings

    resolved = resolve_job_settings(request_params={}, user_prefs=user_prefs)
    candidates = resolved.ai_candidates
    slot_names = ["Primary", "Secondary", "Tertiary"]

    lines = [
        "🤖 <b>AI Provider Chain Configuration</b>\n",
        "Herald failover moves down your provider chain in order if errors occur.\n",
    ]

    for i, name in enumerate(slot_names):
        if i < len(candidates):
            c = candidates[i]
            desc = get_descriptor(c.provider_id)
            p_name = desc.display_name if desc else c.provider_id.capitalize()
            cfg_note = "" if (c.provider_id == "literal" or is_provider_configured(c.provider_id)) else " ⚠️ <i>(API key missing)</i>"
            lines.append(f"• <b>{name} (Slot {i+1}):</b> <b>{html.escape(p_name)}</b> (<code>{html.escape(c.model_id)}</code>){cfg_note}")
        else:
            lines.append(f"• <b>{name} (Slot {i+1}):</b> <i>None</i>")

    lines.append("\n<i>Tap a slot below to set or replace its provider:</i>")
    text = "\n".join(lines)

    keyboard = [
        [
            {"text": "1️⃣ Set Primary", "callback_data": "h3:p:slot:0"},
            {"text": "2️⃣ Set Secondary", "callback_data": "h3:p:slot:1"},
        ],
        [
            {"text": "3️⃣ Set Tertiary", "callback_data": "h3:p:slot:2"},
            {"text": "🗑 Clear Secondary & Tertiary", "callback_data": "h3:p:clear_subs"},
        ],
        [
            {"text": "← Back to Settings", "callback_data": "h2:settings:main"},
        ],
    ]

    return text, {"inline_keyboard": keyboard}


def format_provider_slot_select(user_prefs: dict, slot_index: int) -> tuple[str, dict[str, Any]]:
    """Format provider selection for a specific slot."""
    from herald.ai.registry import is_provider_configured, list_registered_providers
    from herald.ai.resolution import resolve_job_settings

    resolved = resolve_job_settings(request_params={}, user_prefs=user_prefs)
    candidates = resolved.ai_candidates
    slot_names = ["Primary", "Secondary", "Tertiary"]
    slot_label = slot_names[slot_index] if slot_index < len(slot_names) else f"Slot {slot_index+1}"

    curr_provider_id = candidates[slot_index].provider_id if slot_index < len(candidates) else None

    text = (
        f"🎯 <b>Select {slot_label} Provider (Slot {slot_index+1})</b>\n\n"
        f"Currently assigned: <b>{html.escape(curr_provider_id.capitalize() if curr_provider_id else 'None')}</b>\n\n"
        f"Choose an AI provider below:\n"
        f"<i>⚠️ indicates provider API key is not configured on this server.</i>"
    )

    all_providers = list_registered_providers()
    preferred_order = ["gemini", "groq", "cloudflare", "openai", "openrouter", "mistral", "anthropic", "ollama", "literal"]
    ordered_providers = [p for p in preferred_order if p in all_providers] + [p for p in all_providers if p not in preferred_order]

    keyboard = []
    row = []
    for p_id in ordered_providers:
        desc = all_providers[p_id]
        is_selected = (curr_provider_id == p_id)
        is_cfg = is_provider_configured(p_id) or p_id == "literal"

        badge = "✅ " if is_selected else ("⚠️ " if not is_cfg else "")
        btn_text = f"{badge}{desc.display_name}"
        cb_data = f"h3:p:set:{slot_index}:{p_id}"

        row.append({"text": btn_text, "callback_data": cb_data})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    if slot_index > 0:
        keyboard.append([{"text": "❌ Clear Slot (Set to None)", "callback_data": f"h3:p:set:{slot_index}:none"}])

    keyboard.append([{"text": "← Back to AI Providers", "callback_data": "h3:settings:providers"}])

    return text, {"inline_keyboard": keyboard}


def format_ai_models_menu(user_prefs: dict) -> tuple[str, dict[str, Any]]:
    """Format AI models provider selection menu."""
    from herald.ai.registry import list_registered_providers
    from herald.ai.resolution import resolve_job_settings

    resolved = resolve_job_settings(request_params={}, user_prefs=user_prefs)
    candidates = resolved.ai_candidates

    all_provs = list_registered_providers()
    lines = [
        "🧩 <b>Preferred AI Models</b>\n",
        "Select a provider below to choose your preferred model:\n",
    ]

    for c in candidates:
        if c.provider_id == "literal":
            continue
        desc = all_provs.get(c.provider_id)
        p_name = desc.display_name if desc else c.provider_id.capitalize()
        lines.append(f"• <b>{html.escape(p_name)}:</b> <code>{html.escape(c.model_id)}</code>")

    text = "\n".join(lines)

    selectable_provs = ["gemini", "groq", "cloudflare", "openai", "openrouter", "mistral", "anthropic", "ollama"]
    keyboard = []
    row = []
    for pid in selectable_provs:
        if pid in all_provs:
            d = all_provs[pid]
            row.append({"text": d.display_name, "callback_data": f"h3:m:prov:{pid}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row:
        keyboard.append(row)

    keyboard.append([{"text": "← Back to Settings", "callback_data": "h2:settings:main"}])

    return text, {"inline_keyboard": keyboard}


def format_provider_models_select(user_prefs: dict, provider_id: str) -> tuple[str, dict[str, Any]]:
    """Format model selection for a specific provider."""
    from herald.ai.catalog import get_model_token, get_models_for_provider
    from herald.ai.registry import get_descriptor
    from herald.ai.resolution import resolve_job_settings

    desc = get_descriptor(provider_id)
    p_name = desc.display_name if desc else provider_id.capitalize()

    resolved = resolve_job_settings(request_params={}, user_prefs=user_prefs)
    current_model = None
    for c in resolved.ai_candidates:
        if c.provider_id == provider_id:
            current_model = c.model_id
            break
    if not current_model and desc:
        current_model = desc.default_model

    models = get_models_for_provider(provider_id)

    lines = [
        f"🧩 <b>Models for {html.escape(p_name)}</b>\n",
        f"Active model: <code>{html.escape(current_model or 'Default')}</code>\n",
        "Select a model below:\n",
    ]

    keyboard = []
    for m in models:
        is_active = (m.model_id == current_model)
        mark = "✅ " if is_active else ""
        token = get_model_token(provider_id, m.model_id)
        keyboard.append([{
            "text": f"{mark}{m.display_name}",
            "callback_data": f"h3:m:set:{provider_id}:{token}",
        }])

    keyboard.append([{"text": "← Back to AI Models", "callback_data": "h3:settings:models"}])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def format_speed_menu(user_prefs: dict) -> tuple[str, dict[str, Any]]:
    """Format speed selection submenu."""
    curr_spd = float(user_prefs.get("default_speed", 1.0))
    text = (
        "⚡ <b>Default Audio Speed</b>\n\n"
        f"Current speed: <code>{curr_spd:.1f}x</code>\n\n"
        "Select preferred playback speed for generated podcasts:"
    )
    speeds = [0.8, 0.9, 1.0, 1.1, 1.2]
    buttons = []
    for s in speeds:
        mark = "✅ " if abs(curr_spd - s) < 0.01 else ""
        buttons.append({"text": f"{mark}{s:.1f}x", "callback_data": f"h3:speed:set:{s:.1f}"})

    keyboard = [buttons, [{"text": "← Back to Settings", "callback_data": "h2:settings:main"}]]
    return text, {"inline_keyboard": keyboard}


def format_mode_menu(user_prefs: dict) -> tuple[str, dict[str, Any]]:
    """Format mode selection submenu."""
    curr_mode = str(user_prefs.get("default_mode", "standard")).lower()
    text = (
        "🧭 <b>Default Generation Mode</b>\n\n"
        f"Current mode: <code>{html.escape(curr_mode.capitalize())}</code>\n\n"
        "Select your default mode for new podcasts:\n"
        "• <b>Standard:</b> Full podcast script & dialogue\n"
        "• <b>Brief:</b> Condensed summary episode\n"
        "• <b>Research:</b> External web grounding & verified facts\n"
        "• <b>Literal:</b> Zero AI, verbatim text-to-speech\n"
    )
    modes = [("standard", "Standard"), ("brief", "Brief"), ("research", "Research"), ("literal", "Literal")]
    keyboard = []
    row = []
    for m_id, m_label in modes:
        mark = "✅ " if curr_mode == m_id else ""
        row.append({"text": f"{mark}{m_label}", "callback_data": f"h3:mode:set:{m_id}"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([{"text": "← Back to Settings", "callback_data": "h2:settings:main"}])
    return text, {"inline_keyboard": keyboard}


def format_models_catalog() -> str:
    """Format the full AI models catalog for /models command."""
    from herald.ai.catalog import get_models_for_provider
    from herald.ai.registry import is_provider_configured, list_registered_providers

    providers = list_registered_providers()
    lines = [
        "📚 <b>Herald AI Models Catalog</b>\n",
        "Supported AI providers and their declared model capabilities:\n",
    ]

    preferred_order = ["gemini", "groq", "cloudflare", "openai", "openrouter", "mistral", "anthropic", "ollama", "literal"]
    ordered_providers = [p for p in preferred_order if p in providers] + [p for p in providers if p not in preferred_order]

    for p_id in ordered_providers:
        desc = providers[p_id]
        is_cfg = is_provider_configured(p_id) or p_id == "literal"
        cfg_icon = "🟢 Configured" if is_cfg else "⚪ Not configured (API key missing)"
        lines.append(f"<b>{html.escape(desc.display_name)}</b> (<code>{p_id}</code>) — {cfg_icon}")
        lines.append(f"• Default: <code>{html.escape(desc.default_model)}</code>")

        models = get_models_for_provider(p_id)
        if models:
            m_strs = []
            for m in models:
                ctx_k = f" ({m.context_window // 1000}k ctx)" if m.context_window else ""
                m_strs.append(f"<code>{html.escape(m.model_id)}</code>{ctx_k}")
            lines.append(f"• Models: {', '.join(m_strs)}")
        lines.append("")

    lines.append("<i>Use /settings ➔ 🧩 AI Models to choose your preferred model per provider.</i>")
    return "\n".join(lines)


def get_job_display_title(job: PodcastJob) -> str:
    """
    Return authoritative display title with strict precedence:
    job.custom_title -> (job.script_json or {}).get("episode_title") -> "Herald Episode"
    """
    if job.custom_title and str(job.custom_title).strip():
        return str(job.custom_title).strip()
    if job.script_json and isinstance(job.script_json, dict):
        ep_title = job.script_json.get("episode_title")
        if ep_title and str(ep_title).strip():
            return str(ep_title).strip()
    return "Herald Episode"


def format_approval(
    job: PodcastJob,
    script_json: dict | None,
    eta_info: dict | None = None,
    prior_job: PodcastJob | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Format interactive approval card message with Approve and Cancel buttons.
    If prior_job is provided, includes prior run context and settings comparison (Case C).
    Returns:
        (text, reply_markup_dict)
    """
    script_obj = script_json or job.script_json or {}
    title_raw = get_job_display_title(job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)
    desc = script_obj.get("episode_description") or ""
    desc_clean = html.escape(desc[:150] + "..." if len(desc) > 150 else desc)

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    source_words = len((job.source_text or "").split())
    dur_data = calculate_script_duration(script_obj, job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    narration_words = dur_data.get("narration_word_count", 0)
    pred_duration = format_duration_sec(dur_data.get("predicted_duration_seconds", 0))

    voice = html.escape(job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))

    eta_range = (eta_info or {}).get("estimated_completion_range") or "approximately 3–5 minutes"
    short_id = html.escape(job.id[:8])

    ai_prov, ai_model = get_job_ai_identity(job)
    ai_line = f"\n• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>" if ai_prov and ai_model else ""

    desc_section = f"\n<i>{desc_clean}</i>\n" if desc_clean else ""

    prior_section = ""
    header_title = "📋 <b>Podcast Ready for Approval</b>"
    button_approve_text = "✅ Approve & Generate"

    if prior_job:
        header_title = "🔄 <b>Podcast Rerun Ready for Approval</b>"
        button_approve_text = "✅ Approve Rerun & Generate"
        p_dt = prior_job.created_at
        p_date_str = p_dt.strftime("%b %d, %H:%M UTC") if p_dt else "earlier"
        p_status = prior_job.status
        p_short = html.escape(prior_job.id[:8])

        curr_s = get_job_generation_settings(job)
        prior_s = get_job_generation_settings(prior_job)
        if are_generation_settings_identical(curr_s, prior_s):
            settings_line = "• <b>Settings:</b> Identical to prior run\n"
        else:
            diff_text = f"Prior: {format_settings_display(prior_s)} ➔ New: {format_settings_display(curr_s)}"
            settings_line = f"• <b>Settings Changes:</b>\n  {html.escape(diff_text)}\n"

        prior_section = (
            f"\n🔄 <b>Prior Generation:</b> Job <code>{p_short}</code> ({html.escape(p_status)}, {html.escape(p_date_str)})\n"
            f"{settings_line}"
        )

    text = (
        f"{header_title}\n\n"
        f"<b>{title}</b>{desc_section}\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Source:</b> {source_words:,} words\n"
        f"• <b>Narration:</b> {narration_words:,} words (~{pred_duration})\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}\n"
        f"• <b>Estimated Range:</b> {html.escape(eta_range)}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>\n"
        f"{prior_section}\n"
        f"<i>Review details above and approve to start audio synthesis:</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": button_approve_text,
                    "callback_data": f"h2:approve:{job.id}",
                },
                {
                    "text": "❌ Cancel",
                    "callback_data": f"h2:deny:{job.id}",
                },
            ]
        ]
    }

    return text, reply_markup


def format_rerun_confirmation(
    new_job: PodcastJob,
    prior_job: PodcastJob,
) -> tuple[str, dict[str, Any]]:
    """
    Format interactive rerun confirmation card for duplicate content when confirmation is off (Case D).
    Returns:
        (text, reply_markup_dict)
    """
    title_raw = get_job_display_title(new_job) or get_job_display_title(prior_job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)

    p_dt = prior_job.created_at
    p_date_str = p_dt.strftime("%b %d, %H:%M UTC") if p_dt else "earlier"
    p_status = prior_job.status
    p_short_id = html.escape(prior_job.id[:8])

    curr_settings = get_job_generation_settings(new_job)
    prior_settings = get_job_generation_settings(prior_job)
    same_settings = are_generation_settings_identical(curr_settings, prior_settings)

    mode_str = html.escape(curr_settings.get("mode", "standard").capitalize())
    voice_str = html.escape(curr_settings.get("voice", "af_heart"))
    speed_val = float(curr_settings.get("speed", 1.0))

    if same_settings:
        settings_info = (
            f"• <b>Settings:</b> Identical to prior run\n"
            f"  (Mode: <code>{mode_str}</code>, Voice: <code>{voice_str}</code> @ {speed_val:.1f}x)"
        )
    else:
        diff_str = f"Prior: {format_settings_display(prior_settings)} ➔ New: {format_settings_display(curr_settings)}"
        settings_info = (
            f"• <b>Settings Modified:</b>\n"
            f"  {html.escape(diff_str)}"
        )

    text = (
        f"🔄 <b>Prior Generation Found</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"This content was previously processed in job <code>{p_short_id}</code> ({html.escape(p_status)}, {html.escape(p_date_str)}).\n\n"
        f"{settings_info}\n\n"
        f"<i>Would you like to run this content again as a new generation?</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🔄 Confirm Rerun",
                    "callback_data": f"h2:rerun_approve:{new_job.id}",
                },
                {
                    "text": "❌ Cancel",
                    "callback_data": f"h2:deny:{new_job.id}",
                },
            ]
        ]
    }
    return text, reply_markup


def format_queued(job: PodcastJob, script_json: dict | None, eta_info: dict | None = None) -> str:
    """Format rich queued card for automatically queued or explicitly approved jobs."""
    script_obj = script_json or job.script_json or {}
    title_raw = get_job_display_title(job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)
    desc = script_obj.get("episode_description") or ""
    desc_clean = html.escape(desc[:150] + "..." if len(desc) > 150 else desc)

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    source_words = len((job.source_text or "").split())
    dur_data = calculate_script_duration(script_obj, job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    narration_words = dur_data.get("narration_word_count", 0)
    pred_duration = format_duration_sec(dur_data.get("predicted_duration_seconds", 0))

    voice = html.escape(job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))

    # Source type
    src_type_raw = job.source_type or "text"
    if job.source_url:
        src_disp = f"URL ({html.escape(job.source_url[:35])}{'...' if len(job.source_url) > 35 else ''})"
    elif src_type_raw == "email_body":
        src_disp = "Email body"
    else:
        src_disp = html.escape(src_type_raw.capitalize())

    jobs_ahead = (eta_info or {}).get("jobs_ahead", 0)
    eta_range = (eta_info or {}).get("estimated_completion_range") or "approximately 3–5 minutes"
    short_id = html.escape(job.id[:8])

    ai_prov, ai_model = get_job_ai_identity(job)
    ai_line = f"\n• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>" if ai_prov and ai_model else ""

    queue_line = f"\n• <b>Queue Position:</b> {jobs_ahead} jobs ahead" if jobs_ahead > 0 else "\n• <b>Queue Position:</b> Next up"
    desc_section = f"\n<i>{desc_clean}</i>\n" if desc_clean else ""

    return (
        f"🎙️ <b>Podcast Queued for Synthesis</b>\n\n"
        f"<b>{title}</b>{desc_section}\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Source:</b> {src_disp} ({source_words:,} words)\n"
        f"• <b>Narration:</b> {narration_words:,} words (~{pred_duration})\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}"
        f"{queue_line}\n"
        f"• <b>Estimated Range:</b> {html.escape(eta_range)}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>\n\n"
        f"Queued for synthesis. Herald will begin when TTS capacity is available."
    )


def format_completion(
    job: PodcastJob,
    actual_chunks_count: int | None = None,
    file_size_bytes: int | None = None,
    active_processing_seconds: int | float | None = None,
) -> str:
    """
    Format concise rich caption for audio delivery adhering to Telegram's 1024-char limit.
    """
    script_obj = job.script_json or {}
    title_raw = get_job_display_title(job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)
    desc = script_obj.get("episode_description") or ""

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    dur_str = format_duration_sec(job.audio_duration_seconds)
    size_mb_str = f"{file_size_bytes / (1024 * 1024):.1f} MB" if file_size_bytes else ""
    dur_size_str = f"{dur_str} ({size_mb_str})" if size_mb_str else dur_str

    # Word counts
    source_words = len((job.source_text or "").split()) if job.source_text else 0
    dur_data = calculate_script_duration(script_obj, job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    narration_words = dur_data.get("narration_word_count", 0)

    # Active processing time calculation
    proc_time_str = ""
    if active_processing_seconds is not None and active_processing_seconds > 0:
        proc_time_str = format_duration_sec(active_processing_seconds)
    elif job.completed_at and job.created_at:
        total_sec = (job.completed_at - job.created_at).total_seconds()
        if job.approved_at and job.approval_requested_at:
            hold_sec = (job.approved_at - job.approval_requested_at).total_seconds()
            active_sec = max(1, int(total_sec - hold_sec))
        else:
            active_sec = max(1, int(total_sec))
        proc_time_str = format_duration_sec(active_sec)

    chunks_str = f"{actual_chunks_count} chunks" if actual_chunks_count else ""
    voice = html.escape(job.kokoro_voice or job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.kokoro_speed or job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    short_id = html.escape(job.id[:8])

    ai_prov, ai_model = get_job_ai_identity(job)
    ai_line = f"\n• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>" if ai_prov and ai_model else ""

    # Truncate description safely to stay well within 1024 chars
    desc_clean = html.escape(desc[:120] + "..." if len(desc) > 120 else desc) if desc else ""
    desc_section = f"\n<i>{desc_clean}</i>\n" if desc_clean else ""

    words_line = ""
    if source_words > 0 and narration_words > 0:
        words_line = f"\n• <b>Words:</b> {source_words:,} src / {narration_words:,} nar"
    elif narration_words > 0:
        words_line = f"\n• <b>Words:</b> {narration_words:,} nar"

    chunks_line = f"\n• <b>TTS Chunks:</b> {chunks_str}" if chunks_str else ""
    proc_line = f"\n• <b>Processing Time:</b> {proc_time_str}" if proc_time_str else ""

    caption = (
        f"🎙️ <b>{title}</b>{desc_section}\n"
        f"• <b>Duration:</b> {dur_size_str}\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}"
        f"{words_line}"
        f"{chunks_line}"
        f"{proc_line}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>"
    )

    # Ensure strictly within Telegram's 1024-char caption limit
    if len(caption) > 1024:
        # Emergency trim of description
        desc_section = ""
        caption = (
            f"🎙️ <b>{title}</b>\n"
            f"• <b>Duration:</b> {dur_size_str}\n"
            f"• <b>Mode:</b> {mode_str}\n"
            f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
            f"{ai_line}"
            f"{words_line}"
            f"{chunks_line}"
            f"{proc_line}\n"
            f"• <b>Job ID:</b> <code>{short_id}</code>"
        )
    return caption


def format_completion_markup(job: PodcastJob) -> dict[str, Any]:
    """Return inline keyboard markup with Download MP3 and Diagnostics buttons for completed podcasts."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📥 Download MP3",
                    "callback_data": f"h2:download:{job.id}",
                },
                {
                    "text": "🛠️ Diagnostics",
                    "callback_data": f"h2:diag:{job.id}",
                },
            ]
        ]
    }


def format_diagnostics_card(job: PodcastJob, db: Any = None) -> str:
    """Format concise Telegram HTML diagnostic card for a job."""
    from herald.db.models import AIInteraction, PodcastTTSChunk

    title = html.escape(get_job_display_title(job))
    short_id = html.escape(job.id[:8])
    status = html.escape(job.status)
    mode_str = (job.request_mode or "standard").capitalize()
    if job.request_mode == "research" and job.research_depth:
        mode_str += f" ({job.research_depth.capitalize()})"
    mode_esc = html.escape(mode_str)
    src_type = html.escape(job.source_type or "text")

    voice = html.escape(job.kokoro_voice or job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.kokoro_speed or job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))

    # Duration and processing time
    proc_time_str = "N/A"
    created_utc = job.created_at.replace(tzinfo=UTC) if (job.created_at and job.created_at.tzinfo is None) else job.created_at
    if created_utc:
        comp_utc = job.completed_at.replace(tzinfo=UTC) if (job.completed_at and job.completed_at.tzinfo is None) else job.completed_at
        deliv_utc = job.delivered_at.replace(tzinfo=UTC) if (job.delivered_at and job.delivered_at.tzinfo is None) else job.delivered_at
        end_time = comp_utc or deliv_utc or datetime.now(UTC)
        total_sec = (end_time - created_utc).total_seconds()
        app_req_utc = job.approval_requested_at.replace(tzinfo=UTC) if (job.approval_requested_at and job.approval_requested_at.tzinfo is None) else job.approval_requested_at
        app_done_utc = job.approved_at.replace(tzinfo=UTC) if (job.approved_at and job.approved_at.tzinfo is None) else job.approved_at
        if app_done_utc and app_req_utc:
            hold_sec = (app_done_utc - app_req_utc).total_seconds()
            active_sec = max(1, int(total_sec - hold_sec))
        else:
            active_sec = max(1, int(total_sec))
        proc_time_str = format_duration_sec(active_sec)

    created_str = created_utc.strftime("%Y-%m-%d %H:%M:%S UTC") if created_utc else "N/A"

    # AI identity & tokens
    ai_prov, ai_model = get_job_ai_identity(job)
    if job.request_mode == "literal":
        ai_line = "• <b>AI:</b> <code>None (Literal mode)</code>"
    elif ai_prov and ai_model:
        ai_line = f"• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>"
    else:
        ai_line = "• <b>AI:</b> <code>None</code>"

    # TTS chunks count
    chunks_str = ""
    if db:
        tts_count = db.query(PodcastTTSChunk).filter(PodcastTTSChunk.job_id == job.id).count()
        if tts_count > 0:
            chunks_str = f"\n• <b>TTS Chunks:</b> {tts_count}"
        ai_calls = db.query(AIInteraction).filter(AIInteraction.job_id == job.id).all()
        if ai_calls:
            tot_tok = sum(c.total_tokens for c in ai_calls if c.total_tokens is not None)
            tok_str = f" ({tot_tok:,} tokens)" if tot_tok else ""
            ai_line += f"\n• <b>AI Interactions:</b> {len(ai_calls)} call(s){tok_str}"

    # Retries / Errors
    retry_parts = []
    if job.attempt_count and job.attempt_count > 1:
        retry_parts.append(f"intake: {job.attempt_count}")
    if job.synthesis_attempt_count and job.synthesis_attempt_count > 1:
        retry_parts.append(f"synthesis: {job.synthesis_attempt_count}")
    if job.delivery_attempt_count and job.delivery_attempt_count > 1:
        retry_parts.append(f"delivery: {job.delivery_attempt_count}")
    if job.verify_repair_count:
        retry_parts.append(f"repair: {job.verify_repair_count}")
    retries_line = f"\n• <b>Retries:</b> {', '.join(retry_parts)}" if retry_parts else ""

    error_section = ""
    if job.error_code or job.failed_stage:
        err_stage = html.escape(job.failed_stage or "UNKNOWN")
        err_code = html.escape(job.error_code or "ERROR")
        err_det = html.escape(job.error_detail[:200] + "..." if job.error_detail and len(job.error_detail) > 200 else (job.error_detail or ""))
        error_section = (
            f"\n\n⚠️ <b>Failure Details:</b>\n"
            f"• <b>Stage:</b> <code>{err_stage}</code>\n"
            f"• <b>Error:</b> <code>{err_code}</code>\n"
            f"• <i>{err_det}</i>"
        )
        if job.auto_diagnostics_json:
            diags = job.auto_diagnostics_json if isinstance(job.auto_diagnostics_json, list) else [job.auto_diagnostics_json]
            if diags:
                latest = diags[-1]
                probe_summary = latest.get("summary")
                if probe_summary:
                    error_section += f"\n• <b>Probe:</b> <code>{html.escape(str(probe_summary))}</code>"

    audio_line = ""
    if job.audio_duration_seconds:
        dur_str = format_duration_sec(job.audio_duration_seconds)
        size_mb = (job.audio_bytes / (1024 * 1024)) if job.audio_bytes else 0
        audio_line = f"\n• <b>Audio Output:</b> {dur_str} ({size_mb:.1f} MB)"

    card = (
        f"🛠️ <b>Diagnostics: {title}</b>\n\n"
        f"• <b>Job ID:</b> <code>{short_id}</code> (<code>{job.id}</code>)\n"
        f"• <b>Status:</b> <code>{status}</code>\n"
        f"• <b>Mode:</b> {mode_esc}\n"
        f"• <b>Source Type:</b> <code>{src_type}</code>\n"
        f"{ai_line}\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{chunks_str}"
        f"{audio_line}\n"
        f"• <b>Created:</b> {created_str}\n"
        f"• <b>Processing Time:</b> {proc_time_str}"
        f"{retries_line}"
        f"{error_section}\n\n"
        f"📦 <i>Downloading diagnostic support package (ZIP)...</i>"
    )

    if len(card) > 4000:
        card = card[:3900] + "\n\n<i>[Truncated to fit Telegram limits]</i>"

    return card


def format_voices_browser(current_default: str) -> tuple[str, dict[str, Any]]:
    """
    Format interactive voice browser message and generate inline keyboard markup.
    Returns:
        (text, reply_markup_dict)
    """
    from herald.services.voice_manager import get_all_voice_metadata

    curr_clean = current_default.lower().strip()
    voices = get_all_voice_metadata()

    lines = [
        "🗣️ <b>Herald Voice Catalog</b>\n",
        "Select a voice below to preview a sample or set your default voice:\n",
    ]

    keyboard = []
    for meta in voices:
        vid = meta["voice_id"]
        dname = meta["display_name"]
        gender = meta["gender"]
        desc = meta["description"]
        is_curr = vid == curr_clean

        marker = " 🟢 <i>(Default)</i>" if is_curr else ""
        lines.append(f"• <b>{html.escape(dname)}</b> (<code>{html.escape(vid)}</code>) — <i>{html.escape(gender)}</i>{marker}\n  {html.escape(desc)}")

        btn_sample = {
            "text": f"🔊 Sample {dname}",
            "callback_data": f"h2:voice:sample:{vid}",
        }
        btn_set = {
            "text": "✅ Selected" if is_curr else f"Use {dname}",
            "callback_data": f"h2:voice:set:{vid}",
        }
        keyboard.append([btn_sample, btn_set])

    keyboard.append([
        {
            "text": "← Back to Settings",
            "callback_data": "h2:settings:main",
        }
    ])

    lines.append("\n<i>Tip: You can also use <code>Voice: &lt;name&gt;</code> at the top of any message.</i>")
    text = "\n".join(lines)
    reply_markup = {"inline_keyboard": keyboard}

    return text, reply_markup


def format_first_chunk_progress(
    job: PodcastJob,
    total_chunks: int,
    eta_range: str,
    completed_chunks: int = 1,
) -> str:
    """
    Format milestone notification card sent when TTS synthesis progress is reported.
    Includes truthful AI provider/model attribution, Kokoro voice/speed, chunk progress, and updated ETA.
    """
    title_raw = get_job_display_title(job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)
    short_id = html.escape(job.id[:8])

    # Truthful attribution
    ai_prov, ai_model = get_job_ai_identity(job)
    if job.request_mode == RequestMode.LITERAL.value:
        ai_line = "• <b>Script:</b> Literal reader (zero AI calls)"
    elif ai_prov and ai_model:
        ai_line = f"• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>"
    else:
        ai_line = "• <b>AI:</b> <code>None</code>"

    voice = html.escape(job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    tts_line = f"• <b>Voice Synthesis:</b> Kokoro TTS (<code>{voice}</code> @ {speed:.1f}x)"

    if completed_chunks > 1:
        prog_str = f"{completed_chunks}/{total_chunks} segments completed"
    else:
        prog_str = f"First segment synthesized (1/{total_chunks})"

    return (
        f"⏳ <b>Audio Synthesis in Progress</b>\n\n"
        f"<b>{title}</b>\n"
        f"• <b>Progress:</b> {prog_str}\n"
        f"• <b>Remaining ETA:</b> {html.escape(eta_range)}\n"
        f"{ai_line}\n"
        f"{tts_line}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>\n\n"
        f"<i>Encoding and delivering audio file as soon as all segments complete.</i>"
    )


def format_generation_failure_card(
    job: PodcastJob | None = None,
    job_id: str | None = None,
    status: str = JobState.FAILED_FINAL.value,
    error_message: str | None = None,
    db: Any = None,
) -> str:
    """
    Format full failed-job card for failed generation / scripting jobs.
    Includes status (e.g. FAILED_FINAL), job ID, concise diagnostic summary,
    and copyable /diagnostics command.
    """
    effective_id = job.id if job else (job_id or "")
    short_id = html.escape(effective_id[:8]) if effective_id else "unknown"
    effective_status = status or (job.status if job else JobState.FAILED_FINAL.value)
    status_escaped = html.escape(effective_status)

    safe_msg = html.escape(
        error_message
        or (getattr(job, "error_detail", None) or getattr(job, "error_message", None) if job else "")
        or "An error occurred while processing the request."
    )

    diag_line = ""
    try:
        from herald.services.failure_diagnostics import format_concise_failure_summary

        diag_record = None
        if job and job.auto_diagnostics_json:
            diag_record = job.auto_diagnostics_json[-1]
        elif job and job.diagnostic_context:
            diag_record = job.diagnostic_context
        elif db and effective_id:
            job_rec = db.query(PodcastJob).filter(PodcastJob.id == effective_id).first()
            if job_rec and job_rec.auto_diagnostics_json:
                diag_record = job_rec.auto_diagnostics_json[-1]
            elif job_rec and job_rec.diagnostic_context:
                diag_record = job_rec.diagnostic_context

        if diag_record and isinstance(diag_record, dict):
            diag_line = f"\n{format_concise_failure_summary(diag_record)}"
    except Exception as diag_err:
        logger.debug("Could not format failure diagnostic line: %s", diag_err)

    return (
        f"❌ <b>Podcast Generation Failed</b>\n\n"
        f"• <b>ID:</b> <code>{short_id}</code>\n"
        f"• <b>Status:</b> <code>{status_escaped}</code>\n"
        f"• <b>Reason:</b> {safe_msg}{diag_line}\n\n"
        f"Use <code>/diagnostics {short_id}</code> for support details."
    )

