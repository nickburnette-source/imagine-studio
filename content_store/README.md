# Content-store (throwaway S3 router)

Thin local HTTP API that mints **S3 presigned URLs** so xAI can `PUT` ZDR video
outputs into your bucket, then your app can pull them back.

**Cost model:** API runs on your laptop (free). You only pay for S3 PUT/GET +
storage. Use a lifecycle rule to expire `staging/` after 1 day.

## Setup

```bash
# .env
CONTENT_S3_BUCKET=your-bucket-name
CONTENT_S3_PREFIX=staging/
AWS_REGION=us-east-1
AWS_PROFILE=default   # if you use named profiles / SSO
```

IAM on the bucket (minimum):

- `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `arn:aws:s3:::bucket/staging/*`
- `s3:ListBucket` on the bucket (optional; HeadBucket for `/health`)
- Caller must be allowed to `s3:PutObject` **with the same Content-Type** you presign

Optional 1-day expiry (run once):

```bash
aws s3api put-bucket-lifecycle-configuration --bucket "$CONTENT_S3_BUCKET" --lifecycle-configuration '{
  "Rules": [{
    "ID": "expire-staging",
    "Status": "Enabled",
    "Filter": { "Prefix": "staging/" },
    "Expiration": { "Days": 1 }
  }]
}'
```

## Run

```bash
source .venv/bin/activate
pip install -e .

python -m content_store --check   # HeadBucket
python -m content_store           # http://127.0.0.1:8787
# or: content-store
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness + bucket access |
| `POST` | `/v1/uploads` | Mint `put_url` + `get_url` |
| `GET` | `/v1/files/<key>` | 302 → presigned GET |
| `GET` | `/v1/files/<key>?proxy=1` | Stream body through API |
| `GET` | `/v1/files/<key>/meta` | Head object |
| `DELETE` | `/v1/files/<key>` | Delete object |

### Mint upload (for xAI `output.upload_url`)

```bash
curl -s -X POST http://127.0.0.1:8787/v1/uploads \
  -H 'Content-Type: application/json' \
  -d '{"content_type":"video/mp4","ext":"mp4","prefix":"video/"}' | jq
```

Response:

```json
{
  "key": "staging/video/a1b2….mp4",
  "put_url": "https://….amazonaws.com/…&X-Amz-Signature=…",
  "get_url": "https://…",
  "proxy_url": "http://127.0.0.1:8787/v1/files/…?proxy=1",
  "expires_in": 1800
}
```

Give **`put_url`** to xAI. After generation finishes, download:

```bash
# via API proxy
curl -o out.mp4 'http://127.0.0.1:8787/v1/files/staging%2Fvideo%2F….mp4?proxy=1'

# or direct S3 (get_url from mint response)
curl -o out.mp4 "$GET_URL"
```

### Python helper

```python
from content_store.client import mint_upload, download_to
from pathlib import Path

u = mint_upload(content_type="video/mp4", prefix="video/")
# u["put_url"] → xAI output.upload_url
download_to(u["key"], Path("Idle.mp4"))
```

## Why not proxy the PUT?

xAI must reach a **public** URL. S3 presigned PUT is public and cheap.
Proxying PUT through this API would require a public host (tunnel/VPS) — extra
cost and ops. This service stays on localhost and only **mints** URLs.

## Tear down

1. Stop the process
2. Empty + delete the bucket (or leave lifecycle to drain)
3. Delete `content_store/` if you abandon the experiment
