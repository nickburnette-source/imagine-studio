"""python -m content_store"""

from __future__ import annotations

import argparse

from content_store.config import CONTENT_HOST, CONTENT_PORT
from content_store.server import serve


def main() -> None:
    p = argparse.ArgumentParser(description="Thin S3 content-store API (throwaway)")
    p.add_argument("--host", default=CONTENT_HOST)
    p.add_argument("--port", type=int, default=CONTENT_PORT)
    p.add_argument(
        "--check",
        action="store_true",
        help="HeadBucket + exit (no server)",
    )
    args = p.parse_args()

    if args.check:
        from content_store import s3 as s3lib

        info = s3lib.ensure_bucket_access()
        print("ok", info)
        return

    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
