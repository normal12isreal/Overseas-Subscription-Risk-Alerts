# 外部工具选型与调用示例

核对日期：2026-10-21。用户已确认需要的是 YouTube 视频字幕。
本页记录候选工具和接口；仅核对项目文档，未安装、未实测、未接入业务，也未发送邮件。以下命令均为以后安装配置后的示例。

## 1. 建议组合

先采用 Apprise + yt-dlp，覆盖邮件通知、视频搜索和字幕获取。需要直接返回结构化字幕时，再考虑 youtube-transcript-api；不用现在引入三个依赖。

| 能力 | 建议工具 | 配置或输入 | 输出 |
| --- | --- | --- | --- |
| 邮件通知 | [Apprise](https://github.com/caronc/apprise) | 邮箱发送授权、固定收件人、标题与正文 | 发送操作结果 |
| 关键词找视频 | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 搜索词与结果上限 5 | 视频观看链接 |
| 链接取字幕 | yt-dlp | 视频 URL、所需语言 | 已有人工或自动字幕文件 |
| 结构化字幕备选 | [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) | 视频 ID、语言优先级 | text/start/duration 字幕片段 |

这些是现成底层工具。项目仍需少量调用代码来整理输入、结果和错误；当前尚未编写。

## 2. 邮件通知：Apprise

适合“先配置一次，以后只传标题和正文”的需求。配置文件保存通知目标，节点不需要每次重复构造邮箱设置。

需要准备发送账号、邮箱商要求的授权方式、收件人；自定义 SMTP 还需服务器、端口和加密设置。授权码不必然等于邮箱登录密码。Outlook / Microsoft 365 当前应查看 Apprise 的 Azure 邮件插件说明，不能套用普通密码 SMTP 配置。

安装配置以后，命令形式为：

```powershell
apprise --config "apprise.txt" --title "服务风险提醒" --body "这里是生成的处理方案"
```

`apprise.txt` 需先按所用邮箱配置通知 URL；它不是已经存在的项目文件。凭据配置不进入 Git。收件人由用户配置，模型只生成标题和正文。

节点收到工具成功回执以后才记录已发送。邮件服务器接受请求不等于保证到达收件箱；通知去重与重试由本项目单独管理，不能假定 Apprise 自动完成业务去重。

来源：[CLI 与配置参数](https://appriseit.com/cli/)、[邮箱配置与授权要求](https://appriseit.com/services/email/)。

## 3. 搜索前 5 个视频：yt-dlp

```powershell
yt-dlp --ignore-config --flat-playlist --skip-download --print "https://www.youtube.com/watch?v=%(id)s" "ytsearch5:service account suspended appeal"
```

替换冒号后面的搜索词。`ytsearch5:` 请求最多 5 个视频，输出每行一个观看链接，不下载视频。结果遵循此次搜索返回的顺序，不保证与用户登录浏览器的个性化排序一致，也不代表内容质量已经过筛选。

源码包含 `ytsearch5` 的五结果示例：[YouTube 搜索实现与测试](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_search.py)。输出模板等参数见 [yt-dlp 文档](https://github.com/yt-dlp/yt-dlp#usage-and-options)。

业务代码以后优先使用 Python 接口或 JSON 输出，保留视频 ID、标题和来源 URL；不要解析人类阅读的进度日志。`--get-url` 获取的是媒体流地址，不作为视频观看链接接口。

三个研究方向分别调用搜索；每路最多 5 条时，一轮最多处理 15 个候选视频，重复视频可合并获取字幕。具体超时与并发限制留到实现时配置。

## 4. 按视频链接获取字幕

继续使用 yt-dlp：

```powershell
yt-dlp --ignore-config --skip-download --write-subs --write-auto-subs --sub-langs "en.*,zh.*" --sub-format vtt -o "subtitles/%(id)s.%(ext)s" "VIDEO_URL"
```

把 `VIDEO_URL` 换成实际视频链接。示例尝试获取匹配语言的人工字幕或自动字幕，输出 VTT 文件。后续清理字幕标记、重复片段并保留时间点，再交给提炼节点。

字幕并非任意视频都有；没有可用字幕时要明确返回缺失，不能伪造文本。搜索与字幕获取还可能受网络、限流和平台变动影响。当前 yt-dlp 文档说明，完整 YouTube 支持还需要 EJS 支持组件与受支持的 JavaScript 运行时，部署时按发行包说明核对，不能承诺只放一个文件就始终可用。

来源：[字幕选项](https://github.com/yt-dlp/yt-dlp#subtitle-options)、[运行依赖](https://github.com/yt-dlp/yt-dlp#dependencies)。

若未来更看重直接交给 Python 的结构化数据，可改用 youtube-transcript-api：

```python
from youtube_transcript_api import YouTubeTranscriptApi

segments = YouTubeTranscriptApi().fetch(
    "VIDEO_ID", languages=["en", "zh-Hans", "zh-Hant"]
).to_raw_data()
```

这是备选接口示例，未执行。该库接收视频 ID；传入 URL 前要先提取 ID。返回片段包含 `text`、`start`、`duration`，语言列表表示偏好顺序。它不要求 YouTube API Key，但属于第三方字幕工具，也可能遇到 IP 阻断；无字幕视频仍需要另行设计语音转写方案，当前不增加该分支。

来源：[当前 API、语言与限制说明](https://github.com/jdepoix/youtube-transcript-api)。

## 5. 以后接入节点的边界

沿用流程设计中的接口，不让上层节点依赖具体命令参数：

```text
youtube.search(query, max_videos=5) -> list[VideoSource]
youtube.get_transcript(video_id, languages) -> TranscriptResult
notification.send_email(configured_recipient, subject, body, dedupe_key) -> SendReceipt
```

调用层将搜索结果统一成 ID 与 URL，将字幕结果区分为成功、无可用字幕、失败或超时。发送层的 dedupe_key 由本项目使用，不能直接当成 Apprise 已有参数。

这些底层工具负责操作；研究方向、摘要提炼、紧急程度判断与三路汇总仍由工作流负责。邮件通知工具也不包含读取邮箱的功能；收件箱读取另选接口。

下一次用户明确要求搭建时，再确定邮箱提供方、配置目录及运行环境，完成安装与最小调用验证。
