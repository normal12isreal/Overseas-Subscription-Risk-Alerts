import asyncio
import json
from pathlib import Path
import re
import sys

from .config import Settings
from .domain import Collected, Material, ResearchTask, Source


async def json_process(args: list[str], timeout: float) -> dict:
    """Cancellation/timeout kills the actual child, not just the awaiting task."""
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f"Research subprocess exited {process.returncode}")
    return json.loads(output.decode("utf-8"))


class FixtureResearch:
    def __init__(self, path: Path):
        self.materials = json.loads(path.read_text(encoding="utf-8"))

    async def collect(self, task: ResearchTask) -> Collected:
        return Collected(purpose=task.purpose, query=task.query, status="success",
                         materials=[Material.model_validate(item) for item in self.materials[task.purpose]],
                         note="固定演示资料；不是抓取的视频字幕")


class YouTubeResearch:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def search(self, query: str) -> list[Source]:
        data = await json_process([
            sys.executable, "-m", "yt_dlp", "--ignore-config", "--flat-playlist",
            "--skip-download", "--no-warnings", "--socket-timeout", "10", "--retries", "0",
            "--extractor-retries", "0", "--dump-single-json",
            f"ytsearch{self.settings.max_videos}:{query}",
        ], self.settings.tool_timeout_seconds)
        result = []
        seen = set()
        for entry in data.get("entries", []) or []:
            video_id = entry.get("id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) and video_id not in seen:
                seen.add(video_id)
                result.append(Source(source_id=video_id, video_id=video_id,
                                     url="https://www.youtube.com/watch?v=" + video_id,
                                     title=entry.get("title") or video_id,
                                     published_at=entry.get("upload_date")))
        return result[:self.settings.max_videos]

    async def transcript(self, source: Source) -> Material:
        try:
            value = await json_process([
                sys.executable, "-m", "risk_monitor.transcript_worker", source.video_id,
                "--limit", str(self.settings.transcript_max_characters),
                "--languages", *self.settings.languages,
            ], self.settings.tool_timeout_seconds)
            return Material(source=source, **value)
        except TimeoutError:
            return Material(source=source, status="timed_out", note="字幕请求超时")
        except Exception as exc:
            return Material(source=source, status="failed", note=type(exc).__name__)

    async def collect(self, task: ResearchTask) -> Collected:
        sources = await self.search(task.query)
        if not sources:
            return Collected(purpose=task.purpose, query=task.query, status="empty", note="无搜索结果")
        # Sequential within a branch; all three branches can run in parallel.
        materials = [await self.transcript(source) for source in sources]
        if any(m.status == "success" for m in materials):
            status = "success"
        elif all(m.status == "timed_out" for m in materials):
            status = "timed_out"
        elif any(m.status == "failed" for m in materials):
            status = "failed"
        else:
            status = "empty"
        return Collected(purpose=task.purpose, query=task.query,
                         status=status,
                         materials=materials, note="字幕缺失与失败会逐项保留；不下载视频或音频")
