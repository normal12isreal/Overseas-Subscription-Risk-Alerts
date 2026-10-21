# 外部项目源码

2026-10-21 按用户要求下载以下三个项目，用于后续阅读和接入。均保留独立的 Git 仓库，使用 `--depth 1` 浅克隆，未安装依赖或运行代码。

| 本地目录 | 上游仓库 | 下载时的提交 |
| --- | --- | --- |
| `apprise/` | https://github.com/caronc/apprise | `2b41a452bbbcb054a399a18e79882f627566c6b8` |
| `yt-dlp/` | https://github.com/yt-dlp/yt-dlp | `897341241f2bfb2439fe0367f4c7c9ccd29a3828` |
| `youtube-transcript-api/` | https://github.com/jdepoix/youtube-transcript-api | `b8af68ccf219ecb451018fe41318c12d08729780` |

父项目通过 `.gitignore` 排除这三个源码目录，只提交本说明，不把上游源码复制进主仓库，也未配置 Git 子模块。因此仅克隆父项目不会自动得到这三个目录。

在项目根目录重新下载当前上游版本：

```powershell
git clone --depth 1 https://github.com/caronc/apprise.git external/apprise
git clone --depth 1 https://github.com/yt-dlp/yt-dlp.git external/yt-dlp
git clone --depth 1 https://github.com/jdepoix/youtube-transcript-api.git external/youtube-transcript-api
```

上述命令适用于对应目录不存在时，下载的是执行时的上游版本；表内提交用于标记本次阅读基线。当前只验证了远程来源、提交及工作区状态，尚未验证工具运行效果。工具职责与接口见 [选型文档](../docs/tool-selection.md)。
