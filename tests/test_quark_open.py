import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from netdisk_mcp.limit import PACE
from netdisk_mcp.quark import SIGN_KEY, QuarkAuth, QuarkClient, QuarkError, load_quark_auth


def _sign(method: str, path: str, stamp: str) -> str:
    raw = f"{method.upper()}&{path}&{stamp}&{SIGN_KEY}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OpenApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list[httpx.Request] = []
        self.pace = PACE.wait
        PACE.wait = lambda: None

    def tearDown(self) -> None:
        PACE.wait = self.pace

    def _client(self, handler) -> QuarkClient:
        transport = httpx.MockTransport(handler)
        client = QuarkClient("access-token", "refresh-token", "device-1", transport=transport, clock=lambda: 1700000000000)
        client._poll_interval = 0
        return client

    def test_list_signs_request_and_marks_folder(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "data": {
                        "last_page": True,
                        "file_list": [
                            {"fid": "dir-1", "filename": "考试", "file_type": "0", "size": 0},
                            {"fid": "file-1", "filename": "a.pdf", "file_type": "1", "size": 3},
                        ],
                    },
                },
            )

        rows = self._client(handler).list_files("0")
        request = self.seen[0]
        self.assertEqual(request.headers["x-pan-token"], _sign("POST", "/open/v1/file/list", "1700000000000"))
        self.assertEqual(request.headers["x-pan-client-id"], "third_party_agent")
        self.assertNotIn("pr", parse_qs(urlparse(str(request.url)).query))
        body = json.loads(request.content)
        self.assertEqual(body["parent_fid"], "0")
        self.assertEqual(rows[0]["file_name"], "考试")
        self.assertTrue(rows[0]["is_folder"])
        self.assertFalse(rows[0]["file"])
        self.assertFalse(rows[1]["is_folder"])

    def test_save_targets_explicit_directory_and_waits(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            path = urlparse(str(request.url)).path
            if path.endswith("/share/saveas"):
                return httpx.Response(200, json={"status": 0, "data": {"task_id": "task-1"}})
            return httpx.Response(
                200,
                json={"status": 0, "data": {"status": 2, "save_as": {"save_as_top_fids": ["new-1"]}}},
            )

        saved = self._client(handler).save_entries("pwd", "stoken", [{"fid": "f1"}], "dest-fid")
        save = json.loads(self.seen[0].content)
        self.assertEqual(save["to_pdir_fid"], "dest-fid")
        self.assertTrue(save["save_all"])
        self.assertNotIn("fid_list", save)
        self.assertEqual(saved, ["new-1"])
        self.assertEqual(parse_qs(urlparse(str(self.seen[1].url)).query)["task_id"], ["task-1"])

    def test_create_share_is_permanent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return httpx.Response(
                200,
                json={"status": 0, "data": {"share_url": "https://pan.quark.cn/s/abc", "passcode": "xK9m", "pwd_id": "abc"}},
            )

        share = self._client(handler).create_share("fid-1", "标题", 2)
        body = json.loads(self.seen[0].content)
        self.assertEqual(body["expired_type"], 1)
        self.assertEqual(body["url_type"], 2)
        self.assertEqual(body["fid_list"], ["fid-1"])
        self.assertEqual(share["passcode"], "xK9m")
        self.assertEqual(share["expired"], "permanent")

    def test_inspect_uses_official_share_detail_without_cookie(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "token_info": {"stoken": "token-1", "title": "课程"},
                        "detail_info": {"list": [{"fid": "f1", "file_name": "a.mp4", "file": True, "size": 9, "share_fid_token": "t"}]},
                    },
                },
            )

        info = self._client(handler).inspect("pwd", "")
        request = self.seen[0]
        self.assertIn("/share/sharepage/v2/detail", str(request.url))
        self.assertNotIn("cookie", {key.lower() for key in request.headers})
        self.assertTrue(request.headers["authorization"].startswith("Bearer "))
        self.assertEqual(info["stoken"], "token-1")
        self.assertEqual(info["files"][0]["share_fid_token"], "t")

    def test_missing_auth_fails_closed(self):
        keys = (
            "QUARK_ACCESS_TOKEN",
            "QUARK_ACCESS_TOKEN_FILE",
            "QUARK_REFRESH_TOKEN",
            "QUARK_REFRESH_TOKEN_FILE",
            "QUARK_OPEN_TOKEN_FILE",
            "QUARK_DEVICE_ID",
        )
        previous = {key: os.environ.pop(key, None) for key in keys}
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                os.environ["QUARK_OPEN_TOKEN_FILE"] = str(root / "missing.json")
                auth = load_quark_auth(root / "official")
                self.assertEqual(auth.access_token, "")
                with self.assertRaises(QuarkError):
                    QuarkClient(official_dir=root / "official")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_token_file_precedes_stale_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.json"
            path.write_text(json.dumps({"access_token": "from-file", "refresh_token": "r", "device_id": "d"}), encoding="utf-8")
            os.environ["QUARK_OPEN_TOKEN_FILE"] = str(path)
            os.environ["QUARK_ACCESS_TOKEN"] = "from-env"
            try:
                auth = load_quark_auth()
            finally:
                os.environ.pop("QUARK_OPEN_TOKEN_FILE", None)
                os.environ.pop("QUARK_ACCESS_TOKEN", None)
        self.assertEqual(auth.access_token, "from-file")
        self.assertIsInstance(auth, QuarkAuth)

    def test_refresh_updates_token_file_and_official_config(self):
        calls = {"list": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = urlparse(str(request.url)).path
            if path.endswith("/access_token/rotate"):
                return httpx.Response(
                    200,
                    json={"status": 0, "data": {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}},
                )
            calls["list"] += 1
            if calls["list"] == 1:
                return httpx.Response(401, json={"status": -1, "errno": 401, "error_info": "未授权"})
            return httpx.Response(200, json={"status": 0, "data": {"last_page": True, "file_list": []}})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            official = root / "config.json"
            official.write_text(
                json.dumps({"deviceId": "device-1", "account": {"accessToken": "access-token", "refreshToken": "refresh-token"}}),
                encoding="utf-8",
            )
            token_path = root / "quark_open_token.json"
            os.environ["QUARK_OPEN_TOKEN_FILE"] = str(token_path)
            try:
                client = self._client(handler)
                client.auth.source = str(official)
                self.assertEqual(client.list_files("0"), [])
                saved = json.loads(token_path.read_text(encoding="utf-8"))
                updated = json.loads(official.read_text(encoding="utf-8"))
            finally:
                os.environ.pop("QUARK_OPEN_TOKEN_FILE", None)
        self.assertEqual(saved["access_token"], "new-access")
        self.assertEqual(updated["account"]["accessToken"], "new-access")
        self.assertEqual(updated["account"]["refreshToken"], "new-refresh")
        self.assertEqual(updated["deviceId"], "device-1")


if __name__ == "__main__":
    unittest.main()
