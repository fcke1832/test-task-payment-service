#!/usr/bin/env python3
"""Небольшой HTTP-приёмник webhook'ов для локальной демонстрации.

Пример:
    python scripts/webhook_receiver.py --port 9000
    python scripts/webhook_receiver.py --port 9000 --fail-first 2
"""

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

logger = logging.getLogger("webhook-receiver")


class WebhookHandler(BaseHTTPRequestHandler):
    """Принимает POST-webhook'и и логирует их содержимое."""

    fail_first: ClassVar[int] = 0
    counter: ClassVar[int] = 0
    counter_lock: ClassVar[threading.Lock] = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 (HTTP method name)
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            payload = {"raw": raw_body.decode(errors="replace")}

        with self.counter_lock:
            self.__class__.counter += 1
            delivery_number = self.__class__.counter
            should_fail = delivery_number <= self.fail_first

        logger.info(
            f"delivery={delivery_number} path={self.path} "
            f"webhook_id={self.headers.get('X-Webhook-Id')} "
            f"event={self.headers.get('X-Event-Type')} "
            f"status={'500' if should_fail else '200'} "
            f"payload={json.dumps(payload, ensure_ascii=False)}"
        )

        status = 500 if should_fail else 200
        body = json.dumps(
            {"received": not should_fail, "delivery": delivery_number}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--fail-first",
        type=int,
        default=0,
        help="Вернуть HTTP 500 для первых N доставок (проверка retry)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    WebhookHandler.fail_first = args.fail_first
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = ThreadingHTTPServer((args.host, args.port), WebhookHandler)
    logger.info(
        f"Webhook receiver listening on http://{args.host}:{args.port} "
        f"(fail_first={args.fail_first})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping webhook receiver")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
