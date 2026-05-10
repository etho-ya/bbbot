"""Store Dumalka / RE context bundles (binary + docs + changelog) for operator review."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BINARY_NAME = "artifact.bin"
DOCS_NAME = "DOCUMENTATION.md"
CHANGELOG_NAME = "CHANGELOG.md"
MANIFEST_NAME = "manifest.json"


def _safe_slug(s: str, max_len: int = 48) -> str:
    if not s or not str(s).strip():
        return "unknown"
    cleaned = re.sub(r"[^\w.\-]+", "_", str(s).strip())[:max_len]
    return cleaned or "unknown"


def _bundle_dir_name(version: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{ts}_{_safe_slug(version)}_{uuid.uuid4().hex[:8]}"


def ensure_upload_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)


def list_bundle_dirs(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and (p / MANIFEST_NAME).is_file()]
    dirs.sort(key=lambda p: p.name, reverse=True)
    return dirs


def read_latest_manifest(root: Path) -> Optional[Dict[str, Any]]:
    dirs = list_bundle_dirs(root)
    if not dirs:
        return None
    try:
        with open(dirs[0] / MANIFEST_NAME, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["bundle_id"] = dirs[0].name
        return data
    except (OSError, json.JSONDecodeError):
        return None


def read_recent_manifests(root: Path, limit: int = 10) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for d in list_bundle_dirs(root)[: max(0, limit)]:
        try:
            with open(d / MANIFEST_NAME, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["bundle_id"] = d.name
            out.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def save_context_bundle(
    root: Path,
    max_binary_bytes: int,
    max_text_bytes: int,
    artifact_bytes: Optional[bytes],
    artifact_filename: Optional[str],
    documentation: str,
    changelog: str,
    version: str,
    build_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Write a new bundle under root/<timestamp>_<version_slug>/.
    Returns (manifest_dict, error_message).
    """
    doc_b = documentation.encode("utf-8") if documentation else b""
    chg_b = changelog.encode("utf-8") if changelog else b""

    if len(doc_b) > max_text_bytes:
        return None, f"documentation exceeds {max_text_bytes} bytes"
    if len(chg_b) > max_text_bytes:
        return None, f"changelog exceeds {max_text_bytes} bytes"

    if artifact_bytes is not None and len(artifact_bytes) > max_binary_bytes:
        return None, f"artifact exceeds {max_binary_bytes} bytes"

    has_artifact = artifact_bytes is not None and len(artifact_bytes) > 0
    has_text = bool(documentation.strip()) or bool(changelog.strip())
    if not has_artifact and not has_text:
        return None, "provide at least one of: artifact file, non-empty documentation, non-empty changelog"

    ensure_upload_root(root)
    bundle = root / _bundle_dir_name(version)
    bundle.mkdir(parents=False, exist_ok=False)

    sha256_hex = ""
    if has_artifact:
        sha256_hex = hashlib.sha256(artifact_bytes).hexdigest()
        with open(bundle / BINARY_NAME, "wb") as f:
            f.write(artifact_bytes)

    with open(bundle / DOCS_NAME, "wb") as f:
        f.write(doc_b)
    with open(bundle / CHANGELOG_NAME, "wb") as f:
        f.write(chg_b)

    manifest: Dict[str, Any] = {
        "uploaded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": version or "",
        "build_id": build_id or "",
        "artifact_filename": artifact_filename or "",
        "artifact_present": has_artifact,
        "artifact_sha256": sha256_hex,
        "artifact_size_bytes": len(artifact_bytes) if has_artifact else 0,
        "documentation_bytes": len(doc_b),
        "changelog_bytes": len(chg_b),
        "files": {
            "artifact": BINARY_NAME if has_artifact else None,
            "documentation": DOCS_NAME,
            "changelog": CHANGELOG_NAME,
        },
    }

    with open(bundle / MANIFEST_NAME, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    manifest["bundle_id"] = bundle.name
    return manifest, None
