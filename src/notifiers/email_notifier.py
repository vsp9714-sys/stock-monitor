"""
Email notifier — sends alerts via SMTP (Gmail/any SMTP provider).

Features:
- HTML formatted email with inline styles
- Chart attached as image
- Works with Gmail App Passwords, Outlook, or any SMTP server

Setup (Gmail):
1. Enable 2FA on your Google account
2. Generate an "App Password" at: myaccount.google.com/apppasswords
3. Use that password in secrets.yaml (NOT your regular password)
"""
import logging
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from src.notifiers.base import AlertPayload, BaseNotifier

logger = logging.getLogger(__name__)


class EmailNotifier(BaseNotifier):
    """Sends alerts via SMTP email."""

    def __init__(self, cfg: dict):
        email_cfg = cfg.get("notifications", {}).get("email", {})
        self.smtp_host = email_cfg.get("smtp_host", "smtp.gmail.com")
        self.smtp_port = email_cfg.get("smtp_port", 587)
        self.username = email_cfg.get("username", "")
        self.password = email_cfg.get("password", "")
        self.from_addr = email_cfg.get("from_addr", self.username)
        self.to_addrs = email_cfg.get("to_addrs", [])

        if not self.username or not self.password or not self.to_addrs:
            raise ValueError("Email username, password, and to_addrs must be configured.")

    def _build_html(self, payload: AlertPayload) -> str:
        color = "#3fb950" if payload.direction == "BUY" else "#f85149"
        direction_emoji = "🟢" if payload.direction == "BUY" else "🔴"
        profit_trend = payload.format_profit_trend()

        return f"""
        <html><body style="font-family: Arial, sans-serif; background: #0d1117; color: #e6edf3; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; border: 1px solid #21262d; border-radius: 8px; overflow: hidden;">
          <div style="background: {color}22; border-left: 4px solid {color}; padding: 16px 20px;">
            <h2 style="margin: 0; color: {color};">{direction_emoji} {payload.symbol} — {payload.direction} Signal</h2>
            <p style="margin: 4px 0 0; color: #8b949e;">{payload.format_signal_label()}</p>
          </div>
          <div style="padding: 20px;">
            <p><strong>{payload.company_name}</strong></p>
            <table style="width: 100%; border-collapse: collapse;">
              <tr><td style="padding: 6px 0; color: #8b949e;">Price</td><td><strong>₹{payload.price:,.2f}</strong></td></tr>
              <tr><td style="padding: 6px 0; color: #8b949e;">Market Cap</td><td>{payload.format_mcap()} ({(payload.cap_category or 'N/A').title()} Cap)</td></tr>
              {'<tr><td style="padding: 6px 0; color: #8b949e;">Sector</td><td>' + str(payload.sector) + '</td></tr>' if payload.sector else ''}
              {'<tr><td style="padding: 6px 0; color: #8b949e;">P/E Ratio</td><td>' + f"{payload.pe_ratio:.1f}x" + '</td></tr>' if payload.pe_ratio else ''}
              {'<tr><td style="padding: 6px 0; color: #8b949e;">ROE</td><td>' + f"{payload.roe:.1f}%" + '</td></tr>' if payload.roe else ''}
              {'<tr><td style="padding: 6px 0; color: #8b949e;">Promoter Holding</td><td>' + f"{payload.promoter_holding_pct:.1f}%" + '</td></tr>' if payload.promoter_holding_pct else ''}
            </table>
            {'<div style="margin-top: 12px; padding: 10px; background: #161b22; border-radius: 4px;"><p style="margin: 0 0 6px; color: #8b949e;">Quarterly Net Profit (Last 4Q)</p><p style="margin: 0; font-family: monospace;">' + profit_trend + '</p></div>' if profit_trend != 'N/A' else ''}
            {'<p style="margin-top: 12px;"><a href="' + str(payload.screener_url) + '" style="color: #58a6ff;">View on Screener.in →</a></p>' if payload.screener_url else ''}
          </div>
        </div>
        </body></html>
        """

    def _send_email(self, subject: str, html_body: str, image_path: str = None) -> bool:
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)

        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)
        msg_alt.attach(MIMEText(html_body, "html"))

        if image_path and Path(image_path).exists():
            with open(image_path, "rb") as img:
                img_data = MIMEImage(img.read())
                img_data.add_header("Content-ID", "<chart>")
                img_data.add_header("Content-Disposition", "attachment", filename="chart.png")
                msg.attach(img_data)

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(self.username, self.password)
                server.sendmail(self.from_addr, self.to_addrs, msg.as_string())
            logger.info(f"Email sent to {self.to_addrs}")
            return True
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False

    def send_alert(self, payload: AlertPayload) -> bool:
        subject = f"[{payload.direction}] {payload.symbol} — {payload.format_signal_label()}"
        html = self._build_html(payload)
        return self._send_email(subject, html)

    def send_alert_with_chart(self, payload: AlertPayload) -> bool:
        subject = f"[{payload.direction}] {payload.symbol} — {payload.format_signal_label()}"
        html = self._build_html(payload)
        return self._send_email(subject, html, payload.chart_path)
