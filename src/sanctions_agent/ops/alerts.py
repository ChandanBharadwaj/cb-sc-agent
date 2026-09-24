"""Alert delivery for incidents: log, webhook (Slack/Teams-compatible JSON) and email.

Deduplicated through the incident record: a new incident alerts once; open PAGE incidents re-alert
every ``page_realert_minutes``; a disabled core list re-alerts every ``core_disabled_realert_hours``.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import httpx
import psycopg

from sanctions_agent.db.engine import fetch_all
from sanctions_agent.logs import get_logger
from sanctions_agent.ops import system_settings
from sanctions_agent.settings import get_settings

log = get_logger(__name__)
_SEV = {"INFO": 0, "WARN": 1, "PAGE": 2}


def _format(inc: dict[str, Any]) -> str:
    return (
        f"[{inc['severity']}] {inc['title']}\n"
        f"source: {inc['source_id'] or 'global'} | class: {inc['error_class']} | seen {inc['occurrences']}x\n"
        f"{inc.get('summary') or ''}\n{('diagnosis: ' + inc['diagnosis']) if inc.get('diagnosis') else ''}"
    ).strip()


def send(inc: dict[str, Any], channels: list[str]) -> list[str]:
    s = get_settings()
    text = _format(inc)
    delivered = []
    if "log" in channels:
        log.warning("alert", incident_id=inc["incident_id"], severity=inc["severity"], title=inc["title"])
        delivered.append("log")
    if "webhook" in channels and s.alert_webhook_url:
        try:
            httpx.post(
                s.alert_webhook_url,
                json={"text": text, "incident": {k: str(v) for k, v in inc.items()}},
                timeout=10,
            ).raise_for_status()
            delivered.append("webhook")
        except httpx.HTTPError as e:
            log.error("alert_webhook_failed", error=str(e))
    if "email" in channels and s.smtp_host and s.alert_email_to:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[sanctions-agent][{inc['severity']}] {inc['title']}"
            msg["From"] = s.smtp_from
            msg["To"] = s.alert_email_to
            msg.set_content(text)
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if s.smtp_user and s.smtp_password:
                    smtp.login(s.smtp_user, s.smtp_password.get_secret_value())
                smtp.send_message(msg)
            delivered.append("email")
        except (OSError, smtplib.SMTPException) as e:
            log.error("alert_email_failed", error=str(e))
    return delivered


def dispatch(conn: psycopg.Connection[Any], sender: Any = send) -> int:
    cfg = system_settings.get_all(conn)
    channels = cfg.get("alert_channels") or ["log"]
    due = fetch_all(
        conn,
        """SELECT * FROM incident WHERE status = 'OPEN' AND (
               alerts_sent = 0
            OR (severity = 'PAGE' AND last_alert_at < now() - make_interval(mins => %s))
            OR (error_class = 'CORE_SOURCE_DISABLED' AND last_alert_at < now() - make_interval(hours => %s)))
           ORDER BY opened_at""",
        (int(cfg["page_realert_minutes"]), int(cfg["core_disabled_realert_hours"])),
    )
    n = 0
    for inc in due:
        if _SEV[inc["severity"]] < _SEV["WARN"] and inc["alerts_sent"]:
            continue
        sender(inc, channels)
        conn.execute(
            "UPDATE incident SET alerts_sent = alerts_sent + 1, last_alert_at = now() WHERE incident_id = %s",
            (inc["incident_id"],),
        )
        n += 1
    return n
