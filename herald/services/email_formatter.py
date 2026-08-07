import html
from datetime import UTC, datetime


def format_acknowledgment_email(
    job_id: str,
    episode_title: str,
    request_mode: str,
    estimated_minutes: float,
    estimated_completion_range: str,
) -> dict[str, str]:
    """
    Generate HTML-escaped acknowledgment reply email with plain-text fallback.
    """
    safe_title = html.escape(episode_title or "Herald Episode")
    safe_mode = html.escape((request_mode or "standard").title())
    safe_eta = html.escape(estimated_completion_range)
    safe_job_id = html.escape(job_id)

    text_body = (
        f"Herald has accepted your podcast.\n\n"
        f"Title: {episode_title}\n\n"
        f"Your source was processed successfully and your episode is now queued for audio generation.\n\n"
        f"Requested format: {request_mode.title()}\n"
        f"Estimated episode length: {estimated_minutes} minutes\n"
        f"Estimated completion: {estimated_completion_range}\n"
        f"Job ID: {job_id}\n\n"
        f"You'll receive another email with the private Google Drive link when it is ready."
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2937; background-color: #f9fafb; margin: 0; padding: 20px; }}
  .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 8px; border: 1px solid #e5e7eb; overflow: hidden; font-size: 15px; }}
  .header {{ background-color: #1e293b; color: #ffffff; padding: 24px; text-align: left; }}
  .header h1 {{ margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.02em; }}
  .content {{ padding: 24px; }}
  .card {{ background-color: #f8fafc; border-left: 4px solid #3b82f6; padding: 16px; margin: 16px 0; border-radius: 0 6px 6px 0; }}
  .card-title {{ font-weight: 600; font-size: 17px; color: #0f172a; margin-bottom: 4px; }}
  .meta-table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  .meta-table td {{ padding: 8px 0; border-bottom: 1px solid #f1f5f9; }}
  .meta-label {{ color: #64748b; font-size: 14px; width: 180px; }}
  .meta-value {{ font-weight: 500; color: #334155; }}
  .footer {{ padding: 16px 24px; background-color: #f1f5f9; color: #64748b; font-size: 13px; text-align: center; border-top: 1px solid #e2e8f0; }}
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Herald Submission Accepted</h1>
    </div>
    <div class="content">
      <p>Herald has accepted your podcast request.</p>
      
      <div class="card">
        <div class="card-title">{safe_title}</div>
        <p style="margin:4px 0 0 0; color:#475569; font-size:14px;">Your source was processed successfully and your episode is now queued for audio generation.</p>
      </div>

      <table class="meta-table">
        <tr>
          <td class="meta-label">Requested format</td>
          <td class="meta-value">{safe_mode}</td>
        </tr>
        <tr>
          <td class="meta-label">Estimated episode length</td>
          <td class="meta-value">{estimated_minutes} minutes</td>
        </tr>
        <tr>
          <td class="meta-label">Estimated completion</td>
          <td class="meta-value">{safe_eta}</td>
        </tr>
        <tr>
          <td class="meta-label">Job ID</td>
          <td class="meta-value"><code>{safe_job_id}</code></td>
        </tr>
      </table>

      <p style="color:#475569; font-size:14px;">You’ll receive another email with the private Google Drive link when it is ready.</p>
    </div>
    <div class="footer">
      Herald Email-to-Podcast Automation System
    </div>
  </div>
</body>
</html>"""

    return {"text": text_body, "html": html_body}


def format_completion_email(
    job_id: str,
    episode_title: str,
    episode_description: str,
    drive_web_link: str,
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
    drive_file_id: str,
    source_drive_link: str | None,
    source_drive_id: str | None,
    diagnostics_drive_link: str | None,
    diagnostics_drive_id: str | None,
    created_at_iso: str,
    completed_at_iso: str | None,
    gemini_model: str,
    kokoro_voice: str,
    kokoro_speed: float,
    script_warnings: list[str] | None = None,
) -> dict[str, str]:
    """
    Generate HTML-escaped completion email with clean layout and plain-text fallback.
    """
    safe_title = html.escape(episode_title or "Herald Episode")
    safe_desc = html.escape(episode_description or "")
    safe_link = html.escape(drive_web_link or "#")
    safe_mode = html.escape((request_mode or "standard").title())
    safe_src_type = html.escape(source_type or "email_body")
    safe_src_title = html.escape(source_title or "Untitled Source")
    safe_job_id = html.escape(job_id)
    safe_sha256 = html.escape(sha256 or "N/A")
    safe_drive_id = html.escape(drive_file_id or "N/A")
    safe_src_link = html.escape(source_drive_link or "#") if source_drive_link else "N/A"
    safe_diag_link = html.escape(diagnostics_drive_link or "#") if diagnostics_drive_link else "N/A"
    safe_model = html.escape(gemini_model or "gemini-3.5-flash")
    safe_voice = html.escape(kokoro_voice or "af_heart")

    dur_mins = duration_seconds // 60
    dur_secs = duration_seconds % 60
    dur_str = f"{dur_mins}m {dur_secs}s"
    size_mb = f"{(file_bytes / (1024 * 1024)):.2f} MB"

    warnings_html = ""
    if script_warnings:
        safe_warns = "<br>".join(html.escape(w) for w in script_warnings)
        warnings_html = f"<tr><td class='meta-label'>Script Warnings</td><td class='meta-value' style='color:#dc2626;'>{safe_warns}</td></tr>"

    text_body = (
        f"READY: Your Herald podcast episode is ready!\n\n"
        f"Title: {episode_title}\n"
        f"Description: {episode_description}\n\n"
        f"LISTEN:\n{drive_web_link}\n\n"
        f"EPISODE DETAILS:\n"
        f"- Duration: {dur_str}\n"
        f"- File Size: {size_mb}\n"
        f"- Requested Mode: {request_mode.title()}\n"
        f"- Source Type: {source_type}\n"
        f"- Source Title: {source_title or 'N/A'}\n"
        f"- Script Estimated Duration: {script_estimated_minutes} minutes\n"
        f"- Script Segments: {segments_count}\n\n"
        f"STATS FOR NERDS:\n"
        f"- Job ID: {job_id}\n"
        f"- SHA-256: {sha256}\n"
        f"- TTS Chunks: {chunk_count}\n"
        f"- Retry Attempts: {retry_attempts}\n"
        f"- Drive File ID: {drive_file_id}\n"
        f"- Source Artifact Link: {source_drive_link or 'N/A'}\n"
        f"- Diagnostics Artifact Link: {diagnostics_drive_link or 'N/A'}\n"
        f"- Created: {created_at_iso}\n"
        f"- Completed: {completed_at_iso or 'Just now'}\n"
        f"- Model: {gemini_model}\n"
        f"- Voice/Speed: {kokoro_voice} @ {kokoro_speed}x\n"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2937; background-color: #f9fafb; margin: 0; padding: 20px; }}
  .container {{ max-width: 640px; margin: 0 auto; background: #ffffff; border-radius: 8px; border: 1px solid #e5e7eb; overflow: hidden; font-size: 15px; }}
  .header {{ background-color: #0f172a; color: #ffffff; padding: 24px; text-align: left; }}
  .badge {{ display: inline-block; background-color: #22c55e; color: #ffffff; font-weight: 700; font-size: 12px; padding: 2px 8px; border-radius: 4px; text-transform: uppercase; margin-bottom: 8px; }}
  .header h1 {{ margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }}
  .content {{ padding: 24px; }}
  .btn-container {{ text-align: center; margin: 24px 0; }}
  .btn {{ display: inline-block; background-color: #2563eb; color: #ffffff !important; font-weight: 600; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-size: 16px; box-shadow: 0 2px 4px rgba(37,99,235,0.2); }}
  .section-title {{ font-size: 13px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid #f1f5f9; padding-bottom: 6px; margin-top: 24px; margin-bottom: 12px; }}
  .meta-table {{ width: 100%; border-collapse: collapse; }}
  .meta-table td {{ padding: 6px 0; font-size: 14px; border-bottom: 1px solid #f8fafc; }}
  .meta-label {{ color: #64748b; width: 200px; }}
  .meta-value {{ color: #334155; font-weight: 500; word-break: break-all; }}
  .nerd-box {{ background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 16px; font-size: 13px; margin-top: 12px; }}
  .footer {{ padding: 16px 24px; background-color: #f1f5f9; color: #64748b; font-size: 13px; text-align: center; border-top: 1px solid #e2e8f0; }}
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="badge">READY</div>
      <h1>{safe_title}</h1>
    </div>
    <div class="content">
      <p style="color:#475569; font-size:15px; margin-top:0;">{safe_desc}</p>
      
      <div class="btn-container">
        <a href="{safe_link}" class="btn" target="_blank">Listen on Google Drive &rarr;</a>
      </div>

      <div class="section-title">Episode Details</div>
      <table class="meta-table">
        <tr><td class="meta-label">Actual Duration</td><td class="meta-value">{dur_str}</td></tr>
        <tr><td class="meta-label">File Size</td><td class="meta-value">{size_mb}</td></tr>
        <tr><td class="meta-label">Requested Mode</td><td class="meta-value">{safe_mode}</td></tr>
        <tr><td class="meta-label">Source Type</td><td class="meta-value">{safe_src_type}</td></tr>
        <tr><td class="meta-label">Source Title</td><td class="meta-value">{safe_src_title}</td></tr>
        <tr><td class="meta-label">Script Estimated Duration</td><td class="meta-value">{script_estimated_minutes} minutes</td></tr>
        <tr><td class="meta-label">Script Segments</td><td class="meta-value">{segments_count}</td></tr>
      </table>

      <div class="section-title">Stats for Nerds</div>
      <div class="nerd-box">
        <table class="meta-table">
          <tr><td class="meta-label">Job ID</td><td class="meta-value"><code>{safe_job_id}</code></td></tr>
          <tr><td class="meta-label">SHA-256</td><td class="meta-value"><code>{safe_sha256}</code></td></tr>
          <tr><td class="meta-label">TTS Chunk Count</td><td class="meta-value">{chunk_count}</td></tr>
          <tr><td class="meta-label">Retry Attempts</td><td class="meta-value">{retry_attempts}</td></tr>
          <tr><td class="meta-label">Drive File ID</td><td class="meta-value"><code>{safe_drive_id}</code></td></tr>
          <tr><td class="meta-label">Source Artifact</td><td class="meta-value"><a href="{safe_src_link}" target="_blank">View Source Text</a></td></tr>
          <tr><td class="meta-label">Diagnostics Artifact</td><td class="meta-value"><a href="{safe_diag_link}" target="_blank">View Diagnostics JSON</a></td></tr>
          <tr><td class="meta-label">Created Timestamp</td><td class="meta-value">{created_at_iso}</td></tr>
          <tr><td class="meta-label">Completed Timestamp</td><td class="meta-value">{completed_at_iso or 'Just now'}</td></tr>
          <tr><td class="meta-label">Gemini Model</td><td class="meta-value">{safe_model}</td></tr>
          <tr><td class="meta-label">Kokoro Voice / Speed</td><td class="meta-value">{safe_voice} @ {kokoro_speed}x</td></tr>
          {warnings_html}
        </table>
      </div>
    </div>
    <div class="footer">
      Herald Email-to-Podcast Automation System
    </div>
  </div>
</body>
</html>"""

    return {"text": text_body, "html": html_body}
