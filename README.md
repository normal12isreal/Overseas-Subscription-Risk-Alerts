# 邮箱服务风险监测与应对助手

已经实现 Gmail 读取、邮件风险识别和后续完整 LangGraph 工作流。每轮读取最近两小时的邮件；一封邮件可拆出多个独立事件，每个事件分别进行三路研究和提醒。

## 接续入口

- [流程、节点与检查条件](docs/workflow-design.md)
- [当前实现、代码入口与运行边界](docs/runtime.md)
- [邮件通知、YouTube 搜索与字幕工具选型](docs/tool-selection.md)
- [已下载的外部项目源码与版本](external/README.md)
- [Agent 协作约定](AGENTS.md)

后续接续指令示例：

> 先读 AGENTS.md 和 docs/runtime.md，再查看目标节点的代码；保持每个事件独立研究，以及3小时提醒和24小时复查互不取消。

## 当前进度（2026-10-21）

- 已保存完整业务流程、三路研究与汇总、两条独立的时间机制及工具调用边界。
- 已明确：删除待办只取消 3 小时升级提醒；每个事件的 24 小时重新研究和更新邮件仍然执行。
- 已记录“代码里的删除”的对象、24 小时复查是否持续每日循环、复查后清理范围等待明确项。
- 已将三个上游仓库浅克隆到 `external/`，保留各自 Git 信息；项目依赖安装在 `.venv`。
- 每个事件独立整理事实、生成背景/申诉/应对任务、搜索视频、获取字幕、提炼、等待三路终态并生成方案。
- 搜索采用“具体服务/对象＋问题”的短词，另两路只追加“申诉”和“应对处理方式”，由程序固定拼接。每路前5个视频分别获取字幕，已取得的各份字幕都进入提炼，单视频超过50000字符时标记截断。
- 获取资料先枚举实际转写语言（包含 zh），转写失败时读取作者视频简介并过滤独立推广链接行；记录资料类型和失败原因，简介不冒充完整视频内容。
- 七天内需处理的停用、暂停及身份核验按紧急事件评估；通过业务上下文注入规则和评估时间，程序采用相同规则保底。模拟事件仍按假设的账号问题研究，不查询“模拟通知怎么处理”。
- 用户报告只保留情况、最多三项动作和相关来源，不整段展示三路研究及限制；完整查询、失败状态与证据留在本地JSON中。
- 千问兼容接口默认使用 `qwen-flash`；搜索使用 yt-dlp，字幕使用 youtube-transcript-api，通知使用 Apprise。
- 待办默认写入 `C:\Users\35271\Desktop\tryy`；通知默认暂存到 `data/outbox/`，`--notify` 才实际发送。
- 三个示例均为虚构 giffgaff 号码，模拟通知称将在2026-12-21停用，即相对2026-10-21两个月后；不代表官方政策或真实账号状态。
- Gmail 只读 OAuth 已完成，并实际读取最近两小时的2封邮件，千问判断均无风险；重复轮询跳过这2封邮件，没有再次调用模型。虚构多账号邮件经真实千问识别出3个独立风险。
- 已有邮件读取、去重、识别、独立研究、七天风险上下文、简版报告、待办与通知流程的44项本地测试。当前验证结果与限制见 [实现记录](docs/runtime.md)。未启动后台定时器。

## 运行

在项目根目录使用 PowerShell，无需激活环境：

```powershell
# 首次或授权失效时：浏览器登录，允许 Gmail 只读权限
.\.venv\Scripts\python.exe -m risk_monitor auth

# 读取最近两小时邮件、识别风险、处理事件，同时检查两条到期机制
.\.venv\Scripts\python.exe -m risk_monitor poll

# 模拟事件 + 真实千问 + 真实 YouTube，默认不向外发通知
.\.venv\Scripts\python.exe -m risk_monitor run

# 检查待办和到期复查，不读取邮箱
.\.venv\Scripts\python.exe -m risk_monitor tick

# 查看事件台账，不调用模型
.\.venv\Scripts\python.exe -m risk_monitor status

# 显式启用实际通知；接收人从配置读取
.\.venv\Scripts\python.exe -m risk_monitor tick --notify

# 每小时读最近两小时邮件并检查提醒，前台运行，Ctrl+C停止
.\.venv\Scripts\python.exe -m risk_monitor watch --interval 3600
```

同一事件重复输入复用已完成结果，不重设3小时/24小时起点。发送失败或只暂存时，复用已生成方案重试交付，不在每次重试时重新消费模型和检索。

## 离线学习与验证

```powershell
# 输出只在演示目录，不调用外部服务或写桌面
.\.venv\Scripts\python.exe -m risk_monitor run --model demo --research fixture --data-dir data-demo --todo-dir data-demo/todos --now "2026-10-21T12:00:00+08:00"

# 模拟经过3小时/24小时，不改变系统时间
.\.venv\Scripts\python.exe -m risk_monitor tick --model demo --research fixture --data-dir data-demo --todo-dir data-demo/todos --now "2026-10-21T15:00:00+08:00"
.\.venv\Scripts\python.exe -m risk_monitor tick --model demo --research fixture --data-dir data-demo --todo-dir data-demo/todos --now "2026-10-22T12:00:00+08:00"

.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m risk_monitor graph
.\.venv\Scripts\python.exe -m risk_monitor graph --view inbox
```

`demo` 是确定性模拟模型，`fixture` 是人工整理演示摘要，明确不是实际字幕。不同验证模式使用不同 `--data-dir`，避免将已缓存的演示结果当成新一轮真实研究。

## 配置与环境

- `settings.toml`：目录、并发、时间阈值、每路最多5个视频、邮件数量/正文上限、超时和模型调用预算。
- `.env`：本地模型地址、Key、Telegram Token、Chat ID，以及 Gmail OAuth 文件路径，已忽略，不进入 Git。
- `data/gmail-token.json`：首次授权后保存的账户凭证，已忽略；可自动刷新，撤销或失效后重新运行 auth。
- `.env.example`：可提交的字段模板。
- `requirements.lock.txt`：本次验证环境的依赖版本。

重新准备环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

`poll/watch` 只使用真实时钟和真实千问识别，不接受模拟时间或 demo 模型。默认最多读取10封未处理邮件、每封正文12000字符；达到上限时输出 `has_more=true`，后续轮询继续跳过已处理邮件。大量邮件时应增加 `gmail_max_messages`、适当增加模型预算或缩短轮询间隔。停机超过两小时不会补读更早的邮箱邮件；此前已缓存但识别失败的邮件仍会重试。
