# -*- coding: utf-8 -*-
"""ntfy notification sender."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse, urlunparse

import requests

from src.config import Config
from src.formatters import strip_hidden_markdown_metadata


logger = logging.getLogger(__name__)


# ntfy.sh free tier caps a single message at roughly 4 KiB. A full decision
# dashboard is much larger, so the publisher splits it into readable chunks at
# line boundaries (HTTP 413 otherwise: "JSON body too large").
def _split_ntfy_content(content: str, max_bytes: int = 3500):
    """Split UTF-8 text into chunks whose encoded size stays below max_bytes."""
    if len(content.encode("utf-8")) <= max_bytes:
        return [content]
    chunks = []
    current = []
    current_size = 0
    for line in content.splitlines(keepends=True):
        line_size = len(line.encode("utf-8"))
        if line_size > max_bytes:
            if current:
                chunks.append("".join(current))
                current, current_size = [], 0
            buf, buf_size = [], 0
            for char in line:
                char_size = len(char.encode("utf-8"))
                if buf_size + char_size > max_bytes and buf:
                    chunks.append("".join(buf))
                    buf, buf_size = [], 0
                buf.append(char)
                buf_size += char_size
            current, current_size = buf, buf_size
            continue
        if current_size + line_size > max_bytes and current:
            chunks.append("".join(current))
            current, current_size = [line], line_size
        else:
            current.append(line)
            current_size += line_size
    if current:
        chunks.append("".join(current))
    return chunks


def resolve_ntfy_endpoint(ntfy_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Split NTFY_URL into server root and topic from the final path segment."""
    raw_url = (ntfy_url or "").strip().rstrip("/")
    if not raw_url:
        return None, None

    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None, None

    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if not path_segments:
        return None, None

    topic = unquote(path_segments[-1]).strip()
    if not topic:
        return None, None

    root_path = "/".join(path_segments[:-1])
    server_url = urlunparse(
        parsed._replace(
            path=f"/{root_path}" if root_path else "",
            params="",
            query="",
            fragment="",
        )
    ).rstrip("/")

    return server_url, topic


class NtfySender:
    """Send Markdown text notifications through the ntfy JSON publish API."""

    def __init__(self, config: Config):
        self._ntfy_url = getattr(config, "ntfy_url", None)
        self._ntfy_token = getattr(config, "ntfy_token", None)
        self._webhook_verify_ssl = getattr(config, "webhook_verify_ssl", True)

    def _is_ntfy_configured(self) -> bool:
        return bool(self._ntfy_url)

    def _resolve_ntfy_endpoint(self) -> Tuple[Optional[str], Optional[str]]:
        return resolve_ntfy_endpoint(self._ntfy_url)

    def send_to_ntfy(
        self,
        content: str,
        title: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Publish a notification to ntfy using a JSON body with UTF-8 text."""
        if not self._is_ntfy_configured():
            logger.warning("ntfy URL 未配置，跳过推送")
            return False

        server_url, topic = self._resolve_ntfy_endpoint()
        if not server_url or not topic:
            logger.error("NTFY_URL 必须是包含 topic path 的完整 endpoint，例如 https://ntfy.sh/my-topic")
            return False

        if title is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
            title = f"📈 股票分析报告 - {date_str}"
        sanitized_content = strip_hidden_markdown_metadata(content).strip()

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "daily_stock_analysis",
        }
        token = (self._ntfy_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        chunks = _split_ntfy_content(sanitized_content)
        total = len(chunks)
        if total > 1:
            logger.info("ntfy 内容较长，将分 %s 段发送", total)
        all_ok = True
        for index, chunk in enumerate(chunks, 1):
            chunk_title = title if total == 1 else f"{title} ({index}/{total})"
            payload = {
                "topic": topic,
                "title": chunk_title,
                "message": chunk,
                "markdown": True,
            }
            try:
                response = requests.post(
                    server_url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_seconds or 10,
                    verify=self._webhook_verify_ssl,
                )
                if 200 <= response.status_code < 300:
                    logger.info("ntfy 消息发送成功 (%s/%s)", index, total)
                    continue
                logger.error("ntfy 请求失败: HTTP %s", response.status_code)
                logger.debug("ntfy 响应内容: %s", response.text)
                all_ok = False
            except requests.exceptions.Timeout:
                logger.error("发送 ntfy 消息失败: 请求超时")
                all_ok = False
            except requests.exceptions.RequestException as exc:
                logger.error("发送 ntfy 消息失败: 网络请求异常")
                logger.debug("ntfy 请求异常类型: %s", type(exc).__name__)
                all_ok = False
            except Exception as exc:
                logger.error("发送 ntfy 消息失败: 未知异常")
                logger.debug("ntfy 未知异常类型: %s", type(exc).__name__)
                all_ok = False
        return all_ok
