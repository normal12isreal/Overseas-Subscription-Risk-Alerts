"""Read-only Gmail REST adapter and interactive desktop OAuth bootstrap."""

import base64
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
from functools import partial

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Settings
from .mail import MailMessage, window_start
from .storage import Store, write_json

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(RuntimeError):
    """Only controlled, credential-free messages are surfaced by the CLI."""


def save_credentials(settings: Settings, credentials: Credentials):
    write_json(settings.gmail_token_file, json.loads(credentials.to_json()))


def load_credentials(settings: Settings) -> Credentials:
    if not settings.gmail_token_file.exists():
        raise GmailError("尚未授权 Gmail，请运行 .\\.venv\\Scripts\\python.exe -m risk_monitor auth")
    try:
        credentials = Credentials.from_authorized_user_file(str(settings.gmail_token_file), SCOPES)
        if not credentials.has_scopes(SCOPES):
            raise GmailError("缺少 Gmail 只读权限，请重新运行 auth")
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailError("Gmail 凭证已失效，请重新运行 auth")
            credentials.refresh(partial(Request(), timeout=settings.tool_timeout_seconds))
            save_credentials(settings, credentials)
        return credentials
    except RefreshError:
        raise GmailError("Gmail 授权失效或被撤销，请重新运行 auth") from None


def authorize(settings: Settings):
    path = settings.gmail_credentials_file
    if not path or not path.is_file():
        raise GmailError("请在 .env 的 GMAIL_CREDENTIALS_FILE 填入桌面 OAuth JSON 路径")
    flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES, autogenerate_code_verifier=True)
    try:
        credentials = flow.run_local_server(
            host="localhost", bind_addr="127.0.0.1", port=0, timeout_seconds=600,
            authorization_prompt_message="请在浏览器登录并允许只读 Gmail 访问；若浏览器未打开，请访问：\n{url}",
            success_message="Gmail authorization received. You may close this tab.",
            access_type="offline", prompt="consent",)
    except Exception:
        raise GmailError("Gmail 授权未完成。若 Google 拒绝访问，请检查 OAuth 测试用户及 Gmail API 是否启用，再运行 auth") from None
    save_credentials(settings, credentials)
    # Verify the API rather than treating an OAuth JSON/token as proof of access.
    reader = GmailReader(settings)
    try:
        reader.profile()
    finally:
        if reader.session is not None:
            reader.session.close()
    print("Gmail 只读授权及 API 连接验证成功，凭证已保存到本地。", flush=True)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def body_text(part: dict) -> str:
    headers = {h["name"].lower(): h["value"] for h in part.get("headers", [])}
    if part.get("filename") or headers.get("content-disposition", "").lower().startswith("attachment"):
        return ""
    mime = part.get("mimeType", "").lower()
    parts = part.get("parts", [])
    if parts:
        if mime == "multipart/alternative":
            plain = [p for p in parts if p.get("mimeType") == "text/plain"]
            return "\n".join(filter(None, (body_text(p) for p in (plain or parts[-1:]))))
        return "\n".join(filter(None, (body_text(p) for p in parts)))
    if mime not in {"text/plain", "text/html"}:
        return ""
    encoded = part.get("body", {}).get("data", "")
    if not encoded:
        return ""  # Never download an attachment to obtain a body.
    content_type = Message()
    content_type["content-type"] = headers.get("content-type", mime)
    charset = content_type.get_content_charset() or "utf-8"
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if mime == "text/html":
        parser = _HTMLText()
        parser.feed(text)
        text = "".join(parser.text)
    return text.strip()


def parse_message(raw: dict, mailbox: str, limit: int) -> MailMessage:
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    text = body_text(payload)
    fallback = not text
    if fallback:
        text = raw.get("snippet", "")
    return MailMessage(
        mailbox=mailbox, message_id=raw["id"],
        received_at=datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc),
        sender=str(make_header(decode_header(headers.get("from", ""))))[:500],
        subject=str(make_header(decode_header(headers.get("subject", ""))))[:1000],
        body=text[:limit], truncated=len(text) > limit or fallback)


class GmailReader:
    def __init__(self, settings: Settings, session=None):
        self.settings = settings
        self.session = session  # Injected transport for tests, never alternate API URLs.

    def get(self, resource: str, **params):
        if self.session is None:
            self.session = AuthorizedSession(load_credentials(self.settings), refresh_timeout=self.settings.tool_timeout_seconds)
        try:
            response = self.session.get(API + resource, params=params, timeout=self.settings.tool_timeout_seconds)
        except RefreshError:
            raise GmailError("Gmail 授权已失效，请重新运行 auth") from None
        if response.status_code in {401, 403}:
            raise GmailError(f"Gmail API 返回 {response.status_code}，请核对授权、OAuth 测试用户和 Gmail API 启用状态")
        if response.status_code >= 400:
            raise GmailError(f"Gmail API 暂不可用（HTTP {response.status_code}），稍后重试")
        return response.json()

    def profile(self) -> str:
        address = self.get("/profile")["emailAddress"].strip().casefold()
        return sha256(address.encode("utf-8")).hexdigest()[:24]

    def read(self, now: datetime, store: Store) -> dict:
        mailbox = self.profile()
        start = window_start(now)
        # Epoch seconds avoid Gmail's date-string PST interpretation. Local checks
        # use internalDate milliseconds to enforce the exact inclusive 2h window.
        query = f"after:{math.floor(start.timestamp()) - 1} before:{math.ceil(now.timestamp()) + 1} -in:spam -in:trash -in:drafts"
        stats = {"window_start": start.isoformat(), "window_end": now.isoformat(),
                 "listed": 0, "queued": 0, "duplicates": 0, "skipped": 0,
                 "has_more": False, "errors": []}
        page_token = None
        fetched = 0
        for _ in range(100):  # Bound API pagination independently of the body limit.
            page = self.get("/messages", q=query, maxResults=100,
                            includeSpamTrash=False, **({"pageToken": page_token} if page_token else {}))
            messages = page.get("messages", [])
            for index, item in enumerate(messages):
                stats["listed"] += 1
                if store.mail_known(mailbox, item["id"]):
                    stats["duplicates"] += 1
                    continue
                fetched += 1
                try:
                    raw = self.get("/messages/" + item["id"], format="full")
                    labels = set(raw.get("labelIds", []))
                    received = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc)
                    if labels & {"SPAM", "TRASH", "DRAFT"} or ("SENT" in labels and "INBOX" not in labels):
                        stats["skipped"] += 1
                        store.skip_mail(mailbox, item["id"], received, now)
                    elif not start <= received <= now:
                        # Do not permanently ignore an item a few milliseconds
                        # beyond this round's end: it may be eligible next time.
                        stats["skipped"] += 1
                    else:
                        message = parse_message(raw, mailbox, self.settings.mail_max_characters)
                        if store.queue_mail(message, now):
                            stats["queued"] += 1
                        else:
                            stats["duplicates"] += 1
                except GmailError:
                    raise
                except Exception as exc:
                    stats["errors"].append({"message_id": item["id"], "error": type(exc).__name__})
                if fetched >= self.settings.gmail_max_messages:
                    stats["has_more"] = index + 1 < len(messages) or bool(page.get("nextPageToken"))
                    return stats
            page_token = page.get("nextPageToken")
            if not page_token:
                return stats
        stats["has_more"] = True
        return stats
