"""将来源短链接转为本地视频时间，不改动代码示例。"""
import math
from ..analysis.markdown import parser, map_links
from ..util.time import timestamp

SOURCE_LINK = r"(?<!\\)\[[^\]\n]*\]\(#source-(?P<source>E[1-9]\d*)\)"
TIME_LINK = r"(?<!\\)\[(?P<label>\d{2,}:\d{2})\]\(#video-time-\d+(?:\.\d+)?\)"


def render_timestamps(markdown, sources):
    expected = 0
    in_heading = False
    for token in parser.parse(markdown):
        if token.type == "heading_open":
            in_heading = True
        elif token.type == "heading_close":
            in_heading = False
        heading_links = 0
        for child in token.children or []:
            if child.type == "link_open":
                href = child.attrGet("href") or ""
                if href.startswith("#video-time-"):
                    raise ValueError("video_time_must_use_source_id_not_guessed_seconds")
                if href.startswith("#source-"):
                    if href.removeprefix("#source-") not in sources:
                        raise ValueError("video_time_source_must_belong_to_primary")
                    if not in_heading:
                        raise ValueError("video_time_link_only_in_section_heading_not_body")
                    heading_links += 1
                    expected += 1
        if in_heading and token.type == "inline" and heading_links != 1:
            raise ValueError("each_section_heading_requires_exactly_one_[回看](#source-E1)")
    if not expected:
        raise ValueError("video_time_link_required_use_[回看](#source-E1)_from_primary")
    replaced = 0
    def replace(match):
        nonlocal replaced
        seconds = float(sources[match.group("source")])
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("video_time_source_invalid")
        replaced += 1
        target = f"{seconds:.6f}".rstrip("0").rstrip(".")
        return f"[{timestamp(seconds)}](#video-time-{target})"
    result = map_links(markdown, SOURCE_LINK, replace)
    if replaced != expected:
        raise ValueError("video_time_link_format_must_be_[回看](#source-E1)")
    return result


def plain_timestamps(markdown):
    return map_links(markdown, TIME_LINK, lambda match: match.group("label"))
