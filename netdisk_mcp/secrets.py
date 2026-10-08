"""凭证只从生产机环境变量或宿主机文件读取，禁止出现在工具返回值里。"""

from __future__ import annotations

import os
from pathlib import Path


def read_secret(env_name: str, file_env_name: str) -> str:
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    path = os.getenv(file_env_name, "").strip()
    if not path:
        return ""
    file = Path(path)
    if not file.is_file():
        raise RuntimeError(f"{file_env_name} 指向的文件不存在")
    return file.read_text(encoding="utf-8").strip()
