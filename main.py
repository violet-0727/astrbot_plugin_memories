"""她自己的小回忆（astrbot_plugin_memories）

独立的「事件记忆 + 蛋糕反馈」插件：不依赖、不修改任何其它插件。

* memory_note     记住一件有分量的事
* memory_feedback 给某条回忆加/减蛋糕（正负反馈）
* memory_recall   想起来

阈值（记住的风格 / 频率 / 遗忘 / 整合）存在插件自己的 settings.json，
面板「回忆阈值」页可以直接调。
"""

from __future__ import annotations

import datetime
import json
import os
import random
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .people import PeopleRegistry
from .settings import DEFAULTS, STYLE_LABEL, Settings
from .store import MemoryStore, similarity
from .tools import (
    KIND_LABEL,
    MemoryFeedbackTool,
    MemoryNoteTool,
    MemoryRecallTool,
    cake_text,
)

PLUGIN_NAME = "astrbot_plugin_memories"
API_PREFIX = f"/{PLUGIN_NAME}"

try:
    from quart import request
except Exception:  # pragma: no cover
    request = None


@register(
    PLUGIN_NAME,
    "若剑清",
    "她自己的小回忆：记住有分量的事件，用蛋糕记住正负反馈，聊到时会自己想起来。",
    "0.5.1",
    "",
)
class MemoriesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        self.data_dir = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        self.store = MemoryStore(os.path.join(self.data_dir, "memories.jsonl"))
        self.settings = Settings(os.path.join(self.data_dir, "settings.json"))
        self.people = PeopleRegistry(os.path.join(self.data_dir, "people.json"))
        self.state_path = os.path.join(self.data_dir, "state.json")
        self.state = self._load_state()

        self.enabled = self._bool("enabled", True)
        self.inject_enabled = self._bool("inject_enabled", True)

        self.note_tool = MemoryNoteTool(
            store=self.store,
            guard=self._record_guard,
            after_add=self._after_add,
            resolve_people=self.people.resolve_names,
        )
        self.note_tool.refresh_description(self.settings.style_hint)
        self.tools: list[Any] = [
            self.note_tool,
            MemoryFeedbackTool(store=self.store),
            MemoryRecallTool(store=self.store, settings=self.settings),
        ]
        self.context.add_llm_tools(*self.tools)
        self._register_web_apis()

        # 启动时做一次维护（不会重复跑：一天最多一次）
        try:
            self._maintain(force=False)
        except Exception as exc:
            logger.debug("[Memories] 启动维护失败: %s", exc)

        logger.info(
            "[Memories] 小回忆加载完成：%s 条，风格=%s，数据路径 %s",
            self.store.stats().get("total"),
            self.settings.style,
            self.store.path,
        )

    # ---------------- 基础 ----------------

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _today_start(self) -> float:
        now = datetime.datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # ---------------- 阈值：频率闸门 ----------------

    def _record_guard(self) -> tuple[bool, str]:
        s = self.settings.data
        max_per_day = int(s.get("max_records_per_day") or 0)
        if max_per_day > 0 and self.store.count_since(self._today_start()) >= max_per_day:
            return False, "今天记的已经够多了，这条先不记，等明天吧。"
        gap = int(s.get("min_gap_minutes") or 0)
        if gap > 0:
            last = self.store.last_created_at()
            if last and (time.time() - last) < gap * 60:
                return False, f"刚记过一条，{gap} 分钟内先不记新的。"
        return True, ""

    def _after_add(self, row: dict) -> None:
        self._maintain(force=False, focus=row)

    # ---------------- 阈值：遗忘 + 整合 ----------------

    def _maintain(self, force: bool = False, focus: dict | None = None) -> dict:
        result = {"forgotten": 0, "merged": 0}
        now = time.time()
        state = self.state
        last = float(state.get("last_maintain_at") or 0)
        if not force and now - last < 86400:
            return result

        s = self.settings.data
        # 遗忘
        if s.get("forgetting_enabled"):
            try:
                cands = self.store.forget_candidates(
                    int(s.get("forget_max_cake") or 0), float(s.get("forget_after_days") or 30)
                )
                prob = float(s.get("forget_probability") or 0)
                for row in cands:
                    if random.random() < prob and self.store.delete(str(row.get("id"))):
                        result["forgotten"] += 1
            except Exception as exc:
                logger.debug("[Memories] 遗忘失败: %s", exc)

        # 整合
        if s.get("consolidate_enabled"):
            try:
                result["merged"] = self._consolidate(
                    float(s.get("consolidate_threshold") or 0.72),
                    int(s.get("consolidate_max") or 3),
                    focus,
                )
            except Exception as exc:
                logger.debug("[Memories] 整合失败: %s", exc)

        state["last_maintain_at"] = now
        self._save_state()
        return result

    def _consolidate(self, threshold: float, cap: int, focus: dict | None = None) -> int:
        rows = self.store.all()
        if len(rows) < 2:
            return 0
        if focus is not None:
            # 优先把新记的和已有的比，避免每次全表两两比
            others = [r for r in rows if r.get("id") != focus.get("id")]
            pool = [focus] + others[:120]
        else:
            pool = rows[:150]

        merged = 0
        used: set[str] = set()
        for i, a in enumerate(pool):
            if merged >= cap:
                break
            if a.get("id") in used:
                continue
            for b in pool[i + 1 :]:
                if merged >= cap:
                    break
                if b.get("id") in used:
                    continue
                try:
                    sim = similarity(a, b)
                except Exception:
                    sim = 0.0
                if sim < threshold:
                    continue
                body_a = str(a.get("body") or "").strip()
                body_b = str(b.get("body") or "").strip()
                body = body_a if body_b in body_a else (body_a + "\n" + body_b).strip()
                cake = int(a.get("cake") or 0) + int(b.get("cake") or 0)
                keep = self.store.merge(
                    str(a["id"]),
                    [str(b["id"])],
                    body=body,
                    cake=cake,
                    title=str(a.get("title") or b.get("title") or ""),
                )
                if keep:
                    merged += 1
                    used.add(str(b["id"]))
                    a = keep
        return merged

    # ---------------- 面板 API ----------------

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.info("[Memories] 当前 AstrBot 版本不支持插件 Web API，跳过面板注册")
            return
        routes = [
            ("/list", self.api_list, ["GET"], "列出回忆"),
            ("/stats", self.api_stats, ["GET"], "统计"),
            ("/save", self.api_save, ["POST"], "新增或更新"),
            ("/delete", self.api_delete, ["POST"], "删除"),
            ("/cake", self.api_cake, ["POST"], "加/减蛋糕"),
            ("/anniversaries", self.api_anniversaries, ["GET"], "纪念日"),
            ("/people", self.api_people, ["GET"], "人物列表"),
            ("/people/update", self.api_people_update, ["POST"], "更新人物信息"),
            ("/settings", self.api_settings, ["GET"], "读取阈值"),
            ("/settings/save", self.api_settings_save, ["POST"], "保存阈值"),
            ("/maintain", self.api_maintain, ["POST"], "立即遗忘+整合"),
        ]
        for path, handler, methods, desc in routes:
            try:
                register(f"{API_PREFIX}{path}", handler, methods, desc)
            except Exception as exc:
                logger.warning("[Memories] 注册面板 API 失败 %s: %s", path, exc)

    async def api_list(self):
        items = self.store.all()
        items.sort(
            key=lambda r: (int(r.get("cake") or 0), float(r.get("updated_at") or 0)),
            reverse=True,
        )
        for row in items:
            row["cake_text"] = cake_text(int(row.get("cake") or 0))
            row["kind_label"] = KIND_LABEL.get(str(row.get("kind")), "事情")
        return {"ok": True, "items": items, "stats": self.store.stats()}

    async def api_stats(self):
        return {"ok": True, "stats": self.store.stats()}

    async def api_save(self):
        payload = await self._json_body()
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not title or not body:
            return {"ok": False, "error": "标题和正文都不能为空"}
        kind = str(payload.get("kind") or "event").strip().lower()
        if kind not in KIND_LABEL:
            kind = "event"
        people = payload.get("people") or []
        tags = payload.get("tags") or []
        try:
            cake = int(payload.get("cake") or 0)
        except Exception:
            cake = 0
        happened_at = payload.get("happened_at")
        try:
            happened_at = float(happened_at) if happened_at else None
        except Exception:
            happened_at = None
        quote = str(payload.get("quote") or "").strip()

        people_qq = self.people.resolve_names([str(p).strip() for p in people if str(p).strip()])
        memory_id = str(payload.get("id") or "").strip()
        if memory_id:
            fields = {
                "title": title,
                "body": body,
                "kind": kind,
                "people": [str(p).strip() for p in people if str(p).strip()],
                "people_qq": people_qq,
                "tags": [str(t).strip() for t in tags if str(t).strip()],
                "cake": cake,
                "quote": quote,
            }
            if happened_at:
                fields["happened_at"] = happened_at
            updated = self.store.update(memory_id, **fields)
            if not updated:
                return {"ok": False, "error": "没找到这条回忆"}
            return {"ok": True, "memory": updated}
        row = self.store.add(
            title=title,
            body=body,
            kind=kind,
            people=[str(p).strip() for p in people if str(p).strip()],
            people_qq=people_qq,
            tags=[str(t).strip() for t in tags if str(t).strip()],
            cake=cake,
            happened_at=happened_at,
            quote=quote,
        )
        return {"ok": True, "memory": row}

    async def api_anniversaries(self):
        s = self.settings.data
        window = int(s.get("anniversary_window_days") or 7)
        items = []
        for item in self.store.anniversaries(window):
            row = item["row"]
            items.append(
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "body": row.get("body"),
                    "quote": row.get("quote") or "",
                    "cake": row.get("cake"),
                    "kind": row.get("kind"),
                    "delta": item["delta"],
                    "years": item["years"],
                    "date": item["date"],
                    "year": item["year"],
                    "happened_at": self.store.happened_at(row),
                }
            )
        return {"ok": True, "items": items, "window": window}

    async def api_people(self):
        reg = self.people.all()
        rows = self.store.all()
        buckets: dict[str, list[dict]] = {p["qq"]: [] for p in reg}
        unknown: dict[str, int] = {}
        for row in rows:
            qqs = {str(x) for x in (row.get("people_qq") or [])}
            if not qqs:
                for name in row.get("people") or []:
                    name = str(name).strip()
                    if not name:
                        continue
                    qq = self.people.resolve_name(name)
                    if qq:
                        qqs.add(qq)
                    else:
                        unknown[name] = unknown.get(name, 0) + 1
            for qq in qqs:
                buckets.setdefault(qq, []).append(row)

        people = []
        for p in reg:
            items = buckets.get(p["qq"], [])
            items.sort(key=lambda r: -float(self.store.happened_at(r)))
            people.append(
                {
                    "qq": p["qq"],
                    "name": p.get("name") or p["qq"],
                    "note": p.get("note") or "",
                    "aliases": p.get("aliases") or [],
                    "total": len(items),
                    "sweet": sum(1 for r in items if int(r.get("cake") or 0) > 0),
                    "bitter": sum(1 for r in items if int(r.get("cake") or 0) < 0),
                    "last_seen": p.get("last_seen") or 0,
                    "memories": [
                        {
                            "id": r.get("id"),
                            "title": r.get("title"),
                            "cake": r.get("cake"),
                            "kind": r.get("kind"),
                            "happened_at": self.store.happened_at(r),
                        }
                        for r in items[:20]
                    ],
                }
            )
        people.sort(key=lambda p: (-p["total"], -float(p["last_seen"] or 0)))
        return {"ok": True, "people": people, "unknown": unknown}

    async def api_people_update(self):
        payload = await self._json_body()
        qq = str(payload.get("qq") or "").strip()
        if not qq:
            return {"ok": False, "error": "缺少 qq"}
        if "name" in payload:
            self.people.rename(qq, str(payload.get("name") or ""))
        if "note" in payload:
            self.people.set_note(qq, str(payload.get("note") or ""))
        return {"ok": True, "person": self.people.get(qq)}

    async def api_delete(self):
        payload = await self._json_body()
        memory_id = str(payload.get("id") or "").strip()
        if not memory_id:
            return {"ok": False, "error": "缺少 id"}
        return {"ok": self.store.delete(memory_id)}

    async def api_cake(self):
        payload = await self._json_body()
        memory_id = str(payload.get("id") or "").strip()
        try:
            delta = int(payload.get("delta") or 0)
        except Exception:
            delta = 0
        if not memory_id or delta == 0:
            return {"ok": False, "error": "缺少 id 或 delta"}
        updated = self.store.add_cake(
            memory_id, max(-3, min(3, delta)), str(payload.get("reason") or "面板调整")
        )
        if not updated:
            return {"ok": False, "error": "没找到这条回忆"}
        return {"ok": True, "memory": updated}

    async def api_settings(self):
        return {
            "ok": True,
            "settings": self.settings.data,
            "style_label": STYLE_LABEL.get(self.settings.style, "平衡"),
            "defaults": DEFAULTS,
        }

    async def api_settings_save(self):
        payload = await self._json_body()
        data = self.settings.update(payload)
        self.note_tool.refresh_description(self.settings.style_hint)
        return {"ok": True, "settings": data, "style_label": STYLE_LABEL.get(self.settings.style, "平衡")}

    async def api_maintain(self):
        result = self._maintain(force=True)
        return {"ok": True, "result": result, "stats": self.store.stats()}

    async def _json_body(self) -> dict:
        if request is None:
            return {}
        try:
            payload = await request.get_json(silent=True)
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else {}

    # ---------------- 自动注入 ----------------

    def _known_people(self) -> list[str]:
        names = set()
        for row in self.store.all():
            for p in row.get("people") or []:
                name = str(p).strip()
                if name:
                    names.add(name)
        return sorted(names, key=len, reverse=True)

    @filter.on_llm_request()
    async def inject_memories(self, event: AstrMessageEvent, req: Any) -> None:
        if not self.enabled or not self.inject_enabled:
            return
        text = (getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith(("/回忆", "回忆 ")):
            return

        s = self.settings.data
        limit = max(1, min(int(s.get("inject_limit") or 2), 5))
        picked: list[tuple[str, dict]] = []
        seen: set[str] = set()

        def take(rows: list[dict], tag: str) -> None:
            for row in rows:
                if len(picked) >= limit:
                    return
                rid = str(row.get("id"))
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                picked.append((tag, row))

        # 1) 今天就是纪念日
        if s.get("anniversary_enabled"):
            try:
                for item in self.store.anniversaries(int(s.get("anniversary_window_days") or 7)):
                    if item["delta"] == 0:
                        take([item["row"]], "today")
            except Exception as exc:
                logger.debug("[Memories] 纪念日匹配失败: %s", exc)

        # 2) 说话的人自己
        if s.get("people_recall_enabled"):
            try:
                sender_qq = str(event.get_sender_id() or "").strip()
                if sender_qq:
                    take(self.store.by_qq(sender_qq, limit=1), "person")
            except Exception as exc:
                logger.debug("[Memories] 按人召回失败: %s", exc)

        # 3) 提到某个人
        if s.get("people_recall_enabled"):
            try:
                names = [n for n in self._known_people() if n and n in text]
                if names:
                    take(self.store.by_people(names, limit=1), "people")
            except Exception as exc:
                logger.debug("[Memories] 人物召回失败: %s", exc)

        # 3) 关键词命中
        try:
            hits = self.store.search(
                text,
                limit=limit,
                min_score=float(s.get("min_score") or 1.0),
                bitter_weight=float(s.get("bitter_weight") or 0.85),
            )
            take([row for _score, row in hits], "match")
        except Exception as exc:
            logger.debug("[Memories] 检索失败: %s", exc)

        # 4) 什么都没命中时，偶尔「忽然想起」一件事（每天最多一次）
        if not picked and s.get("proactive_recall_enabled"):
            try:
                today_key = datetime.datetime.now().strftime("%Y-%m-%d")
                if (
                    self.state.get("proactive_day") != today_key
                    and random.random() < float(s.get("proactive_recall_probability") or 0)
                ):
                    row = self.store.pick_random()
                    if row:
                        self.state["proactive_day"] = today_key
                        self._save_state()
                        picked.append(("drift", row))
            except Exception as exc:
                logger.debug("[Memories] 主动想起失败: %s", exc)

        if not picked:
            return

        lines = ["【你心里记着的一些事（你自己的小回忆）】"]
        for tag, row in picked:
            cake = int(row.get("cake") or 0)
            kind = KIND_LABEL.get(str(row.get("kind")), "事情")
            body = str(row.get("body") or "").strip()
            if len(body) > 220:
                body = body[:220] + "…"
            extra = ""
            if tag == "today":
                extra = "（今天正好是它的纪念日）"
            elif tag == "drift":
                extra = "（你刚才忽然想起这件事）"
            lines.append(f"- {row.get('title')}（{cake_text(cake)}｜{kind}）{extra}：{body}")
        if not s.get("say_cake"):
            lines.append("蛋糕是你悄悄记的分量，不要把这个数字说出来，用态度表现就好。")
        block = "\n".join(lines)
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            # 只对本次请求可见：mark_as_temp() 标记后不落历史，
            # 也不碰 req.system_prompt —— 避免每轮改写系统提示词击穿前缀缓存。
            parts.append(TextPart(text=block).mark_as_temp())
        except Exception as exc:
            logger.debug("[Memories] 记忆块注入失败: %s", exc)
            return
        for _tag, row in picked:
            self.store.touch(str(row.get("id")))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_sender(self, event: AstrMessageEvent):
        """把发消息的人记进登记簿，保证回忆能对上 QQ。"""
        try:
            qq = str(event.get_sender_id() or "").strip()
            name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
            self_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
            if qq and name and qq != self_id:
                self.people.upsert(qq, name)
        except Exception:
            pass

    # ---------------- 管理命令 ----------------

    @filter.command("回忆")
    async def memories_command(self, event: AstrMessageEvent):
        """管理小回忆：回忆 / 回忆 甜 / 回忆 苦 / 回忆 搜 关键词 / 回忆 整理"""
        args = (getattr(event, "message_str", "") or "").strip()
        for prefix in ("/回忆", "回忆"):
            if args.startswith(prefix):
                args = args[len(prefix) :].strip()
                break
        action, _, rest = args.partition(" ")
        rest = rest.strip()
        stats = self.store.stats()

        if action in ("", "统计", "状态"):
            s = self.settings.data
            yield event.plain_result(
                f"小回忆共 {stats['total']} 条｜甜 {stats['sweet']}、苦 {stats['bitter']}、平平 {stats['neutral']}\n"
                f"风格 {STYLE_LABEL.get(self.settings.style, '平衡')}｜每天最多 {s.get('max_records_per_day')} 条"
                f"｜遗忘 {'开' if s.get('forgetting_enabled') else '关'}"
                f"｜整合 {'开' if s.get('consolidate_enabled') else '关'}\n"
                "用法：回忆 甜 / 苦 / 搜 关键词 / 删 id / 整理"
            )
            return

        if action in ("甜", "苦"):
            rows = [
                r for r in self.store.all() if (int(r.get("cake") or 0) > 0) == (action == "甜")
            ]
            rows.sort(key=lambda r: abs(int(r.get("cake") or 0)), reverse=True)
            if not rows:
                yield event.plain_result("还没有这一类的回忆。")
                return
            lines = [
                f"[{r.get('id')}] {r.get('title')}（{cake_text(int(r.get('cake') or 0))}）"
                for r in rows[:8]
            ]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("搜", "搜索", "找"):
            if not rest:
                yield event.plain_result("要搜什么？例：回忆 搜 烟花")
                return
            hits = self.store.search(rest, limit=5)
            if not hits:
                yield event.plain_result("没找到相关的回忆。")
                return
            lines = []
            for score, row in hits:
                lines.append(
                    f"[{row.get('id')}] {row.get('title')}（{cake_text(int(row.get('cake') or 0))}，{score:.1f}）\n"
                    f"  {str(row.get('body'))[:160]}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("删", "删除"):
            if not rest:
                yield event.plain_result("要删哪条？例：回忆 删 3f2a1b9c")
                return
            ok = self.store.delete(rest)
            yield event.plain_result("已删掉。" if ok else "没找到这个 id。")
            return

        if action in ("纪念日", "纪念"):
            window = int(self.settings.data.get("anniversary_window_days") or 7)
            items = self.store.anniversaries(window)
            if not items:
                yield event.plain_result(f"最近 {window} 天没有纪念日。")
                return
            lines = []
            for item in items[:10]:
                row = item["row"]
                mark = "今天" if item["delta"] == 0 else (
                    f"{item['delta']} 天后" if item["delta"] > 0 else f"{-item['delta']} 天前"
                )
                lines.append(
                    f"{item['date']}（{mark}，第 {item['years']} 年）"
                    f" {row.get('title')}（{cake_text(int(row.get('cake') or 0))}）"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("整理", "维护", "遗忘"):
            result = self._maintain(force=True)
            yield event.plain_result(
                f"整理完成：忘掉 {result['forgotten']} 条，合并 {result['merged']} 条。"
            )
            return

        yield event.plain_result("用法：回忆 甜 / 苦 / 搜 关键词 / 删 id / 整理")

    async def terminate(self) -> None:
        for tool in getattr(self, "tools", []):
            try:
                self.context.provider_manager.llm_tools.remove_func(tool.name)
            except Exception:
                pass
        logger.info("[Memories] 小回忆插件已停止。")
