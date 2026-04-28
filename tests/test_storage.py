import unittest
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.services import storage
from app.services.storage import LocalStorage, normalize_storage_key
from app.utils.media_urls import build_static_url


class StorageTestCase(unittest.TestCase):
    def test_local_storage_maps_keys_under_base_dir(self):
        base_dir = Path("test_databases") / f"storage_{uuid4().hex}"
        local_storage = LocalStorage(base_dir)

        path = local_storage.path_for(normalize_storage_key("clips", "job_1", "clip.mp4"))
        path.write_bytes(b"clip")

        self.assertTrue(path.exists())
        self.assertEqual(local_storage.key_for_path(path), "clips/job_1/clip.mp4")
        self.assertEqual(local_storage.public_url_for_path(path), "/static/clips/job_1/clip.mp4")

    def test_build_static_url_uses_storage_service_for_local_paths(self):
        original_base_dir = storage.settings.base_data_dir
        original_backend = storage.settings.storage_backend
        base_dir = Path("test_databases") / f"media_{uuid4().hex}"
        local_storage = LocalStorage(base_dir)
        path = local_storage.path_for("exports/job_1_export.zip")
        path.write_bytes(b"zip")

        storage.settings.base_data_dir = str(base_dir)
        storage.settings.storage_backend = "local"
        try:
            signed_url = build_static_url(str(path))
            self.assertIsNotNone(signed_url)
            self.assertTrue(signed_url.startswith("/files/download/"))
        finally:
            storage.settings.base_data_dir = original_base_dir
            storage.settings.storage_backend = original_backend

    def test_build_static_url_uses_signed_urls_for_private_storage_backends(self):
        original_backend = storage.settings.storage_backend
        original_base_dir = storage.settings.base_data_dir
        base_dir = Path("test_databases") / f"private_{uuid4().hex}"
        local_storage = LocalStorage(base_dir)
        path = local_storage.path_for("clips/job_1/clip.mp4")
        path.write_bytes(b"clip")
        storage.settings.base_data_dir = str(base_dir)
        storage.settings.storage_backend = "r2"
        try:
            signed_url = build_static_url(str(path))
            self.assertIsNotNone(signed_url)
            self.assertTrue(signed_url.startswith("/files/download/"))
        finally:
            storage.settings.storage_backend = original_backend
            storage.settings.base_data_dir = original_base_dir

    def test_remote_storage_backends_require_bucket(self):
        with self.assertRaises(ValueError):
            Settings(storage_backend="r2", storage_bucket=None)


if __name__ == "__main__":
    unittest.main()
