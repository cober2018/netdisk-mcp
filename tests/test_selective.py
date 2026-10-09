import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from netdisk_mcp.limit import PACE
from netdisk_mcp.pipeline import IngestError, download_share_files, selective_ingest
from netdisk_mcp.quark import QuarkClient, QuarkError
from netdisk_mcp.rules import RuleError


RULES = {
    "quark_root": "/闲鱼资源",
    "baidu_root": "/闲鱼资源",
    "fallback": "待分类",
    "rules": [{"folder": "技能", "keywords": ["技能"]}],
}


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list[httpx.Request] = []
        self.pace = PACE.wait
        PACE.wait = lambda: None

    def tearDown(self) -> None:
        PACE.wait = self.pace

    def _client(self, handler) -> QuarkClient:
        client = QuarkClient("access-token", "refresh-token", "device-1", transport=httpx.MockTransport(handler), clock=lambda: 1700000000000)
        client._poll_interval = 0
        client._share_page_size = 2
        return client

    def test_share_tree_stops_when_page_repeats(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            path = urlparse(str(request.url)).path
            if path.endswith("/v2/detail"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "token_info": {"stoken": "token-1", "title": "合集"},
                            "detail_info": {"list": [{"fid": "wrap", "file_name": "合集", "file": False, "size": 0}]},
                        },
                    },
                )
            page = int(parse_qs(urlparse(str(request.url)).query)["_page"][0])
            rows = [
                {"fid": "a", "file_name": "a.txt", "file": True, "size": 4},
                {"fid": "b", "file_name": "b.txt", "file": True, "size": 6},
            ]
            if page > 2:
                raise AssertionError("分页重复后仍然继续请求")
            return httpx.Response(200, json={"code": 0, "data": {"list": rows}})

        info = self._client(handler).walk_share_tree("pwd", "", max_depth=2, max_entries=20)
        self.assertTrue(info["truncated"])
        self.assertEqual(info["total_entries"], 2)
        self.assertEqual(info["total_bytes"], 10)
        self.assertEqual([node["path"] for node in info["tree"]], ["a.txt", "b.txt"])
        self.assertLessEqual(len(self.seen), 3)

    def test_depth_limit_does_not_open_boundary_folder(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            path = urlparse(str(request.url)).path
            query = parse_qs(urlparse(str(request.url)).query)
            if path.endswith("/v2/detail"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "token_info": {"stoken": "token-1", "title": "合集"},
                            "detail_info": {"list": [{"fid": "wrap", "file_name": "合集", "file": False}]},
                        },
                    },
                )
            pdir = query["pdir_fid"][0]
            if pdir == "skill":
                raise AssertionError("到达 max_depth 的文件夹不应再打开")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"fid": "skill", "file_name": "技能", "file": False, "size": 0},
                            {"fid": "note", "file_name": "说明.pdf", "file": True, "size": 8},
                        ]
                    },
                },
            )

        info = self._client(handler).walk_share_tree("pwd", "", max_depth=1, max_entries=20)
        self.assertTrue(info["truncated"])
        folder = info["tree"][0]
        self.assertEqual(folder["name"], "技能/")
        self.assertEqual(folder["children"], [])
        self.assertEqual(folder["child_count"], 0)
        self.assertEqual(info["total_bytes"], 8)

    def test_save_selected_uses_fid_list_not_save_all(self):
        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = urlparse(str(request.url)).path
            if path.endswith("/share/saveas"):
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"status": 0, "data": {"task_id": "task-1"}})
            return httpx.Response(200, json={"status": 0, "data": {"status": 2, "save_as": {"save_as_top_fids": ["new-1"]}}})

        client = self._client(handler)
        client.save_selected(
            "pwd",
            "stoken",
            [{"fid": "f1", "share_fid_token": "t1"}, {"fid": "f2", "share_fid_token": ""}],
            "dest-fid",
        )
        self.assertEqual(bodies[0]["fid_list"], ["f1", "f2"])
        self.assertFalse(bodies[0]["save_all"])
        self.assertEqual(bodies[0]["to_pdir_fid"], "dest-fid")
        self.assertNotIn("fid_token_list", bodies[0])
        client.save_selected(
            "pwd",
            "stoken",
            [{"fid": "f1", "share_fid_token": "t1"}, {"fid": "f2", "share_fid_token": "t2"}],
            "dest-fid",
        )
        self.assertEqual(bodies[1]["fid_token_list"], ["t1", "t2"])

    def test_batch_download_url_and_cookie_stay_on_quark_host(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            path = urlparse(str(request.url)).path
            if path.endswith("/get_download_url"):
                body = json.loads(request.content)
                query = parse_qs(urlparse(str(request.url)).query)
                if body.get("fid") != "a" or "fids" in body or "device_id" in query or "access_token" not in query:
                    return httpx.Response(404, json={"status": 1, "error_info": "unexpected download request"})
                return httpx.Response(
                    200,
                    json={
                        "status": 0,
                        "data": {
                            "fid": "download-state-a|suffix",
                            "download_url": "https://dl.quark.cn/a",
                            "file_name": "a.txt",
                        },
                    },
                )
            if request.url.host == "dl.quark.cn":
                return httpx.Response(200, content=b"hello")
            if request.url.host == "example.com":
                return httpx.Response(200, content=b"nope")
            return httpx.Response(404, json={"status": 1, "error_info": "missing"})

        client = self._client(handler)
        rows = client.get_download_urls(["a"])
        self.assertEqual(json.loads(self.seen[0].content), {"fid": "a"})
        self.assertEqual(urlparse(str(self.seen[0].url)).path, "/open/v1/file/get_download_url")
        self.assertEqual(rows[0]["fid"], "a")
        self.assertEqual(rows[0]["download_url"], "https://dl.quark.cn/a")
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "a.txt"
            self.assertEqual(client.download_file(rows[0]["download_url"], dest), 5)
            self.assertEqual(dest.read_bytes(), b"hello")
            self.assertFalse(dest.with_name("a.txt.part").exists())
            quark_get = next(item for item in self.seen if item.url.host == "dl.quark.cn")
            self.assertIn("x_pan_client_id=third_party_agent", quark_get.headers["cookie"])
            client.download_file("https://example.com/file", Path(directory) / "b.txt")
            plain = next(item for item in self.seen if item.url.host == "example.com")
            self.assertNotIn("cookie", {key.lower() for key in plain.headers})

    def test_download_url_failure_is_isolated_and_429_retried_once(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = urlparse(str(request.url)).path
            if not path.endswith("/get_download_url"):
                return httpx.Response(404, json={"status": 1, "error_info": "missing"})
            fid = json.loads(request.content)["fid"]
            calls.append(fid)
            if fid == "bad":
                return httpx.Response(200, json={"status": 1, "errno": 1, "error_info": "文件被风控"})
            if fid == "rate" and calls.count("rate") == 1:
                return httpx.Response(429, json={"status": 1, "error_info": "too many"})
            return httpx.Response(
                200,
                json={"status": 0, "data": {"fid": "download-state", "download_url": f"https://dl.quark.cn/{fid}"}},
            )

        rows = self._client(handler).get_download_urls(["good", "bad", "rate"])
        self.assertEqual([row["fid"] for row in rows], ["good", "bad", "rate"])
        self.assertEqual(rows[0]["download_url"], "https://dl.quark.cn/good")
        self.assertEqual(rows[1]["download_url"], "")
        self.assertIn("文件被风控", rows[1]["error"])
        self.assertEqual(rows[2]["download_url"], "https://dl.quark.cn/rate")
        self.assertEqual(calls.count("rate"), 2)


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.rules = Path(self.directory.name) / "rules.json"
        self.rules.write_text(json.dumps(RULES), encoding="utf-8")
        self.previous = os.environ.get("NETDISK_RULES_PATH")
        os.environ["NETDISK_RULES_PATH"] = str(self.rules)
        self.fake = FakeQuark()
        self.original = __import__("netdisk_mcp.pipeline", fromlist=["QuarkClient"]).QuarkClient
        import netdisk_mcp.pipeline as pipeline

        pipeline.QuarkClient = lambda *args, **kwargs: self.fake
        self.pipeline = pipeline

    def tearDown(self) -> None:
        self.pipeline.QuarkClient = self.original
        if self.previous is None:
            os.environ.pop("NETDISK_RULES_PATH", None)
        else:
            os.environ["NETDISK_RULES_PATH"] = self.previous
        self.directory.cleanup()

    def test_missing_path_is_skipped_and_hit_is_saved(self):
        result = selective_ingest("https://pan.quark.cn/s/abc", ["抖音/", "没有的"], category="技能", title="抖音技能")
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_missing"], ["没有的"])
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["dest_path"], "/闲鱼资源/技能/抖音技能")
        self.assertEqual(result["expired"], "permanent")
        self.assertEqual(result["share_url"], "https://pan.quark.cn/s/newone")
        self.assertIn("save", [call[0] for call in self.fake.calls])
        self.assertEqual(result["verified_count"], 1)

    def test_non_empty_dest_does_not_save(self):
        self.fake.listing = [{"fid": "old"}]

        def fail_save(*args, **kwargs):
            raise AssertionError("非空目录不应转存")

        self.fake.save_selected = fail_save
        with self.assertRaises(IngestError):
            selective_ingest("https://pan.quark.cn/s/abc", ["抖音"], category="技能")
        self.assertNotIn("save", [call[0] for call in self.fake.calls])

    def test_shallow_dest_is_rejected(self):
        with self.assertRaises(RuleError):
            selective_ingest("https://pan.quark.cn/s/abc", ["抖音"], dest_path="/闲鱼资源/技能")
        self.assertEqual(self.fake.calls, [])

    def test_empty_include_paths_reject_before_any_client(self):
        self.pipeline.QuarkClient = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应连接网盘"))
        with self.assertRaises(IngestError):
            selective_ingest("https://pan.quark.cn/s/abc", [])

    def test_share_failure_after_save_keeps_dest_for_retry(self):
        self.fake.share_error = "创建分享失败"
        result = selective_ingest("https://pan.quark.cn/s/abc", ["抖音"], category="技能", title="抖音技能")
        self.assertFalse(result["ok"])
        self.assertTrue(result["saved"])
        self.assertEqual(result["dest_path"], "/闲鱼资源/技能/抖音技能")
        self.assertNotIn("share_url", result)

    def test_empty_verify_does_not_hand_out_link(self):
        self.fake.verify_error = "新分享可以打开，但里面没有文件，不能上架"
        result = selective_ingest("https://pan.quark.cn/s/abc", ["抖音"], category="技能")
        self.assertFalse(result["ok"])
        self.assertNotIn("share_url", result)


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.local = Path(self.directory.name) / "out"
        self.fake = FakeQuark()
        self.fake.files = [
            {"fid": f"f{i}", "name": f"{i}.txt", "path": f"{i}.txt", "size": 600 * 1024} for i in range(5)
        ]
        import netdisk_mcp.pipeline as pipeline

        self.pipeline = pipeline
        self.original = pipeline.QuarkClient
        pipeline.QuarkClient = lambda *args, **kwargs: self.fake

    def tearDown(self) -> None:
        self.pipeline.QuarkClient = self.original
        self.directory.cleanup()

    def test_max_files_stops_and_records_skipped(self):
        result = download_share_files("/闲鱼资源/技能/抖音技能", [], str(self.local), max_files=2, max_total_mb=200)
        self.assertEqual(result["downloaded"], 2)
        self.assertEqual([item["reason"] for item in result["skipped"]], ["exceeds max_files"] * 3)
        self.assertEqual(len([call for call in self.fake.calls if call[0] == "get"]), 2)

    def test_max_bytes_stops(self):
        result = download_share_files("/闲鱼资源/技能/抖音技能", [], str(self.local), max_files=10, max_total_mb=1)
        self.assertEqual(result["downloaded"], 1)
        self.assertTrue(all(item["reason"] == "exceeds max_total_mb" for item in result["skipped"]))

    def test_parent_segment_is_rejected(self):
        with self.assertRaises(IngestError):
            download_share_files("/闲鱼资源/技能/抖音技能", [], "/tmp/../etc")
        self.assertEqual(self.fake.calls, [])

    def test_existing_file_is_skipped(self):
        self.local.mkdir()
        (self.local / "0.txt").write_text("keep", encoding="utf-8")
        result = download_share_files("/闲鱼资源/技能/抖音技能", ["0.txt", "1.txt"], str(self.local), max_files=10, max_total_mb=20)
        self.assertEqual((self.local / "0.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(result["skipped"][0]["reason"], "already exists")

    def test_three_consecutive_failures_stop(self):
        self.fake.fail = True
        result = download_share_files("/闲鱼资源/技能/抖音技能", [], str(self.local), max_files=10, max_total_mb=20)
        gets = [call for call in self.fake.calls if call[0] == "get"]
        self.assertEqual(len(gets), 3)
        self.assertEqual(result["downloaded"], 0)
        self.assertIn("连续失败已达阈值，已中断", [item["reason"] for item in result["errors"]])

    def test_share_url_saves_subset_and_does_not_create_share(self):
        rules = Path(self.directory.name) / "rules.json"
        rules.write_text(json.dumps(RULES), encoding="utf-8")
        previous = os.environ.get("NETDISK_RULES_PATH")
        os.environ["NETDISK_RULES_PATH"] = str(rules)
        try:
            result = download_share_files("https://pan.quark.cn/s/abc", ["抖音"], str(self.local), max_files=10, max_total_mb=20)
        finally:
            if previous is None:
                os.environ.pop("NETDISK_RULES_PATH", None)
            else:
                os.environ["NETDISK_RULES_PATH"] = previous
        self.assertGreater(result["downloaded"], 0)
        self.assertIn("save", [call[0] for call in self.fake.calls])
        self.assertNotIn("share", [call[0] for call in self.fake.calls])
        self.assertIn("/下载缓存/", self.fake.calls[[call[0] for call in self.fake.calls].index("ensure")][1])

    def test_url_error_records_reason_and_downloads_rest(self):
        original = self.fake.get_download_urls

        def with_failure(fids):
            rows = original(fids)
            rows[0]["download_url"] = ""
            rows[0]["error"] = "文件被风控"
            return rows

        self.fake.get_download_urls = with_failure
        result = download_share_files("/闲鱼资源/技能/抖音技能", [], str(self.local), max_files=10, max_total_mb=20)
        self.assertEqual(result["downloaded"], 4)
        self.assertEqual(result["errors"][0]["reason"], "文件被风控")
        self.assertEqual(len(result["errors"]), 1)

    def test_symlink_local_dir_is_rejected(self):
        real = Path(self.directory.name) / "real"
        real.mkdir()
        link = Path(self.directory.name) / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(IngestError):
            download_share_files("/闲鱼资源/技能/抖音技能", [], str(link))


class FakeQuark:
    def __init__(self) -> None:
        self.calls = []
        self.listing = []
        self.files = []
        self.fail = False
        self.share_error = ""
        self.verify_error = ""
        self.resolved = {
            "stoken": "st",
            "title": "技能合集",
            "found": [{"fid": "a", "name": "抖音", "path": "抖音", "size": 10, "is_folder": True, "share_fid_token": "t"}],
            "missing": ["没有的"],
        }

    def resolve_share_paths(self, pwd_id, password, paths):
        self.calls.append(("resolve", list(paths)))
        found = []
        missing = []
        known = {item["path"]: item for item in self.resolved["found"]}
        for raw in paths:
            path = str(raw).strip().strip("/")
            if path in known:
                found.append(known[path])
            else:
                missing.append(path)
        return {**self.resolved, "found": found, "missing": missing}

    def ensure_path(self, path):
        self.calls.append(("ensure", path))
        return "dest-fid"

    def list_files(self, fid):
        self.calls.append(("list", fid))
        return self.listing

    def save_selected(self, pwd_id, stoken, entries, dest_fid):
        self.calls.append(("save", dest_fid, [item["fid"] for item in entries]))
        return ["new"]

    def create_share(self, fid, title, url_type=1):
        self.calls.append(("share", fid, url_type))
        if self.share_error:
            raise QuarkError(self.share_error)
        return {"share_url": "https://pan.quark.cn/s/newone", "passcode": "", "pwd_id": "newone"}

    def verify(self, pwd_id, password):
        if self.verify_error:
            raise QuarkError(self.verify_error)
        return {"title": "技能合集", "count": 1, "names": ["抖音"]}

    def collect_files(self, path):
        self.calls.append(("collect", path))
        return list(self.files)

    def get_download_urls(self, fids):
        self.calls.append(("urls", list(fids)))
        return [
            {"fid": f"download-state-{fid}|suffix", "download_url": f"https://dl.quark.cn/{fid}", "file_name": fid}
            for fid in fids
        ]

    def download_file(self, url, dest):
        self.calls.append(("get", url))
        if self.fail:
            raise QuarkError("下载失败")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")
        return 2


if __name__ == "__main__":
    unittest.main()
