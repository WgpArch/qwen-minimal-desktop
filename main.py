#!/usr/bin/env python3
"""
Qwen Minimal Desktop Client
Adds Attach button: text files inlined, screenshots/images via vision models.
Standard library + PyGObject only.
"""

import gi
import sqlite3
import json
import re
import fnmatch
import base64
import threading
import urllib.request
from pathlib import Path
from datetime import datetime

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib, Gdk, Pango, Gio

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "qwen-desktop"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.json"

DATA_DIR = Path(GLib.get_user_data_dir()) / "qwen-desktop"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "chats.db"

CSS = """
textview#chatview {
    font-family: sans-serif;
    font-size: 13px;
}
.sidebar {
    background-color: rgba(128,128,128,0.10);
    border-radius: 8px;
}
"""

DEFAULT_CONFIG = {
    "backend": "ollama",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:1.5b",
    "ollama_vision_model": "qwen2.5vl:3b",
    "dashscope_api_key": "",
    "dashscope_model": "qwen-plus",
    "dashscope_vision_model": "qwen-vl-plus",
    "kimi_api_key": "",
    "kimi_model": "kimi-k3",
    "kimi_vision_model": "kimi-k2.5",
    "kimi_base_url": "https://api.moonshot.ai/v1",
    "system_prompt": "You are Qwen, a helpful assistant.",
    "num_ctx": 2048,
    "ollama_threads": 2,
    "max_history_messages": 24,
    "auto_import_dir": str(Path.home() / "Downloads"),
    "auto_import_pattern": "chat-export-*.json",
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            merged = dict(DEFAULT_CONFIG)
            merged.update(saved)
            if set(merged) != set(saved):
                save_config(merged)
            return merged
        except Exception:
            pass
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects (id)
        )
    """)
    c.execute("PRAGMA table_info(projects)")
    cols = [r[1] for r in c.fetchall()]
    if "archived" not in cols:
        c.execute("ALTER TABLE projects ADD COLUMN archived INTEGER DEFAULT 0")
    c.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (1, 'Default')")
    conn.commit()
    return conn


# ---------- Import parsing ----------
def _flatten_content(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [_flatten_content(p) for p in c]
        return "\n".join(p for p in parts if p)
    if isinstance(c, dict):
        for key in ("content", "text", "markdown", "value"):
            if key in c:
                return _flatten_content(c[key])
        return ""
    return ""


def _safe_title(t):
    if not isinstance(t, str):
        return None
    t = t.strip().replace("\n", " ")
    if not t or t[0] in "{[":
        return None
    return t[:40]


def _collect_messages(node, out, seen=None):
    if seen is None:
        seen = set()
    if isinstance(node, dict):
        role = str(node.get("role") or "").lower()
        if role in ("user", "assistant", "system", "human", "ai", "bot"):
            content = node.get("content")
            if not isinstance(content, str):
                content = _flatten_content(content)
            if content and content.strip():
                if role == "human":
                    role = "user"
                elif role in ("ai", "bot"):
                    role = "assistant"
                if (role, content) not in seen:
                    seen.add((role, content))
                    out.append({"role": role, "content": content})
                return
        for v in node.values():
            _collect_messages(v, out, seen)
    elif isinstance(node, list):
        for v in node:
            _collect_messages(v, out, seen)


def parse_chat_file(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    title = None
    out = []
    json_ok = False
    if text.lstrip().startswith(("{", "[")):
        try:
            obj = json.loads(text)
            json_ok = True
            if isinstance(obj, dict):
                title = (_safe_title(obj.get("title"))
                         or _safe_title(obj.get("project_name"))
                         or _safe_title(obj.get("name")))
            elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
                title = _safe_title(obj[0].get("title"))
            _collect_messages(obj, out)
        except Exception:
            out = []
    if not out and not json_ok:
        pattern = re.compile(
            r"^\s*(?:#{1,4}\s*)?(?:\*\*)?\s*(user|assistant|human|ai|qwen|kimi|system)\s*(?:\*\*)?\s*:\s*(.*)$",
            re.I)
        cur = None
        for line in text.splitlines():
            m = pattern.match(line)
            if m:
                raw = m.group(1).lower()
                role = "assistant" if raw in ("assistant", "ai", "qwen", "kimi") else "user"
                if cur:
                    out.append(cur)
                cur = {"role": role, "content": m.group(2).strip()}
            elif cur is not None:
                cur["content"] += "\n" + line
        if cur:
            out.append(cur)
    out = [m for m in out if m["content"].strip()]
    if not out and not json_ok and text.strip():
        out = [{"role": "user", "content": text.strip()}]
    if not title:
        first_user = next((m["content"] for m in out if m["role"] == "user"), None)
        title = _safe_title(first_user) if first_user else None
    if not title:
        title = Path(path).stem
    return out, title


# ---------- Networking ----------
def _http_post(url, payload, extra_headers=None):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    return urllib.request.urlopen(req, timeout=180)


def _stream_sse(resp, on_token, should_stop):
    for raw in resp:
        if should_stop and should_stop():
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        choices = obj.get("choices") or [{}]
        tok = choices[0].get("delta", {}).get("content", "")
        if tok:
            on_token(tok)


def stream_chat(cfg, messages, on_token, should_stop=None, images=None):
    backend = cfg.get("backend", "ollama")
    msgs = messages
    if images:
        msgs = [dict(m) for m in messages]

    if backend == "ollama":
        model = cfg["ollama_model"]
        if images:
            model = cfg.get("ollama_vision_model", "qwen2.5vl:3b")
            msgs[-1]["images"] = [b64 for b64, _mime in images]
        url = cfg["ollama_url"].rstrip("/") + "/api/chat"
        payload = {
            "model": model,
            "messages": msgs,
            "stream": True,
            "options": {"num_ctx": int(cfg.get("num_ctx", 2048)),
                        "num_thread": int(cfg.get("ollama_threads", 2))},
        }
        with _http_post(url, payload) as resp:
            for raw in resp:
                if should_stop and should_stop():
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                obj = json.loads(line)
                tok = obj.get("message", {}).get("content", "")
                if tok:
                    on_token(tok)
                if obj.get("done"):
                    break
    elif backend in ("dashscope", "kimi"):
        if backend == "dashscope":
            url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
            key = cfg["dashscope_api_key"]
            model = cfg.get("dashscope_vision_model", "qwen-vl-plus") if images else cfg["dashscope_model"]
        else:
            url = cfg.get("kimi_base_url", "https://api.moonshot.ai/v1").rstrip("/") + "/chat/completions"
            key = cfg["kimi_api_key"]
            model = cfg.get("kimi_vision_model", "kimi-k2.5") if images else cfg["kimi_model"]
        if images:
            content = [{"type": "text", "text": str(msgs[-1].get("content", ""))}]
            for b64, mime in images:
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"}})
            msgs[-1]["content"] = content
        payload = {"model": model, "messages": msgs, "stream": True}
        headers = {"Authorization": "Bearer " + key}
        with _http_post(url, payload, headers) as resp:
            _stream_sse(resp, on_token, should_stop)
    else:
        raise ValueError("Unknown backend: " + backend)


class QwenMinimalApp(Gtk.Application):
    IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}

    def __init__(self):
        super().__init__(application_id="arch.wgparch.qwenminimal")
        self.conn = init_db()
        self.cfg = load_config()
        self.current_project_id = 1
        self.history = []
        self.generating = False
        self.stop_event = threading.Event()
        self._assistant_text = ""
        self.show_archived = False
        self._assist_start_mark = None
        self._monitor = None
        self._pending_imports = {}
        self._pending = []
        self._current_images = None

    def do_activate(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"), -1)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("Qwen Minimal Desktop")
        self.window.set_default_size(900, 620)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self.on_key_pressed)
        self.window.add_controller(key_ctrl)

        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("margin-start", "margin-end", "margin-top", "margin-bottom"):
            content_box.set_property(m, 8)
        self.window.set_child(content_box)

        # ---------- Sidebar ----------
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.set_size_request(240, -1)
        sidebar.add_css_class("sidebar")
        for m in ("margin-start", "margin-end", "margin-top", "margin-bottom"):
            sidebar.set_property(m, 8)

        new_btn = Gtk.Button(label="+ New Chat")
        new_btn.connect("clicked", self.on_new_project)
        sidebar.append(new_btn)

        chats_label = Gtk.Label(label="Chats", xalign=0)
        chats_label.add_css_class("dim-label")
        sidebar.append(chats_label)

        side_scroll = Gtk.ScrolledWindow()
        side_scroll.set_vexpand(True)
        side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.proj_listbox = Gtk.ListBox()
        self.proj_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.proj_listbox.connect("row-activated", self.on_project_activated)
        side_scroll.set_child(self.proj_listbox)
        sidebar.append(side_scroll)

        side_foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        export_all_btn = Gtk.Button(label="Export All")
        export_all_btn.connect("clicked", self.on_export_all_clicked)
        side_foot.append(export_all_btn)
        self.arch_toggle = Gtk.CheckButton(label="Archived")
        self.arch_toggle.connect("toggled",
                                 lambda w: (setattr(self, "show_archived", w.get_active()),
                                            self.rebuild_project_list()))
        side_foot.append(self.arch_toggle)
        sidebar.append(side_foot)
        content_box.append(sidebar)

        # ---------- Main column ----------
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.set_hexpand(True)
        content_box.append(main_box)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.project_title_label = Gtk.Label(label="Default", xalign=0)
        self.project_title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.project_title_label.set_max_width_chars(28)
        header_box.append(self.project_title_label)

        self.status_label = Gtk.Label(label="", hexpand=True, halign=Gtk.Align.CENTER)
        self.status_label.add_css_class("dim-label")
        header_box.append(self.status_label)

        self.import_btn = Gtk.Button(label="Import")
        self.import_btn.connect("clicked", self.on_import_clicked)
        header_box.append(self.import_btn)
        export_btn = Gtk.Button(label="Export")
        export_btn.connect("clicked", self.on_export_clicked)
        header_box.append(export_btn)
        quit_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        quit_btn.set_tooltip_text("Quit")
        quit_btn.connect("clicked", lambda w: self.quit())
        header_box.append(quit_btn)
        main_box.append(header_box)

        self.search_bar = Gtk.SearchBar()
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        self.search_entry.set_placeholder_text("Search in this chat...")
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_entry.connect("activate", lambda w: self._search(False))
        search_box.append(self.search_entry)
        up_btn = Gtk.Button(label="^")
        up_btn.connect("clicked", lambda w: self._search(True))
        search_box.append(up_btn)
        down_btn = Gtk.Button(label="v")
        down_btn.connect("clicked", lambda w: self._search(False))
        search_box.append(down_btn)
        self.search_bar.set_child(search_box)
        self.search_bar.connect_entry(self.search_entry)
        main_box.append(self.search_bar)

        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.set_vexpand(True)
        self.text_view = Gtk.TextView()
        self.text_view.set_name("chatview")
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.scrolled_window.set_child(self.text_view)
        main_box.append(self.scrolled_window)
        self.setup_tags()

        input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.input_entry = Gtk.Entry()
        self.input_entry.set_placeholder_text("Type your message... (Send while generating = STOP)")
        self.input_entry.set_hexpand(True)
        self.input_entry.connect("activate", self.on_message_sent)
        input_box.append(self.input_entry)
        attach_btn = Gtk.Button.new_from_icon_name("mail-attachment-symbolic")
        attach_btn.set_tooltip_text("Attach files / screenshots (Ctrl+Shift+X clears)")
        attach_btn.connect("clicked", self.on_attach_clicked)
        input_box.append(attach_btn)
        regen_btn = Gtk.Button(label="Regen")
        regen_btn.connect("clicked", self.on_regenerate)
        input_box.append(regen_btn)
        self.send_btn = Gtk.Button(label="Send")
        self.send_btn.connect("clicked", self.on_message_sent)
        input_box.append(self.send_btn)
        main_box.append(input_box)

        self.window.present()
        self.rebuild_project_list()
        self.load_history()
        self.render_history()
        self.status_label.set_label(self._status_text())
        self.project_title_label.set_label(self._project_name(self.current_project_id))
        self.setup_watcher()

    # ---------- attachments ----------
    def on_attach_clicked(self, widget):
        dialog = Gtk.FileDialog()
        sf = Path.home() / "Pictures" / "Screenshots"
        if sf.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(sf)))
        dialog.open_multiple(self.window, None, self._attach_finish)

    def _attach_finish(self, dialog, result, user_data=None):
        try:
            model = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        for i in range(model.get_n_items()):
            path = Path(model.get_item(i).get_path())
            ext = path.suffix.lower()
            if ext in self.IMAGE_EXTS:
                try:
                    data = path.read_bytes()
                except Exception:
                    continue
                if len(data) > 8_000_000:
                    self.append_message("system", f"Image too large (>8MB), skipped: {path.name}")
                    continue
                self._pending.append(("image", path.name,
                                       base64.b64encode(data).decode(),
                                       self.IMAGE_EXTS[ext]))
            else:
                try:
                    txt = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if len(txt) > 200_000:
                    txt = txt[:200_000] + "\n[... truncated at 200KB ...]"
                self._pending.append(("text", path.name, txt))
        self._update_pending_status()

    def _clear_pending(self):
        self._pending = []
        self._update_pending_status()

    def _update_pending_status(self):
        if self._pending:
            names = ", ".join(p[1] for p in self._pending[:3])
            extra = f" +{len(self._pending) - 3}" if len(self._pending) > 3 else ""
            self.status_label.set_label(
                f"pending {len(self._pending)}: {names}{extra}  (Ctrl+Shift+X clears)")
        else:
            self.status_label.set_label(self._status_text())

    # ---------- Downloads auto-watch ----------
    def setup_watcher(self):
        d = Path(self.cfg.get("auto_import_dir", str(Path.home() / "Downloads"))).expanduser()
        if not d.is_dir():
            return
        try:
            gfile = Gio.File.new_for_path(str(d))
            self._monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_dir_event)
        except Exception:
            self._monitor = None

    def _on_dir_event(self, monitor, file, other_file, event_type):
        if event_type not in (Gio.FileMonitorEvent.CREATED,
                              Gio.FileMonitorEvent.CHANGES_DONE_HINT):
            return
        path = file.get_path()
        if not path:
            return
        if not fnmatch.fnmatch(Path(path).name,
                               self.cfg.get("auto_import_pattern", "chat-export-*.json")):
            return
        if path in self._pending_imports:
            try:
                GLib.source_remove(self._pending_imports[path])
            except Exception:
                pass
        self._pending_imports[path] = GLib.timeout_add_seconds(3, self._flush_pending, path)

    def _flush_pending(self, path):
        self._pending_imports.pop(path, None)
        if Path(path).exists():
            self.start_import([path])
        return False

    # ---------- markdown ----------
    def setup_tags(self):
        buf = self.text_view.get_buffer()
        buf.create_tag("bold", weight=Pango.Weight.BOLD)
        buf.create_tag("code_inline", family="monospace", background="#333333", foreground="#f0f0f0")
        buf.create_tag("code_block", family="monospace", background="#222222", foreground="#e0e0e0",
                       left_margin=16, right_margin=16, pixels_above_lines=8, pixels_below_lines=8)
        buf.create_tag("header", weight=Pango.Weight.BOLD, scale=1.3)

    def apply_markdown(self, start_iter, end_iter):
        buf = self.text_view.get_buffer()
        text = buf.get_text(start_iter, end_iter, True)
        for m in re.finditer(r"```.*?\n(.*?)```", text, re.DOTALL):
            s = start_iter.copy()
            s.forward_chars(m.start(1))
            e = s.copy()
            e.forward_chars(m.end(1) - m.start(1))
            buf.apply_tag_by_name("code_block", s, e)
        for m in re.finditer(r"`([^`]+)`", text):
            s = start_iter.copy()
            s.forward_chars(m.start(1))
            e = s.copy()
            e.forward_chars(m.end(1) - m.start(1))
            buf.apply_tag_by_name("code_inline", s, e)
        for m in re.finditer(r"\*\*([^*]+)\*\*", text):
            s = start_iter.copy()
            s.forward_chars(m.start(0))
            e = s.copy()
            e.forward_chars(m.end(0) - m.start(0))
            buf.apply_tag_by_name("bold", s, e)
        for m in re.finditer(r"^(#{1,3})\s+(.*)$", text, re.MULTILINE):
            s = start_iter.copy()
            s.forward_chars(m.start(0))
            e = s.copy()
            e.forward_chars(m.end(2) - m.start(0))
            buf.apply_tag_by_name("header", s, e)

    # ---------- helpers ----------
    def _project_name(self, pid):
        c = self.conn.cursor()
        c.execute("SELECT name FROM projects WHERE id=?", (pid,))
        r = c.fetchone()
        return r[0] if r else "?"

    def _status_text(self):
        b = self.cfg["backend"]
        if b == "ollama":
            return "ollama | " + self.cfg["ollama_model"]
        if b == "kimi":
            return "kimi | " + self.cfg["kimi_model"]
        return "dashscope | " + self.cfg["dashscope_model"]

    def set_busy(self, busy):
        self.send_btn.set_label("Stop" if busy else "Send")
        self.input_entry.set_sensitive(not busy)
        self.status_label.set_label("Thinking... (Send=Stop)" if busy else self._status_text())

    def _scroll_to_end(self):
        buf = self.text_view.get_buffer()
        self.text_view.scroll_to_iter(buf.get_end_iter(), 0.0, False, 0.0, 0.0)

    def _append_token(self, tok):
        buf = self.text_view.get_buffer()
        buf.insert(buf.get_end_iter(), tok)
        self._scroll_to_end()
        return False

    def load_history(self):
        c = self.conn.cursor()
        c.execute("SELECT role, content FROM messages WHERE project_id=? "
                  "AND role IN ('user','assistant') ORDER BY id",
                  (self.current_project_id,))
        self.history = [{"role": r, "content": t} for r, t in c.fetchall()]

    def render_history(self):
        buf = self.text_view.get_buffer()
        buf.set_text("")
        c = self.conn.cursor()
        c.execute("SELECT role, content, timestamp FROM messages WHERE project_id=? ORDER BY id",
                  (self.current_project_id,))
        rows = c.fetchall()
        for role, content, ts in rows:
            if len(content) > 100000:
                content = content[:100000] + " [... truncated for display ...]"
            buf.insert(buf.get_end_iter(), f"[{role.upper()} - {ts}]\n{content}\n\n")
            if role == "assistant":
                end_iter = buf.get_end_iter()
                end_iter.backward_chars(2)
                start_iter = end_iter.copy()
                start_iter.backward_chars(len(content))
                self.apply_markdown(start_iter, end_iter)
        if not rows:
            buf.insert(buf.get_end_iter(), "[SYSTEM]\nEmpty chat. Say something!\n\n")
        self._scroll_to_end()

    def append_message(self, role, content):
        buf = self.text_view.get_buffer()
        ts = datetime.now().strftime("%H:%M:%S")
        buf.insert(buf.get_end_iter(), f"[{role.upper()} - {ts}]\n{content}\n\n")
        self._scroll_to_end()
        c = self.conn.cursor()
        c.execute("INSERT INTO messages (project_id, role, content) VALUES (?, ?, ?)",
                  (self.current_project_id, role, content))
        self.conn.commit()

    # ---------- search & keys ----------
    def on_key_pressed(self, ctrl, keyval, keycode, state):
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_bar.set_search_mode(True)
            self.search_entry.grab_focus()
            return True
        if (state & Gdk.ModifierType.CONTROL_MASK and state & Gdk.ModifierType.SHIFT_MASK
                and keyval in (Gdk.KEY_x, Gdk.KEY_X)):
            self._clear_pending()
            return True
        return False

    def on_search_changed(self, entry):
        buf = self.text_view.get_buffer()
        buf.select_range(buf.get_start_iter(), buf.get_start_iter())
        if entry.get_text():
            self._search(False)

    def _search(self, backward=False):
        q = self.search_entry.get_text()
        buf = self.text_view.get_buffer()
        if not q:
            return
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE
        sel = buf.get_selection_bounds()
        if backward:
            anchor = sel[0] if sel else buf.get_end_iter()
            res = anchor.backward_search(q, flags, None)
            if res is None:
                res = buf.get_end_iter().backward_search(q, flags, None)
        else:
            anchor = sel[1] if sel else buf.get_start_iter()
            res = anchor.forward_search(q, flags, None)
            if res is None:
                res = buf.get_start_iter().forward_search(q, flags, None)
        if res:
            ms, me = res
            buf.select_range(ms, me)
            self.text_view.scroll_to_iter(ms, 0.1, False, 0.0, 0.0)

    # ---------- projects / sidebar ----------
    def rebuild_project_list(self):
        lb = self.proj_listbox
        while (child := lb.get_first_child()):
            lb.remove(child)
        c = self.conn.cursor()
        c.execute("SELECT id, name, archived FROM projects ORDER BY archived, id DESC")
        for pid, name, archived in c.fetchall():
            if archived and not self.show_archived:
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            lbl = Gtk.Label(label=("[arch] " if archived else "") + name,
                            hexpand=True, xalign=0)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            if archived:
                lbl.add_css_class("dim-label")
            if pid == self.current_project_id:
                lbl.add_css_class("accent")
            row.append(lbl)
            a = Gtk.Button(label="Arch")
            a.set_tooltip_text("Archive / unarchive this chat")
            a.connect("clicked", self.on_archive_project, pid)
            row.append(a)
            d = Gtk.Button(label="Del")
            d.set_tooltip_text("Delete this chat permanently")
            d.connect("clicked", self.on_delete_project, pid)
            row.append(d)
            lbrow = Gtk.ListBoxRow()
            lbrow.set_child(row)
            lbrow.proj_id = pid
            lb.append(lbrow)

    def on_project_activated(self, listbox, row):
        pid = getattr(row, "proj_id", None)
        if pid is None or pid == self.current_project_id:
            return
        self.switch_project(pid)

    def switch_project(self, pid):
        self.current_project_id = pid
        self.project_title_label.set_label(self._project_name(pid))
        self.load_history()
        self.render_history()

    def on_new_project(self, widget):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM projects")
        n = c.fetchone()[0] + 1
        c.execute("INSERT INTO projects (name) VALUES (?)", (f"New Chat {n}",))
        self.conn.commit()
        self.switch_project(c.lastrowid)
        self.rebuild_project_list()

    def on_archive_project(self, widget, pid):
        c = self.conn.cursor()
        c.execute("UPDATE projects SET archived = 1 - archived WHERE id=?", (pid,))
        self.conn.commit()
        c.execute("SELECT archived FROM projects WHERE id=?", (pid,))
        if pid == self.current_project_id and c.fetchone()[0] == 1:
            c.execute("SELECT id FROM projects WHERE archived=0 ORDER BY id LIMIT 1")
            r = c.fetchone()
            if r:
                self.switch_project(r[0])
        self.rebuild_project_list()

    def on_delete_project(self, widget, pid):
        c = self.conn.cursor()
        c.execute("DELETE FROM messages WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
        self.conn.commit()
        c.execute("SELECT id FROM projects ORDER BY id LIMIT 1")
        r = c.fetchone()
        if not r:
            c.execute("INSERT INTO projects (name) VALUES ('Default')")
            self.conn.commit()
            r = (c.lastrowid,)
        self.switch_project(r[0])
        self.rebuild_project_list()

    # ---------- generation ----------
    def on_message_sent(self, widget):
        if self.generating:
            self.stop_event.set()
            return
        text = self.input_entry.get_text().strip()
        if not text and not self._pending:
            return

        parts = [text] if text else []
        images = []
        for item in self._pending:
            if item[0] == "text":
                parts.append(f"--- Attached file: {item[1]} ---\n{item[2]}\n--- End of {item[1]} ---")
            else:
                images.append((item[2], item[3]))
                parts.append(f"[image: {item[1]}]")
        full_text = "\n\n".join(parts)

        if self._project_name(self.current_project_id).startswith("New Chat"):
            newname = full_text[:40].replace("\n", " ")
            c0 = self.conn.cursor()
            c0.execute("UPDATE projects SET name=? WHERE id=?",
                       (newname, self.current_project_id))
            self.conn.commit()
            self.project_title_label.set_label(newname)
            self.rebuild_project_list()

        self.append_message("user", full_text)
        self.history.append({"role": "user", "content": full_text})
        self.input_entry.set_text("")
        self._pending = []
        self._update_pending_status()
        self._start_generation(images)

    def _start_generation(self, images=None):
        self.generating = True
        self.stop_event.clear()
        self._assistant_text = ""
        self._current_images = images
        self.set_busy(True)
        ts = datetime.now().strftime("%H:%M:%S")
        self._append_token(f"[ASSISTANT - {ts}]\n")
        buf = self.text_view.get_buffer()
        self._assist_start_mark = buf.create_mark(None, buf.get_end_iter(), True)
        n = int(self.cfg.get("max_history_messages", 24))
        messages = [{"role": "system", "content": self.cfg["system_prompt"]}] + self.history[-n:]
        threading.Thread(target=self._worker, args=(messages,), daemon=True).start()

    def on_regenerate(self, widget):
        if self.generating:
            return
        c = self.conn.cursor()
        c.execute("SELECT id, role FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 1",
                  (self.current_project_id,))
        row = c.fetchone()
        if row and row[1] == "assistant":
            c.execute("DELETE FROM messages WHERE id=?", (row[0],))
            self.conn.commit()
        self.load_history()
        if not self.history or self.history[-1]["role"] != "user":
            self.append_message("system", "Nothing to regenerate (need a user message first).")
            return
        self.render_history()
        self._start_generation(None)

    def _preflight(self):
        """Return an error string if the needed model/key is missing, else None."""
        imgs = bool(self._current_images)
        b = self.cfg.get("backend", "ollama")
        if b == "ollama":
            model = (self.cfg.get("ollama_vision_model", "qwen2.5vl:3b") if imgs
                     else self.cfg["ollama_model"])
            try:
                req = urllib.request.Request(self.cfg["ollama_url"].rstrip("/") + "/api/tags")
                with urllib.request.urlopen(req, timeout=10) as r:
                    names = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
            except Exception as e:
                return "Ollama unreachable: " + str(e)
            if not any(n == model or n.startswith(model + ":")
                       or n == model.split(":")[0] for n in names):
                if imgs:
                    return (f"Vision model '{model}' not installed.\n"
                            f"Fix A (local): ollama pull {model}\n"
                            f"Fix B (cloud): set kimi_api_key or dashscope_api_key in "
                            f"~/.config/qwen-desktop/config.json and switch backend.")
                return f"Model '{model}' not installed. Run: ollama pull {model}"
        else:
            key = self.cfg.get("kimi_api_key" if b == "kimi" else "dashscope_api_key", "")
            if not str(key).strip():
                return (f"Backend '{b}' selected but its API key is empty. "
                        f"Edit ~/.config/qwen-desktop/config.json")
        return None

    def _worker(self, messages):
        def on_token(tok):
            self._assistant_text += tok
            GLib.idle_add(self._append_token, tok)
        try:
            err = self._preflight()
            if err:
                GLib.idle_add(self._generation_error, err)
                return
            stream_chat(self.cfg, messages, on_token,
                        should_stop=self.stop_event.is_set, images=self._current_images)
            GLib.idle_add(self._finish_generation)
        except Exception as e:
            GLib.idle_add(self._generation_error, str(e))

    def _finish_generation(self):
        self.generating = False
        self.set_busy(False)
        buf = self.text_view.get_buffer()
        if self._assist_start_mark:
            start_iter = buf.get_iter_at_mark(self._assist_start_mark)
            end_iter = buf.get_end_iter()
            self.apply_markdown(start_iter, end_iter)
            buf.delete_mark(self._assist_start_mark)
            self._assist_start_mark = None
        if self._assistant_text.strip():
            self.history.append({"role": "assistant", "content": self._assistant_text})
            c = self.conn.cursor()
            c.execute("INSERT INTO messages (project_id, role, content) VALUES (?, ?, ?)",
                      (self.current_project_id, "assistant", self._assistant_text))
            self.conn.commit()
            self._append_token("\n\n")
        else:
            self._append_token("(stopped / empty)\n\n")
        self._assistant_text = ""
        return False

    def _generation_error(self, msg):
        self.generating = False
        self.set_busy(False)
        hint = msg
        if "refused" in msg or "URLError" in msg or "not known" in msg:
            hint += "\nHint: check ~/.config/qwen-desktop/config.json (backend/key) or: systemctl status ollama"
        if "404" in msg:
            hint += "\nHint: model not found. Run: ollama list   (and pull the model from your config)"
        self._append_token("\n")
        self.append_message("system", "ERROR: " + hint)
        self._assistant_text = ""
        return False

    # ---------- import (threaded) ----------
    def on_import_clicked(self, widget):
        dialog = Gtk.FileDialog()
        dialog.open_multiple(self.window, None, self._import_finish)

    def _import_finish(self, dialog, result, user_data=None):
        try:
            model = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = [model.get_item(i).get_path() for i in range(model.get_n_items())]
        if paths:
            self.start_import(paths)

    def start_import(self, paths):
        self.import_btn.set_sensitive(False)
        self.status_label.set_label("Importing in background...")
        threading.Thread(target=self._import_worker, args=(paths,), daemon=True).start()

    def _import_worker(self, paths):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        imported, last_pid = 0, None
        for path in paths:
            try:
                msgs, title = parse_chat_file(path)
            except Exception:
                continue
            if not msgs:
                continue
            c.execute("SELECT COUNT(*) FROM projects WHERE name=?", (title,))
            if c.fetchone()[0]:
                title = title + "_" + datetime.now().strftime("%H%M%S")
            c.execute("INSERT INTO projects (name) VALUES (?)", (title,))
            pid = c.lastrowid
            c.executemany(
                "INSERT INTO messages (project_id, role, content) VALUES (?, ?, ?)",
                [(pid, m["role"], m["content"]) for m in msgs])
            imported += 1
            last_pid = pid
        conn.commit()
        conn.close()
        GLib.idle_add(self._import_done, imported, last_pid)

    def _import_done(self, imported, last_pid):
        self.import_btn.set_sensitive(True)
        self.status_label.set_label(self._status_text())
        if last_pid:
            self.switch_project(last_pid)
        self.rebuild_project_list()
        self.append_message("system", f"Imported {imported} file(s).")
        return False

    # ---------- export ----------
    def on_export_clicked(self, widget):
        dialog = Gtk.FileDialog()
        dialog.set_initial_name(f"project_{self.current_project_id}.json")
        dialog.save(self.window, None, self._export_finish)

    def _export_finish(self, dialog, result, user_data=None):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        c = self.conn.cursor()
        c.execute("SELECT role, content, timestamp FROM messages WHERE project_id=? ORDER BY id",
                  (self.current_project_id,))
        rows = c.fetchall()
        data = {
            "app": "qwen-minimal-desktop",
            "project_id": self.current_project_id,
            "project_name": self._project_name(self.current_project_id),
            "exported_at": datetime.now().isoformat(),
            "messages": [{"role": r, "content": t, "timestamp": ts} for r, t, ts in rows],
        }
        Path(gfile.get_path()).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        self.append_message("system", f"Exported {len(rows)} messages to {gfile.get_path()}")

    def on_export_all_clicked(self, widget):
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose folder for all project exports")
        dialog.select_folder(self.window, None, self._export_all_finish)

    def _export_all_finish(self, dialog, result, user_data=None):
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        outdir = Path(gfile.get_path())
        c = self.conn.cursor()
        c.execute("SELECT id, name FROM projects ORDER BY id")
        count = 0
        for pid, name in c.fetchall():
            c2 = self.conn.cursor()
            c2.execute("SELECT role, content, timestamp FROM messages "
                       "WHERE project_id=? ORDER BY id", (pid,))
            rows = c2.fetchall()
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:60] or f"project_{pid}"
            data = {
                "app": "qwen-minimal-desktop",
                "project_id": pid,
                "project_name": name,
                "exported_at": datetime.now().isoformat(),
                "messages": [{"role": r, "content": t, "timestamp": ts} for r, t, ts in rows],
            }
            (outdir / f"{safe}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False))
            count += 1
        self.append_message("system", f"Exported {count} projects to {outdir}")


if __name__ == "__main__":
    app = QwenMinimalApp()
    app.run(None)
