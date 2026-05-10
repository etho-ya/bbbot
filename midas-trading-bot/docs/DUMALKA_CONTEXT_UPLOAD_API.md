# Dumalka context bundle upload — API for Risk Engine (Антон)

Midas exposes an endpoint so Думалка / RE can push **one bundle per release** (or ad-hoc): optional **binary artifact**, **documentation** (Markdown recommended), and **changelog** text. Files are stored on the bot host under `dumalka_uploads/` for operator review and context continuity.

## Authentication

Same as `/dumalka/command` and `/dumalka/positions`:

| Header | Value |
|--------|--------|
| `X-Dumalka-Token` | Shared secret = `DUMALKA_TOKEN` on Midas (agreed with Дмитрий) |

If `DUMALKA_TOKEN` is unset on Midas, behaviour matches other Dumalka routes (open auth — not recommended in production).

## Upload bundle

**`POST /dumalka/context-upload`**

- **Content-Type:** `multipart/form-data`
- **Parts (form fields):**

| Field | Required | Description |
|-------|----------|-------------|
| `artifact` | No | File: binary (e.g. engine build, model blob, `.so`). Omit if docs-only. |
| `documentation` | No* | UTF-8 text, Markdown recommended (API notes, zone policy, integration summary). |
| `changelog` | No* | UTF-8 text: what changed on RE / Dumalka side. |
| `version` | No | Semantic or internal version string (e.g. `1.4.2`, `re-2026-03-27`). |
| `build_id` | No | CI / git SHA / build number. |

\* At least one of **`artifact`** (non-empty body), **non-empty `documentation`**, or **non-empty `changelog`** is required.

**Limits** (configurable on Midas via env):

| Setting | Default |
|---------|---------|
| `DUMALKA_UPLOAD_MAX_BINARY_MB` | 64 |
| `DUMALKA_UPLOAD_MAX_TEXT_MB` | 8 (per `documentation` and per `changelog`) |
| `DUMALKA_UPLOAD_DIR` | `<project>/dumalka_uploads` |

**Success response** `200`:

```json
{
  "ok": true,
  "manifest": {
    "bundle_id": "20260327T121530.123456Z_1_4_2_a1b2c3d4",
    "uploaded_at": "2026-03-27T12:15:30.123456Z",
    "version": "1.4.2",
    "build_id": "ci-99281",
    "artifact_filename": "dumalka-engine",
    "artifact_present": true,
    "artifact_sha256": "...",
    "artifact_size_bytes": 12345678,
    "documentation_bytes": 2048,
    "changelog_bytes": 512,
    "files": {
      "artifact": "artifact.bin",
      "documentation": "DOCUMENTATION.md",
      "changelog": "CHANGELOG.md"
    }
  }
}
```

**Errors:**

| HTTP | `error` (examples) |
|------|---------------------|
| 401 | `unauthorized` |
| 400 | `provide at least one of: artifact file, non-empty documentation, non-empty changelog` |
| 413 | `artifact exceeds … bytes` / `documentation exceeds …` / `changelog exceeds …` |

## Read back manifests (no binary download)

**`GET /dumalka/context-upload/latest`** — JSON manifest of the newest bundle only.

**`GET /dumalka/context-upload/recent?limit=10`** — `limit` 1…50, newest first.

Same `X-Dumalka-Token` header. These endpoints do **not** return file bodies; use SSH / volume access on the bot host to read `dumalka_uploads/<bundle_id>/`.

## On-disk layout (on Midas server)

```
dumalka_uploads/
  <bundle_id>/
    manifest.json      # same fields as in API + file names
    artifact.bin       # only if binary was uploaded
    DOCUMENTATION.md
    CHANGELOG.md
```

`bundle_id` is sortable (UTC timestamp prefix + version slug + short random suffix).

## Example: `curl`

```bash
BASE="https://<midas-host>:8001"   # or http + port as deployed
TOKEN="<DUMALKA_TOKEN>"

curl -sS -X POST "${BASE}/dumalka/context-upload" \
  -H "X-Dumalka-Token: ${TOKEN}" \
  -F "version=1.4.2" \
  -F "build_id=$(git rev-parse --short HEAD)" \
  -F "documentation=<./DUMALKA_INTEGRATION.md" \
  -F "changelog=<./CHANGELOG_DUMALKA.txt" \
  -F "artifact=@./dist/dumalka-engine-linux-amd64;type=application/octet-stream"
```

Docs-only (no binary):

```bash
curl -sS -X POST "${BASE}/dumalka/context-upload" \
  -H "X-Dumalka-Token: ${TOKEN}" \
  -F "version=1.4.3" \
  -F "documentation=$(cat ./API_DELTA.md)" \
  -F "changelog=Fixed zone-1 SL sync; see ticket RE-442"
```

Verify latest:

```bash
curl -sS "${BASE}/dumalka/context-upload/latest" \
  -H "X-Dumalka-Token: ${TOKEN}" | jq .
```

## Example: Python (`httpx`)

```python
import httpx

def push_bundle(base_url: str, token: str, version: str, build_id: str,
                doc_path: str, changelog_path: str, artifact_path: str | None):
    files = {}
    data = {"version": version, "build_id": build_id}
    if artifact_path:
        files["artifact"] = open(artifact_path, "rb")
    with open(doc_path, "r", encoding="utf-8") as f:
        data["documentation"] = f.read()
    with open(changelog_path, "r", encoding="utf-8") as f:
        data["changelog"] = f.read()
    try:
        r = httpx.post(
            f"{base_url.rstrip('/')}/dumalka/context-upload",
            headers={"X-Dumalka-Token": token},
            data=data,
            files=files or None,
            timeout=120.0,
        )
        r.raise_for_status()
        return r.json()
    finally:
        if "artifact" in files:
            files["artifact"].close()
```

## Reverse proxy / body size

If Nginx (or similar) fronts Midas, raise `client_max_body_size` above the largest expected artifact (default Midas limit 64 MB binary).

## Contact

Integration questions: **Дмитрий** (Midas). Token: same **`DUMALKA_TOKEN`** as for `/dumalka/command`.
