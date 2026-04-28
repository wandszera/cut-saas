import json
import re
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.models.clip import Clip
from app.models.job import Job
from app.services.publication import build_clip_publication_package
from app.services.storage import get_storage, normalize_storage_key


def _safe_archive_stem(value: str | None, fallback: str) -> str:
    raw_value = (value or "").strip() or fallback
    stem = Path(raw_value).stem
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return normalized[:80] or fallback


def _parse_export_created_at(filename: str) -> datetime | None:
    match = re.search(r"_export_(\d{8}_\d{6}_\d{6})\.zip$", filename)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S_%f").replace(tzinfo=UTC)
    except ValueError:
        return None


def _serialize_clip(clip: Clip) -> dict:
    return {
        "clip_id": clip.id,
        "source": clip.source,
        "mode": clip.mode,
        "start": clip.start_time,
        "end": clip.end_time,
        "duration": clip.duration,
        "score": clip.score,
        "reason": clip.reason,
        "headline": clip.headline,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "suggested_filename": clip.suggested_filename,
        "render_preset": clip.render_preset,
        "publication_status": clip.publication_status,
        "subtitles_burned": clip.subtitles_burned,
        "output_path": clip.output_path,
        "publication": build_clip_publication_package(clip),
    }


def build_job_export_bundle(job: Job, clips: list[Clip]) -> str:
    storage = get_storage()
    created_at = datetime.now(UTC)
    export_stamp = created_at.strftime("%Y%m%d_%H%M%S_%f")
    zip_path = storage.path_for(normalize_storage_key("exports", f"job_{job.id}_export_{export_stamp}.zip"))
    manifest = {
        "job_id": job.id,
        "title": job.title,
        "source_value": job.source_value,
        "detected_niche": job.detected_niche,
        "clips_count": len(clips),
        "clips": [_serialize_clip(clip) for clip in clips],
        "created_at": created_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        for index, clip in enumerate(clips, start=1):
            clip_stem = _safe_archive_stem(clip.suggested_filename, f"clip-{index:02d}")
            clip_manifest = _serialize_clip(clip)
            publication = clip_manifest["publication"]
            archive.writestr(
                f"metadata/{clip_stem}.json",
                json.dumps(clip_manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                f"metadata/{clip_stem}.txt",
                "\n".join(
                    [
                        f"Titulo: {publication['title']}",
                        f"Descricao: {publication['description']}",
                        f"Hashtags: {' '.join(publication['hashtags'])}",
                        "",
                        publication["caption"],
                    ]
                ).strip()
                + "\n",
            )

            if not clip.output_path:
                continue
            file_path = Path(clip.output_path)
            if not file_path.exists():
                continue
            archive_name = clip.suggested_filename or file_path.name
            archive.write(file_path, arcname=f"clips/{archive_name}")

    return str(zip_path)


def list_job_export_bundles(job_id: int) -> list[dict]:
    rows = []
    pattern = f"job_{job_id}_export*.zip"
    export_objects = get_storage().list("exports", pattern)
    export_paths = [Path(item.path) for item in export_objects if item.path]
    for path in sorted(export_paths, key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": modified_at,
                "created_at": _parse_export_created_at(path.name) or modified_at,
            }
        )
    return rows
