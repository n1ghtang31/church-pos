"""Sending email over SMTP. Without SMTP_HOST, emails are written to the log instead."""

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("sale-pos")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# starttls (port 587), ssl (port 465) or none
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").lower()
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "sale-pos@localhost")


def deliver(message):
    """Hand a message to the mail server. Tests replace this to catch what's sent."""
    if not SMTP_HOST:
        logger.info("SMTP_HOST is not set, so this email was not sent:\n%s", message.get_body(("plain",)).get_content())
        return
    smtp_class = smtplib.SMTP_SSL if SMTP_SECURITY == "ssl" else smtplib.SMTP
    with smtp_class(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        if SMTP_SECURITY == "starttls":
            smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)


def send(to, subject, body, attachments=()):
    """attachments is a list of (filename, text) pairs, sent as CSV files."""
    message = EmailMessage()
    message["From"] = SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    for filename, text in attachments:
        message.add_attachment(text.encode(), maintype="text", subtype="csv", filename=filename)
    deliver(message)
