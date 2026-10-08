"""目录分配。规则来自生产机上的 JSON，不在对话里临时发明路径。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


class RuleError(ValueError):
    pass


def load_rules() -> dict:
    path = os.getenv("NETDISK_RULES_PATH", "").strip()
    if path:
        file = Path(path)
        if not file.is_file():
            raise RuleError(f"NETDISK_RULES_PATH 不存在: {path}")
        return _normalize(json.loads(file.read_text(encoding="utf-8")), file.parent)
    local = Path.cwd() / "directory_rules.json"
    if local.is_file():
        return _normalize(json.loads(local.read_text(encoding="utf-8")), local.parent)
    return _normalize({}, Path.cwd())


def _normalize(raw: dict, base: Path) -> dict:
    if not isinstance(raw, dict):
        raise RuleError("目录规则必须是 JSON 对象")
    rules = []
    for item in raw.get("rules") or []:
        if not isinstance(item, dict):
            continue
        folder = str(item.get("folder") or "").strip().strip("/")
        keywords = [str(k).strip() for k in item.get("keywords") or [] if str(k).strip()]
        if folder and keywords:
            rules.append({"folder": folder, "keywords": keywords})
    quark_root = os.getenv("NETDISK_QUARK_ROOT") or raw.get("quark_root") or "/闲鱼资源"
    baidu_root = os.getenv("NETDISK_BAIDU_ROOT") or raw.get("baidu_root") or "/闲鱼资源"
    fallback = os.getenv("NETDISK_FALLBACK_FOLDER") or raw.get("fallback") or "待分类"
    return {
        "quark_root": _abs(str(quark_root)),
        "baidu_root": _abs(str(baidu_root)),
        "fallback": str(fallback).strip().strip("/"),
        "rules": rules,
        "source": str(base),
    }


def _abs(path: str) -> str:
    path = path.strip() or "/闲鱼资源"
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def choose_category(title: str, filenames: list[str], rules: dict, override: str = "") -> str:
    forced = (override or "").strip().strip("/")
    if forced:
        if "/" in forced:
            raise RuleError("category 只能是一级目录名，不能带 /")
        return forced
    text = (title or "") + "\n" + "\n".join(filenames)
    folded = text.casefold()
    best = ""
    best_len = -1
    for rule in rules["rules"]:
        for keyword in rule["keywords"]:
            if keyword.casefold() in folded and len(keyword) > best_len:
                best = rule["folder"]
                best_len = len(keyword)
    return best or rules["fallback"]


_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_title(title: str) -> str:
    cleaned = _INVALID.sub(" ", title or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = "未命名资源"
    return cleaned[:80]


def build_dest(provider: str, rules: dict, category: str, title: str, dest_path: str = "") -> str:
    root = rules["quark_root"] if provider == "quark" else rules["baidu_root"]
    if dest_path:
        return assert_resource_dir(provider, rules, dest_path)
    return assert_resource_dir(provider, rules, f"{root}/{category}/{sanitize_title(title)}")


def assert_resource_dir(provider: str, rules: dict, path: str) -> str:
    """资源目录至少是「根目录/分类/资源名」。拒绝分享根目录或整个分类。"""
    root = rules["quark_root"] if provider == "quark" else rules["baidu_root"]
    explicit = _abs(path)
    if not explicit.startswith(root + "/"):
        raise RuleError(f"目录必须在 {root} 下面")
    rest = explicit[len(root):].strip("/")
    if "/" not in rest:
        raise RuleError("这是品类目录或根目录，不能整目录做成一条分享")
    return explicit
