import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_report_agent import transcript_foundation as foundation
from video_report_agent.transcript import build_transcript


def build(root, language, text, *, mode="asr-only", configured=None):
    asr = root / "asr.json"
    asr.write_text(json.dumps({
        "language": language,
        "segments": [{"ordinal": 0, "start_ms": 0, "end_ms": 2000, "text": text}],
    }, ensure_ascii=False))
    return build_transcript(
        argparse.Namespace(
            transcript_mode=mode, transcript_sources=["asr"] if mode in {"auto", "selected"}
            else None, ocr_mode="off", subtitle_file=None,
        ),
        source_path=root / "source.mp4", artifact_root=root, asr_path=asr,
        source_duration_ms=3000,
        manifest={"video_id": "youtube-jNQXAC9IVRw", "transcript_mode": mode,
                  "asr_language": configured or language},
    )[0]


@pytest.mark.parametrize("mode", ["asr-only", "selected", "auto", "fused"])
@pytest.mark.parametrize("language,text,expected", [
    ("ja", "私は東京の図書館で勉強します。", "私は東京の図書館で勉強します。"),
    ("zh", "我在東京的圖書館學習。", "我在东京的图书馆学习。"),
    ("en", "I study at the library.", "I study at the library."),
    ("fr", "J’étudie à la bibliothèque.", "J’étudie à la bibliothèque."),
    ("ko", "도서관에서 공부합니다.", "도서관에서 공부합니다."),
])
def test_only_chinese_is_simplified(tmp_path, monkeypatch, mode, language, text, expected):
    monkeypatch.setattr(
        "video_report_agent.transcript.discover_subtitle_source",
        lambda *a, **kw: foundation.SubtitleDiscovery(status="ABSENT"),
    )
    result = build(tmp_path, language, text, mode=mode, configured="auto")
    assert result.canonical_units[0].canonical_text == expected
    assert result.source_events[0].text == text
    assert result.source_events[0].normalized_text == expected
    assert result.manifest.normalizer_version != "nfkc-whitespace-punctuation-opencc-t2s.v1"


@pytest.mark.parametrize("requested,detected,expected", [
    ("en", "en", "en"), ("ja", "ja", "ja"), ("zh", "zh", "zh"),
    ("auto", "ja", "ja"), (None, None, "zh"),
])
@pytest.mark.parametrize("embedded", [False, True])
def test_subtitle_discovery_prefers_transcription_language(
    tmp_path, monkeypatch, requested, detected, expected, embedded,
):
    text = {"en": "English subtitles", "ja": "日本語の字幕です。", "zh": "中文字幕"}
    srt = "1\n00:00:00,000 --> 00:00:02,000\n{}\n"
    # Isolate the optional parser dependency; discovery and transcript building remain real.
    monkeypatch.setitem(sys.modules, "pysubs2", SimpleNamespace(
        load=lambda path, **kw: [SimpleNamespace(
            start=0, end=2000, text=Path(path).read_text().splitlines()[-1]
        )]
    ))
    if embedded:
        streams = [
            {"codec_name": "subrip", "tags": {"language": code}}
            for code in ("en", "ja", "zh")
        ]
        monkeypatch.setattr(foundation, "_probe_subtitle_streams", lambda *a: streams)
        monkeypatch.setattr("shutil.which", lambda _: "/test/ffmpeg")

        def extract(command, **kwargs):
            position = int(command[command.index("-map") + 1].rsplit(":", 1)[1])
            Path(command[-1]).write_text(srt.format(text[("en", "ja", "zh")[position]]))
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(foundation.subprocess, "run", extract)
    else:
        for code in ("en", "ja", "zh"):
            (tmp_path / f"source.{code}.srt").write_text(srt.format(text[code]))
    result = build(tmp_path, detected, text[expected], mode="fused", configured=requested)
    subtitle = next(event for event in result.source_events if event.source == "subtitle_track")
    assert subtitle.text == text[expected]


def test_chinese_default_keeps_order_among_chinese_tracks(tmp_path):
    paths = [tmp_path / name for name in ("source.chs.srt", "source.zh.srt")]
    for path in paths:
        path.write_text("fixture")
    assert foundation.select_subtitle_file(reversed(paths)) == paths[0]


def test_subtitle_language_tag_preserves_japanese_with_chinese_asr(tmp_path, monkeypatch):
    text = "私は東京の図書館で勉強します。"
    subtitle = tmp_path / "source.ja.srt"
    subtitle.write_text("fixture")
    monkeypatch.setitem(sys.modules, "pysubs2", SimpleNamespace(
        load=lambda *a, **kw: [SimpleNamespace(start=0, end=2000, text=text)]
    ))
    events = foundation.parse_subtitle_file(subtitle, language="zh")
    assert events[0].normalized_text == text
    discovery = foundation.discover_subtitle_source(tmp_path / "source.mp4")
    assert discovery.language == "ja"
