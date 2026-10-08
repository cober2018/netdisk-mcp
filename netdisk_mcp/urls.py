"""分享链接识别。无法识别时直接失败，不猜测网盘类型。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


class LinkError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedLink:
    provider: str
    url: str
    # 夸克是 /s/ 后的 pwd_id；百度是去掉首位 1 之后、给 verify 用的 surl。
    key: str
    # 百度 /s/ 后的原始短码（含首位 1）。夸克与 key 相同。
    full_key: str
    password: str


_QUARK_HOSTS = {"pan.quark.cn", "pan.quark.com"}
_BAIDU_HOSTS = {"pan.baidu.com", "yun.baidu.com"}


def parse_share_url(raw: str) -> ParsedLink:
    text = (raw or "").strip()
    if not text:
        raise LinkError("分享链接为空")
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    password = _password(parsed.query)

    if host in _QUARK_HOSTS:
        match = re.search(r"/s/([0-9A-Za-z]+)", parsed.path)
        if not match:
            raise LinkError("夸克链接里没有 /s/<id>")
        key = match.group(1)
        return ParsedLink("quark", text, key, key, password)

    if host in _BAIDU_HOSTS:
        query = parse_qs(parsed.query)
        if not password:
            password = (query.get("pwd") or query.get("password") or [""])[0]
        full = ""
        match = re.search(r"/s/([0-9A-Za-z_-]+)", parsed.path)
        if match:
            full = match.group(1)
        elif query.get("surl"):
            full = query["surl"][0]
        if not full:
            raise LinkError("百度链接里没有 /s/<短码> 或 surl")
        # 开放平台 verify 的 surl 去掉开头的 1；list/transfer 失败时再回退完整短码。
        key = full[1:] if full.startswith("1") and len(full) > 1 else full
        return ParsedLink("baidu", text, key, full, password)

    raise LinkError(f"无法识别的网盘链接（{host or '无域名'}）。只接受 pan.quark.cn 和 pan.baidu.com")


def _password(query: str) -> str:
    values = parse_qs(query)
    for name in ("pwd", "password", "passcode"):
        if values.get(name) and values[name][0]:
            return values[name][0]
    return ""
