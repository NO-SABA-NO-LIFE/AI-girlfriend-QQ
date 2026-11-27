import subprocess
import sys
import textwrap


def test_runtime_sync_preserves_builtins() -> None:
    script = textwrap.dedent('''
        import builtins

        from atri_qq_bot.insights.store import InsightsStore
        from atri_qq_bot.memory import UserMemoryStore
        from atri_qq_bot.onebot.server import OneBotServer
        from atri_qq_bot.onebot.server_parts import server_events
        from atri_qq_bot.persona import AtriReplyEngine
        from atri_qq_bot.persona.reply_rules import _group_speaker_label
        from atri_qq_bot.toolbox import ToolAnalyzer
        from atri_webui.qzone_automation import QZoneAutomation
        from atri_webui.server import AtriWebUIHandler

        assert vars(server_events).get("_group_speaker_label") is _group_speaker_label

        classes = (
            OneBotServer, AtriReplyEngine, UserMemoryStore, ToolAnalyzer,
            InsightsStore, QZoneAutomation, AtriWebUIHandler,
        )
        before = dict(vars(builtins))
        for cls in classes:
            _ = object.__new__(cls).__class__
        after = vars(builtins)
        delta = sorted(before.keys() ^ after.keys())
        changed = sorted(
            name for name in before.keys() & after.keys()
            if before[name] is not after[name]
        )
        assert not delta and not changed, (delta, changed)
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
