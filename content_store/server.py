"""
Minimal stdlib HTTP API that routes binary content through S3.

Designed to be throwaway / low-cost:
  - runs on localhost
  - mints short-lived S3 presigned URLs (xAI PUTs straight to S3)
  - can stream downloads back to the local app

Endpoints
---------
  GET  /health
  POST /v1/uploads              mint put_url + get_url for a new object
  GET  /v1/files/<key>          302 → presigned GET  (?proxy=1 streams body)
  GET  /v1/files/<key>/meta     head object JSON
  DELETE /v1/files/<key>        delete object

POST /v1/uploads JSON body (all optional)::

  {
    "content_type": "video/mp4",
    "ext": "mp4",
    "prefix": "video/",   # under CONTENT_S3_PREFIX
    "name": "optional-stem",
    "ttl": 1800
  }
"""

from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from content_store import s3 as s3lib
from content_store.config import (
    CONTENT_API_TOKEN,
    CONTENT_HOST,
    CONTENT_PORT,
    CONTENT_S3_BUCKET,
    CONTENT_S3_PREFIX,
    CONTENT_S3_REGION,
    CONTENT_URL_TTL,
    normalize_prefix,
    require_bucket,
)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not CONTENT_API_TOKEN:
        return True
    auth = handler.headers.get("Authorization") or ""
    if auth == f"Bearer {CONTENT_API_TOKEN}":
        return True
    if handler.headers.get("X-Content-Token") == CONTENT_API_TOKEN:
        return True
    return False


def _parse_key(path: str) -> str | None:
    """Extract key from /v1/files/<key>[/meta|/download]."""
    prefix = "/v1/files/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :]
    for suffix in ("/meta", "/download"):
        if rest.endswith(suffix):
            rest = rest[: -len(suffix)]
            break
    key = unquote(rest).lstrip("/")
    if not key or ".." in key.split("/"):
        return None
    return key


class ContentHandler(BaseHTTPRequestHandler):
    server_version = "content-store/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # quieter than default; still useful
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._dispatch_get()
        except Exception as exc:  # noqa: BLE001
            _json_response(
                self,
                500,
                {"error": str(exc), "trace": traceback.format_exc().splitlines()[-3:]},
            )

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._dispatch_post()
        except Exception as exc:  # noqa: BLE001
            _json_response(
                self,
                500,
                {"error": str(exc), "trace": traceback.format_exc().splitlines()[-3:]},
            )

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            self._dispatch_delete()
        except Exception as exc:  # noqa: BLE001
            _json_response(
                self,
                500,
                {"error": str(exc), "trace": traceback.format_exc().splitlines()[-3:]},
            )

    def _require_auth(self) -> bool:
        if _authorized(self):
            return True
        _json_response(self, 401, {"error": "unauthorized"})
        return False

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/health":
            info: dict[str, Any] = {
                "ok": True,
                "bucket": CONTENT_S3_BUCKET or None,
                "prefix": CONTENT_S3_PREFIX,
                "region": CONTENT_S3_REGION,
                "ttl": CONTENT_URL_TTL,
            }
            if CONTENT_S3_BUCKET:
                try:
                    info["s3"] = s3lib.ensure_bucket_access()
                except Exception as exc:  # noqa: BLE001
                    info["ok"] = False
                    info["s3_error"] = str(exc)
            else:
                info["ok"] = False
                info["s3_error"] = "CONTENT_S3_BUCKET not set"
            _json_response(self, 200 if info["ok"] else 503, info)
            return

        if path == "/":
            _json_response(
                self,
                200,
                {
                    "service": "content-store",
                    "docs": {
                        "POST /v1/uploads": "mint S3 put_url + get_url",
                        "GET /v1/files/<key>": "302 to presigned GET (?proxy=1 streams)",
                        "GET /v1/files/<key>/meta": "head object",
                        "DELETE /v1/files/<key>": "delete object",
                        "GET /health": "liveness + bucket check",
                    },
                },
            )
            return

        if not self._require_auth():
            return

        key = _parse_key(path)
        if key is None:
            _json_response(self, 404, {"error": "not found"})
            return

        if path.endswith("/meta"):
            meta = s3lib.head(key)
            if meta is None:
                _json_response(self, 404, {"error": "not found", "key": key})
                return
            _json_response(
                self,
                200,
                {
                    "key": key,
                    "bucket": require_bucket(),
                    "content_type": meta.get("ContentType"),
                    "content_length": meta.get("ContentLength"),
                    "etag": meta.get("ETag"),
                    "last_modified": meta.get("LastModified").isoformat()
                    if meta.get("LastModified")
                    else None,
                },
            )
            return

        # stream or redirect
        proxy = qs.get("proxy", ["0"])[0] in ("1", "true", "yes")
        if proxy or path.endswith("/download"):
            data, ctype = s3lib.get_bytes(key)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{key.split("/")[-1]}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        url = s3lib.presign_get(key)
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _dispatch_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != "/v1/uploads":
            _json_response(self, 404, {"error": "not found"})
            return
        if not self._require_auth():
            return

        body = _read_json(self)
        content_type = str(body.get("content_type") or "video/mp4")
        ext = str(body.get("ext") or "mp4")
        name = body.get("name")
        name_s = str(name) if name else None
        sub = body.get("prefix")
        # sub-prefix nests under CONTENT_S3_PREFIX
        base = normalize_prefix()
        if sub:
            nested = normalize_prefix(str(sub))
            full_prefix = base + nested
        else:
            full_prefix = base
        ttl = int(body.get("ttl") or CONTENT_URL_TTL)
        ttl = max(60, min(ttl, 12 * 3600))

        key = s3lib.make_key(ext=ext, prefix=full_prefix, name=name_s)
        put_url = s3lib.presign_put(key, content_type=content_type, expires_in=ttl)
        get_url = s3lib.presign_get(key, expires_in=ttl)

        # Local proxy paths (for the app; not for xAI)
        host = self.headers.get("Host") or f"{CONTENT_HOST}:{CONTENT_PORT}"
        scheme = "http"
        encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
        proxy_base = f"{scheme}://{host}/v1/files/{encoded_key}"

        _json_response(
            self,
            201,
            {
                "key": key,
                "bucket": require_bucket(),
                "content_type": content_type,
                "expires_in": ttl,
                # Give this to xAI as output.upload_url
                "put_url": put_url,
                # Direct S3 GET (expires)
                "get_url": get_url,
                # Local API routes
                "proxy_url": f"{proxy_base}?proxy=1",
                "redirect_url": proxy_base,
                "meta_url": f"{proxy_base}/meta",
            },
        )

    def _dispatch_delete(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._require_auth():
            return
        key = _parse_key(path)
        if key is None:
            _json_response(self, 404, {"error": "not found"})
            return
        s3lib.delete(key)
        _json_response(self, 200, {"deleted": True, "key": key})


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or CONTENT_HOST
    port = port if port is not None else CONTENT_PORT
    httpd = ThreadingHTTPServer((host, port), ContentHandler)
    print(
        f"content-store listening on http://{host}:{port}\n"
        f"  bucket={CONTENT_S3_BUCKET or '(unset — set CONTENT_S3_BUCKET)'}\n"
        f"  prefix={CONTENT_S3_PREFIX}  region={CONTENT_S3_REGION}\n"
        f"  POST /v1/uploads  → mint put_url for xAI ZDR\n"
        f"  GET  /health",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        httpd.server_close()
