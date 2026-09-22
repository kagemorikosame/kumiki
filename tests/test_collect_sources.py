"""リリースのときに GPL / LGPL の部品のソースを集める道具（tools/collect_sources.py）

本物の置き場からは落とさない（qtwebengine だけで 500 MB を超える） 手元で HTTP の
置き場を立てて、落とし方・照合・後始末を確かめる
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parent.parent

ARCHIVE = b"source archive body " * 100
PAGE = b"<!doctype html><title>Making sure you're not a bot!</title>"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "collect_sources", ROOT / "tools" / "collect_sources.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Handler(BaseHTTPRequestHandler):
    """``/archive`` はアーカイブ、``/page`` はボット避けの HTML、``/nohead`` は HEAD を断る"""

    #: 届いた頼み（方法、道） 置き場はスレッドをまたぐので、インスタンスではなく型に持つ
    requests: ClassVar[list[tuple[str, str]]] = []

    def _answer(self, body: bool) -> None:
        type(self).requests.append((self.command, self.path))
        if self.path == "/nohead" and self.command == "HEAD":
            self.send_response(403)
            self.end_headers()
            return
        if self.path in ("/archive", "/nohead"):
            payload, kind = ARCHIVE, "application/x-gzip"
        elif self.path == "/page":
            payload, kind = PAGE, "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._answer(body=True)

    def do_HEAD(self) -> None:
        self._answer(body=False)

    def log_message(self, format: str, *args: object) -> None:
        # 試験の出力を置き場の記録で埋めない
        pass


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _source(tool: ModuleType, url: str, sha256: str | None = None) -> object:
    return tool.Source("sample", "1.0", url, sha256, "GPL-2.0-or-later", "sample-1.0.tar.gz")


class TestCollect:
    def test_it_writes_the_archive_and_the_manifest(
        self, tool: ModuleType, server: str, tmp_path: Path
    ) -> None:
        """落とした物と一緒に、取得元と sha256 を残す

        残さないと、添付したソースが積んだ版の物かを後から確かめられない
        """
        expected = hashlib.sha256(ARCHIVE).hexdigest()
        manifest = tool.collect([_source(tool, f"{server}/archive", expected)], tmp_path)
        assert (tmp_path / "sample-1.0.tar.gz").read_bytes() == ARCHIVE
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        assert entries[0]["sha256"] == expected
        assert entries[0]["url"] == f"{server}/archive"
        assert entries[0]["verified_against_upstream"] is True
        sums = (tmp_path / tool.SUMS_NAME).read_text(encoding="utf-8")
        assert sums == f"{expected}  sample-1.0.tar.gz\n"

    def test_an_unpinned_archive_records_what_came(
        self, tool: ModuleType, server: str, tmp_path: Path
    ) -> None:
        # 上流が sha256 を出していない物も、落とした時点の値を残す
        manifest = tool.collect([_source(tool, f"{server}/archive")], tmp_path)
        entry = json.loads(manifest.read_text(encoding="utf-8"))[0]
        assert entry["sha256"] == hashlib.sha256(ARCHIVE).hexdigest()
        assert entry["verified_against_upstream"] is False

    def test_a_wrong_checksum_leaves_nothing(
        self, tool: ModuleType, server: str, tmp_path: Path
    ) -> None:
        """上流の公開値と合わなければ止まり、何も残さない

        残すと、中身の違うソースを「積んだ版の物」として添付する
        """
        with pytest.raises(RuntimeError, match="sha256"):
            tool.download(_source(tool, f"{server}/archive", "0" * 64), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_a_bot_page_is_not_taken_for_the_source(
        self, tool: ModuleType, server: str, tmp_path: Path
    ) -> None:
        # ボット避けの画面は 200 で返る 数えると HTML をソースとして添付する
        with pytest.raises(RuntimeError, match="HTML"):
            tool.download(_source(tool, f"{server}/page"), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_a_finished_archive_is_not_fetched_again(
        self, tool: ModuleType, server: str, tmp_path: Path
    ) -> None:
        """落とし終えた物は落とし直さない qtwebengine は 500 MB を超える"""
        (tmp_path / "sample-1.0.tar.gz").write_bytes(ARCHIVE)
        source = _source(tool, f"{server}/archive", hashlib.sha256(ARCHIVE).hexdigest())
        tool.download(source, tmp_path)
        assert _Handler.requests == []

    def test_a_stale_archive_is_fetched_again(
        self, tool: ModuleType, server: str, tmp_path: Path
    ) -> None:
        # 公開値と合わない物（前の版の残りなど）は落とし直す
        (tmp_path / "sample-1.0.tar.gz").write_bytes(b"old")
        source = _source(tool, f"{server}/archive", hashlib.sha256(ARCHIVE).hexdigest())
        tool.download(source, tmp_path)
        assert (tmp_path / "sample-1.0.tar.gz").read_bytes() == ARCHIVE


class TestCheck:
    def test_a_reachable_archive_passes(self, tool: ModuleType, server: str) -> None:
        assert tool.check_urls([_source(tool, f"{server}/archive")]) == []
        # 確かめるだけで中身は落とさない
        assert [method for method, _ in _Handler.requests] == ["HEAD"]

    def test_a_server_refusing_head_is_asked_for_one_byte(
        self, tool: ModuleType, server: str
    ) -> None:
        """Bitbucket の置き場は HEAD に 403 を返す それだけで届かないと数えない"""
        assert tool.check_urls([_source(tool, f"{server}/nohead")]) == []

    def test_a_missing_archive_is_reported(self, tool: ModuleType, server: str) -> None:
        problems = tool.check_urls([_source(tool, f"{server}/gone")])
        assert len(problems) == 1 and "/gone" in problems[0]

    def test_a_bot_page_is_reported(self, tool: ModuleType, server: str) -> None:
        assert tool.check_urls([_source(tool, f"{server}/page")]) != []


class TestTheList:
    def test_the_gpl_parts_are_all_there(self, tool: ModuleType) -> None:
        """GPL と LGPL の部品のうち、ソースを渡さなければならない物が全部ある"""
        names = {source.name.split()[0] for source in tool.SOURCES}
        assert {"FFmpeg", "pyav-ffmpeg", "x264", "x265", "LAME", "libiconv", "Qt"} <= names
        assert any(source.name.startswith("PySide6") for source in tool.SOURCES)

    def test_file_names_do_not_collide(self, tool: ModuleType) -> None:
        # 名前がぶつかると、後から落とした物が前の物を黙って上書きする
        names = [source.filename for source in tool.SOURCES]
        assert len(names) == len(set(names))
        assert all("/" not in name and "\\" not in name for name in names)

    def test_the_notices_say_where_each_one_comes_from(self, tool: ModuleType) -> None:
        """一覧（THIRD_PARTY_NOTICES.md）にも同じ部品と版が載っている

        道具だけ直して一覧を直し忘れると、使う人に見せる入手先と添付する物が食い違う
        """
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for source in tool.SOURCES:
            assert source.version in notices, source.name
            assert source.name.split()[0] in notices, source.name

    def test_the_versions_match_what_is_bundled(self, tool: ModuleType) -> None:
        """手で書いた版が、いま入っている PyAV と PySide6 の版と合う

        PyAV か PySide6 を上げたらここで落ちる 一覧を直さないと別の版のソースを添付する
        """
        assert tool.installed_mismatches() == []

    def test_a_version_mismatch_stops_the_tool(
        self, tool: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tool, "installed_mismatches", lambda: ["積んでいる FFmpeg は 9.0"])
        called: list[object] = []

        def check(sources: object) -> list[str]:
            called.append(sources)
            return []

        monkeypatch.setattr(tool, "check_urls", check)
        assert tool.main(["--check"]) == 1
        assert called == [], "版が食い違っているのに先へ進んだ"
