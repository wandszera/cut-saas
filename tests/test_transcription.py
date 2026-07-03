import json
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from app.services import transcription


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transcribe(self, audio_path: str, verbose: bool, fp16: bool) -> dict:
        self.calls.append(
            {
                "audio_path": audio_path,
                "verbose": verbose,
                "fp16": fp16,
            }
        )
        return {
            "language": "pt",
            "text": "texto teste",
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.25, "text": " texto teste "},
            ],
        }


class FakeFasterSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeFasterModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transcribe(self, audio_path: str, beam_size: int):
        self.calls.append(
            {
                "audio_path": audio_path,
                "beam_size": beam_size,
            }
        )
        return iter([FakeFasterSegment(0.0, 2.5, " texto rapido ")]), SimpleNamespace(language="pt")


class FakeStorage:
    def __init__(self, output_path: Path):
        self.output_path = output_path

    def path_for(self, _key: str) -> Path:
        return self.output_path

    def sync_path(self, _path: Path) -> None:
        return None


class SlowModel:
    def transcribe(self, _audio_path: str, *, verbose: bool, fp16: bool) -> dict:
        time.sleep(0.45)
        return {
            "language": "pt",
            "text": "teste de transcricao",
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.0, "text": "teste de transcricao"},
            ],
        }


def test_transcribe_audio_reuses_cached_whisper_model() -> None:
    fake_model = FakeModel()
    load_calls: list[str] = []

    def fake_load_model(model_name: str) -> FakeModel:
        load_calls.append(model_name)
        return fake_model

    storage = SimpleNamespace(
        path_for=lambda key: f"C:/tmp/{key.split('/')[-1]}",
        sync_path=lambda path: None,
    )
    progress_messages: list[str] = []
    captured_writes: list[str] = []

    file_mock = mock_open()

    def _record_write(data: str) -> int:
        captured_writes.append(data)
        return len(data)

    file_mock.return_value.write.side_effect = _record_write

    transcription._MODEL_CACHE.clear()
    transcription._DEVICE_CAPABILITY_CACHE = None
    with (
        patch.object(transcription.settings, "transcription_provider", "openai_whisper"),
        patch.object(transcription.settings, "whisper_model", "base"),
        patch.object(transcription.settings, "whisper_precision", "auto"),
        patch.object(transcription, "get_storage", return_value=storage),
        patch.object(transcription, "_detect_cuda_fp16_support", return_value=True),
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", file_mock),
        patch.dict("sys.modules", {"whisper": SimpleNamespace(load_model=fake_load_model)}),
    ):
        first_output = transcription.transcribe_audio(
            "C:/tmp/sample.wav",
            1,
            progress_callback=progress_messages.append,
        )
        first_payload = json.loads("".join(captured_writes))
        captured_writes.clear()

        second_output = transcription.transcribe_audio(
            "C:/tmp/sample.wav",
            2,
            progress_callback=progress_messages.append,
        )
        second_payload = json.loads("".join(captured_writes))

    assert load_calls == ["base"]
    assert fake_model.calls == [
        {"audio_path": "C:\\tmp\\sample.wav", "verbose": False, "fp16": True},
        {"audio_path": "C:\\tmp\\sample.wav", "verbose": False, "fp16": True},
    ]
    assert progress_messages.count("Carregando modelo Whisper (base)") == 1
    assert progress_messages.count("Reutilizando modelo Whisper (base)") == 1
    assert "GPU detectada, usando transcricao Whisper em fp16" in progress_messages
    assert first_output == "C:/tmp/job_1.json"
    assert second_output == "C:/tmp/job_2.json"
    assert first_payload["job_id"] == 1
    assert second_payload["job_id"] == 2
    assert first_payload["segments"][0]["text"] == "texto teste"


def test_transcribe_audio_can_force_fp32_mode() -> None:
    fake_model = FakeModel()
    file_mock = mock_open()

    with (
        patch.object(transcription.settings, "transcription_provider", "openai_whisper"),
        patch.object(transcription.settings, "whisper_model", "base"),
        patch.object(transcription.settings, "whisper_precision", "fp32"),
        patch.object(
            transcription,
            "get_storage",
            return_value=SimpleNamespace(path_for=lambda key: "C:/tmp/job_3.json", sync_path=lambda path: None),
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", file_mock),
        patch.object(transcription, "_get_whisper_model", return_value=fake_model),
    ):
        transcription.transcribe_audio("C:/tmp/sample.wav", 3)

    assert fake_model.calls == [
        {"audio_path": "C:\\tmp\\sample.wav", "verbose": False, "fp16": False},
    ]


def test_transcribe_audio_uses_faster_whisper_when_selected() -> None:
    fake_model = FakeFasterModel()
    file_mock = mock_open()
    captured_writes: list[str] = []

    def _record_write(data: str) -> int:
        captured_writes.append(data)
        return len(data)

    file_mock.return_value.write.side_effect = _record_write

    with (
        patch.object(transcription.settings, "transcription_provider", "faster_whisper"),
        patch.object(transcription.settings, "whisper_model", "base"),
        patch.object(transcription.settings, "whisper_precision", "fp32"),
        patch.object(
            transcription,
            "get_storage",
            return_value=SimpleNamespace(path_for=lambda key: "C:/tmp/job_4.json", sync_path=lambda path: None),
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", file_mock),
        patch.object(transcription, "_get_faster_whisper_model", return_value=fake_model),
    ):
        output = transcription.transcribe_audio("C:/tmp/sample.wav", 4)

    payload = json.loads("".join(captured_writes))

    assert fake_model.calls == [
        {"audio_path": "C:\\tmp\\sample.wav", "beam_size": 5},
    ]
    assert output == "C:/tmp/job_4.json"
    assert payload["job_id"] == 4
    assert payload["language"] == "pt"
    assert payload["text"] == "texto rapido"
    assert payload["segments"][0]["text"] == "texto rapido"


def test_transcribe_audio_repeats_heartbeat_while_model_is_busy() -> None:
    artifacts_dir = Path("test_tmp")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    audio_path = artifacts_dir / "transcription-heartbeat-audio.mp3"
    output_path = artifacts_dir / "transcription-heartbeat-output.json"
    audio_path.write_bytes(b"fake audio")
    progress_messages: list[str] = []

    try:
        with (
            patch.object(transcription, "_PROGRESS_HEARTBEAT_INTERVAL_SECONDS", 0.1),
            patch.object(transcription, "_resolve_transcription_provider", return_value="openai_whisper"),
            patch.object(transcription, "_resolve_fp16_mode", return_value=False),
            patch.object(transcription, "_get_whisper_model", return_value=SlowModel()),
            patch.object(transcription, "get_storage", return_value=FakeStorage(output_path)),
        ):
            result_path = transcription.transcribe_audio(
                str(audio_path),
                123,
                progress_callback=progress_messages.append,
            )
    finally:
        if audio_path.exists():
            audio_path.unlink()
        if output_path.exists():
            output_path.unlink()

    assert result_path == str(output_path)
    assert progress_messages.count("Executando transcricao do audio") >= 2
    assert "Transcricao finalizada" in progress_messages
