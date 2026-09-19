# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""스터디 라이브러리 (Zotero 스타일): 즐겨찾기 · 컬렉션 · 메모 · 태그

저장: ~/Library/Application Support/DabbaView/library.json (Windows: %APPDATA%/DabbaView)
  studies     StudyInstanceUID → 환자명·ID·검사일·설명·모달리티·폴더·메모·태그·소속 컬렉션
  collections [{id, name, parent, order}]  (트리, 한 스터디가 여러 컬렉션에 속할 수 있음)
  tags        [{name, color}]  +  quick_tags (빠른 태그 버튼)
메모 본문의 #태그는 자동으로 스터디 태그가 됨
"""
import datetime
import html
import json
import os
import re
import sys
import uuid

from PyQt5.QtCore import QObject, pyqtSignal

FORMAT = "dabbaview-library"
VERSION = 1
TAG_COLORS = ["#e5484d", "#f76b15", "#ffc53d", "#46a758", "#12a594", "#0090ff",
              "#8e4ec6", "#d6409f", "#978365", "#6e56cf"]
DEFAULT_QUICK_TAGS = ["interesting", "teaching", "followup"]
_HASHTAG = re.compile(r"(?<![\w&])#([\w가-힣][\w가-힣\-]*)")


def library_path():
    override = os.environ.get("DABBAVIEW_LIBRARY")
    if override:
        return override
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/DabbaView")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "DabbaView")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                            "DabbaView")
    return os.path.join(base, "library.json")


def normalize_tag(tag):
    return str(tag).strip().lstrip("#").strip().lower()


def hashtags(text):
    return sorted({normalize_tag(t) for t in _HASHTAG.findall(text or "")})


def study_info(series_list, study_uid):
    """불러온 시리즈들에서 스터디 정보 (폴더 = 그 스터디 파일들의 공통 폴더)"""
    members = [s for s in series_list if (s.study_uid or "") == study_uid]
    if not members:
        return None
    first = members[0]
    files = []
    for s in members:
        for ds in s.slices:
            name = getattr(ds, "filename", None)
            if isinstance(name, str) and name:
                files.append(name)
    folder, dirs = "", []
    if files:
        dirs = sorted({os.path.dirname(f) for f in files})
        try:
            folder = os.path.commonpath(dirs)
        except ValueError:
            folder = dirs[0]
    # 시리즈가 형제 폴더에 흩어져 있으면 공통 상위 폴더에 다른 검사도 섞여 있을 수 있으므로
    # 열 때는 이 스터디의 폴더들만 불러옴 (너무 많으면 공통 폴더)
    members.sort(key=lambda s: (s.series_number is None, s.series_number or 0))
    return {
        "study_uid": study_uid, "patient_name": first.patient_name, "patient_id": first.patient_id,
        "study_date": first.study_date, "description": first.study_description,
        "modalities": sorted({s.modality for s in members if s.modality}),
        "series": len(members), "images": sum(s.num_slices for s in members),
        "folder": folder, "paths": dirs if 0 < len(dirs) <= 50 else [folder],
        "first_series_uid": members[0].series_uid,
    }


class LibraryStore(QObject):
    changed = pyqtSignal()

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self.path = path or library_path()
        self.studies = {}
        self.collections = []
        self.tags = []
        self.quick_tags = list(DEFAULT_QUICK_TAGS)
        self.load_error = None
        self.load()

    # ─── 파일 ───
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("format") != FORMAT:
                raise ValueError("DabbaView 라이브러리 파일이 아닙니다.")
            self._set(data)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.load_error = str(e)   # 손상된 파일은 덮어쓰지 않도록 백업
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass

    def _set(self, data):
        self.studies = {uid: dict(s) for uid, s in (data.get("studies") or {}).items()}
        self.collections = [dict(c) for c in data.get("collections") or []]
        self.tags = [dict(t) for t in data.get("tags") or []]
        self.quick_tags = list(data.get("quick_tags") or DEFAULT_QUICK_TAGS)

    def to_dict(self):
        return {"format": FORMAT, "version": VERSION,
                "saved": datetime.datetime.now().isoformat(timespec="seconds"),
                "studies": self.studies, "collections": self.collections,
                "tags": self.tags, "quick_tags": self.quick_tags}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)   # 저장 중 종료돼도 기존 파일은 온전

    def _changed(self):
        self.save()
        self.changed.emit()

    # ─── 스터디 ───
    def add_study(self, info):
        """(항목, 새로 추가됐는지). 이미 있으면 정보(폴더 등)만 갱신"""
        uid = info["study_uid"]
        entry = self.studies.get(uid)
        created = entry is None
        if created:
            entry = {"added": datetime.datetime.now().isoformat(timespec="seconds"),
                     "tags": [], "collections": [], "note_html": "", "note_text": ""}
            self.studies[uid] = entry
        if created:
            entry.update(info)   # 새 항목은 빈 값도 그대로 (필드가 항상 있게)
        else:                    # 기존 항목은 새로 알게 된 값만 덮어씀
            entry.update({k: v for k, v in info.items() if v not in (None, "")})
        self._changed()
        return entry, created

    def remove_study(self, uid):
        if self.studies.pop(uid, None) is not None:
            self._changed()

    def get(self, uid):
        return self.studies.get(uid)

    # ─── 컬렉션 ───
    def collection(self, cid):
        return next((c for c in self.collections if c["id"] == cid), None)

    def children(self, parent=None):
        return sorted((c for c in self.collections if c.get("parent") == parent),
                      key=lambda c: (c.get("order", 0), c["name"].lower()))

    def descendants(self, cid):
        out, stack = [], [cid]
        while stack:
            current = stack.pop()
            out.append(current)
            stack += [c["id"] for c in self.collections if c.get("parent") == current]
        return out

    def add_collection(self, name, parent=None):
        siblings = self.children(parent)
        c = {"id": uuid.uuid4().hex[:10], "name": name.strip() or "새 컬렉션", "parent": parent,
             "order": (max((s.get("order", 0) for s in siblings), default=-1) + 1)}
        self.collections.append(c)
        self._changed()
        return c

    def rename_collection(self, cid, name):
        c = self.collection(cid)
        if c and name.strip():
            c["name"] = name.strip()
            self._changed()

    def delete_collection(self, cid):
        """하위 컬렉션까지 삭제. 스터디는 라이브러리에 남고 소속만 빠짐"""
        gone = set(self.descendants(cid))
        self.collections = [c for c in self.collections if c["id"] not in gone]
        for s in self.studies.values():
            s["collections"] = [c for c in s.get("collections", []) if c not in gone]
        self._changed()

    def move_collection(self, cid, delta):
        c = self.collection(cid)
        if c is None:
            return
        siblings = self.children(c.get("parent"))
        i = siblings.index(c)
        j = max(0, min(len(siblings) - 1, i + delta))
        if i == j:
            return
        siblings.insert(j, siblings.pop(i))
        for order, s in enumerate(siblings):
            s["order"] = order
        self._changed()

    def set_parent(self, cid, parent):
        c = self.collection(cid)
        if c is None or parent in self.descendants(cid):
            return False   # 자기 자신·하위로는 못 옮김
        c["parent"] = parent
        c["order"] = len(self.children(parent))
        self._changed()
        return True

    def add_to_collection(self, uids, cid):
        n = 0
        for uid in uids:
            s = self.studies.get(uid)
            if s is not None and cid not in s.setdefault("collections", []):
                s["collections"].append(cid)
                n += 1
        if n:
            self._changed()
        return n

    def remove_from_collection(self, uids, cid):
        for uid in uids:
            s = self.studies.get(uid)
            if s is not None and cid in s.get("collections", []):
                s["collections"].remove(cid)
        self._changed()

    def count_in(self, cid):
        ids = set(self.descendants(cid))
        return sum(1 for s in self.studies.values() if ids & set(s.get("collections", [])))

    # ─── 태그 ───
    def tag_color(self, name):
        name = normalize_tag(name)
        for t in self.tags:
            if t["name"] == name:
                return t["color"]
        color = TAG_COLORS[len(self.tags) % len(TAG_COLORS)]
        self.tags.append({"name": name, "color": color})
        return color

    def set_tag_color(self, name, color):
        self.tag_color(name)
        for t in self.tags:
            if t["name"] == normalize_tag(name):
                t["color"] = color
        self._changed()

    def all_tags(self):
        used = {t for s in self.studies.values() for t in s.get("tags", [])}
        for name in sorted(used | set(self.quick_tags)):
            self.tag_color(name)
        return sorted(used)

    def set_tags(self, uid, tags):
        s = self.studies.get(uid)
        if s is None:
            return
        s["tags"] = sorted({normalize_tag(t) for t in tags if normalize_tag(t)})
        for t in s["tags"]:
            self.tag_color(t)
        self._changed()

    def toggle_tag(self, uids, tag):
        tag = normalize_tag(tag)
        entries = [self.studies[u] for u in uids if u in self.studies]
        if not entries or not tag:
            return
        add = not all(tag in e.get("tags", []) for e in entries)
        for e in entries:
            tags = set(e.get("tags", []))
            (tags.add if add else tags.discard)(tag)
            e["tags"] = sorted(tags)
        self.tag_color(tag)
        self._changed()

    # ─── 메모 ───
    def set_note(self, uid, note_html, note_text):
        s = self.studies.get(uid)
        if s is None:
            return
        if s.get("note_html") == note_html:
            return
        s["note_html"], s["note_text"] = note_html, note_text
        s["note_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        found = hashtags(note_text)
        if found:   # 메모의 #태그 → 스터디 태그
            s["tags"] = sorted(set(s.get("tags", [])) | set(found))
            for t in found:
                self.tag_color(t)
        self._changed()

    # ─── 검색 ───
    def search(self, query="", collection=None, tags=None):
        """collection: None=전체, "unfiled"=컬렉션 없음, 그 외 id(하위 포함) / tags: 모두 포함(AND)"""
        terms = [t.lower() for t in query.split() if t.strip()]
        want_tags = {normalize_tag(t) for t in (tags or [])}
        scope = set(self.descendants(collection)) if collection not in (None, "unfiled") else None
        out = []
        for uid, s in self.studies.items():
            if collection == "unfiled" and s.get("collections"):
                continue
            if scope is not None and not scope & set(s.get("collections", [])):
                continue
            stags = set(s.get("tags", []))
            if want_tags and not want_tags <= stags:
                continue
            haystack = " ".join(str(s.get(k, "")) for k in
                                ("patient_name", "patient_id", "description", "study_date",
                                 "note_text", "folder")).lower()
            ok = True
            for term in terms:
                if term.startswith("#"):
                    if normalize_tag(term) not in stags:
                        ok = False
                        break
                elif term not in haystack and not any(term in t for t in stags):
                    ok = False
                    break
            if ok:
                out.append(uid)
        out.sort(key=lambda u: (self.studies[u].get("study_date") or "", u), reverse=True)
        return out

    # ─── 내보내기 / 가져오기 ───
    def export_notes(self, uids, fmt="md"):
        lines = []
        for uid in uids:
            s = self.studies.get(uid)
            if s is None:
                continue
            title = f"{s.get('patient_name', '')} ({s.get('patient_id', '')}) — " \
                    f"{_date(s.get('study_date'))} {s.get('description', '')}".strip()
            tags = " ".join(f"#{t}" for t in s.get("tags", []))
            if fmt == "md":
                lines += [f"## {title}", "",
                          f"- Study UID: `{uid}`", f"- 폴더: `{s.get('folder', '')}`"]
                if tags:
                    lines.append(f"- 태그: {tags}")
                lines += ["", _html_to_markdown(s.get("note_html", "")) or s.get("note_text", ""), ""]
            else:
                lines += [title, "=" * min(60, max(10, len(title))), f"Study UID: {uid}",
                          f"폴더: {s.get('folder', '')}"]
                if tags:
                    lines.append(f"태그: {tags}")
                lines += ["", s.get("note_text", ""), "", ""]
        return "\n".join(lines).rstrip() + "\n"

    def export_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)

    def import_json(self, path):
        """병합: 같은 스터디는 태그·컬렉션 합치고 메모는 비어 있을 때만 채움. (새 스터디, 새 컬렉션)"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("format") != FORMAT:
            raise ValueError("DabbaView 라이브러리 파일(.json)이 아닙니다.")
        known = {c["id"] for c in self.collections}
        new_collections = [dict(c) for c in data.get("collections", []) if c["id"] not in known]
        self.collections += new_collections
        new_studies = 0
        for uid, s in (data.get("studies") or {}).items():
            mine = self.studies.get(uid)
            if mine is None:
                self.studies[uid] = dict(s)
                new_studies += 1
                continue
            mine["tags"] = sorted(set(mine.get("tags", [])) | set(s.get("tags", [])))
            mine["collections"] = sorted(set(mine.get("collections", [])) | set(s.get("collections", [])))
            if not mine.get("note_text") and s.get("note_text"):
                mine["note_html"], mine["note_text"] = s.get("note_html", ""), s["note_text"]
        for t in data.get("tags", []):
            if not any(x["name"] == t["name"] for x in self.tags):
                self.tags.append(dict(t))
        self._changed()
        return new_studies, len(new_collections)


def _date(value):
    v = str(value or "")
    return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and v.isdigit() else v


def _html_to_markdown(text):
    """QTextEdit HTML → 간단한 Markdown (굵게·기울임·목록·문단)"""
    if not text:
        return ""
    body = re.search(r"<body[^>]*>(.*)</body>", text, re.S | re.I)
    s = body.group(1) if body else text
    s = re.sub(r"<span[^>]*font-weight:\s*(?:600|700|bold)[^>]*>(.*?)</span>", r"**\1**", s, flags=re.S)
    s = re.sub(r"<span[^>]*font-style:\s*italic[^>]*>(.*?)</span>", r"*\1*", s, flags=re.S)
    s = re.sub(r"</?(b|strong)>", "**", s)
    s = re.sub(r"</?(i|em)>", "*", s)
    counters = []

    def list_start(m):
        counters.append(1 if m.group(1).lower() == "ol" else None)
        return ""
    s = re.sub(r"<(ul|ol)[^>]*>", list_start, s, flags=re.I)

    def item(m):
        if counters and counters[-1] is not None:
            n = counters[-1]
            counters[-1] += 1
            return f"\n{n}. "
        return "\n- "
    s = re.sub(r"<li[^>]*>", item, s, flags=re.I)
    s = re.sub(r"</(ul|ol)>", lambda m: (counters.pop() if counters else None) and "" or "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()
