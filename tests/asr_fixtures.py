"""Local model files for backend tests that replace the expensive inference engine."""

import sys
from types import SimpleNamespace


def complete_model_directory(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (directory / name).write_bytes(b"synthetic model file")
    return directory


def provide_faster_whisper_cache(monkeypatch, root, *, available=None):
    directory = complete_model_directory(root / "model-cache/snapshot")

    def resolve(repository, **kwargs):
        assert repository == "Systran/faster-whisper-small"
        assert kwargs["local_files_only"] is True
        if available is not None and not available():
            raise FileNotFoundError("synthetic cache miss")
        return str(directory)

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper.utils",
        SimpleNamespace(
            _MODELS={"small": "Systran/faster-whisper-small"},
            download_model=resolve,
        ),
    )
    return directory
