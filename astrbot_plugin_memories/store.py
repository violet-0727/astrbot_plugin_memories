"""小回忆的存储：单文件 JSONL + 字符二元组检索，零第三方依赖。

一条回忆长这样：

    {
      "id": "a1b2c3d4e5f6",
      "title": "第一次一起看烟花",
      "body": "那天风很大，他把外套披在我身上",
      "kind": "event",              # event / promise / preference / wound
      "people": ["若剑清"],
      "tags": ["重要"],
      "cake": 3,                    # 蛋糕值：正=甜，负=苦
      "hits": 2,
      "history": [{"delta": 1, "reason": "他说记得那天", "at": 1792...}],
      "created_at": ..., "updated_at": ..., "last_hit_at": ...
    }
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid

_PUNCT = re.compile(
    r"[\s\u3000,，.。!！?？;；:：、\"'“”‘’()（）\[\]【】<>《》~～…—\-_/\\|*#`]+"
)

CAKE_MIN = -10
CAKE_MAX = 10


def normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def grams(text: str) -> set[str]:
    t = normalize(text)
    if not t:
        return set()
    if len(t) == 1:
        return {t}
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _clamp(value: int) -> int:
    return max(CAKE_MIN, min(CAKE_MAX, int(value)))


def similarity(a: dict, b: dict) -> float:
    """两条回忆的相似度（标题 + 正文的二元组 Jaccard）。"""
    ta = grams(str(a.get("title") or "") + str(a.get("body") or ""))
    tb = grams(str(b.get("title") or "") + str(b.get("body") or ""))
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


class MemoryStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.load()

    # ---------------- 持久化 ----------------

    def load(self) -> None:
        rows: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and obj.get("id"):
                        rows.append(obj)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        with self._lock:
            self._rows = rows

    def _save_locked(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for row in self._rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    # ---------------- 读写 ----------------

    def add(
        self,
        title: str,
        body: str,
        kind: str = "event",
        people: list[str] | None = None,
        tags: list[str] | None = None,
        cake: int = 0,
        dedupe: bool = True,
        happened_at: float | None = None,
        quote: str = "",
        people_qq: list[str] | None = None,
    ) -> dict:
        now = time.time()
        title = (title or "").strip()[:120]
        body = (body or "").strip()
        with self._lock:
            if dedupe:
                for row in self._rows:
                    if title and row.get("title") == title:
                        row["body"] = body or row.get("body", "")
                        row["kind"] = kind or row.get("kind", "event")
                        row["people"] = people or row.get("people") or []
                        row["tags"] = tags or row.get("tags") or []
                        row["cake"] = _clamp(int(row.get("cake") or 0) + int(cake or 0))
                        row["updated_at"] = now
                        self._save_locked()
                        return dict(row)
            row = {
                "id": uuid.uuid4().hex[:12],
                "title": title,
                "body": body,
                "kind": (kind or "event").strip()[:24],
                "people": [str(p).strip() for p in (people or []) if str(p).strip()],
                "people_qq": [str(q).strip() for q in (people_qq or []) if str(q).strip()],
                "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
                "cake": _clamp(cake),
                "happened_at": float(happened_at or 0),  # 0 = 没填，按创建时间算
                "quote": (quote or "").strip()[:300],
                "hits": 0,
                "history": ([{"delta": int(cake), "reason": "初次记住", "at": now}] if cake else []),
                "created_at": now,
                "updated_at": now,
                "last_hit_at": 0,
            }
            self._rows.append(row)
            self._save_locked()
            return dict(row)

    def update(self, memory_id: str, **fields: object) -> dict | None:
        allowed = {
            "title",
            "body",
            "kind",
            "people",
            "people_qq",
            "tags",
            "cake",
            "happened_at",
            "quote",
        }
        with self._lock:
            for row in self._rows:
                if row.get("id") == memory_id:
                    for key, value in fields.items():
                        if key in allowed and value is not None:
                            if key == "cake":
                                value = _clamp(int(value))
                            row[key] = value
                    row["updated_at"] = time.time()
                    self._save_locked()
                    return dict(row)
        return None

    def add_cake(self, memory_id: str, delta: int, reason: str = "") -> dict | None:
        """加/减蛋糕：正面 +，负面 -，同时记一笔流水。"""
        with self._lock:
            for row in self._rows:
                if row.get("id") == memory_id:
                    before = int(row.get("cake") or 0)
                    after = _clamp(before + int(delta))
                    row["cake"] = after
                    row["updated_at"] = time.time()
                    history = list(row.get("history") or [])
                    history.append(
                        {
                            "delta": int(delta),
                            "reason": (reason or "").strip()[:120],
                            "at": time.time(),
                        }
                    )
                    row["history"] = history[-20:]
                    self._save_locked()
                    return dict(row)
        return None

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            before = len(self._rows)
            self._rows = [r for r in self._rows if r.get("id") != memory_id]
            changed = len(self._rows) != before
            if changed:
                self._save_locked()
            return changed

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows]

    def get(self, memory_id: str) -> dict | None:
        for row in self.all():
            if row.get("id") == memory_id:
                return row
        return None

    def stats(self) -> dict:
        rows = self.all()
        sweet = sum(1 for r in rows if int(r.get("cake") or 0) > 0)
        bitter = sum(1 for r in rows if int(r.get("cake") or 0) < 0)
        return {
            "total": len(rows),
            "sweet": sweet,
            "bitter": bitter,
            "neutral": len(rows) - sweet - bitter,
            "path": self.path,
        }

    # ---------------- 检索 ----------------

    def search(self, query: str, limit: int = 3, min_score: float = 1.0, bitter_weight: float = 0.85) -> list[tuple[float, dict]]:
        q = grams(query)
        if not q:
            return []
        qn = math.sqrt(len(q)) or 1.0
        now = time.time()
        scored: list[tuple[float, dict]] = []
        for row in self.all():
            haystack = " ".join(
                [
                    str(row.get("title") or ""),
                    str(row.get("body") or ""),
                    " ".join(str(t) for t in (row.get("tags") or [])),
                    " ".join(str(p) for p in (row.get("people") or [])),
                ]
            )
            ng = grams(haystack)
            if not ng:
                continue
            common = len(q & ng)
            if not common:
                continue
            base = common / qn
            cake = int(row.get("cake") or 0)
            weight = 1.0 + 0.6 * math.log1p(abs(cake))
            if cake < 0:
                weight *= max(0.2, min(1.5, float(bitter_weight)))  # 苦的回忆按配置加权
            last = float(row.get("last_hit_at") or row.get("updated_at") or 0)
            days = max(0.0, (now - last) / 86400.0)
            recency = 1.0 + 0.3 * math.exp(-days / 30.0)
            score = base * weight * recency
            if grams(str(row.get("title") or "")) & q:
                score += 1.0
            if score >= min_score:
                scored.append((score, dict(row)))
        scored.sort(key=lambda item: (-item[0], -int(item[1].get("cake") or 0)))
        return scored[: max(1, limit)]

    # ---------------- 维护用 ----------------

    def count_since(self, ts: float) -> int:
        """从某个时间点之后新建了几条。"""
        return sum(1 for r in self.all() if float(r.get("created_at") or 0) >= ts)

    def last_created_at(self) -> float:
        rows = self.all()
        if not rows:
            return 0.0
        return max(float(r.get("created_at") or 0) for r in rows)

    def forget_candidates(self, max_cake: int, older_than_days: float) -> list[dict]:
        """长期没被想起、且情绪分量不重的回忆，才有资格被忘掉。"""
        now = time.time()
        out = []
        for row in self.all():
            cake = int(row.get("cake") or 0)
            if abs(cake) > max_cake:
                continue
            last = float(
                row.get("last_hit_at") or row.get("updated_at") or row.get("created_at") or 0
            )
            if (now - last) / 86400.0 >= older_than_days:
                out.append(row)
        return out

    def merge(
        self,
        keep_id: str,
        drop_ids: list[str],
        body: str | None = None,
        cake: int | None = None,
        title: str | None = None,
    ) -> dict | None:
        """把几条回忆并成一条：留 keep，删 drop，可选替换标题/正文/蛋糕。"""
        with self._lock:
            keep = next((r for r in self._rows if r.get("id") == keep_id), None)
            if keep is None:
                return None
            if title:
                keep["title"] = title
            if body:
                keep["body"] = body
            if cake is not None:
                keep["cake"] = _clamp(int(cake))
            keep["updated_at"] = time.time()
            drop = {i for i in drop_ids if i and i != keep_id}
            if drop:
                self._rows = [r for r in self._rows if r.get("id") not in drop]
            self._save_locked()
            return dict(keep)

    # ---------------- 时间 / 人物 ----------------

    @staticmethod
    def happened_at(row: dict) -> float:
        return float(row.get("happened_at") or row.get("created_at") or 0)

    def anniversaries(self, window_days: int = 7, today: "date | None" = None) -> list[dict]:
        """把「月-日」落在今天前后 window_days 天内的回忆挑出来，按临近排序。"""
        from datetime import date as _date

        today = today or _date.today()
        import time as _time

        out = []
        for row in self.all():
            explicit = float(row.get("happened_at") or 0)
            ts = explicit or self.happened_at(row)
            if not ts:
                continue
            # 没填日期的近期记录不算纪念日，免得「今天刚记的」全变成纪念日
            if not explicit and (_time.time() - ts) < 90 * 86400:
                continue
            try:
                when = _date.fromtimestamp(ts)
            except Exception:
                continue
            delta = self._md_delta(when, today)
            if abs(delta) > window_days:
                continue
            years = today.year - when.year
            if delta > 0:
                years += 1
            out.append(
                {
                    "row": row,
                    "delta": delta,
                    "years": max(0, years),
                    "date": when.strftime("%m-%d"),
                    "year": when.year,
                }
            )
        out.sort(key=lambda x: (abs(x["delta"]), -abs(int(x["row"].get("cake") or 0))))
        return out

    @staticmethod
    def _md_delta(when, today) -> int:
        """距离今年这个「月-日」还有几天；0 = 今天，负数 = 今年已过。"""
        from datetime import date as _date

        try:
            this_year = when.replace(year=today.year)
        except ValueError:  # 2 月 29
            this_year = when.replace(year=today.year, day=28)
        delta = (this_year - today).days
        if delta < -180:
            try:
                nxt = when.replace(year=today.year + 1)
            except ValueError:
                nxt = when.replace(year=today.year + 1, day=28)
            delta = (nxt - today).days
        return delta

    def by_people(self, names: list[str], limit: int = 3) -> list[dict]:
        """按人名找回记忆，甜的优先。"""
        wanted = {str(n).strip() for n in names if str(n).strip()}
        if not wanted:
            return []
        rows = []
        for row in self.all():
            people = {str(p).strip() for p in (row.get("people") or [])}
            if people & wanted:
                rows.append(row)
        rows.sort(key=lambda r: (-int(r.get("cake") or 0), -self.happened_at(r)))
        return rows[: max(1, limit)]

    def by_qq(self, qq: str, limit: int = 50) -> list[dict]:
        """按 QQ 号找回这个人的回忆。"""
        qq = str(qq or "").strip()
        if not qq:
            return []
        rows = [r for r in self.all() if qq in {str(x) for x in (r.get("people_qq") or [])}]
        rows.sort(key=lambda r: (-int(r.get("cake") or 0), -self.happened_at(r)))
        return rows[: max(1, limit)]

    def pick_random(self, exclude_ids: set[str] | None = None, min_abs_cake: int = 0) -> dict | None:
        """按蛋糕分量加权随机挑一条，用于「忽然想起」。"""
        import random as _random

        exclude = exclude_ids or set()
        pool = [r for r in self.all() if r.get("id") not in exclude and abs(int(r.get("cake") or 0)) >= min_abs_cake]
        if not pool:
            return None
        weights = [1.0 + 0.8 * abs(int(r.get("cake") or 0)) for r in pool]
        return _random.choices(pool, weights=weights, k=1)[0]

    def touch(self, memory_id: str) -> None:
        with self._lock:
            for row in self._rows:
                if row.get("id") == memory_id:
                    row["hits"] = int(row.get("hits") or 0) + 1
                    row["last_hit_at"] = time.time()
                    return
