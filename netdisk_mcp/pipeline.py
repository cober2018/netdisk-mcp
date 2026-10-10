"""入库主流程：识别链接 → 按规则建资源目录 → 转存 → 用新文件创建永久分享 → 回读确认。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from netdisk_mcp.baidu import BaiduClient, BaiduError
from netdisk_mcp.limit import PACE
from netdisk_mcp.quark import QuarkClient, QuarkError
from netdisk_mcp.rules import assert_resource_dir, build_dest, choose_category, load_rules, sanitize_title
from netdisk_mcp.urls import parse_share_url

DOWNLOAD_FAILURE_LIMIT = 3
CLI_TIMEOUT_SECONDS = 1800


class IngestError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


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


def inspect_share_tree(share_url: str, password: str = "", max_depth: int = 4, max_entries: int = 2000) -> dict:
    link = parse_share_url(share_url)
    if link.provider != "quark":
        raise IngestError("目录树这次只支持夸克分享")
    info = QuarkClient().walk_share_tree(link.key, password or link.password, int(max_depth), int(max_entries))
    return {"ok": True, "provider": "quark", **info}


def selective_ingest(
    share_url: str,
    include_paths: list[str] | str | None,
    category: str = "",
    dest_path: str = "",
    title: str = "",
    private: bool = False,
) -> dict:
    paths = _path_list(include_paths)
    if not paths:
        raise IngestError("include_paths 不能为空，拒绝创建空分享")
    link = parse_share_url(share_url)
    if link.provider != "quark":
        raise IngestError("选择性转存这次只支持夸克")
    rules = load_rules()
    if dest_path:
        assert_resource_dir("quark", rules, dest_path)
    client = QuarkClient()
    resolved = client.resolve_share_paths(link.key, link.password, paths)
    if not resolved["found"]:
        missing = "、".join(resolved["missing"]) or "（空）"
        raise IngestError(f"include_paths 没有匹配到分享里的文件，没有转存: {missing}")
    names = [item["name"] for item in resolved["found"]]
    share_title = sanitize_title(title or resolved["title"])
    chosen = choose_category(share_title, names, rules, category)
    dest = build_dest("quark", rules, chosen, share_title, dest_path)
    dest_fid = client.ensure_path(dest)
    if client.list_files(dest_fid):
        raise IngestError(f"资源目录已存在且非空，拒绝混入后整目录分享: {dest}")
    try:
        client.save_selected(link.key, resolved["stoken"], resolved["found"], dest_fid)
    except QuarkError as exc:
        return {
            "ok": False,
            "saved": "unknown",
            "error": str(exc),
            "provider": "quark",
            "source_url": link.url,
            "title": share_title,
            "category": chosen,
            "dest_path": dest,
            "shared_folder_id": str(dest_fid),
            "selected_count": len(resolved["found"]),
            "skipped_missing": resolved["missing"],
            "next": (
                "转存状态未知。先用 list_directory 查看 dest_path：非空说明转存已落地，"
                "调用 create_share_for_saved_folder 补永久分享；为空则处理掉空目录后重试 selective_ingest。"
            ),
        }
    try:
        share = client.create_share(dest_fid, share_title, 2 if private else 1)
        checked = client.verify(_pwd_id(share), share["passcode"])
    except QuarkError as exc:
        return {
            "ok": False,
            "saved": True,
            "error": str(exc),
            "provider": "quark",
            "source_url": link.url,
            "title": share_title,
            "category": chosen,
            "dest_path": dest,
            "shared_folder_id": str(dest_fid),
            "selected_count": len(resolved["found"]),
            "skipped_missing": resolved["missing"],
            "next": "转存已完成。调用 create_share_for_saved_folder，provider 用 quark，path 用 dest_path。不要分享分类目录，也不要上架 source_url。",
        }
    return {
        "ok": True,
        "provider": "quark",
        "source_url": link.url,
        "title": share_title,
        "category": chosen,
        "dest_path": dest,
        "shared_folder_id": str(dest_fid),
        "selected_count": len(resolved["found"]),
        "selected_bytes": sum(int(item.get("size") or 0) for item in resolved["found"]),
        "skipped_missing": resolved["missing"],
        "share_url": share["share_url"],
        "passcode": share.get("passcode") or "",
        "expired": "permanent",
        "verified": True,
        "verified_count": checked["count"],
        "do_not_publish_source_url": True,
        "next": "只把 share_url 和 passcode 交给封面与闲鱼自动发货。source_url 是别人的链接，不能上架。",
    }


def download_share_files(
    share_url_or_dest_path: str,
    include_paths: list[str] | str | None = None,
    local_dir: str = "",
    max_files: int = 50,
    max_total_mb: int = 200,
    make_zip: bool = False,
    overwrite: bool = False,
) -> dict:
    root = _prepare_local_dir(local_dir)
    paths = _path_list(include_paths)
    text = (share_url_or_dest_path or "").strip()
    if text.startswith("/"):
        return _download_own(text, paths, root, int(max_files), int(max_total_mb), make_zip, overwrite)
    link = parse_share_url(text)
    if link.provider != "quark":
        raise IngestError("下载这次只支持夸克")
    return _download_share(link, paths, root, int(max_files), int(max_total_mb), make_zip, overwrite)


def _download_own(
    path: str,
    include_paths: list[str],
    root: Path,
    max_files: int,
    max_total_mb: int,
    make_zip: bool,
    overwrite: bool,
) -> dict:
    client = QuarkClient()
    chosen, missing = _filter_files(client.collect_files(path), include_paths)
    if include_paths and not chosen:
        raise IngestError("include_paths 没有匹配到可下载的文件")
    return _download_files(client, root, chosen, missing, max_files, max_total_mb, make_zip, overwrite)


def _download_share(link, include_paths: list[str], root: Path, max_files: int, max_total_mb: int, make_zip: bool, overwrite: bool) -> dict:
    """官方没有分享直链。选中项先转存到下载缓存，再批量取自己网盘的下载地址。不创建分享。"""
    client = QuarkClient()
    paths = include_paths
    if not paths:
        info = client.walk_share_tree(link.key, link.password, max_depth=1, max_entries=5000)
        if info["truncated"]:
            raise IngestError("分享内容超过单次范围，请指定 include_paths，不要整包下载")
        paths = [node["path"] for node in info["tree"]]
        if not paths:
            raise IngestError("分享是空的，没有可下载的文件")
    resolved = client.resolve_share_paths(link.key, link.password, paths)
    if not resolved["found"]:
        raise IngestError("include_paths 没有匹配到分享里的文件，没有转存也没有下载")
    rules = load_rules()
    dest = build_dest("quark", rules, "下载缓存", sanitize_title(resolved["title"]))
    dest_fid = client.ensure_path(dest)
    if client.list_files(dest_fid):
        raise IngestError(
            f"下载缓存已存在且非空，拒绝混入: {dest}。若这些文件已经转存好，请把该路径传给 download_share_files 直接下载。"
        )
    client.save_selected(link.key, resolved["stoken"], resolved["found"], dest_fid)
    return _download_files(
        client,
        root,
        client.collect_files(dest),
        resolved["missing"],
        max_files,
        max_total_mb,
        make_zip,
        overwrite,
    )


def _download_files(
    client: QuarkClient,
    root: Path,
    files: list[dict],
    missing: list[str],
    max_files: int,
    max_total_mb: int,
    make_zip: bool,
    overwrite: bool,
) -> dict:
    skipped = [{"name": name, "reason": "not found"} for name in missing]
    pending = []
    for item in files:
        target = _safe_target(root, item["path"])
        if target.exists() and not overwrite:
            skipped.append({"name": item["name"], "reason": "already exists"})
            continue
        pending.append((item, target))
    chosen, quota_skipped, _planned = _apply_quota(pending, max_files, max_total_mb)
    skipped.extend(quota_skipped)
    rows = client.get_download_urls([item["fid"] for item, _target in chosen]) if chosen else []
    downloaded = 0
    total_bytes = 0
    errors: list[dict] = []
    written: list[Path] = []
    streak = 0
    for index, (item, target) in enumerate(chosen):
        row = rows[index] if index < len(rows) else {}
        url = str(row.get("download_url") or "")
        if not url:
            errors.append({"name": item["name"], "reason": str(row.get("error") or "没有下载地址")})
            streak += 1
        else:
            try:
                size = client.download_file(url, target)
            except QuarkError as exc:
                errors.append({"name": item["name"], "reason": str(exc)})
                streak += 1
            else:
                streak = 0
                downloaded += 1
                total_bytes += size
                written.append(target)
        if streak >= DOWNLOAD_FAILURE_LIMIT:
            errors.append({"name": "", "reason": "连续失败已达阈值，已中断"})
            break
    if make_zip and written:
        _write_zip(root, written, overwrite, skipped)
    return {
        "ok": True,
        "provider": "quark",
        "local_dir": str(root),
        "downloaded": downloaded,
        "skipped": skipped,
        "total_bytes": total_bytes,
        "errors": errors,
    }


def _apply_quota(pending: list[tuple[dict, Path]], max_files: int, max_total_mb: int) -> tuple[list, list[dict], int]:
    if max_files < 1 or max_total_mb <= 0:
        raise IngestError("max_files 必须大于 0，max_total_mb 必须大于 0")
    limit = int(max_total_mb * 1024 * 1024)
    chosen = []
    skipped = []
    total = 0
    reason = ""
    for item, target in pending:
        if reason:
            skipped.append({"name": item["name"], "reason": reason})
            continue
        size = int(item.get("size") or 0)
        if len(chosen) >= max_files:
            reason = "exceeds max_files"
            skipped.append({"name": item["name"], "reason": reason})
            continue
        if total + size > limit:
            reason = "exceeds max_total_mb"
            skipped.append({"name": item["name"], "reason": reason})
            continue
        chosen.append((item, target))
        total += size
    return chosen, skipped, total


def _filter_files(files: list[dict], include_paths: list[str]) -> tuple[list[dict], list[str]]:
    if not include_paths:
        return files, []
    chosen = []
    missing = []
    seen: set[str] = set()
    for raw in include_paths:
        path = str(raw or "").strip().strip("/")
        matched = [item for item in files if item["path"] == path or item["path"].startswith(path + "/")]
        if not matched:
            missing.append(path or str(raw))
            continue
        for item in matched:
            if item["path"] in seen:
                continue
            seen.add(item["path"])
            chosen.append(item)
    return chosen, missing


def _path_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise IngestError("include_paths 必须是字符串列表")
            return [str(item) for item in parsed]
        return [text]
    return [str(item) for item in value]


def _prepare_local_dir(local_dir: str) -> Path:
    if not str(local_dir or "").strip():
        raise IngestError("local_dir 必填")
    raw = Path(local_dir)
    if any(part == ".." for part in raw.parts):
        raise IngestError("local_dir 不能包含 ..")
    if not raw.is_absolute():
        raise IngestError("local_dir 必须是绝对路径")
    if raw.is_symlink():
        raise IngestError("local_dir 不能是符号链接")
    raw.mkdir(parents=True, exist_ok=True)
    if not raw.is_dir():
        raise IngestError("local_dir 不是目录")
    return raw


def _safe_target(root: Path, relative: str) -> Path:
    rel = Path(str(relative or "").replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise IngestError(f"文件路径不能跳出 local_dir: {relative}")
    current = root
    root_resolved = root.resolve()
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise IngestError("拒绝跟随符号链接")
        if current.exists():
            resolved = current.resolve()
            if resolved != root_resolved and root_resolved not in resolved.parents:
                raise IngestError("文件路径不能跳出 local_dir")
            if not current.is_dir():
                raise IngestError(f"路径中间不是目录: {relative}")
    target = root.joinpath(*rel.parts)
    if target.is_symlink():
        raise IngestError("拒绝跟随符号链接")
    return target


def _write_zip(root: Path, written: list[Path], overwrite: bool, skipped: list[dict]) -> None:
    zip_path = root / "bundle.zip"
    if zip_path.is_symlink():
        raise IngestError("拒绝跟随符号链接")
    if zip_path.exists() and not overwrite:
        skipped.append({"name": "bundle.zip", "reason": "already exists"})
        return
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in written:
            archive.write(path, arcname=path.relative_to(root).as_posix())


def upload_file_or_folder(
    local_path: str,
    dest_dir: str = "",
    category: str = "",
    title: str = "",
    create_share: bool = True,
    runner=None,
) -> dict:
    """把本地文件或文件夹传进夸克资源目录，需要时用官方 CLI，成功后可补永久公开分享。"""
    source = _validate_local_upload(local_path)
    rules = load_rules()
    share_title = sanitize_title(title or source.name)
    if dest_dir:
        dest = assert_resource_dir("quark", rules, dest_dir)
    else:
        chosen = choose_category(share_title, [source.name], rules, category)
        dest = build_dest("quark", rules, chosen, share_title)
    command_prefix = _resolve_cli_command()
    client = QuarkClient()
    dest_fid = client.ensure_path(dest)
    if client.list_files(dest_fid):
        raise IngestError(
            f"资源目录已存在且非空，拒绝混入: {dest}。一次上传对应一个空的资源目录，"
            "已有内容要用 create_share_for_saved_folder 单独处理。",
            "DEST_NOT_EMPTY",
        )
    outcome = _run_cli_upload(command_prefix, source, dest_fid, runner)
    if source.is_dir():
        fid = _find_uploaded_folder(client, dest_fid, source.name)
        if not fid:
            raise IngestError(f"上传完成但目标目录里没有找到文件夹「{source.name}」，没有创建分享", "UPLOAD_FOLDER_MISSING")
    else:
        fid = outcome["fids"][0] if outcome["fids"] else ""
        if not fid:
            raise IngestError("官方 CLI 上传结果里没有 fid，没有创建分享", "UPLOAD_FAILED")
    result = {
        "ok": True,
        "provider": "quark",
        "local_path": str(source),
        "dest_path": f"{dest}/{source.name}",
        "fid": fid,
        "file_name": source.name,
        "file_size": _local_size(source),
        "instant_upload": outcome["instant"],
        "share_created": False,
    }
    if not create_share:
        return result
    try:
        share = client.create_share(fid, share_title, 1)
        checked = client.verify(_pwd_id(share), share["passcode"])
    except QuarkError as exc:
        return {
            "ok": False,
            "uploaded": True,
            "error": str(exc),
            "provider": "quark",
            "local_path": str(source),
            "dest_path": result["dest_path"],
            "fid": fid,
            "title": share_title,
            "next": "上传已完成。调用 create_share_for_saved_folder，provider 用 quark，path 用 dest_path，不要分享分类目录。",
        }
    result.update(
        {
            "title": share_title,
            "share_created": True,
            "share_url": share["share_url"],
            "passcode": share.get("passcode") or "",
            "expired": "permanent",
            "verified": True,
            "verified_count": checked["count"],
            "next": "只把 share_url 和 passcode 交给封面与闲鱼自动发货。",
        }
    )
    return result


def _validate_local_upload(local_path: str) -> Path:
    raw = Path(str(local_path or "").strip())
    if not raw.is_absolute():
        raise IngestError("local_path 必须是绝对路径", "LOCAL_PATH_INVALID")
    if raw.is_symlink():
        raise IngestError("local_path 不能是符号链接", "LOCAL_PATH_INVALID")
    if not raw.exists():
        raise IngestError(f"本地文件不存在: {raw}", "FILE_NOT_FOUND")
    if raw.is_dir():
        has_file = False
        for entry in raw.rglob("*"):
            if entry.is_symlink():
                raise IngestError(f"文件夹里包含符号链接，拒绝上传: {entry}", "LOCAL_PATH_INVALID")
            if entry.is_file():
                has_file = True
        if not has_file:
            raise IngestError(f"本地文件夹是空的，没有可上传的内容: {raw}", "LOCAL_PATH_INVALID")
    return raw


def _local_size(source: Path) -> int:
    try:
        if source.is_file():
            return source.stat().st_size
        return sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
    except OSError:
        return 0


def _resolve_cli_command() -> list[str]:
    """官方安装脚本不再放全局命令，优先用 QUARK_CLI_SCRIPT 指到 quark-drive.cjs。"""
    configured = os.getenv("QUARK_CLI_SCRIPT", "").strip()
    if configured:
        script = Path(configured)
        if not script.is_file():
            raise IngestError(f"QUARK_CLI_SCRIPT 指向的文件不存在: {configured}", "CLI_NOT_FOUND")
        if script.suffix == ".cjs":
            node = shutil.which("node")
            if not node:
                raise IngestError("运行官方 CLI 需要 node，找不到 node 可执行文件", "CLI_NOT_FOUND")
            return [node, str(script)]
        return [str(script)]
    for name in ("quark-drive", "quarkclouddrive"):
        found = shutil.which(name)
        if found:
            return [found]
    raise IngestError(
        "找不到夸克官方 CLI。安装官方 Skills 后，把 QUARK_CLI_SCRIPT 指到其中的 scripts/quark-drive.cjs。",
        "CLI_NOT_FOUND",
    )


def _run_cli_upload(command_prefix: list[str], source: Path, dest_fid: str, runner) -> dict:
    """调用官方 CLI upload。上传目录时 CLI 会自己建同名文件夹，fids 只含文件。"""
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    timeout = _cli_timeout_seconds()
    command = [*command_prefix, "upload", str(source), "--parent-fid", dest_fid]
    PACE.wait()
    workdir = tempfile.mkdtemp(prefix="netdisk-upload-")
    try:
        if runner is not None:
            completed = runner(command, workdir, env, timeout)
        else:
            try:
                completed = subprocess.run(
                    command,
                    cwd=workdir,
                    env=env,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise IngestError(f"官方 CLI 上传超时（{timeout} 秒）", "UPLOAD_FAILED") from exc
            except OSError as exc:
                raise IngestError(f"官方 CLI 无法执行: {exc}", "CLI_NOT_FOUND") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    rows = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    result = next(
        (row for row in reversed(rows) if row.get("action") == "upload" and row.get("type") == "result"),
        None,
    )
    data = (result or {}).get("data") or {}
    fids = [str(item) for item in data.get("fids") or [] if item]
    failures = [row for row in rows if row.get("type") == "list" and row.get("code") not in (0, None)]
    if result is None or int(result.get("code") or 0) != 0 or not fids:
        detail = (failures[0].get("msg") if failures else "") or (getattr(completed, "stderr", "") or "").strip()[-300:]
        raise IngestError(f"官方 CLI 上传失败: {detail or '没有返回上传结果'}", "UPLOAD_FAILED")
    if failures:
        raise IngestError(
            f"官方 CLI 上传不完整，{len(failures)} 个文件失败，目标目录可能已有部分文件，没有创建分享: "
            f"{failures[0].get('msg') or '未知原因'}",
            "UPLOAD_FAILED",
        )
    return {"fids": fids, "instant": bool(data.get("instantUpload"))}


def _cli_timeout_seconds() -> int:
    raw = os.getenv("QUARK_CLI_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return CLI_TIMEOUT_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return CLI_TIMEOUT_SECONDS


def _find_uploaded_folder(client: QuarkClient, dest_fid: str, name: str) -> str:
    """列表按更新时间倒序，第一个同名文件夹就是刚上传的。"""
    for item in client.list_files(dest_fid):
        if item.get("is_folder") and item.get("name") == name:
            return str(item.get("fid") or "")
    return ""


def public_error(exc: Exception) -> dict:
    if isinstance(exc, (IngestError, QuarkError, BaiduError, ValueError, RuntimeError)):
        payload = {"ok": False, "error": str(exc)}
        code = getattr(exc, "code", "")
        if code:
            payload["code"] = code
        return payload
    return {"ok": False, "error": "网盘操作失败"}
