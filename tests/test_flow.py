import os
import unittest
from pathlib import Path

from netdisk_mcp.rules import RuleError, assert_resource_dir, build_dest, choose_category, load_rules, sanitize_title
from netdisk_mcp.urls import LinkError, parse_share_url


RULES = {
    "quark_root": "/闲鱼资源",
    "baidu_root": "/闲鱼资源",
    "fallback": "待分类",
    "rules": [
        {"keywords": ["考研", "英语"], "folder": "考试"},
        {"keywords": ["photoshop"], "folder": "软件"},
    ],
}


class UrlTests(unittest.TestCase):
    def test_quark(self):
        link = parse_share_url("https://pan.quark.cn/s/abc123?pwd=xyz")
        self.assertEqual(link.provider, "quark")
        self.assertEqual(link.key, "abc123")
        self.assertEqual(link.password, "xyz")

    def test_baidu_strips_leading_one(self):
        link = parse_share_url("https://pan.baidu.com/s/1AbCdEf?pwd=wxyz")
        self.assertEqual(link.provider, "baidu")
        self.assertEqual(link.full_key, "1AbCdEf")
        self.assertEqual(link.key, "AbCdEf")
        self.assertEqual(link.password, "wxyz")

    def test_unknown_host_fails(self):
        with self.assertRaises(LinkError):
            parse_share_url("https://example.com/s/abc")


class RuleTests(unittest.TestCase):
    def test_longer_keyword_wins(self):
        self.assertEqual(choose_category("考研英语一", ["真题.pdf"], RULES), "考试")

    def test_fallback(self):
        self.assertEqual(choose_category("杂项", ["a.zip"], RULES), "待分类")

    def test_override_rejects_nested_category(self):
        with self.assertRaises(RuleError):
            choose_category("考研", [], RULES, "考试/子目录")

    def test_dest_is_resource_folder_not_category(self):
        dest = build_dest("quark", RULES, "考试", "考研英语 真题")
        self.assertEqual(dest, "/闲鱼资源/考试/考研英语 真题")

    def test_reject_category_directory(self):
        with self.assertRaises(RuleError):
            assert_resource_dir("baidu", RULES, "/闲鱼资源/考试")

    def test_reject_root(self):
        with self.assertRaises(RuleError):
            build_dest("baidu", RULES, "考试", "真题", dest_path="/闲鱼资源")

    def test_sanitize(self):
        self.assertEqual(sanitize_title(' a/b:c*?.pdf '), "a b c .pdf")
        self.assertEqual(sanitize_title("   "), "未命名资源")

    def test_load_missing_file_uses_fallback(self):
        previous = os.getcwd()
        os.environ.pop("NETDISK_RULES_PATH", None)
        try:
            os.chdir("/tmp")
            rules = load_rules()
        finally:
            os.chdir(previous)
        self.assertEqual(rules["fallback"], "待分类")
        self.assertFalse(Path("/tmp/directory_rules.json").exists())


if __name__ == "__main__":
    unittest.main()
