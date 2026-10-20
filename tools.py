"""给模型用的三个工具：记住一件事、调整蛋糕、想起一些事。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

KIND_LABEL = {
    "event": "事情",
    "promise": "约定",
    "preference": "偏好",
    "wound": "心结",
}

NOTE_BASE_DESC = (
    "把一件值得记住的事写进你的小回忆。只记真正有分量的：第一次、约定、"
    "被认真对待或被伤到的时刻、对方反复提到的东西。日常寒暄不要记。"
    "cake 是这件事在你心里的分量：让你开心就 +1~+3，让你难受就 -1~-3，"
    "普通重要但不带情绪就留 0。"
)


def cake_text(cake: int) -> str:
    if cake > 0:
        return "甜 %d" % cake
    if cake < 0:
        return "苦 %d" % abs(cake)
    return "平平"


@dataclass
class MemoryNoteTool(FunctionTool):
    store: Any = field(repr=False, default=None)
    guard: Callable[[], tuple[bool, str]] | None = field(repr=False, default=None)
    after_add: Callable[[dict], None] | None = field(repr=False, default=None)
    resolve_people: Callable[[list[str]], list[str]] | None = field(repr=False, default=None)
    style_hint: str = field(default="")
    name: str = "memory_note"
    description: str = NOTE_BASE_DESC
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "一句话概括，短，例如「第一次一起看烟花」"},
                "body": {"type": "string", "description": "具体发生了什么，写细节，一两句就够"},
                "kind": {
                    "type": "string",
                    "description": "类型：event 事情 / promise 约定 / preference 偏好 / wound 心结",
                },
                "people": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "涉及的人，例如 ['若剑清']",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选标签",
                },
                "cake": {
                    "type": "integer",
                    "description": "蛋糕值 -10~10：正=甜，负=苦，0=普通",
                },
            },
            "required": ["title", "body"],
        }
    )

    def refresh_description(self, style_hint: str) -> None:
        """按「记住的风格」刷新工具说明，让模型知道该松还是该紧。"""
        self.style_hint = style_hint or ""
        self.description = NOTE_BASE_DESC + ((" 记住的风格：" + self.style_hint) if self.style_hint else "")

    async def run(
        self,
        event: AstrMessageEvent,
        title: str,
        body: str,
        kind: str = "event",
        people: list[str] | None = None,
        tags: list[str] | None = None,
        cake: int = 0,
    ) -> str:
        title = (title or "").strip()
        body = (body or "").strip()
        if not title or not body:
            return "错误：title 和 body 都不能为空。"
        if callable(self.guard):
            try:
                ok, why = self.guard()
            except Exception:
                ok, why = True, ""
            if not ok:
                return why or "这条先不记了。"
        kind = (kind or "event").strip().lower()
        if kind not in KIND_LABEL:
            kind = "event"
        try:
            cake = int(cake)
        except Exception:
            cake = 0
        people = people or []
        people_qq: list[str] = []
        if callable(self.resolve_people):
            try:
                people_qq = self.resolve_people(people) or []
            except Exception:
                people_qq = []
        row = self.store.add(
            title=title,
            body=body,
            kind=kind,
            people=people,
            people_qq=people_qq,
            tags=tags or [],
            cake=cake,
        )
        if callable(self.after_add):
            try:
                self.after_add(row)
            except Exception:
                pass
        return (
            f"记住了：{row.get('title')}（{KIND_LABEL.get(row.get('kind'), '事情')}，"
            f"蛋糕 {cake_text(int(row.get('cake') or 0))}）"
        )


@dataclass
class MemoryFeedbackTool(FunctionTool):
    store: Any = field(repr=False, default=None)
    name: str = "memory_feedback"
    description: str = (
        "给某条回忆加或减蛋糕。对方让你开心、把这件事做得更好、或者再次提到它时，"
        "可以加；对方让你难受、让你失望时，可以减。一次只加 1 或减 1，"
        "别一次加很多。蛋糕是悄悄记的，不要把这个数字说出口。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "回忆 id，知道就填"},
                "keyword": {"type": "string", "description": "不知道 id 时用关键词定位"},
                "delta": {"type": "integer", "description": "加/减多少，通常 1 或 -1"},
                "reason": {"type": "string", "description": "为什么加/减，一句话"},
            },
            "required": ["delta"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        delta: int = 1,
        memory_id: str = "",
        keyword: str = "",
        reason: str = "",
    ) -> str:
        try:
            delta = int(delta)
        except Exception:
            delta = 1
        if delta == 0:
            return "错误：delta 不能是 0。"
        delta = max(-3, min(3, delta))

        row = None
        if memory_id:
            row = self.store.get(memory_id)
        if row is None and keyword.strip():
            hits = self.store.search(keyword.strip(), limit=1, min_score=0.8)
            if hits:
                row = hits[0][1]
        if row is None:
            return "没找到这条回忆，可以先用 memory_recall 查一下。"

        updated = self.store.add_cake(str(row["id"]), delta, reason)
        if not updated:
            return "没找到这条回忆。"
        return (
            f"{'加' if delta > 0 else '减'}了 {abs(delta)} 块蛋糕：{updated.get('title')}"
            f"（现在 {cake_text(int(updated.get('cake') or 0))}）"
        )


@dataclass
class MemoryRecallTool(FunctionTool):
    store: Any = field(repr=False, default=None)
    settings: Any = field(repr=False, default=None)
    name: str = "memory_recall"
    description: str = (
        "想起和小回忆有关的事。聊到某个人、某个约定、某件以前发生过的事时，"
        "先查一遍你再开口，比装作记得更真。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "想回忆的关键词或场景"},
                "limit": {"type": "integer", "description": "返回几条，1-5，默认 3"},
            },
            "required": ["query"],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str, limit: int = 3) -> str:
        if not query.strip():
            return "错误：query 不能为空。"
        limit = max(1, min(int(limit or 3), 5))
        min_score = 1.0
        bitter_weight = 0.85
        if self.settings is not None:
            min_score = float(self.settings.data.get("min_score", 1.0))
            bitter_weight = float(self.settings.data.get("bitter_weight", 0.85))
        hits = self.store.search(
            query, limit=limit, min_score=min_score, bitter_weight=bitter_weight
        )
        if not hits:
            return "小回忆里没有相关的。"
        lines = []
        for _score, row in hits:
            cake = int(row.get("cake") or 0)
            kind = KIND_LABEL.get(str(row.get("kind")), "事情")
            lines.append(f"- {row.get('title')}（{kind}｜{cake_text(cake)}）：{row.get('body')}")
            self.store.touch(str(row.get("id")))
        return "\n".join(lines)
