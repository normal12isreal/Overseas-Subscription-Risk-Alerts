"""Bounded child-process boundary for the synchronous transcript library."""

import argparse
import json
import re
import sys


def fetch_transcript(video_id, languages, limit):
    from youtube_transcript_api import YouTubeTranscriptApi

    available = YouTubeTranscriptApi().list(video_id)
    tracks = list(available)
    codes = []
    for language in languages:
        # YouTube also uses base codes such as zh, rather than only zh-Hans.
        for candidate in [language, language.split("-")[0]]:
            if candidate not in codes:
                codes.append(candidate)
    for language in languages:
        for track in tracks:
            if track.language_code.split("-")[0] == language.split("-")[0]:
                if track.language_code not in codes:
                    codes.append(track.language_code)
    if not tracks:
        return {"status": "unavailable", "note": "未发现可用的转写文稿轨道"}
    # A different available language is still usable evidence for the model.
    codes.extend(t.language_code for t in tracks if t.language_code not in codes)
    selected = available.find_transcript(codes)
    transcript = selected.fetch()
    segments = []
    remaining = limit
    truncated = False
    for entry in transcript:
        if remaining <= 0:
            truncated = True
            break
        text = entry.text[:remaining]
        segments.append({"text": text, "start": entry.start, "duration": entry.duration})
        remaining -= len(text)
        truncated |= len(text) < len(entry.text)
    note = "YouTube转写文稿（自动生成）" if selected.is_generated else "YouTube转写文稿（非自动生成轨道）"
    if truncated:
        note += "；已按字符预算截断"
    return {"status": "success" if segments else "unavailable", "segments": segments,
            "content_kind": "transcript", "language_code": selected.language_code,
            "text": "\n".join(s["text"] for s in segments), "note": note}


def fetch_description(video_id, limit):
    from yt_dlp import YoutubeDL

    with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                    "ignore_no_formats_error": True, "socket_timeout": 6,
                    "retries": 0, "extractor_retries": 0}) as downloader:
        info = downloader.extract_info("https://www.youtube.com/watch?v=" + video_id,
                                       download=False)
    description = (info or {}).get("description") or ""
    lines = []
    # Remove standalone promotional/contact lines, retaining substantive paragraphs.
    promo = re.compile(
        r"^(?:欢迎订阅|订阅频道|商业合作(?:邮箱)?|商务合作(?:邮箱)?|合作邮箱|"
        r"个人合作网站(?:地址)?|[^：:\n]{0,35}(?:注册链接|邀请链接|搜索机器人|星球链接|全球链接)|"
        r"万能搜索|Flybird\s*小郑家园群)\s*[:：]", re.IGNORECASE)
    for line in description.splitlines():
        line = line.strip()
        if promo.match(line) or re.fullmatch(r"https?://\S+", line):
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    note = "作者视频简介，非转写文稿；已去除独立推广/联系链接行；不代表官方政策"
    if len(text) > limit:
        note += "；已按字符预算截断"
    return {"status": "success" if text else "unavailable", "content_kind": "description",
            "text": text[:limit], "note": note if text else "未取得可用的视频简介"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video_id")
    parser.add_argument("--languages", nargs="+", default=["en"])
    parser.add_argument("--limit", type=int, default=12000)
    parser.add_argument("--description-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.description_only:
            result = fetch_description(args.video_id, args.limit)
        else:
            result = fetch_transcript(args.video_id, args.languages, args.limit)
    except Exception as exc:
        name = type(exc).__name__
        result = {"status": "unavailable" if name in {
            "NoTranscriptFound", "TranscriptsDisabled", "VideoUnavailable", "AgeRestricted"
        } else "failed", "note": name}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
