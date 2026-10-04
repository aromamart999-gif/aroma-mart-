#!/usr/bin/env python3
"""Production WSGI entry point, served by Waitress behind Caddy."""

from __future__ import annotations

import io
import os
import sys
from email.message import Message
from http import HTTPStatus

import server
from waitress import serve


class WSGIRequest(server.Handler):
    """Small WSGI adapter so the existing route logic runs under Waitress."""

    def __init__(self, environ: dict):
        self.command = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        query = environ.get("QUERY_STRING", "")
        self.path = path + ("?" + query if query else "")
        self.client_address = (
            environ.get("REMOTE_ADDR", ""),
            int(environ.get("REMOTE_PORT", "0") or "0"),
        )
        self.headers = Message()
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").title()
                self.headers.add_header(name, value)
        for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(key):
                self.headers.add_header(key.replace("_", "-"), environ[key])

        content_length = int(environ.get("CONTENT_LENGTH") or "0")
        self.rfile = io.BytesIO(environ["wsgi.input"].read(content_length))
        self.wfile = io.BytesIO()
        self.status = HTTPStatus.OK.value
        self.response_headers: list[tuple[str, str]] = []

    def send_response(self, code: int, message: str | None = None) -> None:
        self.status = int(code)
        self.response_headers = []

    def send_header(self, key: str, value: str) -> None:
        self.response_headers.append((str(key), str(value)))

    def end_headers(self) -> None:
        pass


def application(environ: dict, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method == "HEAD":
        method = "GET"
        head_only = True
    else:
        head_only = False

    handler = WSGIRequest(environ)
    route = {"GET": "do_GET", "POST": "do_POST", "PUT": "do_PUT", "DELETE": "do_DELETE"}.get(method)
    if not route:
        payload = b"Method not allowed."
        start_response("405 Method Not Allowed", [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Allow", "GET, HEAD, POST, PUT, DELETE"),
            ("X-Content-Type-Options", "nosniff"),
        ])
        return [b"" if head_only else payload]

    try:
        getattr(handler, route)()
        body = handler.wfile.getvalue()
        reason = HTTPStatus(handler.status).phrase
        start_response(f"{handler.status} {reason}", handler.response_headers)
        return [b"" if head_only else body]
    except Exception as error:
        # Keep request payloads, access tokens and customer details out of logs.
        sys.stderr.write(f"AMORA request failed ({type(error).__name__}).\n")
        payload = b'{"error":"The store is temporarily unavailable."}'
        start_response("500 Internal Server Error", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ])
        return [payload]


def main() -> None:
    server.init_db()
    host = os.environ.get("AMORA_HOST", "127.0.0.1")
    port = int(os.environ.get("AMORA_PORT", "4173"))
    if host == "0.0.0.0":
        # The deployment compose file does not publish this app port; Caddy is
        # its only peer on a private network and overwrites forwarded headers.
        proxy_settings = {
            "trusted_proxy": "*",
            "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"},
            "clear_untrusted_proxy_headers": True,
        }
    else:
        proxy_settings = {}
    print(f"AMORA MART production WSGI app listening on {host}:{port}")
    serve(
        application,
        host=host,
        port=port,
        threads=8,
        max_request_header_size=16 * 1024,
        max_request_body_size=server.MAX_BODY,
        expose_tracebacks=False,
        **proxy_settings,
    )


if __name__ == "__main__":
    main()
