"""Discord webhook。dry-run 或沒有設定 webhook 時只印出 payload。"""

from __future__ import annotations

import json
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

MAX_EMBEDS = 10
MAX_EMBED_CHARS = 6000
USERNAME = "flight-bot"


def embed_chars(embed: dict) -> int:
    n = len(embed.get("title", "")) + len(embed.get("description", ""))
    n += len(embed.get("footer", {}).get("text", ""))
    for f in embed.get("fields", []):
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


def batches(embeds: list[dict]) -> list[list[dict]]:
    """一則訊息最多 10 個 embed、總字數不超過 6000。"""
    out: list[list[dict]] = []
    cur: list[dict] = []
    chars = 0
    for e in embeds:
        n = embed_chars(e)
        if cur and (len(cur) >= MAX_EMBEDS or chars + n > MAX_EMBED_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur.append(e)
        chars += n
    if cur:
        out.append(cur)
    return out


class Discord:
    def __init__(self, webhook_url: str | None = None, dry_run: bool = False):
        self.url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL")
        self.dry_run = dry_run or not self.url
        self.sent: list[dict] = []  # dry-run 時留存，方便測試

    def send(self, content: str | None = None, embeds: list[dict] | None = None,
             files: dict[str, bytes] | None = None) -> None:
        embeds = embeds or []
        groups = batches(embeds) or [[]]
        for i, group in enumerate(groups):
            payload: dict = {"username": USERNAME, "embeds": group}
            if content and i == 0:
                payload["content"] = content
            self._post(payload, files if i == 0 else None)

    def _post(self, payload: dict, files: dict[str, bytes] | None) -> None:
        if self.dry_run:
            self.sent.append({"payload": payload, "files": sorted(files) if files else []})
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if files:
                print(f"（附件：{', '.join(sorted(files))}）")
            return

        for attempt in range(3):
            if files:
                multipart = {
                    f"files[{i}]": (name, data, "image/png") for i, (name, data) in enumerate(sorted(files.items()))
                }
                res = requests.post(self.url, data={"payload_json": json.dumps(payload)}, files=multipart, timeout=30)
            else:
                res = requests.post(self.url, json=payload, timeout=30)
            if res.status_code == 429:
                wait = float(res.json().get("retry_after", 2))
                log.warning("Discord rate limit，等待 %.1f 秒", wait)
                time.sleep(wait)
                continue
            res.raise_for_status()
            return
        raise RuntimeError("Discord webhook 一直被 rate limit")
