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
