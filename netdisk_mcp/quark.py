"""夸克网盘。列目录、转存、分享详情和创建分享走官方开放平台，目录仍由本服务指定。"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from netdisk_mcp.limit import PACE
from netdisk_mcp.secrets import read_secret

OPEN_API = "https://open-api-drive.quark.cn"
PAN_API = "https://drive-pc.quark.cn"
CLIENT_ID = "third_party_agent"
SIGN_KEY = "cf134812e2de4032bd1cb7c3727e84b3"
AGENT_ID = "netdisk-mcp"


class QuarkError(RuntimeError):
    pass


@dataclass
class QuarkAuth:
    access_token: str = ""
    refresh_token: str = ""
    device_id: str = ""
    expires_at: float = 0
    source: str = ""


def quark_auth_configured() -> bool:
    auth = load_quark_auth()
    return bool(auth.access_token or auth.refresh_token)


def load_quark_auth(official_dir: Path | None = None) -> QuarkAuth:
    """优先读本服务刷新后的令牌文件，其次环境变量，最后官方 CLI 的 config.json。"""
    token_path = _token_file()
    if token_path.is_file():
        loaded = _auth_from_mapping(_read_json(token_path), source=str(token_path))
        if loaded.access_token or loaded.refresh_token:
            return loaded
    access = _optional_secret("QUARK_ACCESS_TOKEN", "QUARK_ACCESS_TOKEN_FILE")
    refresh = _optional_secret("QUARK_REFRESH_TOKEN", "QUARK_REFRESH_TOKEN_FILE")
    device = os.getenv("QUARK_DEVICE_ID", "").strip()
    if access or refresh:
        return QuarkAuth(access, refresh, device, source="env")
    return _auth_from_official(official_dir or Path.home() / ".quarkclouddrive")


class QuarkClient:
    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        device_id: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
        official_dir: Path | None = None,
    ) -> None:
        if access_token or refresh_token:
            self.auth = QuarkAuth(access_token, refresh_token, device_id, source="explicit")
        else:
            self.auth = load_quark_auth(official_dir)
        if not self.auth.access_token and not self.auth.refresh_token:
            raise QuarkError(
                "未配置夸克开放平台授权。设置 QUARK_ACCESS_TOKEN 和 QUARK_REFRESH_TOKEN，"
                "或先用官方 CLI 登录。网页 Cookie 不再用于夸克接口。"
            )
        self._transport = transport
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._sleep = sleep or time.sleep
        self._poll_interval = 1.0
        self._poll_timeout = 15 * 60
        self._refreshed = False

    def list_files(self, parent_fid: str) -> list[dict]:
        files: list[dict] = []
        cursor: Any = None
        seen: set[str] = set()
        while True:
            body: dict[str, Any] = {
                "parent_fid": parent_fid or "0",
                "sort": "updated_at:desc",
                "size": 100,
            }
            if cursor is not None:
                body["query_cursor"] = cursor
            data = self._open("POST", "/open/v1/file/list", body=body)
            batch = data.get("file_list") or []
            if not isinstance(batch, list):
                raise QuarkError("夸克文件列表格式异常")
            files.extend(_normalize_file(item) for item in batch)
            if data.get("last_page", True):
                return files
            cursor = data.get("next_query_cursor")
            marker = json.dumps(cursor, ensure_ascii=False, sort_keys=True)
            if cursor in (None, "") or marker in seen:
                raise QuarkError("夸克文件列表分页没有返回可用的下一页游标")
            seen.add(marker)

    def create_folder(self, parent_fid: str, name: str) -> str:
        PACE.wait()
        data = self._open(
            "POST",
            "/open/v1/dir",
            body={"dir_path": name, "pdir_fid": parent_fid or "0"},
        )
        fid = data.get("fid") or ""
        if not fid:
            raise QuarkError(f"创建目录成功但没有返回 fid: {name}")
        return fid

    def resolve_path(self, path: str, create: bool = False) -> str:
        current = "0"
        built = ""
        for part in [item for item in path.strip("/").split("/") if item]:
            built += "/" + part
            if create:
                current = self.create_folder(current, part)
                continue
            existing = next(
                (item for item in self.list_files(current) if item["file_name"] == part and item["is_folder"]),
                None,
            )
            if not existing:
                raise QuarkError(f"目录不存在: {built}")
            current = existing["fid"]
        return current

    def ensure_path(self, path: str) -> str:
        return self.resolve_path(path, create=True)

    def inspect(self, pwd_id: str, password: str) -> dict:
        stoken, files, title = self._share_files(pwd_id, password)
        return {"title": title, "files": files, "stoken": stoken}

    def save_entries(self, pwd_id: str, stoken: str, files: list[dict], dest_fid: str) -> list[str]:
        """整份分享转存到调用方指定的目录。目录来自规则，不是官方默认位置。"""
        if not files:
            raise QuarkError("分享是空的，没有可转存的文件")
        if not stoken:
            raise QuarkError("没有分享 stoken，无法转存")
        if not dest_fid:
            raise QuarkError("转存必须指定目标目录")
        PACE.wait()
        data = self._open(
            "POST",
            "/open/v1/share/saveas",
            body={
                "pwd_id": pwd_id,
                "stoken": stoken,
                "to_pdir_fid": dest_fid,
                "save_all": True,
            },
        )
        task_id = data.get("task_id") or ""
        if not task_id:
            raise QuarkError("转存没有返回 task_id")
        finished = self._wait_task(task_id)
        save_as = finished.get("save_as") or {}
        return list(save_as.get("save_as_top_fids") or finished.get("save_as_top_fids") or [])

    def create_share(self, fid: str, title: str, url_type: int = 1) -> dict:
        """用自己网盘里的 fid 创建分享。expired_type 固定为 1（永久）。"""
        if not fid or fid == "0":
            raise QuarkError("拒绝分享根目录")
        PACE.wait()
        data = self._open(
            "POST",
            "/agent/v1/share/create",
            body={
                "fid_list": [fid],
                "title": title,
                "url_type": url_type,
                "expired_type": 1,
            },
        )
        if not data.get("share_url") and data.get("task_id"):
            data = {**data, **self._wait_task(data["task_id"])}
        share_url = data.get("share_url") or ""
        if not share_url:
            raise QuarkError("创建分享没有返回 share_url")
        return {
            "share_id": data.get("share_id") or "",
            "share_url": share_url,
            "passcode": data.get("passcode") or data.get("pwd") or "",
            "pwd_id": data.get("pwd_id") or "",
            "expired": "permanent",
            "url_type": url_type,
        }

    def verify(self, pwd_id: str, password: str) -> dict:
        _stoken, files, title = self._share_files(pwd_id, password)
        if not files:
            raise QuarkError("新分享可以打开，但里面没有文件，不能上架")
        return {"title": title, "count": len(files), "names": [item["name"] for item in files]}

    def _share_files(self, pwd_id: str, password: str) -> tuple[str, list[dict], str]:
        data = self._pan(
            "POST",
            "/1/clouddrive/share/sharepage/v2/detail",
            body={"pwd_id": pwd_id, "passcode": password or "", "page": 1, "size": 100, "force": 0},
        )
        token_info = data.get("token_info") or {}
        stoken = token_info.get("stoken") or ""
        if not stoken:
            raise QuarkError("没有拿到分享 stoken，提取码可能不对或链接已失效")
        detail = data.get("detail_info") or {}
        files = [_normalize_file(item) for item in detail.get("list") or data.get("list") or []]
        title = token_info.get("title") or (data.get("share") or {}).get("title") or ""
        seen = {item["fid"] for item in files if item["fid"]}
        page = 1
        while len(files) >= page * 100:
            page += 1
            extra = self._pan(
                "GET",
                "/1/clouddrive/share/sharepage/detail",
                query={
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": "0",
                    "_page": page,
                    "_size": 100,
                    "_fetch_total": 1,
                    "_fetch_share": 1,
                },
            )
            batch = extra.get("list") or (extra.get("detail_info") or {}).get("list") or []
            added = []
            for item in batch:
                normalized = _normalize_file(item)
                if normalized["fid"] and normalized["fid"] in seen:
                    continue
                if normalized["fid"]:
                    seen.add(normalized["fid"])
                added.append(normalized)
            if not added:
                break
            files.extend(added)
        if not title and files:
            title = files[0]["name"]
        return stoken, files, title

    def _wait_task(self, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._poll_timeout
        while True:
            data = self._open("GET", "/open/v1/file/query_task", query={"task_id": task_id})
            status = data.get("status")
            if status == 2:
                return data
            if status not in (0, 1, None):
                raise QuarkError(f"夸克任务失败，status={status}")
            if time.monotonic() >= deadline:
                raise QuarkError("夸克任务超时，没有等到转存或分享完成")
            self._sleep(self._poll_interval)

    def _open(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> dict:
        self._ensure_fresh()
        payload, http_status = self._signed(method, path, query=query, body=body, with_access_token=True)
        if _auth_failed(payload, http_status) and self.auth.refresh_token and not self._refreshed:
            self._refresh()
            payload, http_status = self._signed(method, path, query=query, body=body, with_access_token=True)
        return _open_data(payload, http_status)

    def _pan(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> dict:
        def send() -> tuple[dict, int]:
            params = self._common_query(query)
            params.update({"pr": "ucpro", "fr": "third"})
            return self._send(method, PAN_API + path, query=params, body=body, sign_path="")

        self._ensure_fresh()
        payload, http_status = send()
        if _auth_failed(payload, http_status) and self.auth.refresh_token and not self._refreshed:
            self._refresh()
            payload, http_status = send()
        return _pan_data(payload, http_status)

    def _signed(
        self,
        method: str,
        path: str,
        *,
        query: dict | None,
        body: dict | None,
        with_access_token: bool,
    ) -> tuple[dict, int]:
        extra = self._common_query(query) if with_access_token else {"req_id": str(uuid.uuid4())}
        if not with_access_token:
            extra.pop("access_token", None)
        return self._send(method, OPEN_API + path, query=extra, body=body, sign_path=path)

    def _send(
        self,
        method: str,
        url: str,
        *,
        query: dict,
        body: dict | None,
        sign_path: str,
    ) -> tuple[dict, int]:
        headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Agent-ID": AGENT_ID}
        if self.auth.access_token:
            headers["Authorization"] = f"Bearer {self.auth.access_token}"
        if sign_path:
            stamp = str(self._clock())
            headers.update(
                {
                    "x-pan-client-id": CLIENT_ID,
                    "x-pan-tm": stamp,
                    "x-pan-token": _sign(method, sign_path, stamp),
                }
            )
        with httpx.Client(timeout=60.0, transport=self._transport) as client:
            response = client.request(method, url, params=query, json=body, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuarkError(f"夸克返回的不是 JSON（HTTP {response.status_code}）") from exc
        if not isinstance(payload, dict):
            raise QuarkError("夸克返回的不是 JSON 对象")
        return payload, response.status_code

    def _common_query(self, extra: dict | None) -> dict:
        incoming = dict(extra or {})
        incoming.pop("access_token", None)
        incoming.pop("req_id", None)
        query: dict[str, Any] = {"req_id": str(uuid.uuid4()), "access_token": self.auth.access_token}
        if self.auth.device_id:
            query["device_id"] = self.auth.device_id
        query.update(incoming)
        return query

    def _ensure_fresh(self) -> None:
        if self.auth.expires_at and time.time() >= self.auth.expires_at - 60:
            self._refresh()
        elif not self.auth.access_token:
            self._refresh()

    def _refresh(self) -> None:
        if self._refreshed:
            raise QuarkError("夸克授权已失效，请重新登录官方开放平台")
        if not self.auth.refresh_token or not self.auth.device_id:
            raise QuarkError("夸克 access_token 已失效，且没有可刷新的 refresh_token 和 device_id")
        self._refreshed = True
        payload, http_status = self._signed(
            "POST",
            "/agent/v1/oauth/access_token/rotate",
            query=None,
            body={"refresh_token": self.auth.refresh_token, "device_id": self.auth.device_id},
            with_access_token=False,
        )
        data = _open_data(payload, http_status)
        access = data.get("access_token") or ""
        refresh = data.get("refresh_token") or ""
        if not access or not refresh:
            raise QuarkError("刷新夸克授权没有返回新的 token")
        expires_in = data.get("expires_in") or 0
        previous_access = self.auth.access_token
        previous_refresh = self.auth.refresh_token
        self.auth.access_token = access
        self.auth.refresh_token = refresh
        self.auth.expires_at = time.time() + float(expires_in) if expires_in else 0
        token_path = _token_file()
        _write_token_file(
            token_path,
            {
                "access_token": access,
                "refresh_token": refresh,
                "device_id": self.auth.device_id,
                "expires_at": self.auth.expires_at,
            },
        )
        _sync_source_tokens(self.auth.source, token_path, previous_access, previous_refresh, access, refresh)


def _sign(method: str, path: str, stamp: str) -> str:
    raw = f"{method.upper()}&{path}&{stamp}&{SIGN_KEY}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _auth_failed(payload: dict, http_status: int) -> bool:
    if http_status == 401:
        return True
    text = f"{payload.get('error_info') or ''}{payload.get('agent_msg') or ''}{payload.get('message') or ''}{payload.get('msg') or ''}"
    return any(word in text for word in ("未授权", "未完成授权", "登录授权", "access_token"))


def _open_data(payload: dict, http_status: int) -> dict:
    status = payload.get("status", 0)
    if http_status >= 400 or status not in (0, None):
        message = payload.get("agent_msg") or payload.get("error_info") or payload.get("message") or "未知错误"
        errno = payload.get("errno", status)
        raise QuarkError(f"夸克接口失败: {message} (code={errno})")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise QuarkError("夸克接口没有返回 data")
    return data


def _pan_data(payload: dict, http_status: int) -> dict:
    if payload.get("status") == 0 and "code" not in payload:
        data = payload.get("data") or {}
        return data if isinstance(data, dict) else {}
    code = payload.get("code", 0)
    if http_status >= 400 or code not in (0, None):
        message = payload.get("message") or payload.get("msg") or "未知错误"
        raise QuarkError(f"夸克分享接口失败: {message} (code={code})")
    data = payload.get("data") or {}
    return data if isinstance(data, dict) else {}


def _normalize_file(item: dict) -> dict:
    name = item.get("file_name") or item.get("filename") or ""
    file_type = item.get("file_type")
    is_folder = item.get("dir") is True or item.get("file") is False or str(file_type) == "0"
    return {
        "fid": item.get("fid") or "",
        "file_name": name,
        "name": name,
        "file": not is_folder,
        "is_folder": is_folder,
        "share_fid_token": item.get("share_fid_token") or "",
        "size": item.get("size") or 0,
    }


def _token_file() -> Path:
    configured = os.getenv("QUARK_OPEN_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path(".secrets") / "quark_open_token.json"


def _optional_secret(env_name: str, file_env_name: str) -> str:
    if not os.getenv(env_name, "").strip() and not os.getenv(file_env_name, "").strip():
        return ""
    return read_secret(env_name, file_env_name)


def _auth_from_official(root: Path) -> QuarkAuth:
    if not root.is_dir():
        return QuarkAuth()
    paths = [root / "config.json"]
    paths.extend(path / "config.json" for path in root.iterdir() if path.is_dir())
    for path in paths:
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        data = _read_json(path)
        device = ""
        if isinstance(data, dict):
            device = str(data.get("deviceId") or data.get("device_id") or "")
        found = _walk_tokens(data)
        if not found:
            continue
        found.sort(key=lambda item: (bool(item.refresh_token), item.expires_at), reverse=True)
        chosen = found[0]
        if not chosen.device_id:
            chosen.device_id = device
        chosen.source = str(path)
        return chosen
    return QuarkAuth()


def _walk_tokens(node: Any) -> list[QuarkAuth]:
    found: list[QuarkAuth] = []
    if isinstance(node, dict):
        access = node.get("accessToken") or node.get("access_token") or ""
        refresh = node.get("refreshToken") or node.get("refresh_token") or ""
        if isinstance(access, str) and isinstance(refresh, str) and (access or refresh):
            device = node.get("deviceId") or node.get("device_id") or ""
            expires = node.get("expires_at") or node.get("accessTokenExpiredAt") or 0
            found.append(QuarkAuth(access, refresh, str(device or ""), _as_float(expires)))
        for value in node.values():
            found.extend(_walk_tokens(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_tokens(value))
    return found


def _auth_from_mapping(data: Any, source: str) -> QuarkAuth:
    if not isinstance(data, dict):
        return QuarkAuth()
    found = _walk_tokens(data)
    if not found:
        return QuarkAuth()
    chosen = found[0]
    device = data.get("device_id") or data.get("deviceId") or chosen.device_id
    chosen.device_id = str(device or "")
    chosen.source = source
    return chosen


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sync_source_tokens(source: str, token_path: Path, old_access: str, old_refresh: str, access: str, refresh: str) -> None:
    """官方 CLI 和本服务共用一次刷新就会作废旧 refresh_token，所以原配置里的令牌也要换掉。"""
    if not source or source in {"env", "explicit"}:
        return
    path = Path(source)
    try:
        if not path.is_file() or path.resolve() == token_path.resolve():
            return
        data = _read_json(path)
    except OSError:
        return
    if not isinstance(data, dict):
        return
    if not _replace_known_tokens(data, old_access, old_refresh, access, refresh):
        return
    _write_token_file(path, data)


def _replace_known_tokens(node: Any, old_access: str, old_refresh: str, access: str, refresh: str) -> bool:
    changed = False
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in {"accessToken", "access_token"} and value == old_access and access:
                node[key] = access
                changed = True
            elif key in {"refreshToken", "refresh_token"} and value == old_refresh and refresh:
                node[key] = refresh
                changed = True
            elif _replace_known_tokens(value, old_access, old_refresh, access, refresh):
                changed = True
    elif isinstance(node, list):
        for value in node:
            if _replace_known_tokens(value, old_access, old_refresh, access, refresh):
                changed = True
    return changed


def _write_token_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0
