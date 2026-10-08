"""百度网盘开放平台。转存用 xpan share transfer，创建分享强制永久，不使用 7 天和默认提取码。"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import unquote

import httpx

from netdisk_mcp.limit import PACE
from netdisk_mcp.secrets import read_secret

BASE = "https://pan.baidu.com"


class BaiduError(RuntimeError):
    pass


class BaiduClient:
    def __init__(self, token: str = "") -> None:
        self.token = token or read_secret("BAIDU_NETDISK_ACCESS_TOKEN", "BAIDU_NETDISK_ACCESS_TOKEN_FILE")
        if not self.token:
            raise BaiduError("未配置 BAIDU_NETDISK_ACCESS_TOKEN。把生产机上的 access_token 注入环境变量。")
        self.share_pwd = os.getenv("NETDISK_BAIDU_SHARE_PWD", "").strip()
        if self.share_pwd and len(self.share_pwd) != 4:
            raise BaiduError("NETDISK_BAIDU_SHARE_PWD 必须是 4 位。留空表示公开分享。")

    def _request(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None, sekey: str = "") -> dict:
        query = dict(params or {})
        query["access_token"] = self.token
        headers = {
            "User-Agent": "pan.baidu.com",
            "Referer": "https://pan.baidu.com/",
        }
        if sekey:
            headers["Cookie"] = f"BDCLND={sekey}"
        with httpx.Client(timeout=60.0) as client:
            response = client.request(method, BASE + path, params=query, data=data, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise BaiduError(f"百度返回的不是 JSON（HTTP {response.status_code}）") from exc
        errno = payload.get("errno")
        if response.status_code >= 400 or (errno not in (0, None)):
            message = payload.get("errmsg") or payload.get("show_msg") or payload.get("error_msg") or "未知错误"
            raise BaiduError(f"百度接口失败: {message} (errno={errno})")
        return payload

    def list_dir(self, path: str) -> list[dict]:
        payload = self._request(
            "GET",
            "/rest/2.0/xpan/file",
            params={
                "method": "list",
                "dir": path or "/",
                "order": "time",
                "desc": 1,
                "start": 0,
                "limit": 200,
                "web": 1,
            },
        )
        return payload.get("list") or []

    def ensure_path(self, path: str) -> str:
        PACE.wait()
        payload = self._request(
            "POST",
            "/rest/2.0/xpan/file",
            params={"method": "create"},
            data={"path": path, "isdir": 1, "rtype": 1},
        )
        fs_id = payload.get("fs_id")
        if fs_id:
            return str(fs_id)
        name = path.rstrip("/").split("/")[-1]
        parent = path.rsplit("/", 1)[0] or "/"
        for item in self.list_dir(parent):
            if item.get("server_filename") == name and item.get("isdir") in (1, True):
                return str(item.get("fs_id"))
        raise BaiduError(f"目录已处理但没有拿到 fs_id: {path}")

    def inspect(self, surl: str, full_key: str, password: str) -> dict:
        sekey = self._verify(surl, full_key, password)
        return self._list_share(surl, full_key, sekey)

    def save_share(self, info: dict, dest_path: str) -> None:
        ids = [item["fs_id"] for item in info["files"]]
        if not ids:
            raise BaiduError("分享是空的，没有可转存的文件")
        PACE.wait()
        self._request(
            "POST",
            "/rest/2.0/xpan/share",
            params={"method": "transfer", "shareid": info["share_id"], "from": info["uk"]},
            data={
                "sekey": info["sekey"],
                "fsidlist": json.dumps(ids),
                "path": dest_path,
                "async": 1,
                "ondup": "newcopy",
            },
            sekey=info["sekey"],
        )
        self._wait_files(dest_path, len(ids))

    def create_share(self, fs_id: str, title: str) -> dict:
        if not fs_id:
            raise BaiduError("没有可分享的 fs_id")
        PACE.wait()
        form = {
            "fid_list": json.dumps([int(fs_id)]),
            "period": 0,
            "channel_list": "[]",
            "schannel": 4,
        }
        if self.share_pwd:
            form["pwd"] = self.share_pwd
        payload = self._request(
            "POST",
            "/rest/2.0/xpan/share",
            params={"method": "set"},
            data=form,
        )
        link = payload.get("link") or payload.get("shorturl") or ""
        if link and not link.startswith("http"):
            link = "https://pan.baidu.com/s/1" + link.lstrip("/")
        if not link:
            raise BaiduError("创建分享没有返回链接。开放平台应用可能没有分享权限。")
        period = payload.get("period")
        if period not in (0, "0", None):
            raise BaiduError(f"百度返回的有效期不是永久（period={period}），已拒绝把这条链接交给上架")
        return {
            "share_id": str(payload.get("shareid") or payload.get("share_id") or ""),
            "share_url": link,
            "passcode": payload.get("pwd") or self.share_pwd or "",
            "expired": "permanent",
            "title": title,
        }

    def verify(self, share_url_key: str, full_key: str, password: str) -> dict:
        sekey = self._verify(share_url_key, full_key, password)
        info = self._list_share(share_url_key, full_key, sekey)
        if not info["files"]:
            raise BaiduError("新分享可以打开，但里面没有文件，不能上架")
        return {"title": info["title"], "count": len(info["files"]), "names": [item["name"] for item in info["files"]]}

    def _verify(self, surl: str, full_key: str, password: str) -> str:
        last = "提取码校验失败"
        for candidate in _unique([surl, full_key]):
            try:
                payload = self._request(
                    "POST",
                    "/rest/2.0/xpan/share",
                    params={"method": "verify", "surl": candidate},
                    data={"pwd": password or ""},
                )
            except BaiduError as exc:
                last = str(exc)
                continue
            randsk = payload.get("randsk") or ""
            if randsk:
                return unquote(randsk)
        raise BaiduError(last)

    def _list_share(self, surl: str, full_key: str, sekey: str) -> dict:
        last = "读取分享列表失败"
        for candidate in _unique([surl, full_key]):
            try:
                payload = self._request(
                    "GET",
                    "/rest/2.0/xpan/share",
                    params={
                        "method": "list",
                        "shorturl": candidate,
                        "root": 1,
                        "page": 1,
                        "num": 100,
                        "web": 1,
                        "sekey": sekey,
                    },
                    sekey=sekey,
                )
            except BaiduError as exc:
                last = str(exc)
                continue
            files = []
            for item in payload.get("list") or []:
                files.append(
                    {
                        "fs_id": item.get("fs_id"),
                        "name": item.get("server_filename") or "",
                        "is_folder": item.get("isdir") in (1, True),
                        "size": item.get("size") or 0,
                    }
                )
            return {
                "title": payload.get("title") or (files[0]["name"] if files else ""),
                "share_id": str(payload.get("share_id") or payload.get("shareid") or ""),
                "uk": str(payload.get("uk") or ""),
                "sekey": sekey,
                "files": files,
            }
        raise BaiduError(last)

    def _wait_files(self, path: str, expected: int) -> None:
        for _ in range(30):
            try:
                items = self.list_dir(path)
            except BaiduError:
                items = []
            if len(items) >= expected:
                return
            time.sleep(1)
        raise BaiduError("转存超时，目标目录里还没有对应文件，没有创建分享")


def _unique(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
