import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from netdisk_mcp.limit import PACE
from netdisk_mcp.pipeline import IngestError, upload_file_or_folder
from netdisk_mcp.quark import QuarkError
from netdisk_mcp.rules import RuleError


RULES = {
    "quark_root": "/闲鱼资源",
    "baidu_root": "/闲鱼资源",
    "fallback": "待分类",
    "rules": [{"folder": "技能", "keywords": ["技能"]}],
}


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.pace = PACE.wait
        PACE.wait = lambda: None
        self.rules = Path(self.directory.name) / "rules.json"
        self.rules.write_text(json.dumps(RULES), encoding="utf-8")
        self.previous = os.environ.get("NETDISK_RULES_PATH")
        os.environ["NETDISK_RULES_PATH"] = str(self.rules)
        self.cli = os.environ.get("QUARK_CLI_SCRIPT")
        self.cli_script = Path(self.directory.name) / "quark-drive.cjs"
        self.cli_script.write_text("//stub", encoding="utf-8")
        os.environ["QUARK_CLI_SCRIPT"] = str(self.cli_script)
        self.fake = FakeQuark()
        self.original = _pipeline().QuarkClient
        _pipeline().QuarkClient = lambda *args, **kwargs: self.fake
        self.which = _pipeline().shutil.which
        _pipeline().shutil.which = lambda name: "/usr/bin/node" if name == "node" else None

    def tearDown(self) -> None:
        PACE.wait = self.pace
        _pipeline().shutil.which = self.which
        _pipeline().QuarkClient = self.original
        if self.previous is None:
            os.environ.pop("NETDISK_RULES_PATH", None)
        else:
            os.environ["NETDISK_RULES_PATH"] = self.previous
        if self.cli is None:
            os.environ.pop("QUARK_CLI_SCRIPT", None)
        else:
            os.environ["QUARK_CLI_SCRIPT"] = self.cli
        self.directory.cleanup()

    def _local_file(self, name="product.zip", content=b"zip") -> Path:
        path = Path(self.directory.name) / name
        path.write_bytes(content)
        return path

    @staticmethod
    def _runner(stdout="", stderr="", returncode=0, seen=None):
        def run(command, workdir, env, timeout):
            if seen is not None:
                seen.append({"command": command, "workdir": workdir, "env": env, "timeout": timeout})
            return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

        return run

    @staticmethod
    def _result(fids, instant=False, success=None):
        payload = {
            "code": 0,
            "msg": "成功",
            "data": {"fids": fids, "successCount": success if success is not None else len(fids), "instantUpload": instant},
            "action": "upload",
            "type": "result",
        }
        return json.dumps(payload, ensure_ascii=False)

    def test_missing_local_path_fails_before_any_client_call(self):
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(str(Path(self.directory.name) / "nope.zip"), runner=self._runner())
        self.assertEqual(caught.exception.code, "FILE_NOT_FOUND")
        self.assertEqual(self.fake.calls, [])

    def test_relative_path_and_symlink_are_rejected(self):
        with self.assertRaises(IngestError) as relative:
            upload_file_or_folder("product.zip", runner=self._runner())
        self.assertEqual(relative.exception.code, "LOCAL_PATH_INVALID")
        link = Path(self.directory.name) / "link"
        link.symlink_to(Path(self.directory.name), target_is_directory=True)
        with self.assertRaises(IngestError) as symlink:
            upload_file_or_folder(str(link), runner=self._runner())
        self.assertEqual(symlink.exception.code, "LOCAL_PATH_INVALID")
        self.assertEqual(self.fake.calls, [])

    def test_shallow_dest_dir_is_rejected_before_client(self):
        with self.assertRaises(RuleError):
            upload_file_or_folder(str(self._local_file()), dest_dir="/闲鱼资源/技能", runner=self._runner())
        self.assertEqual(self.fake.calls, [])

    def test_missing_cli_script_fails_before_client(self):
        os.environ["QUARK_CLI_SCRIPT"] = str(Path(self.directory.name) / "missing.cjs")
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(str(self._local_file()), runner=self._runner())
        self.assertEqual(caught.exception.code, "CLI_NOT_FOUND")
        self.assertEqual(self.fake.calls, [])

    def test_single_file_upload_shares_the_file(self):
        seen = []
        runner = self._runner(stdout=self._result(["file-9"], instant=True), seen=seen)
        local = self._local_file()
        result = upload_file_or_folder(str(local), dest_dir="/闲鱼资源/技能/闲鱼实操Skills合集", runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["dest_path"], "/闲鱼资源/技能/闲鱼实操Skills合集/product.zip")
        self.assertEqual(result["fid"], "file-9")
        self.assertTrue(result["instant_upload"])
        self.assertTrue(result["share_created"])
        self.assertEqual(result["share_url"], "https://pan.quark.cn/s/newone")
        self.assertEqual(result["verified_count"], 1)
        command = seen[0]["command"]
        self.assertEqual(command[0], "/usr/bin/node")
        self.assertEqual(command[1], str(self.cli_script))
        self.assertEqual(command[2], "upload")
        self.assertIn("product.zip", command[3])
        self.assertEqual(command[-2:], ["--parent-fid", "dest-fid"])
        self.assertEqual(seen[0]["env"].get("QUARK_CLI_SCRIPT"), str(self.cli_script))
        self.assertNotIn("PYTHONPATH", seen[0]["env"])
        self.assertNotIn("PYTHONHOME", seen[0]["env"])
        self.assertIn(("share", "file-9"), [(call[0], call[1]) for call in self.fake.calls])

    def test_folder_upload_shares_created_folder_not_files(self):
        self.fake.listings = [
            [],
            [{"fid": "folder-9", "name": "产品包", "is_folder": True, "size": 0}],
        ]
        folder = Path(self.directory.name) / "产品包"
        folder.mkdir()
        (folder / "a.txt").write_bytes(b"a")
        result = upload_file_or_folder(str(folder), category="技能", title="抖音技能", runner=self._runner(stdout=self._result(["f1", "f2"])))
        self.assertEqual(result["dest_path"], "/闲鱼资源/技能/抖音技能/产品包")
        self.assertEqual(result["fid"], "folder-9")
        self.assertIn(("share", "folder-9"), [(call[0], call[1]) for call in self.fake.calls])

    def test_create_share_false_skips_share_and_verify(self):
        result = upload_file_or_folder(
            str(self._local_file()), dest_dir="/闲鱼资源/技能/合集", create_share=False, runner=self._runner(stdout=self._result(["file-9"]))
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["share_created"])
        self.assertNotIn("share_url", result)
        self.assertNotIn("share", [call[0] for call in self.fake.calls])
        self.assertNotIn("verify", [call[0] for call in self.fake.calls])

    def test_cli_failure_stops_before_share(self):
        failed_list = json.dumps({"code": 500, "msg": "磁盘已满", "data": {}, "action": "upload", "type": "list"}, ensure_ascii=False)
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(
                str(self._local_file()),
                dest_dir="/闲鱼资源/技能/合集",
                runner=self._runner(stdout=failed_list, stderr="node panic", returncode=1),
            )
        self.assertEqual(caught.exception.code, "UPLOAD_FAILED")
        self.assertIn("磁盘已满", str(caught.exception))
        self.assertNotIn("share", [call[0] for call in self.fake.calls])

    def test_zero_success_count_fails(self):
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(
                str(self._local_file()),
                dest_dir="/闲鱼资源/技能/合集",
                runner=self._runner(stdout=self._result([], success=0)),
            )
        self.assertEqual(caught.exception.code, "UPLOAD_FAILED")
        self.assertNotIn("share", [call[0] for call in self.fake.calls])

    def test_dest_not_empty_refuses_before_upload(self):
        self.fake.listing = [{"fid": "old", "name": "旧文件.zip", "is_folder": False, "size": 1}]
        seen = []
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(
                str(self._local_file()), dest_dir="/闲鱼资源/技能/合集", runner=self._runner(seen=seen)
            )
        self.assertEqual(caught.exception.code, "DEST_NOT_EMPTY")
        self.assertIn("拒绝混入", str(caught.exception))
        self.assertEqual(seen, [])
        self.assertNotIn("share", [call[0] for call in self.fake.calls])

    def test_empty_folder_is_rejected_before_client(self):
        empty = Path(self.directory.name) / "空目录"
        empty.mkdir()
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(str(empty), dest_dir="/闲鱼资源/技能/合集", runner=self._runner())
        self.assertEqual(caught.exception.code, "LOCAL_PATH_INVALID")
        self.assertIn("空", str(caught.exception))
        self.assertEqual(self.fake.calls, [])

    def test_symlink_inside_folder_is_rejected(self):
        folder = Path(self.directory.name) / "产品包"
        folder.mkdir()
        target = Path(self.directory.name) / "secret.txt"
        target.write_text("s", encoding="utf-8")
        (folder / "link.txt").symlink_to(target)
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(str(folder), dest_dir="/闲鱼资源/技能/合集", runner=self._runner())
        self.assertEqual(caught.exception.code, "LOCAL_PATH_INVALID")
        self.assertIn("符号链接", str(caught.exception))
        self.assertEqual(self.fake.calls, [])

    def test_partial_upload_failure_refuses_share(self):
        ok_list = json.dumps(
            {"code": 0, "msg": "上传成功", "data": {"recordId": "r1", "fileId": "f1"}, "action": "upload", "type": "list"},
            ensure_ascii=False,
        )
        bad_list = json.dumps(
            {"code": 500, "msg": "网络中断", "data": {}, "action": "upload", "type": "list"}, ensure_ascii=False
        )
        with self.assertRaises(IngestError) as caught:
            upload_file_or_folder(
                str(self._local_file()),
                dest_dir="/闲鱼资源/技能/合集",
                runner=self._runner(stdout=ok_list + "\n" + bad_list + "\n" + self._result(["f1"])),
            )
        self.assertEqual(caught.exception.code, "UPLOAD_FAILED")
        self.assertIn("不完整", str(caught.exception))
        self.assertIn("网络中断", str(caught.exception))
        self.assertNotIn("share", [call[0] for call in self.fake.calls])

    def test_share_failure_keeps_upload_result(self):
        self.fake.share_error = "创建分享失败"
        result = upload_file_or_folder(
            str(self._local_file()), dest_dir="/闲鱼资源/技能/合集", runner=self._runner(stdout=self._result(["file-9"]))
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["dest_path"], "/闲鱼资源/技能/合集/product.zip")
        self.assertNotIn("share_url", result)


def _pipeline():
    import netdisk_mcp.pipeline as pipeline

    return pipeline


class FakeQuark:
    def __init__(self) -> None:
        self.calls = []
        self.listing = []
        self.listings = []
        self.share_error = ""

    def ensure_path(self, path):
        self.calls.append(("ensure", path))
        return "dest-fid"

    def list_files(self, fid):
        self.calls.append(("list", fid))
        if self.listings:
            return self.listings.pop(0)
        return self.listing

    def create_share(self, fid, title, url_type=1):
        self.calls.append(("share", fid, title, url_type))
        if self.share_error:
            raise QuarkError(self.share_error)
        return {"share_url": "https://pan.quark.cn/s/newone", "passcode": "", "pwd_id": "newone"}

    def verify(self, pwd_id, password):
        self.calls.append(("verify", pwd_id))
        return {"title": "合集", "count": 1, "names": ["product.zip"]}


if __name__ == "__main__":
    unittest.main()
