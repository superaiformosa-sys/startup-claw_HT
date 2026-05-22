"""
Weekly report email sender.
Reads config from env vars: EMAIL_SENDER, EMAIL_PASSWORD, REPORT_RECIPIENTS (comma-separated).
Attaches the report as a PDF (via weasyprint) with the HTML as a fallback body.
Falls back to writing a local HTML file if SMTP fails after MAX_RETRIES attempts.
"""
import io
import logging
import os
import smtplib
import time
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
MAX_RETRIES = 3

EMAIL_SENDER      = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD    = os.environ.get("EMAIL_PASSWORD", "")
REPORT_RECIPIENTS = [
    r.strip() for r in os.environ.get("REPORT_RECIPIENTS", "").split(",") if r.strip()
]


def _is_configured() -> bool:
    return bool(EMAIL_SENDER and EMAIL_PASSWORD and REPORT_RECIPIENTS)


def _html_to_pdf(html_content: str) -> bytes | None:
    try:
        from weasyprint import HTML
        buf = io.BytesIO()
        HTML(string=html_content).write_pdf(buf)
        return buf.getvalue()
    except Exception as e:
        logger.warning("PDF generation failed (%s) — will send HTML only", e)
        return None


def _save_fallback(html_content: str) -> str:
    filename = f"weekly_report_{date.today()}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info("Report saved to file as fallback: %s", filename)
    return filename


def _build_message(html_content: str, subject: str) -> MIMEMultipart:
    pdf_bytes = _html_to_pdf(html_content)

    if pdf_bytes:
        # mixed: PDF attachment + HTML alternative body
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(html_content, "html", "utf-8"))
        part = MIMEApplication(pdf_bytes, _subtype="pdf")
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"weekly_report_{date.today()}.pdf",
        )
        msg.attach(part)
        logger.info("PDF attachment ready (%d KB)", len(pdf_bytes) // 1024)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html_content, "html", "utf-8"))

    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(REPORT_RECIPIENTS)
    return msg


def send_weekly_report(html_content: str, subject: str | None = None) -> bool:
    """
    Send the weekly report via Gmail SMTP with a PDF attachment.

    Returns True if sent successfully.
    Returns False and saves HTML to local file if all retries fail or credentials missing.
    """
    if not _is_configured():
        logger.error(
            "Email not configured. Set EMAIL_SENDER, EMAIL_PASSWORD, "
            "REPORT_RECIPIENTS in .env. Saving report to local file."
        )
        _save_fallback(html_content)
        return False

    if subject is None:
        subject = f"新創情報周報 — {date.today().strftime('%Y-%m-%d')}"

    msg = _build_message(html_content, subject)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
                smtp.sendmail(EMAIL_SENDER, REPORT_RECIPIENTS, msg.as_string())
            logger.info("Email sent to: %s", ", ".join(REPORT_RECIPIENTS))
            return True
        except smtplib.SMTPAuthenticationError as e:
            logger.error("SMTP authentication failed (check EMAIL_PASSWORD): %s", e)
            last_error = e
            break  # auth failure is not retryable
        except Exception as e:
            last_error = e
            wait = 2 ** attempt
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Email attempt %d/%d failed: %s — retrying in %ds",
                    attempt, MAX_RETRIES, e, wait,
                )
                time.sleep(wait)
            else:
                logger.error("Email attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)

    logger.error("All %d email attempts failed. Last error: %s", MAX_RETRIES, last_error)
    _save_fallback(html_content)
    return False


def dry_run(html_content: str) -> None:
    """Log what would be sent without actually sending."""
    pdf_bytes = _html_to_pdf(html_content)
    if not _is_configured():
        logger.info("[dry-run] Email not configured — would save to local file.")
        return
    logger.info(
        "[dry-run] Would send '%s' from %s to: %s (html=%d bytes, pdf=%s)",
        f"新創情報周報 — {date.today()}",
        EMAIL_SENDER,
        ", ".join(REPORT_RECIPIENTS),
        len(html_content),
        f"{len(pdf_bytes) // 1024}KB" if pdf_bytes else "failed",
    )
