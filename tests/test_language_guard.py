import asyncio
from pathlib import Path

from atri_qq_bot.config import BotConfig
from atri_qq_bot.language_guard import has_illegal_language_or_garbage
from atri_qq_bot.memory import UserMemoryStore
from atri_qq_bot.persona import AtriReplyEngine, _persona_violations


def _config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key="ollama",
        openai_base_url="http://127.0.0.1:11434/v1",
        openai_model="qwen3:4b-instruct",
        temperature=0.60,
        max_tokens=180,
        frequency_penalty=0.35,
        memory_path=tmp_path / "users.json",
    )


def test_language_guard_blocks_foreign_scripts_and_garbage() -> None:
    assert has_illegal_language_or_garbage("كيفية تقييم هذا الملف؟")
    assert has_illegal_language_or_garbage("안녕，今天怎么样")
    assert has_illegal_language_or_garbage("ท้องถิ่น我卡住了")
    assert has_illegal_language_or_garbage("Привет，先别这样")
    assert has_illegal_language_or_garbage("שלום，今天怎么样")
    assert has_illegal_language_or_garbage("γειά σου，先别这样")
    assert has_illegal_language_or_garbage("[PAD151831] 这句乱了")
    assert has_illegal_language_or_garbage("⎢⎢≴≴")
    assert has_illegal_language_or_garbage("garip çözümler üretme")
    assert has_illegal_language_or_garbage("BB69A5F64DA53BB1B02031762176BC0B")
    assert not has_illegal_language_or_garbage("哼，今天先喝口水再继续。")
    assert not has_illegal_language_or_garbage("ちょっと待って (｀・ω・´)")
    assert not has_illegal_language_or_garbage("important_interaction")


def test_persona_marks_illegal_language_as_violation() -> None:
    violations = _persona_violations("今天好烦", "كيفية تقييم هذا الملف？我先回答")

    assert violations


def test_memory_skips_illegal_language_before_write(tmp_path: Path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")

    store.observe_user("private:1", "كيفية تقييم هذا الملف？", now=1000)
    store.observe_bot("private:1", "안녕，今天怎么样", now=1001)

    assert store.recent_history("private:1") == []


def test_reply_retries_language_drift_with_lower_temperature(tmp_path: Path) -> None:
    class GuardedEngine(AtriReplyEngine):
        def __init__(self, config: BotConfig) -> None:
            super().__init__(config)
            self.temperatures: list[float | None] = []

        async def _reply_with_api(self, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
            self.temperatures.append(kwargs.get("temperature_override"))
            if len(self.temperatures) == 1:
                return "كيفية تقييم هذا الملف؟"
            return "哼，这句我重新说清楚：今天先别硬撑，喝口水再慢慢来。"

    engine = GuardedEngine(_config(tmp_path))

    reply = asyncio.run(engine.reply("private:1", "随便聊两句今天的安排"))

    assert "كيفية" not in reply
    assert "今天" in reply
    assert len(engine.temperatures) == 2
    assert engine.temperatures[0] is None
    assert engine.temperatures[1] is not None
    assert engine.temperatures[1] <= 0.45


def test_language_guard_blocks_traditional_and_variant_characters() -> None:
    # 截图中的真实繁体/异体回复必须被判为语言漂移。
    assert has_illegal_language_or_garbage(
        "戳冋妳啦！誰讓妳壹直戳個沒完，高性能機器人也是會反擊的。"
    )
    assert has_illegal_language_or_garbage(
        "才丕是亂碼，那叫獨特旳字形編碼！本機可是優秀的高性能機器人。"
    )
    # 单个繁体字或异体字也属于违规，fail-closed。
    assert has_illegal_language_or_garbage("妳看")
    assert has_illegal_language_or_garbage("冋去")


def test_language_guard_keeps_normal_simplified() -> None:
    assert not has_illegal_language_or_garbage("你别装睡啦，戳你一下怎么了。")
    assert not has_illegal_language_or_garbage("这才不是乱码呢，我打字一直都这样。")
    # 简体中合法的字不能误报：沈（沈阳）、乾（乾坤）、著（著名）。
    assert not has_illegal_language_or_garbage("沈阳出了本著名的书，乾坤都在里面。")
    # 简繁同形的常用字也不能误报：熟、煮、燃、葡萄、菊、薛。
    assert not has_illegal_language_or_garbage(
        "成熟的薛阿姨把葡萄和菊花煮好，等燃料燃烧起来。"
    )


def test_memory_skips_traditional_reply_before_write(tmp_path: Path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")

    store.observe_user("private:1", "你戳我干嘛", now=1000)
    store.observe_bot("private:1", "妳才亂戳呢，機器人會反擊", now=1001)

    history = store.recent_history("private:1")
    # 用户简体消息保留，繁体机器人回复被判污染、不写入。
    assert len(history) == 1
    assert history[0].get("role") == "user"


def test_reply_retries_traditional_drift_into_simplified(tmp_path: Path) -> None:
    class GuardedEngine(AtriReplyEngine):
        def __init__(self, config: BotConfig) -> None:
            super().__init__(config)
            self.temperatures: list[float | None] = []

        async def _reply_with_api(self, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
            self.temperatures.append(kwargs.get("temperature_override"))
            if len(self.temperatures) == 1:
                return "妳們這些亂碼，機器人也是會反擊的。"
            return "哼，戳我是要付出代价的，我可是高性能的。"

    engine = GuardedEngine(_config(tmp_path))

    reply = asyncio.run(engine.reply("private:1", "戳你一下"))

    assert "亂" not in reply and "妳" not in reply
    assert "高性能" in reply
    assert len(engine.temperatures) == 2
    assert engine.temperatures[1] <= 0.45
