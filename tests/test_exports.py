import json
import shutil
import unittest
from datetime import UTC
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from app.core.config import settings
from app.models.clip import Clip
from app.models.job import Job
from app.services.exports import build_job_export_bundle, list_job_export_bundles


class ExportBundleTestCase(unittest.TestCase):
    def setUp(self):
        self.original_base_data_dir = settings.base_data_dir
        self.temp_dir = Path("test_databases") / f"exports_{uuid4().hex}"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        settings.base_data_dir = str(self.temp_dir)

    def tearDown(self):
        settings.base_data_dir = self.original_base_data_dir
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def _build_job(self) -> Job:
        return Job(
            id=42,
            title="Video Original",
            source_value="https://example.com/video",
            detected_niche="podcast",
        )

    def _build_clip(self, video_path: Path) -> Clip:
        return Clip(
            id=7,
            job_id=42,
            source="candidate",
            mode="short",
            start_time=10.0,
            end_time=70.0,
            duration=60.0,
            score=9.1,
            reason="gancho forte",
            text="texto do clip",
            headline="Titulo pronto",
            description="Descricao pronta",
            hashtags="#cortes #shorts",
            suggested_filename="clip pronto.mp4",
            render_preset="clean",
            publication_status="ready",
            subtitles_burned=True,
            output_path=str(video_path),
        )

    def test_export_bundle_includes_publication_metadata_files(self):
        video_path = self.temp_dir / "clip-source.mp4"
        video_path.write_bytes(b"fake video")
        job = self._build_job()
        clip = self._build_clip(video_path)

        zip_path = build_job_export_bundle(job, [clip])

        with ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("metadata/clip-pronto.json", names)
            self.assertIn("metadata/clip-pronto.txt", names)
            self.assertIn("clips/clip pronto.mp4", names)

            manifest = json.loads(archive.read("manifest.json"))
            clip_manifest = json.loads(archive.read("metadata/clip-pronto.json"))
            publication_text = archive.read("metadata/clip-pronto.txt").decode("utf-8")

        self.assertEqual(manifest["clips_count"], 1)
        self.assertEqual(clip_manifest["publication"]["title"], "Titulo pronto")
        self.assertEqual(clip_manifest["publication"]["hashtags"], ["#cortes", "#shorts"])
        self.assertEqual(clip_manifest["publication"]["status_label"], "Pronto")
        self.assertIn("Titulo: Titulo pronto", publication_text)
        self.assertIn("#cortes #shorts", publication_text)

    def test_export_bundle_preserves_export_history(self):
        video_path = self.temp_dir / "clip-source.mp4"
        video_path.write_bytes(b"fake video")
        job = self._build_job()
        clip = self._build_clip(video_path)

        first_zip = build_job_export_bundle(job, [clip])
        second_zip = build_job_export_bundle(job, [clip])
        exports = list_job_export_bundles(job.id)

        self.assertNotEqual(Path(first_zip).name, Path(second_zip).name)
        self.assertEqual(len(exports), 2)
        self.assertTrue(all(row["name"].startswith("job_42_export_") for row in exports))
        self.assertTrue(all(row["created_at"].tzinfo is UTC for row in exports))
        self.assertTrue(all(row["modified_at"].tzinfo is UTC for row in exports))


if __name__ == "__main__":
    unittest.main()
