"""入库主流程：识别链接 → 按规则建资源目录 → 转存 → 用新文件创建永久分享 → 回读确认。"""

from __future__ import annotations

from netdisk_mcp.baidu import BaiduClient, BaiduError
from netdisk_mcp.quark import QuarkClient, QuarkError
from netdisk_mcp.rules import assert_resource_dir, build_dest, choose_category, load_rules, sanitize_title
from netdisk_mcp.urls import parse_share_url


class IngestError(RuntimeError):
    pass


def inspect_share(share_url: str) -> dict:
    link = parse_share_url(share_url)
    if link.provider == "quark":
        info = QuarkClient().inspect(link.key, link.password)
        files = info["files"]
        title = info["title"]
    else:
        info = BaiduClient().inspect(link.key, link.full_key, link.password)
        files = info["files"]
        title = info["title"]
    rules = load_rules()
    names = [item["name"] for item in files]
    category = choose_category(title, names, rules)
    dest = build_dest(link.provider, rules, category, title)
    return {
        "ok": True,
        "provider": link.provider,
        "title": title,
        "file_count": len(files),
        "files": [{"name": item["name"], "is_folder": item["is_folder"], "size": item["size"]} for item in files],
        "suggested_category": category,
        "suggested_dest": dest,
        "has_password": bool(link.password),
    }


def ingest_share(share_url: str, category: str = "", dest_path: str = "", private: bool = False) -> dict:
    link = parse_share_url(share_url)
    rules = load_rules()
    if link.provider == "quark":
        return _ingest_quark(link, rules, category, dest_path, 2 if private else 1)
    return _ingest_baidu(link, rules, category, dest_path)


def create_own_share(provider: str, path: str, title: str, private: bool = False) -> dict:
    """转存已经成功、但创建分享失败时使用。只分享这一个资源目录。"""
    provider = provider.strip().lower()
    if provider not in {"quark", "baidu"}:
        raise IngestError("provider 只能是 quark 或 baidu")
    rules = load_rules()
    dest = assert_resource_dir(provider, rules, path)
    safe_title = sanitize_title(title)
    if provider == "quark":
        client = QuarkClient()
        fid = client.resolve_path(dest, create=False)
        if not client.list_files(fid):
            raise IngestError(f"目录是空的，不能分享: {dest}")
        share = client.create_share(fid, safe_title, 2 if private else 1)
        checked = client.verify(_pwd_id(share), share["passcode"])
        return _result("quark", "", safe_title, dest.split("/")[-2], dest, fid, [], [], share, checked)
    client = BaiduClient()
    if not client.list_dir(dest):
        raise IngestError(f"目录是空的，不能分享: {dest}")
    folder_id = client.ensure_path(dest)
    share = client.create_share(folder_id, safe_title)
    from netdisk_mcp.urls import parse_share_url

    created = parse_share_url(share["share_url"])
    checked = client.verify(created.key, created.full_key, share["passcode"])
    return _result("baidu", "", safe_title, dest.split("/")[-2], dest, folder_id, [], [], share, checked)


def verify_share(share_url: str, password: str = "") -> dict:
    link = parse_share_url(share_url)
    password = password or link.password
    if link.provider == "quark":
        checked = QuarkClient().verify(link.key, password)
    else:
        checked = BaiduClient().verify(link.key, link.full_key, password)
    return {"ok": True, "provider": link.provider, "verified": True, **checked}


def _ingest_quark(link, rules: dict, category: str, dest_path: str, url_type: int) -> dict:
    client = QuarkClient()
    info = client.inspect(link.key, link.password)
    if not info["files"]:
        raise IngestError("源分享没有文件")
    names = [item["name"] for item in info["files"]]
    chosen = choose_category(info["title"], names, rules, category)
    dest = build_dest("quark", rules, chosen, info["title"], dest_path)
    dest_fid = client.ensure_path(dest)
    if client.list_files(dest_fid):
        raise IngestError(f"资源目录已存在且非空，拒绝混入后整目录分享: {dest}")
    saved_ids = client.save_entries(link.key, info["stoken"], info["files"], dest_fid)
    share = client.create_share(dest_fid, sanitize_title(info["title"]), url_type)
    checked = client.verify(_pwd_id(share), share["passcode"])
    return _result("quark", link.url, info["title"], chosen, dest, dest_fid, saved_ids, names, share, checked)


def _ingest_baidu(link, rules: dict, category: str, dest_path: str) -> dict:
    client = BaiduClient()
    info = client.inspect(link.key, link.full_key, link.password)
    if not info["files"]:
        raise IngestError("源分享没有文件")
    if not info.get("share_id") or not info.get("uk"):
        raise IngestError("百度分享列表没有 share_id 或 uk。开放平台应用需要开通分享转存权限。")
    names = [item["name"] for item in info["files"]]
    chosen = choose_category(info["title"], names, rules, category)
    dest = build_dest("baidu", rules, chosen, info["title"], dest_path)
    folder_id = client.ensure_path(dest)
    if client.list_dir(dest):
        raise IngestError(f"资源目录已存在且非空，拒绝混入后整目录分享: {dest}")
    client.save_share(info, dest)
    share = client.create_share(folder_id, sanitize_title(info["title"]))
    from netdisk_mcp.urls import parse_share_url

    created = parse_share_url(share["share_url"])
    checked = client.verify(created.key, created.full_key, share["passcode"])
    return _result("baidu", link.url, info["title"], chosen, dest, folder_id, [], names, share, checked)


def _pwd_id(share: dict) -> str:
    if share.get("pwd_id"):
        return share["pwd_id"]
    from netdisk_mcp.urls import parse_share_url

    return parse_share_url(share["share_url"]).key


def _result(provider, source_url, title, category, dest, folder_id, saved_ids, names, share, checked) -> dict:
    return {
        "ok": True,
        "provider": provider,
        "source_url": source_url,
        "title": sanitize_title(title),
        "category": category,
        "dest_path": dest,
        "shared_folder_id": str(folder_id),
        "saved_file_ids": [str(item) for item in saved_ids],
        "file_names": names,
        "share_url": share["share_url"],
        "passcode": share.get("passcode") or "",
        "expired": "permanent",
        "verified": True,
        "verified_count": checked["count"],
        "do_not_publish_source_url": True,
        "next": "只把 share_url 和 passcode 交给封面与闲鱼自动发货。source_url 是别人的链接，不能上架。",
    }


def public_error(exc: Exception) -> dict:
    if isinstance(exc, (IngestError, QuarkError, BaiduError, ValueError, RuntimeError)):
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "网盘操作失败"}
