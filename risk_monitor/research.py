import asyncio
import json
from pathlib import Path
import re
import sys
from time import monotonic

from .config import Settings
from .domain import Collected, Material, Purpose, ResearchPlan, ResearchTask, Source


def simple_research_plan(service: str, service_status: str, notice: str) -> ResearchPlan:
    """Object + actual issue, then exactly the two user-defined suffixes."""
    issue = {"scheduled_deactivation": "停用", "suspended": "暂停",
             "cancelled": "取消", "verification_required": "身份认证"}[service_status]
    if service_status in {"scheduled_deactivation", "suspended"}:
        if re.search(r"封号|封禁|\bban(?:ned)?\b", notice, re.IGNORECASE):
            issue = "封号"
        elif re.search(r"暂停|suspend", notice, re.IGNORECASE):
            issue = "暂停"
    object_name = " 银行卡" if re.search(r"银行卡|\b(?:bank|debit|credit)\s+card\b", notice, re.IGNORECASE) else ""
    base = service.strip() + object_name + " " + issue
    directions = [(Purpose.BACKGROUND, "", "了解该问题"),
                  (Purpose.APPEAL, " 申诉", "查找申诉方法"),
                  (Purpose.RESPONSE, " 应对处理方式", "查找应对处理方式")]
    return ResearchPlan(tasks=[ResearchTask(purpose=p, query=base + suffix, goal=goal)
                               for p, suffix, goal in directions])


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
        deadline = monotonic() + self.settings.tool_timeout_seconds
        args = [sys.executable, "-m", "risk_monitor.transcript_worker", source.video_id,
                "--limit", str(self.settings.transcript_max_characters),
                "--languages", *self.settings.languages]
        try:
            # Reserve part of the existing per-video budget for description fallback.
            value = await json_process(args, min(12, self.settings.tool_timeout_seconds / 2))
            result = Material(source=source, **value)
            if result.status == "success" and (result.text or result.segments):
                return result
        except TimeoutError:
            result = Material(source=source, status="timed_out", note="转写文稿请求超时")
        except Exception as exc:
            result = Material(source=source, status="failed", note=type(exc).__name__)
        transcript_note = result.note
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            value = await json_process([*args, "--description-only"], remaining)
            fallback = Material(source=source, **value)
            fallback.note = f"转写文稿未取得：{transcript_note}；{fallback.note}"
            return fallback
        except TimeoutError:
            return Material(source=source, status="timed_out",
                            note=f"转写文稿未取得：{transcript_note}；视频简介请求超时")
        except Exception as exc:
            return Material(source=source, status="failed",
                            note=f"转写文稿未取得：{transcript_note}；视频简介请求失败：{type(exc).__name__}")

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
                         materials=materials, note="优先转写文稿，失败时读取作者简介；资料类型与失败原因逐项保留；不下载视频或音频")
