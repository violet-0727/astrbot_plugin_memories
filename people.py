"""人物登记簿：QQ 号 ↔ 名字，用来保证「这条回忆是跟谁发生的」。

数据在 data/plugin_data/astrbot_plugin_memories/people.json：

    {"people": [
      {"qq": "2799902706", "name": "若剑清", "aliases": [], "note": "",
       "first_seen": 1792..., "last_seen": 1792...}
    ]}
"""

from __future__ import annotations

import json
import os
import time


class PeopleRegistry:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._rows: list[dict] = []
        self._dirty = False
        self.load()

    # ---------------- 持久化 ----------------

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("people") if isinstance(data, dict) else data
            self._rows = [r for r in (rows or []) if isinstance(r, dict) and str(r.get("qq") or "").strip()]
            for row in self._rows:
                row["qq"] = str(row.get("qq")).strip()
                row["name"] = str(row.get("name") or "").strip()
                row["aliases"] = [str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()]
        except FileNotFoundError:
            self._rows = []
        except Exception:
            self._rows = []

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"people": self._rows}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._dirty = False

    # ---------------- 读写 ----------------

    def get(self, qq: str) -> dict | None:
        qq = str(qq or "").strip()
        for row in self._rows:
            if row.get("qq") == qq:
                return row
        return None

    def upsert(self, qq: str, name: str = "") -> dict | None:
        qq = str(qq or "").strip()
        name = str(name or "").strip()
        if not qq:
            return None
        row = self.get(qq)
        if row is None:
            row = {
                "qq": qq,
                "name": name,
                "aliases": [],
                "note": "",
                "first_seen": time.time(),
                "last_seen": time.time(),
            }
            self._rows.append(row)
            self.save()
            return row
        changed = False
        if name and row.get("name") != name:
            old = str(row.get("name") or "").strip()
            if old and old not in row["aliases"]:
                row["aliases"].append(old)
            row["name"] = name
            changed = True
        if time.time() - float(row.get("last_seen") or 0) > 600:
            row["last_seen"] = time.time()
            changed = True
        if changed:
            self.save()
        return row

    def rename(self, qq: str, name: str) -> dict | None:
        row = self.get(qq)
        if row is None:
            return None
        name = str(name or "").strip()
        if name and row.get("name") != name:
            old = str(row.get("name") or "").strip()
            if old and old not in row["aliases"]:
                row["aliases"].append(old)
            row["name"] = name
            self.save()
        return row

    def set_note(self, qq: str, note: str) -> dict | None:
        row = self.get(qq)
        if row is None:
            return None
        row["note"] = str(note or "").strip()[:200]
        self.save()
        return row

    def all(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    # ---------------- 反查 ----------------

    def resolve_name(self, name: str) -> str:
        """名字 → QQ 号，找不到返回空串。"""
        target = str(name or "").strip()
        if not target:
            return ""
        for row in self._rows:
            if row.get("name") == target:
                return row["qq"]
            if target in (row.get("aliases") or []):
                return row["qq"]
        return ""

    def resolve_names(self, names: list[str] | None) -> list[str]:
        out = []
        for name in names or []:
            qq = self.resolve_name(name)
            if qq and qq not in out:
                out.append(qq)
        return out
