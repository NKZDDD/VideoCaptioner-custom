# -*- coding: utf-8 -*-
"""Persistence helpers for editable batch projects.

The manifest is intentionally tiny.  Media and subtitle files stay where the
existing pipeline already writes them; the manifest only links them together
so a completed episode can be reopened after the application restarts.
"""

import datetime
import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Dict, List, Optional

from app.common.config import cfg
from app.config import APPDATA_PATH
from app.core.concurrency import atomic_text, locked_store
from app.core.customer_templates import task_profile


PROJECT_FILE_NAME = "batch_project.json"
PROJECT_STORE_PATH = APPDATA_PATH / "batch_projects"


def _absolute(path: Optional[str]) -> str:
    if not path:
        return ""
    return str(Path(path).expanduser().resolve())


@locked_store
def save_batch_project(
    video_path: str,
    editable_subtitle: str,
    styled_subtitle: str,
    output_video: str,
    style_name: str = "",
    style_template: str = "",
    subtitle_layout: str = "",
    status: str = "subtitle_ready",
) -> str:
    """Create or refresh the manifest for one full-process batch episode."""
    # Keep recoverable manifests on the share rather than a workstation's scratch directory.
    project_id = hashlib.sha1(_absolute(video_path).lower().encode('utf-8')).hexdigest()[:16]
    project_dir = PROJECT_STORE_PATH / 'projects' / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    work_manifest_path = project_dir / PROJECT_FILE_NAME
    PROJECT_STORE_PATH.mkdir(parents=True, exist_ok=True)
    manifest_path = PROJECT_STORE_PATH / f"{project_id}.json"

    data = {
        "version": 1,
        "title": Path(video_path).stem,
        "source_video": _absolute(video_path),
        "editable_subtitle": _absolute(editable_subtitle),
        "styled_subtitle": _absolute(styled_subtitle),
        "output_video": _absolute(output_video),
        "style_name": str(style_name or ""),
        "style_template": style_template or "",
        "subtitle_layout": str(subtitle_layout or ""),
        "status": status,
        "work_manifest": str(work_manifest_path),
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    data['operator'] = os.environ.get('USERNAME', '')
    data['customer_template_snapshot'] = task_profile(video_path)
    data['computer'] = platform.node()
    atomic_text(manifest_path, json.dumps(data, ensure_ascii=False, indent=2))
    # Keep a mirror beside the existing work files for diagnostics and recovery.
    try:
        atomic_text(work_manifest_path, json.dumps(data, ensure_ascii=False, indent=2))
    except OSError:
        pass
    return str(manifest_path)


def load_batch_project(manifest_path: str) -> Dict:
    path = Path(manifest_path)
    return json.loads(path.read_text(encoding="utf-8"))


@locked_store
def update_batch_project(manifest_path: str, **values) -> Dict:
    path = Path(manifest_path)
    data = load_batch_project(str(path))
    data.update(values)
    data["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    work_manifest = data.get("work_manifest", "")
    if work_manifest and Path(work_manifest) != path:
        try:
            atomic_text(Path(work_manifest), json.dumps(data, ensure_ascii=False, indent=2))
        except OSError:
            pass
    return data


def list_batch_projects() -> List[Dict]:
    """Return saved projects, newest first; damaged manifests are ignored."""
    candidates = []
    if PROJECT_STORE_PATH.exists():
        candidates.extend(PROJECT_STORE_PATH.glob("*.json"))
    root = Path(cfg.work_dir.value)
    if root.exists():
        candidates.extend(root.glob(f"*/{PROJECT_FILE_NAME}"))

    projects_by_video = {}
    for manifest_path in candidates:
        try:
            data = load_batch_project(str(manifest_path))
            data["manifest_path"] = str(manifest_path)
            key = str(data.get("source_video", manifest_path)).lower()
            previous = projects_by_video.get(key)
            if not previous or data.get("updated_at", "") > previous.get(
                "updated_at", ""
            ):
                projects_by_video[key] = data
        except (OSError, ValueError, TypeError):
            continue
    projects = list(projects_by_video.values())
    projects.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return projects


def find_batch_project(video_path: str) -> Optional[str]:
    target = _absolute(video_path).lower()
    for project in list_batch_projects():
        if str(project.get("source_video", "")).lower() == target:
            return project.get("manifest_path")
    return None
