import json
from types import SimpleNamespace

import pytest
from yt_dlp.utils import match_filter_func

from video_report_agent.ingest import (
    UrlIngestError,
    _metadata,
    download_bilibili_video,
    resolve_source,
)

WATCH = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.mark.parametrize("suffix", ["&utm_source=bilibili.com", "&ref=https://b23.tv/test"])
def test_platform_dispatch_uses_host_not_tracking_query(suffix):
    assert resolve_source(WATCH + suffix).canonical_url == WATCH


@pytest.mark.parametrize("url", [
    WATCH + "&list=PLexample",
    "https://youtu.be/jNQXAC9IVRw?list=PLexample",
    "https://www.youtube.com/shorts/jNQXAC9IVRw?list=PLexample",
])
def test_single_video_links_discard_playlist_parameter(url):
    assert resolve_source(url).canonical_url == WATCH


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PLexample",
    "https://www.youtube.com/watch?list=PLexample",
    "https://evil.example/watch?v=jNQXAC9IVRw&ref=youtube.com",
])
def test_playlist_only_and_untrusted_hosts_are_rejected(url):
    with pytest.raises(UrlIngestError):
        resolve_source(url)


def test_youtube_412_identifies_youtube(tmp_path, monkeypatch):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    with pytest.raises(UrlIngestError, match="YouTube.*HTTP 412"):
        download_bilibili_video(
            resolve_source(WATCH), tmp_path,
            runner=lambda *a, **kw: SimpleNamespace(
                returncode=1, stdout="", stderr="HTTP Error 412: Precondition Failed"
            ),
        )


LIVE_METADATA = [
    {"is_live": True},
    {"live_status": "is_live"},
    {"live_status": "is_upcoming"},
    {"is_live": False, "live_status": "is_live"},
]


@pytest.mark.parametrize("live", LIVE_METADATA)
def test_live_metadata_is_rejected_by_filter_before_media_download(tmp_path, monkeypatch, live):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        predicate = match_filter_func(command[command.index("--match-filter") + 1])
        info = {"id": "jNQXAC9IVRw", "title": "Live", "duration": 60, **live}
        assert predicate(info, incomplete=False) is not None, "live media must be rejected"
        return SimpleNamespace(returncode=101, stdout="", stderr="does not pass filter")

    with pytest.raises(UrlIngestError, match="直播"):
        download_bilibili_video(resolve_source(WATCH), tmp_path, runner=runner)
    assert len(calls) == 1
    assert not list((tmp_path / "download").iterdir())


@pytest.mark.parametrize("live", LIVE_METADATA)
def test_live_metadata_cannot_be_reused_as_completed_video(tmp_path, live):
    info = tmp_path / "source.info.json"
    info.write_text(json.dumps({"title": "Live", "uploader": "Author", "duration": 60, **live}))
    with pytest.raises(UrlIngestError, match="直播"):
        _metadata(info, resolve_source(WATCH))


@pytest.mark.parametrize("live_status", ["not_live", "was_live", "post_live"])
def test_completed_recordings_remain_downloadable(tmp_path, monkeypatch, live_status):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")

    def runner(command, **kwargs):
        predicate = match_filter_func(command[command.index("--match-filter") + 1])
        info = {
            "id": "jNQXAC9IVRw", "title": "Recording", "uploader": "Author", "duration": 60,
            "is_live": False, "live_status": live_status,
        }
        assert predicate(info, incomplete=False) is None
        media = tmp_path / "download" / "source.m4a"
        media.write_bytes(b"fixture")
        (media.parent / "source.info.json").write_text(json.dumps(info))
        return SimpleNamespace(returncode=0, stdout=str(media), stderr="")

    result = download_bilibili_video(
        resolve_source(WATCH), tmp_path, runner=runner, audio_only=True
    )
    assert result.title == "Recording"
