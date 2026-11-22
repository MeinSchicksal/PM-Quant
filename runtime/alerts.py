"""Lightweight Non-Blocking Ops Alerting for PM-Quant (7x24 Unattended VPS).

Supports zero-extra-dependency push notifications via environment variables:
  - PM_TELEGRAM_BOT_TOKEN + PM_TELEGRAM_CHAT_ID
  - PM_BARK_URL (e.g. https://api.day.app/<your_key>)
  - PM_ALERT_WEBHOOK_URL (Feishu / DingTalk / Discord / Slack / Generic JSON)

All deliveries run on a single background worker thread with per-key rate
limiting (cooldown) so alert storms or network outages can never block the
trading pipeline.
"""
import os
import queue
import socket
import threading
import time
import urllib.parse
from typing import Dict, Optional

import requests

_LOCK = threading.Lock()
_LAST_SENT: Dict[str, float] = {}
_QUEUE: "queue.Queue[tuple]" = queue.Queue(maxsize=64)
_WORKER_STARTED = False
_HOSTNAME = socket.gethostname()


def is_alerting_configured() -> bool:
    """Return True if at least one external alert channel is configured."""
    tg = bool(os.environ.get("PM_TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("PM_TELEGRAM_CHAT_ID", "").strip())
    bark = bool(os.environ.get("PM_BARK_URL", "").strip())
    hook = bool(os.environ.get("PM_ALERT_WEBHOOK_URL", "").strip())
    return tg or bark or hook


def _send_all(title: str, message: str, level: str):
    prefix = f"[PM-Quant/{level.upper()}@{_HOSTNAME}]"
    full_title = f"{prefix} {title}"
    body_text = f"{full_title}\n{message}"

    tg_token = os.environ.get("PM_TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("PM_TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": body_text, "disable_web_page_preview": True},
                timeout=(3.0, 5.0),
            )
        except Exception:
            pass

    bark_url = os.environ.get("PM_BARK_URL", "").strip().rstrip("/")
    if bark_url:
        try:
            enc_title = urllib.parse.quote(full_title, safe="")
            enc_body = urllib.parse.quote(message, safe="")
            requests.get(f"{bark_url}/{enc_title}/{enc_body}", timeout=(3.0, 5.0))
        except Exception:
            pass

    hook_url = os.environ.get("PM_ALERT_WEBHOOK_URL", "").strip()
    if hook_url:
        try:
            low = hook_url.lower()
            if "feishu.cn" in low or "larksuite.com" in low:
                payload = {"msg_type": "text", "content": {"text": body_text}}
            elif "dingtalk.com" in low:
                payload = {"msgtype": "text", "text": {"content": body_text}}
            elif "discord.com" in low or "discordapp.com" in low:
                payload = {"content": body_text}
            else:
                payload = {"title": full_title, "message": message, "level": level, "text": body_text}
            requests.post(hook_url, json=payload, timeout=(3.0, 5.0))
        except Exception:
            pass


def _worker_loop():
    while True:
        try:
            title, message, level = _QUEUE.get()
            _send_all(title, message, level)
        except Exception:
            pass


def _ensure_worker():
    global _WORKER_STARTED
    with _LOCK:
        if _WORKER_STARTED:
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="PMAlertWorker")
        t.start()
        _WORKER_STARTED = True


def notify(
    key: str,
    title: str,
    message: str,
    level: str = "warn",
    cooldown_s: float = 900.0,
    now: Optional[float] = None,
) -> bool:
    """Enqueue a deduplicated alert if any channel is configured.

    Returns True when accepted into the delivery queue, False when suppressed
    by cooldown or when no alert channel is configured.
    """
    if not is_alerting_configured():
        return False
    ts = time.monotonic() if now is None else float(now)
    with _LOCK:
        last_t = _LAST_SENT.get(key)
        if last_t is not None and cooldown_s > 0 and (ts - last_t) < cooldown_s:
            return False
        _LAST_SENT[key] = ts
        if len(_LAST_SENT) > 256:
            oldest = min(_LAST_SENT, key=_LAST_SENT.get)
            _LAST_SENT.pop(oldest, None)
    _ensure_worker()
    try:
        _QUEUE.put_nowait((title, message, level))
        return True
    except queue.Full:
        return False
