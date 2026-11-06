"""Bilibili page-state compatibility for the pinned yt-dlp extractor."""

from __future__ import annotations

import re
from functools import wraps

from yt_dlp.extractor.bilibili import BiliBiliBangumiIE, BiliBiliIE
from yt_dlp.utils import ExtractorError


def install_bilibili_details_fallback() -> None:
    """Use signed public details when Bilibili omits its HTML initial state.

    Adapted from yt-dlp PR #17898 (Unlicense), pinned upstream revision
    e88263195a928487cd1802f1a626931f738db5e3:
    https://github.com/yt-dlp/yt-dlp/pull/17898
    Keep the existing extractor for formats, parts, subtitles and access checks.
    Remove this compatibility patch when the pinned upstream includes the fix.
    """
    original = BiliBiliIE._search_json
    if getattr(original, "_video_report_details_fallback", False):
        return
    original_extract = BiliBiliIE._real_extract

    @wraps(original_extract)
    def real_extract(self, url):
        prefix, requested_id = self._match_valid_url(url).group("prefix", "id")
        if prefix.upper() == "BV" and re.fullmatch(r"[0-9A-Za-z]{10}", requested_id):
            identity = {"bvid": "BV" + requested_id}
        elif prefix.upper() == "AV" and re.fullmatch(r"[0-9]+", requested_id):
            identity = {"aid": requested_id}
        else:
            raise ExtractorError("Invalid Bilibili video ID", expected=True)
        self._video_report_details_identity = identity
        try:
            return original_extract(self, url)
        finally:
            del self._video_report_details_identity

    @wraps(original)
    def search_json(self, start_pattern, webpage, name, video_id, **kwargs):
        state = original(self, start_pattern, webpage, name, video_id, **kwargs)
        if name != "initial state" or state:
            return state
        risk = original(
            self, r"\bwindow\._riskdata_\s*=", webpage, "risk", video_id, default={}
        )
        if isinstance(risk, dict) and risk.get("v_voucher"):
            raise ExtractorError("You have exceeded the rate limit. Try again later", expected=True)

        # The search call omits the prefix, so use the original URL's identity.
        # Numeric BV suffixes must never be interpreted as an AV/aid request.
        query = {"platform": "web", **self._video_report_details_identity}
        headers = {
            **self.geo_verification_headers(),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        details = self._download_json(
            "https://api.bilibili.com/x/web-interface/wbi/view/detail", video_id,
            note="Downloading video details", query=self._sign_wbi(query, video_id),
            headers=headers,
        )
        if not isinstance(details, dict) or type(details.get("code")) is not int:
            raise ExtractorError("Invalid Bilibili video details response")
        code = details["code"]
        if code in {-403, -404}:
            # Preserve the pinned extractor's login and unavailable/region errors.
            return {"error": {"trueCode": code}}
        if code:
            raise ExtractorError(f"Bilibili video details returned error {code}", expected=True)
        data = details.get("data")
        view = data.get("View") if isinstance(data, dict) else None
        if not isinstance(view, dict):
            raise ExtractorError("Bilibili video details contain no video data")
        redirect = view.get("redirect_url")
        if isinstance(redirect, str) and BiliBiliBangumiIE.suitable(redirect):
            # Let the original extractor retain its Bangumi redirection behavior.
            return state

        bvid, aid, cid = view.get("bvid"), view.get("aid"), view.get("cid")
        if (
            not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
            or type(aid) is not int or aid <= 0
            or type(cid) is not int or cid <= 0
            or not isinstance(view.get("title"), str) or not view["title"].strip()
            or ("bvid" in query and bvid != query["bvid"])
            or ("aid" in query and aid != int(query["aid"]))
        ):
            raise ExtractorError("Bilibili video details do not match the requested video")
        pages = view.get("pages")
        if pages is not None and (
            not isinstance(pages, list)
            or any(not isinstance(page, dict) or type(page.get("cid")) is not int
                   or page["cid"] <= 0 for page in pages)
        ):
            raise ExtractorError("Bilibili video details contain invalid video parts")
        owner = view.get("owner")
        tags = data.get("Tags")
        return {
            "videoData": view,
            "upData": owner if isinstance(owner, dict) else {},
            "tags": tags if isinstance(tags, list) else [],
        }

    search_json._video_report_details_fallback = True
    BiliBiliIE._search_json = search_json
    BiliBiliIE._real_extract = real_extract
