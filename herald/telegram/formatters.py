"""
Centralized HTML message formatters for Telegram interface.
All dynamic text values MUST be escaped with html.escape when used in HTML parse_mode.
"""

import html


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
        "/status — Live system health, AI status, and queue depth\n"
        "/ai_check — Fresh AI provider connection test (alias: /ai-check)\n"
        "/queue — Pending and processing jobs\n"
        "/readme — Project documentation\n\n"
        "<i>Upcoming capabilities: /settings, /voices, /download, /diagnostics</i>"
    )


def format_settings(user_prefs: dict, instance_settings: object = None) -> tuple[str, dict]:
    """
    Format settings message and generate inline keyboard markup for confirmation toggle.
    Returns:
        (text, reply_markup_dict)
    """
    confirm_on = bool(user_prefs.get("confirm_before_tts", False))
    default_voice = html.escape(str(user_prefs.get("default_voice", "af_heart")))
    default_speed = float(user_prefs.get("default_speed", 1.0))
    default_mode = html.escape(str(user_prefs.get("default_mode", "standard")).capitalize())
    ai_provider = html.escape(str(user_prefs.get("ai_provider", "None (Literal only)")))

    confirm_str = "🟢 On" if confirm_on else "⚪ Off"
    button_text = "🔕 Disable Confirm Before TTS" if confirm_on else "🔔 Enable Confirm Before TTS"
    button_callback = "h2:settings:confirm:off" if confirm_on else "h2:settings:confirm:on"

    text = (
        "⚙️ <b>Herald Preferences & Settings</b>\n\n"
        f"• <b>Default Mode:</b> <code>{default_mode}</code>\n"
        f"• <b>Default Voice:</b> <code>{default_voice}</code>\n"
        f"• <b>Default Speed:</b> <code>{default_speed:.1f}x</code>\n"
        f"• <b>Confirm Before TTS:</b> {confirm_str}\n"
        f"• <b>AI Provider:</b> <code>{ai_provider}</code>\n\n"
        "<i>Tap below to toggle pre-TTS approval confirmation:</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": button_text,
                    "callback_data": button_callback,
                }
            ]
        ]
    }

    return text, reply_markup
