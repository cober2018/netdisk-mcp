"""夸克网盘。鉴权和路径与 quark-nd-mcp 相同；补上它没有的「用自己的文件创建分享」。"""

from __future__ import annotations

import time
from typing import Any

import httpx

from netdisk_mcp.limit import PACE
from netdisk_mcp.secrets import read_secret

API = "https://drive.quark.cn/1/clouddrive"
SHARE_API = "https://drive-pc.quark.cn/1/clouddrive"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Electron/18.3.5.4 Safari/537.36"
)


class QuarkError(RuntimeError):
    pass


class QuarkClient:
    def __init__(self, cookie: str = "") -> None:
        self.cookie = cookie or read_secret("QUARK_COOKIE", "QUARK_COOKIE_FILE")
        if not self.cookie:
            raise QuarkError("未配置 QUARK_COOKIE。把生产机上的夸克 Cookie 注入环境变量，不要写进仓库。")

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://pan.quark.cn",
            "User-Agent": UA,
            "Content-Type": "application/json",
        }

    def _call(self, base: str, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        query = {"pr": "ucpro", "fr": "pc"}
        if params:
            query.update(params)
        with httpx.Client(timeout=60.0) as client:
            response = client.request(method, base + path, params=query, json=body, headers=self._headers())
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuarkError(f"夸克返回的不是 JSON（HTTP {response.status_code}）") from exc
        code = payload.get("code", 0)
        if response.status_code >= 400 or code not in (0, None):
            message = payload.get("message") or payload.get("msg") or "未知错误"
            raise QuarkError(f"夸克接口失败: {message} (code={code})")
        return payload

    def list_files(self, parent_fid: str) -> list[dict]:
        files: list[dict] = []
        page = 1
        size = 100
        while True:
            payload = self._call(
                API,
                "GET",
                "/file/sort",
                params={
                    "pdir_fid": parent_fid,
                    "_page": page,
                    "_size": size,
                    "_fetch_total": 1,
                    "_sort": "file_type:asc,updated_at:desc",
                },
            )
            batch = ((payload.get("data") or {}).get("list")) or []
            files.extend(batch)
            total = (payload.get("metadata") or {}).get("_total", len(files))
            if page * size >= total or not batch:
                return files
            page += 1

    def create_folder(self, parent_fid: str, name: str) -> str:
        PACE.wait()
        payload = self._call(
            API,
            "POST",
            "/file",
            body={
                "dir_init_lock": False,
                "dir_path": "",
                "file_name": name,
                "pdir_fid": parent_fid,
            },
        )
        fid = ((payload.get("data") or {}).get("fid")) or ""
        if not fid:
            raise QuarkError(f"创建目录成功但没有返回 fid: {name}")
        return fid

    def resolve_path(self, path: str, create: bool = False) -> str:
        current = "0"
        built = ""
        for part in [p for p in path.strip("/").split("/") if p]:
            built += "/" + part
            children = self.list_files(current)
            existing = next(
                (item for item in children if item.get("file_name") == part and item.get("file") is False),
                None,
            )
            if existing:
                current = existing["fid"]
                continue
            if not create:
                raise QuarkError(f"目录不存在: {built}")
            current = self.create_folder(current, part)
        return current

    def ensure_path(self, path: str) -> str:
        return self.resolve_path(path, create=True)

    def inspect(self, pwd_id: str, password: str) -> dict:
        stoken = self._stoken(pwd_id, password)
        files, title = self._detail(pwd_id, stoken, "0")
        return {"title": title, "files": files, "stoken": stoken}

    def save_entries(self, pwd_id: str, stoken: str, files: list[dict], dest_fid: str) -> list[str]:
        if not files:
            raise QuarkError("分享是空的，没有可转存的文件")
        PACE.wait()
        payload = self._call(
            API,
            "POST",
            "/share/sharepage/save",
            body={
                "fid_list": [item["fid"] for item in files],
                "fid_token_list": [item.get("share_fid_token") or "" for item in files],
                "to_pdir_fid": dest_fid,
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "scene": "link",
            },
        )
        task_id = ((payload.get("data") or {}).get("task_id")) or ""
        if not task_id:
            raise QuarkError("转存没有返回 task_id")
        return self._wait(API, task_id).get("save_as_top_fids") or []

    def create_share(self, fid: str, title: str, url_type: int = 1) -> dict:
        """用自己网盘里的 fid 创建分享。expired_type=1 为永久，不提供短有效期。"""
        if not fid or fid == "0":
            raise QuarkError("拒绝分享根目录")
        PACE.wait()
        payload = self._call(
            SHARE_API,
            "POST",
            "/share",
            body={
                "fid_list": [fid],
                "title": title,
                "url_type": url_type,
                "expired_type": 1,
            },
        )
        data = payload.get("data") or {}
        share_id = data.get("share_id") or ""
        task_id = data.get("task_id") or ""
        if not share_id and task_id:
            share_id = self._wait(SHARE_API, task_id).get("share_id") or ""
        if not share_id:
            raise QuarkError("创建分享没有返回 share_id")
        info = self._call(SHARE_API, "POST", "/share/password", body={"share_id": share_id}).get("data") or {}
        share_url = info.get("share_url") or ""
        if not share_url:
            raise QuarkError("创建分享没有返回 share_url")
        return {
            "share_id": share_id,
            "share_url": share_url,
            "passcode": info.get("passcode") or info.get("pwd") or "",
            "pwd_id": info.get("pwd_id") or "",
            "expired": "permanent",
            "url_type": url_type,
        }

    def verify(self, pwd_id: str, password: str) -> dict:
        stoken = self._stoken(pwd_id, password)
        files, title = self._detail(pwd_id, stoken, "0")
        if not files:
            raise QuarkError("新分享可以打开，但里面没有文件，不能上架")
        return {"title": title, "count": len(files), "names": [item["name"] for item in files]}

    def _stoken(self, pwd_id: str, password: str) -> str:
        payload = self._call(
            API,
            "POST",
            "/share/sharepage/token",
            body={"pwd_id": pwd_id, "passcode": password or ""},
        )
        stoken = ((payload.get("data") or {}).get("stoken")) or ""
        if not stoken:
            raise QuarkError("没有拿到分享 stoken，提取码可能不对或链接已失效")
        return stoken

    def _detail(self, pwd_id: str, stoken: str, folder_id: str) -> tuple[list[dict], str]:
        files: list[dict] = []
        title = ""
        page = 1
        size = 50
        while True:
            payload = self._call(
                API,
                "GET",
                "/share/sharepage/detail",
                params={
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": folder_id,
                    "force": 0,
                    "_page": page,
                    "_size": size,
                    "_fetch_total": 1,
                    "_fetch_share": 1,
                    "_sort": "file_type:asc,updated_at:desc",
                    "ver": 2,
                },
            )
            data = payload.get("data") or {}
            if not title:
                share = data.get("share") or {}
                title = share.get("title") or share.get("share_title") or data.get("title") or ""
            for item in data.get("list") or []:
                files.append(
                    {
                        "fid": item.get("fid") or "",
                        "name": item.get("file_name") or "",
                        "is_folder": item.get("dir") is True or item.get("file") is False,
                        "share_fid_token": item.get("share_fid_token") or "",
                        "size": item.get("size") or 0,
                    }
                )
            total = (payload.get("metadata") or {}).get("_total", len(files))
            if page * size >= total or not data.get("list"):
                break
            page += 1
        if not title and files:
            title = files[0]["name"]
        return files, title

    def _wait(self, base: str, task_id: str) -> dict[str, Any]:
        for _ in range(60):
            payload = self._call(base, "GET", "/task", params={"task_id": task_id, "retry_index": 0})
            data = payload.get("data") or {}
            status = data.get("status")
            if status == 2:
                save_as = data.get("save_as") or {}
                return {
                    "share_id": data.get("share_id") or "",
                    "save_as_top_fids": save_as.get("save_as_top_fids") or [],
                }
            if status not in (0, 1, None):
                raise QuarkError(f"夸克任务失败，status={status}")
            time.sleep(1)
        raise QuarkError("夸克任务超时，没有等到转存或分享完成")
