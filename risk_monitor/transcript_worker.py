"""Bounded child-process boundary for the synchronous transcript library."""

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video_id")
    parser.add_argument("--languages", nargs="+", default=["en"])
    parser.add_argument("--limit", type=int, default=12000)
    args = parser.parse_args()
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        transcript = YouTubeTranscriptApi().fetch(args.video_id, languages=args.languages)
        segments = []
        remaining = args.limit
        truncated = False
        for entry in transcript:
            if remaining <= 0:
                truncated = True
                break
            text = entry.text[:remaining]
            segments.append({"text": text, "start": entry.start, "duration": entry.duration})
            remaining -= len(text)
            truncated |= len(text) < len(entry.text)
        result = {"status": "success" if segments else "unavailable", "segments": segments,
                  "text": "\n".join(s["text"] for s in segments),
                  "note": "已按字符预算截断" if truncated else "已有字幕，非音频转写"}
    except Exception as exc:
        name = type(exc).__name__
        result = {"status": "unavailable" if name in {
            "NoTranscriptFound", "TranscriptsDisabled", "VideoUnavailable", "AgeRestricted"
        } else "failed", "note": name}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
