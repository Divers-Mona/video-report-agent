import copy
import json
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.utils import ExtractorError

from video_report_agent.bilibili_ytdlp import install_bilibili_details_fallback

URL = "https://www.bilibili.com/video/BV1MrnR6xEus/"


@pytest.fixture
def details():
    return {"code": 0, "data": {
        "View": {
            "bvid": "BV1MrnR6xEus", "aid": 117506815560337, "cid": 43071897832,
            "title": "全套 ict 量化流程简析", "desc": "说明", "pubdate": 1,
            "owner": {"name": "TraderXian", "mid": 123},
            "stat": {"like": 3, "view": 4, "reply": 2},
            "pages": [{"page": 1, "part": "第一部分", "cid": 43071897832},
                      {"page": 2, "part": "第二部分", "cid": 43071897833}],
        },
        "Tags": [{"tag_name": "量化"}],
    }}


@pytest.fixture
def extractor(monkeypatch, details):
    # Undo the process-wide compatibility installation after each test.
    monkeypatch.setattr(BiliBiliIE, "_search_json", BiliBiliIE._search_json)
    monkeypatch.setattr(BiliBiliIE, "_real_extract", BiliBiliIE._real_extract)
    ie = BiliBiliIE(YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}))
    calls = []
    monkeypatch.setattr(ie, "_download_webpage_handle",
                        lambda url, *a, **k: ("<html></html>", SimpleNamespace(url=url)))
    monkeypatch.setattr(ie, "geo_verification_headers", lambda: {})
    monkeypatch.setattr(ie, "_sign_wbi", lambda query, _: {**query, "w_rid": "signed"})

    def download_json(url, *args, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/view/detail"):
            return copy.deepcopy(details)
        if url.endswith("/pagelist"):
            return {"data": details.get("data", {}).get("View", {}).get("pages", [])}
        raise AssertionError(f"Unexpected network request: {url}")

    monkeypatch.setattr(ie, "_download_json", download_json)
    monkeypatch.setattr(ie, "_download_playinfo", lambda bvid, cid, **k: {
        "dash": {"audio": [{"baseUrl": "https://media.test/audio.m4s", "id": 30280,
                            "mimeType": "audio/mp4", "codecs": "mp4a.40.2"}]},
        "timelength": 1741264 if cid == 43071897832 else 60000,
    })
    monkeypatch.setattr(ie, "_get_chapters", lambda *a: [])
    monkeypatch.setattr(ie, "extract_subtitles", lambda *a: {})
    monkeypatch.setattr(ie, "extract_comments", lambda *a: None)
    return ie, calls


def test_missing_page_state_uses_signed_details_and_existing_formats(extractor):
    ie, calls = extractor
    # The pinned upstream rejects these otherwise complete video details.
    with pytest.raises(ExtractorError, match="Unable to extract initial state"):
        ie._real_extract(URL)
    calls.clear()
    install_bilibili_details_fallback()
    result = ie._real_extract(URL)
    assert result["id"] == "BV1MrnR6xEus_p1"
    assert result["title"] == "全套 ict 量化流程简析 p01 第一部分"
    assert result["duration"] == 1741.264
    assert result["uploader"] == "TraderXian" and result["tags"] == ["量化"]
    assert result["formats"][0]["url"] == "https://media.test/audio.m4s"
    assert result["formats"][0]["vcodec"] == "none"
    query = calls[0][1]["query"]
    assert query == {"platform": "web", "bvid": "BV1MrnR6xEus", "w_rid": "signed"}
    assert calls[0][1]["headers"]["Referer"] == "https://www.bilibili.com/"


def test_selected_part_and_av_id_still_use_original_extraction(extractor):
    ie, calls = extractor
    install_bilibili_details_fallback()
    result = ie._real_extract("https://www.bilibili.com/video/av117506815560337/?p=2")
    assert calls[0][1]["query"]["aid"] == "117506815560337"
    assert result["id"] == "BV1MrnR6xEus_p2"
    assert result["title"].endswith("p02 第二部分")
    assert result["duration"] == 60


def test_numeric_bv_suffix_is_not_an_av_request(extractor, details):
    ie, calls = extractor
    details["data"]["View"]["bvid"] = "BV0057002198"
    install_bilibili_details_fallback()
    result = ie._real_extract("https://www.bilibili.com/video/BV0057002198/")
    assert calls[0][1]["query"] == {
        "platform": "web", "bvid": "BV0057002198", "w_rid": "signed",
    }
    assert result["id"] == "BV0057002198_p1"
    assert not hasattr(ie, "_video_report_details_identity")


def test_numeric_bv_rejects_unrelated_av_details(extractor):
    ie, calls = extractor
    install_bilibili_details_fallback()
    with pytest.raises(ExtractorError, match="do not match"):
        ie._real_extract("https://www.bilibili.com/video/BV0057002198/")
    assert calls[0][1]["query"]["bvid"] == "BV0057002198"
    assert "aid" not in calls[0][1]["query"]
    assert not hasattr(ie, "_video_report_details_identity")


def test_existing_page_state_does_not_request_details(extractor, details, monkeypatch):
    ie, calls = extractor
    page = "window.__INITIAL_STATE__=" + json.dumps({
        "videoData": details["data"]["View"], "upData": {"name": "页面作者"},
    }) + ";"
    monkeypatch.setattr(ie, "_download_webpage_handle",
                        lambda url, *a, **k: (page, SimpleNamespace(url=url)))
    install_bilibili_details_fallback()
    assert ie._real_extract(URL)["uploader"] == "页面作者"
    assert not any(url.endswith("/view/detail") for url, _ in calls)


def test_risk_page_preserves_rate_limit_failure(extractor, monkeypatch):
    ie, calls = extractor
    monkeypatch.setattr(ie, "_download_webpage_handle", lambda url, *a, **k: (
        'window._riskdata_={"v_voucher":"challenge"};', SimpleNamespace(url=url)))
    install_bilibili_details_fallback()
    with pytest.raises(ExtractorError, match="exceeded the rate limit"):
        ie._real_extract(URL)
    assert calls == []


@pytest.mark.parametrize("code,message", [
    (-403, "registered users"), (-404, "deleted or geo-restricted"),
])
def test_details_preserve_access_errors(extractor, details, code, message):
    ie, _ = extractor
    details["code"] = code
    install_bilibili_details_fallback()
    with pytest.raises(ExtractorError, match=message):
        ie._real_extract(URL)


@pytest.mark.parametrize("change", [
    lambda d: d.update(code=True),
    lambda d: d.update(code=-352),
    lambda d: d.update(data={}),
    lambda d: d["data"]["View"].update(bvid="BV1234567890"),
    lambda d: d["data"]["View"].update(cid=True),
    lambda d: d["data"]["View"].update(pages=[{"cid": "43071897832"}]),
])
def test_unusable_or_mismatched_details_fail_explicitly(extractor, details, change):
    ie, _ = extractor
    change(details)
    install_bilibili_details_fallback()
    with pytest.raises(ExtractorError):
        ie._real_extract(URL)


def test_bangumi_redirect_still_uses_original_route(extractor, details):
    ie, _ = extractor
    details["data"]["View"]["redirect_url"] = "https://www.bilibili.com/bangumi/play/ep21495"
    install_bilibili_details_fallback()
    result = ie._real_extract(URL)
    assert result["_type"] == "url" and result["ie_key"] == "BiliBiliBangumi"


def test_installation_is_idempotent(extractor):
    install_bilibili_details_fallback()
    first = BiliBiliIE._search_json
    install_bilibili_details_fallback()
    assert BiliBiliIE._search_json is first
