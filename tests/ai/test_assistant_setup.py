"""アシスタントの実行環境 同梱の Claude Code・ログインの手掛かり・会話へ渡す設定

SDK は import しない 配る先の機械と同じく、SDK の中身は偽物の置き場で作る
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from sashimono.ai import environment
from sashimono.ai.bridge import EditorBridge
from sashimono.ai.environment import AI_PACK
from sashimono.ai.models import effort_for
from sashimono.ai.session import AgentSession
from sashimono.runtime import install_command
from sashimono.ui.workspace import Preferences, PreferenceStore
from tests.test_runtime_after_install import write_distribution


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """SDK も claude も入っていない機械 戻り値は、導入先に見立てたフォルダ"""
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setattr(sys, "path", [str(site)])
    for name in list(sys.modules):
        if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
            monkeypatch.delitem(sys.modules, name)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    return site


def _install_sdk(site: Path, *, bundled: bool = True) -> Path:
    write_distribution(site, "claude-agent-sdk", "claude_agent_sdk", "0.2.152")
    cli = site / "claude_agent_sdk" / "_bundled" / "claude.exe"
    if bundled:
        cli.parent.mkdir(parents=True)
        cli.write_bytes(b"")
    return cli


class TestBundledClaudeCode:
    def test_nothing_is_found_before_installing(self, machine: Path) -> None:
        # 入っていないのに見つかったことにすると、導入の案内が出ず、送った瞬間に失敗する
        del machine
        assert environment.bundled_claude_cli() is None
        assert environment.runtime_status().installed is False

    def test_installing_the_sdk_alone_is_enough(self, machine: Path) -> None:
        """直す前は PATH の claude を要求し、SDK だけでは「使えない」と出ていた

        SDK は Claude Code 本体を同梱している Node で別に入れていない人は、
        導入を済ませても入力欄が開かなかった
        """
        cli = _install_sdk(machine)
        assert environment.bundled_claude_cli() == cli
        status = environment.runtime_status()
        assert status.missing_commands == ()
        assert status.ready is True

    def test_an_sdk_older_than_required_asks_to_be_replaced(self, machine: Path) -> None:
        """前の条件（0.2 以上）で入れた古い SDK を「導入済み」と見ないこと

        見てしまうと入力欄が開き、考える深さを選んだ所で、古い SDK が知らない
        引数を渡されて会話を始めた瞬間に落ちる
        """
        write_distribution(machine, "claude-agent-sdk", "claude_agent_sdk", "0.2.10")
        status = environment.runtime_status()
        assert status.ready is False
        assert status.needs_upgrade is True
        assert "古い版" in status.summary()
        # 配布版の導入先（--target）では --upgrade が無いと入れ替わらない
        command = install_command(AI_PACK, extra=False, upgrade=status.needs_upgrade)
        assert "--upgrade" in command

    def test_an_old_sdk_without_the_bundle_still_asks_for_claude(self, machine: Path) -> None:
        _install_sdk(machine, bundled=False)
        status = environment.runtime_status()
        assert status.ready is False
        assert "npm install" in status.summary()


class TestCredentials:
    def test_no_login_is_detected(self, machine: Path) -> None:
        del machine
        assert environment.credentials_found() is False

    def test_a_logged_in_claude_code_is_detected(self, machine: Path, tmp_path: Path) -> None:
        del machine
        folder = tmp_path / "home" / ".claude"
        folder.mkdir()
        (folder / ".credentials.json").write_text("{}", encoding="utf-8")
        assert environment.credentials_found() is True

    def test_an_api_key_in_the_environment_counts(
        self, machine: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del machine
        # 値の中身は見ない 在るかどうかだけ
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
        assert environment.credentials_found() is True

    def test_the_login_window_runs_only_claude_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        started: list[list[str]] = []

        def popen(argv: list[str], **_kwargs: object) -> None:
            started.append(argv)

        monkeypatch.setattr(subprocess, "Popen", popen)
        cli = tmp_path / "claude.exe"
        environment.open_login_window(cli)
        assert started == [[str(cli)]]


def _session(model: str | None = None, effort: str | None = None) -> AgentSession:
    # 渡す設定を組み立てるだけで、ブリッジは使わない
    return AgentSession(cast(EditorBridge, object()), model=model, effort=effort)


class TestSessionOptions:
    def test_nothing_is_forced_by_default(self, machine: Path) -> None:
        # 既定で何かを渡すと、そのモデルを使えないアカウントでは会話が始まらない
        del machine
        values = _session().option_values()
        assert values["model"] is None
        assert "effort" not in values

    def test_the_chosen_model_and_effort_are_passed(self, machine: Path) -> None:
        # 渡し忘れると、選んだ深さが効かないまま Claude Code の既定で考える
        del machine
        values = _session("claude-opus-5-5", "xhigh").option_values()
        assert values["model"] == "claude-opus-5-5"
        assert values["effort"] == "xhigh"

    def test_haiku_gets_no_effort(self, machine: Path) -> None:
        # Haiku 4.5 はエフォートを受け付けない 渡すと会話が始まる前に失敗する
        del machine
        assert "effort" not in _session("claude-haiku-4-5-20251001", "high").option_values()
        assert effort_for("claude-haiku-4-5-20251001", "high") is None

    def test_the_bundled_claude_code_is_left_to_the_sdk(self, machine: Path) -> None:
        # ここで npm の claude.cmd を渡すと、SDK が起動を断る
        _install_sdk(machine)
        assert "cli_path" not in _session().option_values()

    def test_an_old_sdk_gets_the_separately_installed_claude(
        self, machine: Path, tmp_path: Path
    ) -> None:
        _install_sdk(machine, bundled=False)
        local = tmp_path / "home" / ".local" / "bin" / "claude.exe"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"")
        assert _session().option_values()["cli_path"] == str(local)

    def test_the_options_match_what_the_sdk_accepts(self) -> None:
        """名前を 1 つでも違えると、会話を始めた瞬間に TypeError で落ちる"""
        sdk = pytest.importorskip("claude_agent_sdk")
        fields = set(sdk.ClaudeAgentOptions.__dataclass_fields__)
        values = _session("claude-sonnet-5", "low").option_values()
        assert set(values) <= fields


class TestPreferences:
    def test_the_choices_survive_a_restart(self, tmp_path: Path) -> None:
        store = PreferenceStore(tmp_path / "preferences.json")
        chosen = Preferences(ai_model="claude-fable-5-1", ai_effort="max", chat_enter_sends=False)
        store.save(chosen)
        assert store.load() == chosen

    def test_unknown_values_fall_back_to_the_default(self, tmp_path: Path) -> None:
        # 手で書き換えた値や、なくなったモデルで起動を止めない
        path = tmp_path / "preferences.json"
        path.write_text(
            json.dumps({"ai_model": "claude-2", "ai_effort": 3, "chat_enter_sends": "yes"}),
            encoding="utf-8",
        )
        loaded = PreferenceStore(path).load()
        assert (loaded.ai_model, loaded.ai_effort, loaded.chat_enter_sends) == ("", "", True)

    def test_the_settings_dialog_keeps_them(self, qt_application: object) -> None:
        del qt_application
        from sashimono.ui.preferences_dialog import PreferencesDialog

        chosen = Preferences(ai_model="claude-sonnet-5", ai_effort="medium", chat_enter_sends=False)
        assert PreferencesDialog(chosen).preferences() == chosen
