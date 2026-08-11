import html
from datetime import datetime


def get_canonical_drive_url(file_id: str | None, web_link: str | None) -> str | None:
    """
    Return valid Drive web link if present, or construct canonical fallback URL
    if file_id is present. Returns None if neither is present.
    """
    if web_link and web_link.strip():
        return web_link.strip()
    if file_id and file_id.strip():
        return f"https://drive.google.com/file/d/{file_id.strip()}/view"
    return None


def _format_iso_datetime(iso_str: str | None) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %H:%M UTC")
    except Exception:
        return iso_str


def format_acknowledgment_email(
    job_id: str,
    episode_title: str,
    request_mode: str,
    estimated_minutes: float,
    estimated_completion_range: str,
) -> dict[str, str]:
    """
    Generate HTML acknowledgment reply email with plain-text fallback.
    """
    safe_title = html.escape(episode_title or "Herald Episode")
    safe_mode = html.escape((request_mode or "standard").title())
    safe_eta = html.escape(estimated_completion_range or "10-15 minutes")
    safe_job_id = html.escape(job_id)

    text_body = (
        f"HERALD — SUBMISSION ACKNOWLEDGMENT\n\n"
        f"Your podcast request has been accepted and queued for audio generation.\n\n"
        f"Episode Title: {episode_title}\n"
        f"Requested Format: {request_mode.title()}\n"
        f"Estimated Duration: {estimated_minutes} minutes\n"
        f"Estimated Completion: {estimated_completion_range}\n"
        f"Job ID: {job_id}\n\n"
        f"You will receive another email with your private Google Drive link as soon as audio generation completes."
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Podcast Queued - Herald</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background-color: #f8fafc; margin: 0; padding: 24px 12px;">
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
          <!-- Header -->
          <tr>
            <td style="background-color: #0f172a; padding: 24px; text-align: left;">
              <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 4px;">HERALD</div>
              <h1 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: -0.02em;">Your podcast is in the queue</h1>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding: 24px;">
              <h2 style="margin: 0 0 12px 0; font-size: 18px; font-weight: 700; color: #0f172a;">{safe_title}</h2>
              <p style="margin: 0 0 20px 0; font-size: 14px; color: #475569;">Your source was processed successfully and your script has been generated. Audio synthesis is now underway.</p>
              
              <!-- Details Table -->
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="margin-bottom: 20px; border-top: 1px solid #f1f5f9;">
                <tr>
                  <td style="padding: 10px 0; font-size: 14px; color: #64748b; width: 160px; border-bottom: 1px solid #f1f5f9;">Requested Mode</td>
                  <td style="padding: 10px 0; font-size: 14px; color: #0f172a; font-weight: 600; border-bottom: 1px solid #f1f5f9;">{safe_mode}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f1f5f9;">Estimated Duration</td>
                  <td style="padding: 10px 0; font-size: 14px; color: #0f172a; font-weight: 600; border-bottom: 1px solid #f1f5f9;">{estimated_minutes} minutes</td>
                </tr>
                <tr>
                  <td style="padding: 10px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f1f5f9;">Estimated Completion</td>
                  <td style="padding: 10px 0; font-size: 14px; color: #2563eb; font-weight: 600; border-bottom: 1px solid #f1f5f9;">{safe_eta}</td>
                </tr>
              </table>

              <p style="margin: 0 0 16px 0; font-size: 14px; color: #475569;">You will receive another email with your private Google Drive link as soon as delivery is complete.</p>

              <!-- De-emphasized Job ID -->
              <div style="font-size: 12px; color: #94a3b8; background-color: #f8fafc; padding: 8px 12px; border-radius: 4px; display: inline-block;">
                Job ID: <code style="font-family: monospace; color: #64748b;">{safe_job_id}</code>
              </div>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding: 16px 24px; background-color: #f8fafc; border-top: 1px solid #e2e8f0; text-align: center; font-size: 12px; color: #94a3b8;">
              Herald Email-to-Podcast Automation System
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    return {"text": text_body, "html": html_body}


def format_completion_email(
    job_id: str,
    episode_title: str,
    episode_description: str,
    drive_web_link: str | None,
    duration_seconds: int,
    file_bytes: int,
    request_mode: str,
    source_type: str,
    source_title: str | None,
    script_estimated_minutes: float,
    segments_count: int,
    sha256: str,
    chunk_count: int,
    retry_attempts: int,
    drive_file_id: str | None,
    source_drive_link: str | None = None,
    source_drive_id: str | None = None,
    diagnostics_drive_link: str | None = None,
    diagnostics_drive_id: str | None = None,
    created_at_iso: str = "",
    completed_at_iso: str | None = None,
    gemini_model: str = "gemini-3.5-flash",
    kokoro_voice: str = "af_heart",
    kokoro_speed: float = 1.0,
    script_warnings: list[str] | None = None,
    research_notes_drive_link: str | None = None,
    research_notes_drive_id: str | None = None,
    details_drive_link: str | None = None,
    details_drive_id: str | None = None,
) -> dict[str, str]:
    """
    Generate HTML completion reply email with polished product card design and plain-text fallback.
    Applies canonical Google Drive URL fallbacks when web_link is missing but file_id exists.
    """
    audio_url = get_canonical_drive_url(drive_file_id, drive_web_link) or "#"
    details_url = get_canonical_drive_url(details_drive_id, details_drive_link)
    source_url = get_canonical_drive_url(source_drive_id, source_drive_link)
    diag_url = get_canonical_drive_url(diagnostics_drive_id, diagnostics_drive_link)
    notes_url = get_canonical_drive_url(research_notes_drive_id, research_notes_drive_link)

    safe_title = html.escape(episode_title or "Herald Episode")
    safe_desc = html.escape(episode_description or "")
    safe_audio_url = html.escape(audio_url)
    safe_mode = html.escape((request_mode or "standard").title())
    safe_src_type = html.escape((source_type or "email_body").replace("_", " ").title())
    safe_job_id = html.escape(job_id)
    safe_sha256 = html.escape(sha256 or "N/A")
    safe_model = html.escape(gemini_model or "gemini-3.5-flash")
    safe_voice = html.escape(kokoro_voice or "af_heart")

    dur_mins = duration_seconds // 60
    dur_secs = duration_seconds % 60
    dur_str = f"{dur_mins}m {dur_secs:02d}s" if dur_mins > 0 else f"{dur_secs}s"
    size_mb = f"{(file_bytes / (1024 * 1024)):.2f} MB"

    created_fmt = _format_iso_datetime(created_at_iso)
    completed_fmt = _format_iso_datetime(completed_at_iso)

    proc_time_str = ""
    if created_at_iso and completed_at_iso:
        try:
            t0 = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(completed_at_iso.replace("Z", "+00:00"))
            elapsed = int((t1 - t0).total_seconds())
            if elapsed > 0:
                e_min = elapsed // 60
                e_sec = elapsed % 60
                proc_time_str = f" ({e_min}m {e_sec:02d}s processing time)"
        except Exception:
            pass

    # Details Companion Link HTML
    if details_url:
        details_link_html = f'<tr><td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Episode Details File</td><td style="padding: 6px 0; font-size: 14px; border-bottom: 1px solid #f8fafc;"><a href="{html.escape(details_url)}" target="_blank" style="color: #2563eb; font-weight: 600; text-decoration: none;">View Companion Details (.md)</a></td></tr>'
        details_text_val = details_url
    else:
        details_link_html = ""
        details_text_val = None

    # Source link HTML (fallback for legacy)
    if source_url:
        source_link_html = f'<tr><td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Original Source</td><td style="padding: 6px 0; font-size: 14px; border-bottom: 1px solid #f8fafc;"><a href="{html.escape(source_url)}" target="_blank" style="color: #2563eb; font-weight: 600; text-decoration: none;">View source</a></td></tr>'
        source_text_val = source_url
    else:
        source_link_html = ""
        source_text_val = None

    # Diagnostics link HTML (fallback for legacy)
    if diag_url:
        diag_link_html = f'<tr><td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Diagnostics</td><td style="padding: 6px 0; font-size: 14px; border-bottom: 1px solid #f8fafc;"><a href="{html.escape(diag_url)}" target="_blank" style="color: #2563eb; font-weight: 600; text-decoration: none;">View stats</a></td></tr>'
        diag_text_val = diag_url
    else:
        diag_link_html = ""
        diag_text_val = None

    # Research Notes link HTML (fallback for legacy)
    if notes_url:
        notes_link_html = f'<tr><td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Research Notes</td><td style="padding: 6px 0; font-size: 14px; border-bottom: 1px solid #f8fafc;"><a href="{html.escape(notes_url)}" target="_blank" style="color: #2563eb; font-weight: 600; text-decoration: none;">View Research Notes</a></td></tr>'
        notes_text_val = notes_url
    else:
        notes_link_html = ""
        notes_text_val = None


    warnings_html = ""
    if script_warnings:
        safe_warns = "<br>".join(html.escape(w) for w in script_warnings)
        warnings_html = f'<tr><td style="padding: 6px 0; font-size: 13px; color: #64748b; width: 140px;">Script Warnings</td><td style="padding: 6px 0; font-size: 13px; color: #dc2626; font-weight: 500;">{safe_warns}</td></tr>'

    text_files_str = f"- Audio MP3: {audio_url}\n"
    if details_text_val:
        text_files_str += f"- Details (.md): {details_text_val}\n"
    if source_text_val:
        text_files_str += f"- Original Source: {source_text_val}\n"
    if notes_text_val:
        text_files_str += f"- Research Notes: {notes_text_val}\n"
    if diag_text_val:
        text_files_str += f"- Diagnostics: {diag_text_val}\n"

    text_body = (
        f"HERALD — EPISODE READY\n\n"
        f"{episode_title}\n"
        f"{episode_description}\n\n"
        f"LISTEN ON GOOGLE DRIVE:\n{audio_url}\n\n"
        f"EPISODE DETAILS:\n"
        f"Duration: {dur_str}\n"
        f"Requested Mode: {request_mode.title()}\n"
        f"Source Type: {source_type}\n"
        f"Script Estimate: {script_estimated_minutes} min\n"
        f"Segments: {segments_count}\n\n"
        f"FILES:\n"
        f"{text_files_str}\n"
        f"STATS FOR NERDS:\n"
        f"- Job ID: {job_id}\n"
        f"- SHA-256: {sha256}\n"
        f"- TTS Chunks: {chunk_count}\n"
        f"- Retry Attempts: {retry_attempts}\n"
        f"- Gemini Model: {gemini_model}\n"
        f"- Voice / Speed: {kokoro_voice} @ {kokoro_speed}x\n"
        f"- Created: {created_fmt}\n"
        f"- Completed: {completed_fmt}{proc_time_str}\n"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title} - Herald</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background-color: #f8fafc; margin: 0; padding: 24px 12px;">
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
          <!-- Header -->
          <tr>
            <td style="background-color: #0f172a; padding: 24px; text-align: left;">
              <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 4px;">HERALD</div>
              <h1 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: -0.02em;">Your episode is ready</h1>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding: 24px;">
              <h2 style="margin: 0 0 8px 0; font-size: 20px; font-weight: 700; color: #0f172a;">{safe_title}</h2>
              <p style="margin: 0 0 24px 0; font-size: 15px; color: #475569; line-height: 1.6;">{safe_desc}</p>
              
              <!-- Primary CTA Button -->
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="margin-bottom: 24px;">
                <tr>
                  <td align="center">
                    <a href="{safe_audio_url}" target="_blank" style="display: inline-block; background-color: #2563eb; color: #ffffff !important; font-weight: 600; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-size: 16px; box-shadow: 0 2px 4px rgba(37,99,235,0.2);">▶ Listen on Google Drive</a>
                    <p style="margin: 10px 0 0 0; font-size: 13px; color: #64748b; font-weight: 500;">{dur_str} &bull; {safe_mode} &bull; {size_mb}</p>
                  </td>
                </tr>
              </table>

              <!-- Episode Details Section -->
              <div style="font-size: 12px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid #f1f5f9; padding-bottom: 6px; margin-bottom: 12px;">Episode Details</div>
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="margin-bottom: 24px;">
                <tr>
                  <td style="padding: 6px 0; font-size: 14px; color: #64748b; width: 160px; border-bottom: 1px solid #f8fafc;">Duration</td>
                  <td style="padding: 6px 0; font-size: 14px; color: #0f172a; font-weight: 500; border-bottom: 1px solid #f8fafc;">{dur_str}</td>
                </tr>
                <tr>
                  <td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Requested Mode</td>
                  <td style="padding: 6px 0; font-size: 14px; color: #0f172a; font-weight: 500; border-bottom: 1px solid #f8fafc;">{safe_mode}</td>
                </tr>
                <tr>
                  <td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Source</td>
                  <td style="padding: 6px 0; font-size: 14px; color: #0f172a; font-weight: 500; border-bottom: 1px solid #f8fafc;">{safe_src_type}</td>
                </tr>
                <tr>
                  <td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Script Estimate</td>
                  <td style="padding: 6px 0; font-size: 14px; color: #0f172a; font-weight: 500; border-bottom: 1px solid #f8fafc;">{script_estimated_minutes} min</td>
                </tr>
                <tr>
                  <td style="padding: 6px 0; font-size: 14px; color: #64748b; border-bottom: 1px solid #f8fafc;">Segments</td>
                  <td style="padding: 6px 0; font-size: 14px; color: #0f172a; font-weight: 500; border-bottom: 1px solid #f8fafc;">{segments_count}</td>
                </tr>
              </table>

              <!-- Files Section -->
              <div style="font-size: 12px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid #f1f5f9; padding-bottom: 6px; margin-bottom: 12px;">Files</div>
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="margin-bottom: 24px;">
                <tr>
                  <td style="padding: 6px 0; font-size: 14px; color: #64748b; width: 160px; border-bottom: 1px solid #f8fafc;">Audio</td>
                  <td style="padding: 6px 0; font-size: 14px; border-bottom: 1px solid #f8fafc;"><a href="{safe_audio_url}" target="_blank" style="color: #2563eb; font-weight: 600; text-decoration: none;">Listen</a></td>
                </tr>
                {details_link_html}
                {source_link_html}
                {notes_link_html}
                {diag_link_html}
              </table>


              <!-- Stats for Nerds Section -->
              <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 16px; font-size: 13px;">
                <div style="font-size: 12px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px;">Stats for Nerds</div>
                <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0">
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b; width: 140px;">Job ID</td><td style="padding: 4px 0; font-size: 13px; color: #334155; font-family: monospace;">{safe_job_id}</td></tr>
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b;">SHA-256</td><td style="padding: 4px 0; font-size: 13px; color: #334155; font-family: monospace; word-break: break-all;">{safe_sha256}</td></tr>
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b;">TTS Chunks</td><td style="padding: 4px 0; font-size: 13px; color: #334155;">{chunk_count}</td></tr>
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b;">Retry Attempts</td><td style="padding: 4px 0; font-size: 13px; color: #334155;">{retry_attempts}</td></tr>
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b;">Gemini Model</td><td style="padding: 4px 0; font-size: 13px; color: #334155;">{safe_model}</td></tr>
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b;">Voice / Speed</td><td style="padding: 4px 0; font-size: 13px; color: #334155;">{safe_voice} @ {kokoro_speed}x</td></tr>
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b;">Created</td><td style="padding: 4px 0; font-size: 13px; color: #334155;">{created_fmt}</td></tr>
                  <tr><td style="padding: 4px 0; font-size: 13px; color: #64748b;">Completed</td><td style="padding: 4px 0; font-size: 13px; color: #334155;">{completed_fmt}{proc_time_str}</td></tr>
                  {warnings_html}
                </table>
              </div>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding: 16px 24px; background-color: #f8fafc; border-top: 1px solid #e2e8f0; text-align: center; font-size: 12px; color: #94a3b8;">
              Herald Email-to-Podcast Automation System
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    return {"text": text_body, "html": html_body}


