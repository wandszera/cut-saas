import json
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.core.config import settings
from app.models.clip import Clip
from app.models.job import Job


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
    }


def build_job_export_bundle(job: Job, clips: list[Clip]) -> str:
    exports_dir = Path(settings.base_data_dir) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    zip_path = exports_dir / f"job_{job.id}_export.zip"
    manifest = {
        "job_id": job.id,
        "title": job.title,
        "source_value": job.source_value,
        "detected_niche": job.detected_niche,
        "clips_count": len(clips),
        "clips": [_serialize_clip(clip) for clip in clips],
    }

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        for clip in clips:
            if not clip.output_path:
                continue
            file_path = Path(clip.output_path)
            if not file_path.exists():
                continue
            archive_name = clip.suggested_filename or file_path.name
            archive.write(file_path, arcname=f"clips/{archive_name}")

    return str(zip_path)


def list_job_export_bundles(job_id: int) -> list[dict]:
    exports_dir = Path(settings.base_data_dir) / "exports"
    if not exports_dir.exists():
        return []

    rows = []
    pattern = f"job_{job_id}_export*.zip"
    for path in sorted(exports_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime),
            }
        )
    return rows
