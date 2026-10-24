import os
import sys
import wave

import pytest
from asr_fixtures import provide_faster_whisper_cache

from video_report_agent.asr import AsrError, normalize_asr_segments


@pytest.mark.parametrize("fails", [False, True])
def test_local_asr_worker_exits_after_success_or_failure(tmp_path, monkeypatch, fails):
    from video_report_agent.asr import MlxWhisperBackend

    # Run the real spawn/IPC path with a tiny substitute for the expensive model.
    (tmp_path / "mlx_whisper.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def transcribe(audio, **kwargs):\n"
        "    Path(audio + '.pid').write_text(str(os.getpid()))\n"
        "    assert kwargs == dict(path_or_hf_repo='test-model', language='zh', verbose=False)\n"
        "    if Path(audio).name == 'fail.wav':\n"
        "        raise RuntimeError('model failed')\n"
        "    return {'segments': [{'start': 0, 'end': 1, 'text': '测试'}]}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    audio = tmp_path / ("fail.wav" if fails else "ok.wav")
    audio.write_bytes(b"test")
    imported_before = "mlx_whisper" in sys.modules
    backend = MlxWhisperBackend("test-model")
    for _ in range(2):
        if fails:
            with pytest.raises(AsrError, match="transcription failed: RuntimeError"):
                backend.transcribe(audio)
        else:
            result = backend.transcribe(audio)
            assert result.segments[0].text == "测试"
            assert result.raw_result["segments"][0]["end"] == 1
            assert result.model == "test-model"
        pid = int(audio.with_suffix(".wav.pid").read_text())
        assert pid != os.getpid()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert ("mlx_whisper" in sys.modules) == imported_before


def test_empty_raw_asr_segments_are_retained_as_explicit_non_indexed_provenance() -> None:
    normalized = normalize_asr_segments(
        [
            {"start": 0.0, "end": 1.0, "text": "第一段中文内容"},
            {"start": 1.0, "end": 1.5, "text": "  "},
            {"start": 1.5, "end": 3.0, "text": "第二段中文内容"},
        ]
    )

    assert [segment.ordinal for segment in normalized.segments] == [0, 2]
    assert normalized.dropped_empty_raw_segment_ordinals == (1,)


def test_unrepresentable_ranges_are_retained_only_in_raw_provenance() -> None:
    normalized = normalize_asr_segments(
        [
            {"start": 0.0, "end": 1.0, "text": "可表示的内容"},
            {"start": 1.0011, "end": 1.0012, "text": "极短但真实的内容"},
            {"start": 2.0, "end": 2.0, "text": "零时长内容"},
        ]
    )

    assert [segment.ordinal for segment in normalized.segments] == [0]
    assert normalized.dropped_unrepresentable_raw_segment_ordinals == (1,)
    assert normalized.dropped_non_positive_raw_segment_ordinals == (2,)


def test_ms_quantization_preserves_adjacent_raw_order_without_artificial_overlap() -> None:
    normalized = normalize_asr_segments(
        [
            {"start": 0.0001, "end": 1.0001, "text": "第一段"},
            {"start": 1.0002, "end": 2.0002, "text": "第二段"},
        ]
    )

    assert [(segment.start_ms, segment.end_ms) for segment in normalized.segments] == [
        (0, 1000),
        (1000, 2000),
    ]


def test_float_noise_at_an_adjacent_boundary_is_not_a_real_overlap() -> None:
    normalized = normalize_asr_segments(
        [
            {"start": 0.0, "end": 0.1 + 0.2, "text": "第一段"},
            {"start": 0.3, "end": 1.0, "text": "第二段"},
        ]
    )

    assert [(segment.start_ms, segment.end_ms) for segment in normalized.segments] == [
        (0, 300),
        (300, 1000),
    ]


def test_faster_whisper_uses_cpu_int8_and_shared_segment_normalizer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from video_report_agent.asr import FasterWhisperBackend

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    calls = {}

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            calls["model"] = model
            calls["options"] = kwargs

        def transcribe(self, path, *, language, vad_filter):
            assert vad_filter is True
            calls["path"] = path
            calls["language"] = language
            return iter([
                SimpleNamespace(start=0.0, end=1.25, text=" 第一段 "),
                SimpleNamespace(start=1.25, end=2.5, text="第二段"),
            ]), SimpleNamespace(language="zh", language_probability=0.97, duration=2.5)

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel)
    )
    cache = tmp_path / "model-cache"
    directory = provide_faster_whisper_cache(monkeypatch, tmp_path)
    result = FasterWhisperBackend("small", cache).transcribe(audio)

    assert calls["model"] == str(directory)
    assert calls["options"] == {
        "device": "cpu", "compute_type": "int8", "download_root": str(cache),
        "local_files_only": True,
    }
    assert calls["path"] == str(audio)
    assert calls["language"] == "zh"
    assert [(item.start_ms, item.end_ms, item.text) for item in result.segments] == [
        (0, 1250, " 第一段 "), (1250, 2500, "第二段"),
    ]
    assert result.backend == "faster-whisper"
    assert result.provider == "local"
    assert result.raw_result["language_probability"] == 0.97


def test_material_overlap_still_fails_closed() -> None:
    with pytest.raises(AsrError, match="overlap"):
        normalize_asr_segments(
            [
                {"start": 0.0, "end": 1.0, "text": "第一段"},
                {"start": 0.9, "end": 2.0, "text": "第二段"},
            ]
        )


def test_faster_whisper_audio_decoder_accepts_small_pcm_wav(tmp_path):
    audio_module = pytest.importorskip(
        "faster_whisper.audio", reason="local-asr extra is not installed"
    )
    audio_path = tmp_path / "decoder-probe.wav"
    with wave.open(str(audio_path), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(16_000)
        audio_file.writeframes(bytes(16_000 // 4 * 2))

    decoded = audio_module.decode_audio(str(audio_path), sampling_rate=16_000)

    assert len(decoded) == 4_000


def test_faster_whisper_auto_passes_none_and_records_detected_language(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from video_report_agent.asr import FasterWhisperBackend

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    calls = {}

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            pass

        def transcribe(self, path, *, language, vad_filter):
            assert vad_filter is True
            calls["language"] = language
            return (
                iter([SimpleNamespace(start=0.0, end=1.0, text="hello world")]),
                SimpleNamespace(language="en", language_probability=0.95, duration=1.0),
            )

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel)
    )
    provide_faster_whisper_cache(monkeypatch, tmp_path)
    result = FasterWhisperBackend("small", tmp_path / "cache").transcribe(audio, "auto")

    assert calls["language"] is None  # auto → None 传给 Whisper 自动检测
    assert result.language == "en"  # 记录检测出的真实语言，不是传入的 "auto"
    assert result.raw_result["language"] == "en"


def test_faster_whisper_explicit_language_passed_through(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from video_report_agent.asr import FasterWhisperBackend

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    calls = {}

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            pass

        def transcribe(self, path, *, language, vad_filter):
            assert vad_filter is True
            calls["language"] = language
            return (
                iter([SimpleNamespace(start=0.0, end=1.0, text="bonjour")]),
                SimpleNamespace(language="fr", language_probability=0.9, duration=1.0),
            )

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel)
    )
    provide_faster_whisper_cache(monkeypatch, tmp_path)
    result = FasterWhisperBackend("small", tmp_path / "cache").transcribe(audio, "fr")

    assert calls["language"] == "fr"  # 显式语言原样传给 Whisper
    assert result.language == "fr"


def test_mlx_auto_passes_none_and_records_detected_language(tmp_path, monkeypatch):
    from video_report_agent.asr import _transcribe_mlx

    (tmp_path / "mlx_whisper.py").write_text(
        "def transcribe(audio, **kwargs):\n"
        "    assert kwargs == dict(path_or_hf_repo='test-model', language=None, verbose=False)\n"
        "    return {'language': 'en', 'segments': [{'start': 0, 'end': 1, 'text': 'hello'}]}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    audio = tmp_path / "ok.wav"
    audio.write_bytes(b"test")
    result = _transcribe_mlx(audio, model="test-model", language="auto")

    assert result.language == "en"  # mlx-whisper 检测出英语并返回真实语言


def test_asr_language_explicit_beats_env_beats_default(monkeypatch):
    from video_report_agent.media_config import resolve_media_config

    monkeypatch.delenv("ASR_LANGUAGE", raising=False)
    assert resolve_media_config()["asr_language"] == "zh"

    monkeypatch.setenv("ASR_LANGUAGE", "auto")
    assert resolve_media_config()["asr_language"] == "auto"

    assert resolve_media_config(asr_language="en")["asr_language"] == "en"
