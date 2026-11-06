#!/usr/bin/env python3
"""Send a build-completion message through a WeCom self-built application."""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen


API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
REQUIRED_ENV = (
    "WECHAT_WORK_CORP_ID",
    "WECHAT_WORK_AGENT_ID",
    "WECHAT_WORK_SECRET",
    "WECHAT_WORK_TOUSER",
)


class WeComError(RuntimeError):
    """An error response from the WeCom API."""


def request_json(url, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WeComError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise WeComError(f"network error: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WeComError(f"invalid JSON response: {body[:200]}") from exc
    if parsed.get("errcode") not in (None, 0):
        raise WeComError(f"errcode={parsed.get('errcode')}, errmsg={parsed.get('errmsg')}")
    return parsed


def build_message():
    platform = os.environ["WECOM_PLATFORM"]
    version = os.environ["WECOM_VERSION"]
    status = os.environ.get("WECOM_STATUS", "success").lower()
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"{server_url}/{repository}/actions/runs/{run_id}" if repository and run_id else ""

    task_label = os.environ.get('WECOM_TASK_LABEL', '构建')
    status_text = f"✅ {task_label}成功" if status == "success" else f"❌ {task_label}失败"
    lines = [
        f"{status_text}｜RustDesk 自建客户端",
        f"平台：{platform}",
        f"版本：{version}",
    ]
    base = os.environ.get('DIST_BASE_URL', '').rstrip('/')
    capability = os.environ.get('DIST_DOWNLOAD_TOKEN', '')
    dist_platform = os.environ.get('WECOM_DIST_PLATFORM', '')
    files_json = os.environ.get('WECOM_DOWNLOAD_FILES', '')
    if status == 'success' and base and capability and dist_platform and files_json:
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid download origin')
        files = json.loads(files_json)
        if not isinstance(files, list) or not files:
            raise ValueError('Invalid download file list')
        lines.append('下载链接：')
        for item in files:
            if not isinstance(item, str):
                raise ValueError('Invalid download file')
            name = unquote(item)
            if not name or '/' in name or '\\' in name or '\n' in name or '\r' in name:
                raise ValueError('Invalid download file')
            url = '/'.join((base, quote(capability, safe=''), quote(dist_platform, safe=''), quote(name, safe='')))
            lines.append(f'{name}：{url}')
    if run_url:
        lines.append(f"构建详情：{run_url}")
    return "\n".join(lines)


def split_message(message, limit=2048):
    messages = []
    current = ''
    for line in message.splitlines():
        candidate = f'{current}\n{line}' if current else line
        if len(candidate.encode('utf-8')) > limit:
            if not current or len(line.encode('utf-8')) > limit:
                raise ValueError('Notification line exceeds the message limit')
            messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def send_notification():
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        print(
            "::notice::WeCom notification skipped; configure GitHub secret(s): "
            + ", ".join(missing)
        )
        return False

    token_response = request_json(
        f"{API_BASE}/gettoken?"
        + urlencode(
            {
                "corpid": os.environ["WECHAT_WORK_CORP_ID"],
                "corpsecret": os.environ["WECHAT_WORK_SECRET"],
            }
        )
    )
    access_token = token_response.get("access_token")
    if not access_token:
        raise WeComError("gettoken response did not contain access_token")

    for message in split_message(build_message()):
        response = request_json(
            f"{API_BASE}/message/send?" + urlencode({"access_token": access_token}),
            {
            "touser": os.environ["WECHAT_WORK_TOUSER"],
            "msgtype": "text",
            "agentid": int(os.environ["WECHAT_WORK_AGENT_ID"]),
            "text": {"content": message},
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
            },
        )
        invalid_users = response.get("invaliduser", "")
        if invalid_users:
            raise WeComError(f"message was rejected for user(s): {invalid_users}")
    print("WeCom completion notification sent.")
    return True


def main():
    try:
        send_notification()
    except (ValueError, WeComError) as exc:
        print(f"::warning::WeCom notification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
