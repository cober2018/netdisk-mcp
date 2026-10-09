"""stdio MCP。给 OpenClaw 用，不在本机假设闲鱼服务地址。"""

from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import MCPServer

from netdisk_mcp.baidu import BaiduClient
from netdisk_mcp.pipeline import (
    create_own_share,
    download_share_files as save_downloaded_files,
    ingest_share,
    inspect_share,
    inspect_share_tree as read_share_tree,
    public_error,
    selective_ingest as ingest_selection,
    verify_share,
)
from netdisk_mcp.quark import QuarkClient, quark_auth_configured
from netdisk_mcp.secrets import read_secret

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

mcp = MCPServer("netdisk-ingest")


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def provider_status() -> str:
    """检查生产机是否配置了夸克开放平台授权和百度 access_token。不返回凭证内容。"""
    quark = False
    baidu = False
    errors = []
    try:
        quark = quark_auth_configured()
    except RuntimeError as exc:
        errors.append(str(exc))
    try:
        baidu = bool(read_secret("BAIDU_NETDISK_ACCESS_TOKEN", "BAIDU_NETDISK_ACCESS_TOKEN_FILE"))
    except RuntimeError as exc:
        errors.append(str(exc))
    return _dump({"ok": True, "quark_configured": quark, "baidu_configured": baidu, "errors": errors})


@mcp.tool()
def inspect_share_link(share_url: str) -> str:
    """只读。识别夸克或百度链接，列出标题和文件，并给出规则算出的目标目录。不转存、不创建分享。"""
    try:
        return _dump(inspect_share(share_url))
    except Exception as exc:  # noqa: BLE001 - 工具边界要把错误变成 JSON
        return _dump(public_error(exc))


@mcp.tool()
def ingest_share_link(share_url: str, category: str = "", dest_path: str = "", private: bool = False) -> str:
    """上架前的入库。转存到「根目录/分类/资源标题」这一个资源目录，再用该目录创建自己的永久分享。

    category 只在要覆盖规则时填写，且只能是一级目录名。
    dest_path 一般留空。填写时必须是这个资源自己的目录，不能是品类目录。
    private=true 时夸克创建私密分享，提取码由夸克生成；百度提取码来自环境变量 NETDISK_BAIDU_SHARE_PWD。
    返回的 share_url 才是自动发货内容。不要使用入参里的原始链接。
    """
    try:
        return _dump(ingest_share(share_url, category=category, dest_path=dest_path, private=private))
    except Exception as exc:  # noqa: BLE001
        return _dump(public_error(exc))


@mcp.tool()
def create_share_for_saved_folder(provider: str, path: str, title: str, private: bool = False) -> str:
    """只对已经转存好的单个资源目录补创建永久分享。path 至少是「根目录/分类/资源名」，不能是整个分类。"""
    try:
        return _dump(create_own_share(provider, path, title, private=private))
    except Exception as exc:  # noqa: BLE001
        return _dump(public_error(exc))


@mcp.tool()
def verify_share_link(share_url: str, password: str = "") -> str:
    """回读一条分享，确认里面真有文件。上架前如果 ingest 已经 verified=true，不必再调。"""
    try:
        return _dump(verify_share(share_url, password=password))
    except Exception as exc:  # noqa: BLE001
        return _dump(public_error(exc))


@mcp.tool()
def inspect_share_tree(share_url: str, password: str = "", max_depth: int = 4, max_entries: int = 2000) -> str:
    """只读。递归列出夸克分享的目录树，不转存、不创建分享。路径相对内容根，不含分享标题那一层。

    max_depth 或 max_entries 触顶时 truncated 为 true。
    """
    try:
        return _dump(read_share_tree(share_url, password=password, max_depth=max_depth, max_entries=max_entries))
    except Exception as exc:  # noqa: BLE001
        return _dump(public_error(exc))


@mcp.tool()
def selective_ingest(
    share_url: str,
    include_paths: list[str],
    category: str = "",
    dest_path: str = "",
    title: str = "",
    private: bool = False,
) -> str:
    """只转存夸克分享里指定的相对路径，再为这个资源目录创建自己的永久分享。

    include_paths 相对内容根，不是源 URL，也不含分享标题目录。空列表会拒绝，不会产出空分享。
    目标目录已有文件时拒绝混入。分享失败但转存已成功时，返回 dest_path，交给 create_share_for_saved_folder。
    """
    try:
        return _dump(
            ingest_selection(
                share_url,
                include_paths,
                category=category,
                dest_path=dest_path,
                title=title,
                private=private,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _dump(public_error(exc))


@mcp.tool()
def download_share_files(
    share_url_or_dest_path: str,
    include_paths: list[str] | None = None,
    local_dir: str = "",
    max_files: int = 50,
    max_total_mb: int = 200,
    make_zip: bool = False,
    overwrite: bool = False,
) -> str:
    """把夸克文件下载到 local_dir。传入自己的目录路径时直接下载；传入分享链接时只转存选中项到下载缓存，不创建分享。

    include_paths 为空表示该目录下全部。超过 max_files 或 max_total_mb 即停止，其余记入 skipped。
    建议先用 max_files=10 试跑。连续失败 3 次会中断。
    """
    try:
        return _dump(
            save_downloaded_files(
                share_url_or_dest_path,
                include_paths=include_paths,
                local_dir=local_dir,
                max_files=max_files,
                max_total_mb=max_total_mb,
                make_zip=make_zip,
                overwrite=overwrite,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _dump(public_error(exc))


@mcp.tool()
def list_directory(provider: str, path: str = "/") -> str:
    """列出自己网盘的目录。provider 为 quark 或 baidu。夸克 path 用 / 开头的路径，不是 fid。"""
    try:
        provider = provider.strip().lower()
        if provider == "quark":
            client = QuarkClient()
            fid = "0" if path in {"", "/"} else client.resolve_path(path, create=False)
            rows = [
                {"name": item.get("file_name") or "", "is_folder": item.get("file") is False, "id": item.get("fid") or ""}
                for item in client.list_files(fid)
            ]
        elif provider == "baidu":
            rows = [
                {
                    "name": item.get("server_filename") or "",
                    "is_folder": item.get("isdir") in (1, True),
                    "id": str(item.get("fs_id") or ""),
                }
                for item in BaiduClient().list_dir(path or "/")
            ]
        else:
            return _dump({"ok": False, "error": "provider 只能是 quark 或 baidu"})
        return _dump({"ok": True, "provider": provider, "path": path or "/", "entries": rows})
    except Exception as exc:  # noqa: BLE001
        return _dump(public_error(exc))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
