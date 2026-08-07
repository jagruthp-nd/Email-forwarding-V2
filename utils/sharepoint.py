"""
sharepoint.py
-------------
Upload EF Automation report files to a SharePoint document library via Graph.

Config (env):
  SHAREPOINT_SITE_URL   https://netorg726775.sharepoint.com/sites/ITTEAM259
  SHAREPOINT_LIBRARY    Shared Documents
  SHAREPOINT_FOLDER     General/Jagruth/Automation_Reports/EF
  SHAREPOINT_UPLOAD_ENABLED  true|false (default true)

Requires Graph application permission: Sites.ReadWrite.All
(or Sites.Selected with write on this site).

Not gated by DISABLE_GRAPH_WRITES (that flag is for Entra user/mailbox mutations).
Skipped when EF_DRY_RUN=true or SHAREPOINT_UPLOAD_ENABLED=false.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

from .app_config import get_sharepoint_report_url
from .automation_flags import is_dry_run
from .graph_api import _auth_headers, _get

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"


def is_sharepoint_upload_enabled() -> bool:
    if is_dry_run():
        return False
    return os.environ.get("SHAREPOINT_UPLOAD_ENABLED", "true").strip().lower() in (
        "true", "1", "yes",
    )


def _site_path_from_url(site_url: str) -> Tuple[str, str]:
    """
    From https://host/sites/ITTEAM259 → (host, /sites/ITTEAM259)
    """
    parsed = urlparse(site_url.strip())
    host = parsed.netloc
    path = parsed.path.rstrip("/") or "/"
    return host, path


def _resolve_site_id(site_url: str) -> Optional[str]:
    host, path = _site_path_from_url(site_url)
    # GET /sites/{hostname}:{server-relative-path}
    data = _get(f"{_GRAPH}/sites/{host}:{path}")
    if not data or not data.get("id"):
        logger.error("Could not resolve SharePoint site id for %s", site_url)
        return None
    return data["id"]


def _resolve_drive_id(site_id: str, library: str) -> Optional[str]:
    data = _get(f"{_GRAPH}/sites/{site_id}/drives")
    if not data:
        return None
    drives = data.get("value") or []
    lib = (library or "Shared Documents").strip().lower()
    aliases = {lib, "shared documents", "documents"}
    for d in drives:
        name = (d.get("name") or "").strip().lower()
        if name in aliases or name == lib:
            return d.get("id")
    # Default document library
    for d in drives:
        if (d.get("driveType") or "").lower() == "documentlibrary":
            return d.get("id")
    if drives:
        return drives[0].get("id")
    logger.error("No drives found on SharePoint site %s", site_id)
    return None


def upload_report_file(
    *,
    filename: str,
    content: bytes,
    content_type: str = "text/html",
) -> Dict[str, Any]:
    """
    Upload a report file into SHAREPOINT_FOLDER under SHAREPOINT_LIBRARY.

    Returns dict: {ok, web_url, folder_url, error}
    """
    folder_url = get_sharepoint_report_url()
    result: Dict[str, Any] = {
        "ok": False,
        "web_url": "",
        "folder_url": folder_url,
        "error": "",
        "filename": filename,
    }

    if not is_sharepoint_upload_enabled():
        result["error"] = "SharePoint upload disabled (EF_DRY_RUN or SHAREPOINT_UPLOAD_ENABLED=false)"
        logger.info(result["error"])
        return result

    site_url = os.environ.get("SHAREPOINT_SITE_URL", "").strip().rstrip("/")
    library = os.environ.get("SHAREPOINT_LIBRARY", "Shared Documents").strip()
    folder = os.environ.get("SHAREPOINT_FOLDER", "").strip().strip("/")

    if not site_url:
        result["error"] = "SHAREPOINT_SITE_URL not set"
        logger.warning(result["error"])
        return result

    site_id = _resolve_site_id(site_url)
    if not site_id:
        result["error"] = "Failed to resolve SharePoint site (check URL / Sites.ReadWrite.All)"
        return result

    drive_id = _resolve_drive_id(site_id, library)
    if not drive_id:
        result["error"] = f"Failed to resolve library '{library}'"
        return result

    rel = f"{folder}/{filename}" if folder else filename
    # Graph path upload (creates intermediate folders as needed for simple paths)
    put_url = f"{_GRAPH}/drives/{drive_id}/root:/{rel}:/content"

    try:
        headers = _auth_headers()
        headers["Content-Type"] = content_type
        resp = requests.put(put_url, headers=headers, data=content, timeout=120)
        if resp.status_code in (200, 201):
            body = resp.json()
            result["ok"] = True
            result["web_url"] = body.get("webUrl") or folder_url
            logger.info("Uploaded report to SharePoint: %s", result["web_url"])
            return result
        result["error"] = f"Upload HTTP {resp.status_code}: {(resp.text or '')[:400]}"
        logger.error("SharePoint upload failed: %s", result["error"])
        return result
    except Exception as exc:
        result["error"] = str(exc)
        logger.error("SharePoint upload exception: %s", exc)
        return result
