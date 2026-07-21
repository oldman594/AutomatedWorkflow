from __future__ import annotations

import logging
import queue
import threading
from typing import Any

import httpx

from app.config import Settings


class AlertDispatcher:
    def __init__(self, settings: Settings) -> None:
        self.webhook_url = settings.alert_webhook_url
        self.timeout = settings.alert_timeout_seconds
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1000)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.webhook_url or self.thread:
            return
        self.thread = threading.Thread(
            target=self._run,
            name="autoflow-alerts",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        if not self.thread:
            return
        self.messages.put(None)
        self.thread.join(timeout=self.timeout + 1)
        self.thread = None

    def notify(self, event: str, **payload: Any) -> None:
        message = {"event": event, **payload}
        logging.getLogger("autoflow.alerts").error(event, extra=payload)
        if not self.webhook_url:
            return
        try:
            self.messages.put_nowait(message)
        except queue.Full:
            logging.getLogger("autoflow.alerts").exception("alert.queue_full")

    def _run(self) -> None:
        assert self.webhook_url is not None
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                message = self.messages.get()
                if message is None:
                    return
                try:
                    response = client.post(self.webhook_url, json=message)
                    response.raise_for_status()
                except httpx.HTTPError:
                    logging.getLogger("autoflow.alerts").exception("alert.delivery_failed")
