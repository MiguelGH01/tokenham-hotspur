"""Resolve or download the Krisp VIVA voice-isolation model (.kef).

``KrispVivaFilter`` needs a local ``.kef`` file; ``KRISP_VIVA_API_KEY`` only
licenses the SDK. When the key is set and the model is missing, this module
pulls the voice-isolation zip from Krisp's SDK distribution API and caches it.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from loguru import logger

_KRISP_API = "https://api.developers.krisp.ai"


def existing_filter_model_path() -> str | None:
    """Return a ``.kef`` already on disk (configured path or cache)."""
    configured = os.getenv("KRISP_VIVA_FILTER_MODEL_PATH") or os.getenv(
        "KRISP_VIVA_MODEL_PATH"
    )
    if configured and Path(configured).is_file():
        return configured
    cached = _pick_kef(_cache_dir())
    return str(cached) if cached else None


def ensure_filter_model() -> str | None:
    """Fetch the voice-isolation model at process startup if needed.

    If ``KRISP_VIVA_API_KEY`` is set and no ``.kef`` is present, download it
    now (before the first call). Raises if the key is set but download fails.
    """
    path = existing_filter_model_path()
    if path:
        logger.info("Krisp VIVA model ready at {}", path)
        return path

    api_key = os.getenv("KRISP_VIVA_API_KEY")
    if not api_key:
        logger.info("Krisp VIVA disabled: no API key and no local .kef")
        return None

    logger.info("Krisp VIVA model missing; downloading at startup")
    return _download_voice_isolation_model(api_key, _cache_dir())


def _cache_dir() -> Path:
    override = os.getenv("KRISP_VIVA_MODEL_CACHE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / ".krisp-models"


def _pick_kef(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    kefs = list(directory.rglob("*.kef"))
    if not kefs:
        return None
    for pattern in ("vi-tel", "viva-tel", "-tel-"):
        for path in kefs:
            if pattern in path.name.lower():
                return path
    return kefs[0]


def _download_voice_isolation_model(api_key: str, cache_dir: Path) -> str:
    version_id = os.getenv("KRISP_VIVA_SDK_VERSION_ID") or _discover_version_id(api_key)
    if not version_id:
        raise RuntimeError(
            "Set KRISP_VIVA_SDK_VERSION_ID to the Server SDK version id from "
            "https://developers.krisp.ai (SDK Versions tab)."
        )

    payload = _get_json(
        f"{_KRISP_API}/v2/sdk/versions/{version_id}/download-urls",
        api_key,
    )
    data = payload.get("data") or payload
    models = data.get("models") or []
    model = _select_voice_isolation(models)
    if not model or not model.get("download_url"):
        raise RuntimeError(
            f"SDK version {version_id} has no voice_isolation model download URL."
        )

    logger.info(
        "Downloading Krisp VIVA voice-isolation model ({})",
        model.get("filename") or "model.zip",
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_bytes = _get_bytes(model["download_url"])
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "model.zip"
        archive.write_bytes(zip_bytes)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(cache_dir)

    kef = _pick_kef(cache_dir)
    if not kef:
        raise RuntimeError("Downloaded Krisp archive contained no .kef model file.")
    logger.info("Krisp VIVA model ready at {}", kef)
    return str(kef)


def _discover_version_id(api_key: str) -> str | None:
    """Best-effort list; the public docs only document download-urls by id."""
    try:
        payload = _get_json(f"{_KRISP_API}/v2/sdk/versions", api_key)
    except Exception:
        return None
    items = payload.get("data") or payload.get("versions") or payload
    if isinstance(items, dict):
        items = items.get("items") or items.get("results") or []
    if not isinstance(items, list) or not items:
        return None

    def _score(item: dict) -> tuple:
        blob = json.dumps(item).lower()
        return (
            "python" in blob,
            "viva" in blob,
            "server" in blob,
            str(item.get("version") or ""),
        )

    best = max(
        (item for item in items if isinstance(item, dict)),
        key=_score,
        default=None,
    )
    if not best:
        return None
    version_id = best.get("id")
    return str(version_id) if version_id is not None else None


def _select_voice_isolation(models: list) -> dict | None:
    if not models:
        return None
    for model in models:
        tech = str(model.get("technology") or "").lower().replace("-", "_")
        if tech == "voice_isolation":
            return model
    return models[0]


def _get_json(url: str, api_key: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"api-key {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()
