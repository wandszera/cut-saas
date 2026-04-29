import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import MagicMock, patch

from app.core.config import Settings
from app.services import storage
from app.services.storage import LocalStorage, S3Storage, normalize_storage_key
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
            with patch("app.utils.media_urls.get_storage", return_value=local_storage):
                signed_url = build_static_url(str(path))
            self.assertIsNotNone(signed_url)
            self.assertTrue(signed_url.startswith("/files/download/"))
        finally:
            storage.settings.storage_backend = original_backend
            storage.settings.base_data_dir = original_base_dir

    def test_remote_storage_backends_require_bucket(self):
        with self.assertRaises(ValueError):
            Settings(storage_backend="r2", storage_bucket=None)

    def test_remote_storage_backends_require_credentials(self):
        with self.assertRaises(ValueError):
            Settings(storage_backend="s3", storage_bucket="bucket")

    def test_s3_storage_syncs_and_resolves_via_local_cache(self):
        base_dir = Path("test_databases") / f"s3cache_{uuid4().hex}"
        uploaded = {}

        fake_client = MagicMock()

        def _upload_file(filename: str, bucket: str, key: str):
            uploaded[(bucket, key)] = Path(filename).read_bytes()

        def _download_file(bucket: str, key: str, filename: str):
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
            Path(filename).write_bytes(uploaded[(bucket, key)])

        def _head_object(Bucket: str, Key: str):
            if (Bucket, Key) not in uploaded:
                raise AssertionError("missing object")
            return {"ContentLength": len(uploaded[(Bucket, Key)])}

        fake_client.upload_file.side_effect = _upload_file
        fake_client.download_file.side_effect = _download_file
        fake_client.head_object.side_effect = _head_object
        fake_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "clips/job_1/clip.mp4", "Size": 4}],
            "IsTruncated": False,
        }

        with patch("app.services.storage.boto3.client", return_value=fake_client):
            remote_storage = S3Storage(
                base_dir=base_dir,
                bucket="bucket",
                access_key_id="key",
                secret_access_key="secret",
            )
            path = remote_storage.path_for("clips/job_1/clip.mp4")
            path.write_bytes(b"clip")

            remote_storage.sync_path(path)
            path.unlink()
            resolved = remote_storage.resolve_path("clips/job_1/clip.mp4")
            objects = remote_storage.list("clips/job_1", "*.mp4")

        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.exists())
        self.assertEqual(resolved.read_bytes(), b"clip")
        self.assertTrue(remote_storage.exists("clips/job_1/clip.mp4"))
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0].key, "clips/job_1/clip.mp4")


if __name__ == "__main__":
    unittest.main()
