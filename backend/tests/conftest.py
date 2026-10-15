"""pytest 全局夹具：把临时目录限制在工程内。

理由：受限运行环境会把系统临时目录标记为低完整性，pytest 默认的
`make_numbered_dir` 需要在临时根目录上列举文件，因而会被拒绝；改用工程内
自有的临时目录即可规避，且不影响任何测试语义。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

_TMP_ROOT = Path(__file__).resolve().parents[1] / ".pytest-tmp"


@pytest.fixture(scope="session")
def _session_tmp_root() -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    yield _TMP_ROOT
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture()
def tmp_path(_session_tmp_root, request) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in request.node.name)[:60]
    digest = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:8]
    target = _session_tmp_root / f"{safe}-{digest}"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)
