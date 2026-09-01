#!/usr/bin/env python3
"""askr - a lightweight, local question overlay for Omarchy."""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gio, GLib, Gtk, Gdk, Pango


APP_ID = "io.github.zenyatara.askr"
CONFIG_FILE = Path(
    os.environ.get("ASKR_CONFIG") or Path.home() / ".config" / "askr" / "config.toml"
)
DATA_DIR = Path.home() / ".local" / "share" / "askr"
STATE_FILE = DATA_DIR / "history.json"
CAPTURE_DIR = DATA_DIR / "captures"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
# Omarchy records the coding agent the user picked with `omarchy default agent`.
OMARCHY_AGENT_FILE = Path.home() / ".config" / "omarchy" / "defaults" / "agent"
# Player choice depends on the format, and gets it wrong silently in both
# directions: pw-play and paplay decode MP3 through libsndfile, which stops
# partway through and still exits 0, while mpg123 exits 0 on an ogg without
# playing a note. Neither reports failure, so pick by suffix rather than trying
# one list for everything.
MP3_PLAYERS = (
    ["mpg123", "-q"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["mpv", "--no-video", "--really-quiet"],
)
PCM_PLAYERS = (
    ["pw-play"],
    ["paplay"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["mpv", "--no-video", "--really-quiet"],
)
# canberra plays the shutter from whichever sound theme the desktop is using,
# so askr does not have to ship an audio file for it.
SHUTTER_SOUND_ID = "camera-shutter"
SHUTTER_FALLBACK = Path("/usr/share/sounds/freedesktop/stereo/camera-shutter.oga")
PROMPT_SIZE = (560, 96)
ANSWER_SIZE = (540, 780)
# Hyprland maps the answer panel a moment after present(); poll until it shows up.
RESTORE_POLL_MS = 50
RESTORE_MAX_ATTEMPTS = 60
DEBUG = bool(os.environ.get("ASKR_DEBUG") or os.environ.get("ASK_DEBUG"))

# How to drive each coding agent Omarchy can set as the default. Every entry is
# plain data so `[agent.command]` in config.toml can override any part of it, or
# describe an agent askr has never heard of.
#
#   start / resume  argv, with {workdir} {model} {thread_id} substituted
#   model / effort  appended only when that setting has a value
#   image           appended only when a screenshot is attached
#   prompt          flag placed immediately before the prompt, for agents that
#                   take it as a flag value rather than positionally
#   session         flag carrying a conversation id askr mints itself, for
#                   agents that create a session on demand from a given id
#   continue        used instead of `start` once a conversation exists, for
#                   agents that can only continue their most recent session
#   text / thread   dotted paths into each JSON line; `[]` maps over a list
#   text_when       only read `text` from lines matching these dotted paths
#
# codex and claude are verified against their CLIs. The rest follow each agent's
# documented headless flags; report a correction if one drifts.
BUILTIN_AGENTS = {
    "codex": {
        "start": ["codex", "exec", "--json", "--sandbox", "read-only",
                  "--cd", "{workdir}", "--skip-git-repo-check"],
        "resume": ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
                   "{thread_id}"],
        "model": ["--model", "{model}"],
        "effort": ["--config", "model_reasoning_effort=\"{effort}\""],
        "image": ["-i", "{image}"],
        # `--image` takes one or more files, so without a separator it swallows
        # the prompt as a second filename. It also lets a question start with a
        # dash without being parsed as a flag.
        "prompt": ["--"],
        "text": ["item.text", "item.content[].text"],
        "text_when": {"item.type": ["agent_message", "message"]},
        "thread": "thread_id",
    },
    "claude": {
        "start": ["claude", "-p", "--output-format", "stream-json", "--verbose"],
        "resume": ["claude", "-p", "--output-format", "stream-json", "--verbose",
                   "--resume", "{thread_id}"],
        "model": ["--model", "{model}"],
        "prompt": ["--"],
        "text": ["message.content[].text"],
        "text_when": {"type": ["assistant"]},
        "thread": "session_id",
    },
    "grok": {
        "start": ["grok", "--output-format", "streaming-json"],
        "resume": ["grok", "--output-format", "streaming-json", "--resume", "{thread_id}"],
        "model": ["--model", "{model}"],
        "prompt": ["--single"],
        "text": ["update.content.text", "message.content[].text"],
        "thread": "session_id",
    },
    # These print prose rather than JSON, so askr cannot read a conversation id
    # out of their output. pi and omp create a session from an id askr mints;
    # the rest can only pick up their own most recent session.
    "gemini": {
        "start": ["gemini"],
        "continue": ["gemini", "--resume", "latest"],
        "model": ["--model", "{model}"],
        "prompt": ["--prompt"],
        "format": "text",
    },
    "opencode": {
        "start": ["opencode", "run"],
        "continue": ["opencode", "run", "--continue"],
        "model": ["--model", "{model}"],
        "format": "text",
    },
    "crush": {
        "start": ["crush", "run"],
        "continue": ["crush", "run", "--continue"],
        "format": "text",
    },
    "copilot": {
        "start": ["copilot", "--output-format", "text"],
        "continue": ["copilot", "--output-format", "text", "--continue"],
        "model": ["--model", "{model}"],
        "prompt": ["--prompt"],
        "format": "text",
    },
    "pi": {
        "start": ["pi", "--print"],
        "session": ["--session-id", "{thread_id}"],
        "model": ["--model", "{model}"],
        "format": "text",
    },
    "omp": {
        "start": ["omp", "--print"],
        "session": ["--session-id", "{thread_id}"],
        "model": ["--model", "{model}"],
        "prompt": ["--"],
        "format": "text",
    },
}


def load_config():
    """Read config.toml. A missing or broken file must never stop askr starting."""
    try:
        with CONFIG_FILE.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"[askr] ignoring {CONFIG_FILE}: {error}", file=sys.stderr, flush=True)
        return {}


def omarchy_agent():
    """The agent chosen with `omarchy default agent`, or None when unset."""
    try:
        return OMARCHY_AGENT_FILE.read_text().strip() or None
    except OSError:
        return None


def resolve_agent(config):
    """Pick the agent and merge any per-agent overrides over the built-in recipe."""
    section = config.get("agent") or {}
    name = section.get("name") or omarchy_agent() or "codex"
    recipe = dict(BUILTIN_AGENTS.get(name, {}))
    recipe.update(section.get("command") or {})
    recipe.setdefault("format", "jsonl")
    recipe["name"] = name
    return recipe


def expand(value):
    """Expand ~ and $VARS in a configured path."""
    return Path(os.path.expandvars(str(value))).expanduser()


def fill(token, fields):
    """Replace {name} placeholders, leaving every other brace alone."""
    for key, value in fields.items():
        token = token.replace("{" + key + "}", value)
    return token


def dig(data, path):
    """Follow a dotted path into parsed JSON. A `[]` suffix maps over a list."""
    current = [data]
    for part in path.split("."):
        following = []
        key, listed = (part[:-2], True) if part.endswith("[]") else (part, False)
        for item in current:
            if not isinstance(item, dict) or key not in item:
                continue
            value = item[key]
            if listed and isinstance(value, list):
                following.extend(value)
            elif not listed:
                following.append(value)
        current = following
        if not current:
            return []
    return current

TEXT_TAGS = {
    "question": {"foreground": "#8ab4f8"},
    "answer": {"foreground": "#f5f7fb"},
    "error": {"foreground": "#ff9b9b"},
    "markdown-heading": {"weight": Pango.Weight.BOLD, "scale": 1.18},
    "markdown-bold": {"weight": Pango.Weight.BOLD},
    "markdown-italic": {"style": Pango.Style.ITALIC},
    "markdown-code": {
        "family": "monospace", "foreground": "#d7e3f4", "background": "#10151d",
    },
    "markdown-link": {"foreground": "#8ab4f8", "underline": Pango.Underline.SINGLE},
    "waiting": {"foreground": "#7d8799", "style": Pango.Style.ITALIC},
}

CSS = b"""
window { background: transparent; }
.askr-surface {
  background-color: rgba(18, 21, 27, 0.50);
  border: 1px solid rgba(255, 255, 255, 0.20);
  border-radius: 16px;
  box-shadow: 0 10px 32px rgba(0, 0, 0, 0.35);
  padding: 16px;
}
.askr-answer { font-size: 15px; }
entry, textview {
  background-color: rgba(7, 9, 12, 0.48);
  color: #f5f7fb;
  border: 1px solid rgba(255, 255, 255, 0.20);
  border-radius: 10px;
  padding: 10px;
}
button { border-radius: 9px; }
button.icon-toggle, button.icon-action { min-width: 40px; font-size: 17px; font-weight: 700; background-image: none; }
button.icon-on { background-color: #16803d; border-color: #37d06a; color: #ffffff; }
button.icon-off { background-color: #941f2a; border-color: #f25b68; color: #ffffff; }
button.icon-action { background-color: rgba(70, 76, 86, 0.78); color: #ffffff; }
button.icon-recording { background-color: #941f2a; border-color: #f25b68; color: #ffffff; }
button.icon-attached { background-color: #16803d; border-color: #37d06a; color: #ffffff; }
button.icon-on label, button.icon-off label, button.icon-action label, button.icon-recording label, button.icon-attached label { color: #ffffff; font-weight: 700; }
"""


class Askr(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        )
        self.prompt_window = None
        self.answer_window = None
        self.prompt_entry = None
        self.reply_entry = None
        self.voice_toggle = None
        self.brief_toggle = None
        self.mic_button = None
        self.camera_button = None
        self.answer_buffer = None
        self.answer_scroller = None
        self.state_lock = threading.Lock()
        self.config = load_config()
        self.agent = resolve_agent(self.config)
        self.workdir = self.resolve_workdir()
        self.current = self.load_state()
        self.geometry_restored = False
        self.geometry_restore_attempts = 0
        self.voice_enabled = self.current.get("voice_enabled", False)
        self.brief_enabled = self.current.get("brief_enabled", False)
        self.recording = False
        self.pending_image = None
        self.draft = ""
        self.waiting_mark = None
        self.waiting_timer = None
        self.waiting_since = 0.0
        self.running = False
        self.previous_agent = None
        self.adopt_agent()

    def archive_conversation(self):
        """Move the visible conversation into the archive, if there is one."""
        if not self.current.get("messages"):
            return
        self.current.setdefault("archived_conversations", []).append(
            {
                "agent": self.current.get("agent"),
                "thread_id": self.current.get("thread_id"),
                "messages": self.current["messages"],
            }
        )

    def adopt_agent(self):
        """Start a new conversation when the resolved agent is not the stored one.

        Thread ids and models belong to the agent that produced them, so a codex
        conversation cannot be resumed by claude. Changing the Omarchy default
        between runs therefore archives what came before rather than handing one
        agent another's session id.
        """
        name = self.agent["name"]
        previous = self.current.get("agent")
        if previous == name:
            return
        if previous is not None:
            self.archive_conversation()
            self.current["thread_id"] = None
            self.current["messages"] = []
            # Reported in the panel once it exists, so the cleared transcript
            # does not just look like lost history.
            self.previous_agent = previous
            self.debug(f"agent changed {previous} -> {name}; started a new conversation")
        self.current["agent"] = name
        self.save_state()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.build_windows()
        # Keep manual move/resize changes, including changes made while the
        # answer panel is already open.
        GLib.timeout_add_seconds(1, self.remember_answer_geometry)
        self.hold()

    def do_command_line(self, command_line):
        # A second launch is forwarded here by the primary instance, which is how
        # the keybindings reach an already-running askr.
        if "--toggle-answer" in command_line.get_arguments()[1:]:
            self.toggle_answer()
        else:
            self.activate()
        return 0

    def do_activate(self):
        if self.prompt_window.get_visible():
            self.hide_window(self.prompt_window)
        else:
            self.show_prompt()

    def toggle_answer(self):
        """Show the answer panel, or close it exactly the way Escape does."""
        if self.answer_window.get_visible():
            self.hide_window(self.answer_window)
        else:
            self.present_answer()
            self.reply_entry.grab_focus()

    def surface(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("askr-surface")
        return box

    def build_windows(self):
        self.build_prompt_window()
        self.build_answer_window()
        self.restore_visible_history()

    def build_prompt_window(self):
        self.prompt_window = Gtk.ApplicationWindow(application=self, title="askr")
        self.prompt_window.set_decorated(False)
        self.prompt_window.set_resizable(False)
        self.prompt_window.set_default_size(*PROMPT_SIZE)
        self.prompt_window.connect("close-request", self.hide_window)
        self.add_escape_handler(self.prompt_window)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.prompt_entry = Gtk.Entry(hexpand=True)
        self.prompt_entry.connect("activate", self.submit_prompt)
        row.append(self.prompt_entry)

        self.voice_toggle = Gtk.ToggleButton(active=self.voice_enabled)
        self.voice_toggle.connect("toggled", self.set_voice_enabled)
        self.brief_toggle = Gtk.ToggleButton(label="⚡", active=self.brief_enabled)
        self.brief_toggle.connect("toggled", self.set_brief_enabled)
        self.mic_button = Gtk.Button()
        self.mic_button.connect("clicked", self.toggle_voice_input)
        self.camera_button = Gtk.Button()
        self.camera_button.connect("clicked", self.capture_screenshot)
        for button in (self.voice_toggle, self.brief_toggle, self.mic_button,
                       self.camera_button):
            row.append(button)
        self.refresh_prompt()

        surface = self.surface()
        surface.append(row)
        self.prompt_window.set_child(surface)

    def build_answer_window(self):
        self.answer_window = Gtk.ApplicationWindow(application=self, title="askr answer")
        self.answer_window.set_decorated(False)
        self.answer_window.set_transient_for(self.prompt_window)
        self.answer_window.set_default_size(*ANSWER_SIZE)
        self.answer_window.connect("close-request", self.hide_window)
        self.add_escape_handler(self.answer_window)

        self.answer_scroller = Gtk.ScrolledWindow(vexpand=True)
        view = Gtk.TextView(
            editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR
        )
        view.add_css_class("askr-answer")
        self.answer_scroller.set_child(view)
        self.answer_buffer = view.get_buffer()
        for name, attributes in TEXT_TAGS.items():
            self.answer_buffer.create_tag(name, **attributes)

        self.reply_entry = Gtk.Entry()
        self.reply_entry.set_size_request(-1, 32)
        self.reply_entry.connect("activate", self.submit_reply)

        surface = self.surface()
        surface.append(self.answer_scroller)
        surface.append(self.reply_entry)
        self.answer_window.set_child(surface)

    def hide_window(self, window):
        """Close a window the way every path should: save geometry, then hide."""
        if window is self.answer_window:
            self.remember_answer_geometry()
        window.set_visible(False)
        return True

    def present_answer(self):
        self.answer_window.present()
        # Hyprland re-centers the panel every time it is mapped, so re-apply the
        # saved rectangle as soon as the window appears in `hyprctl clients`.
        self.geometry_restored = False
        self.geometry_restore_attempts = 0
        GLib.timeout_add(RESTORE_POLL_MS, self.restore_answer_geometry)
        GLib.idle_add(self.scroll_to_bottom)

    def answer_client(self):
        """Return this process's mapped answer window from Hyprland."""
        try:
            clients = json.loads(
                subprocess.check_output(["hyprctl", "clients", "-j"], text=True)
            )
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None

        for client in clients:
            if (
                str(client.get("pid")) == str(os.getpid())
                and client.get("title") == "askr answer"
            ):
                return client
        return None

    @staticmethod
    def valid_geometry(geometry):
        if not isinstance(geometry, dict):
            return False
        at, size = geometry.get("at"), geometry.get("size")
        return (
            isinstance(at, list)
            and isinstance(size, list)
            and len(at) == 2
            and len(size) == 2
            and all(isinstance(value, int) for value in at + size)
            and size[0] > 0
            and size[1] > 0
        )

    @staticmethod
    def debug(*parts):
        if DEBUG:
            print("[askr]", *parts, file=sys.stderr, flush=True)

    @classmethod
    def hypr_dispatch(cls, expression):
        """Run one Hyprland Lua dispatcher, returning whether it applied.

        Hyprland 0.56 evaluates `hyprctl dispatch` as Lua, so the pre-Lua
        `resizewindowpixel "exact W H,address:0x..."` form no longer parses and
        reports its failure on stdout.
        """
        result = subprocess.run(
            ["hyprctl", "dispatch", expression], capture_output=True, text=True, check=False
        )
        output = result.stdout.strip()
        if result.returncode != 0 or output.startswith("error"):
            cls.debug("dispatch failed:", expression, "->", result.returncode, output)
            return False
        return True

    def restore_answer_geometry(self):
        """Re-apply the saved rectangle, retrying until the window is mapped."""
        self.geometry_restore_attempts += 1
        keep_polling = self.geometry_restore_attempts < RESTORE_MAX_ATTEMPTS
        client = self.answer_client()
        if not client:
            return keep_polling

        geometry = self.current.get("answer_geometry")
        if not self.valid_geometry(geometry):
            # Nothing saved yet, so let the saver record wherever the panel lands.
            self.geometry_restored = True
            return False

        address = client["address"]
        width, height = geometry["size"]
        x, y = geometry["at"]
        # Resizing re-anchors the window, so the move has to come afterwards.
        self.hypr_dispatch(
            f"hl.dsp.window.resize({{x={width},y={height},window='address:{address}'}})"
        )
        self.hypr_dispatch(
            f"hl.dsp.window.move({{x={x},y={y},window='address:{address}'}})"
        )

        applied = self.answer_client() or {}
        if applied.get("at") == [x, y] and applied.get("size") == [width, height]:
            self.geometry_restored = True
            return False
        # Leave geometry_restored unset on failure: marking it restored would let
        # the saver overwrite the saved rectangle with Hyprland's centered default.
        self.debug("restore did not apply:", applied.get("at"), applied.get("size"))
        return keep_polling

    def remember_answer_geometry(self):
        # Nothing to record while the panel is hidden, and polling hyprctl once a
        # second for the life of the app is a process spawn we do not need.
        if not self.answer_window or not self.answer_window.get_visible():
            return True
        # Do not let GTK's initial centered/default allocation overwrite the
        # saved rectangle before the restore dispatch has run.
        if not self.geometry_restored:
            return True
        client = self.answer_client()
        if not client:
            return True

        geometry = {"at": client.get("at"), "size": client.get("size")}
        if self.valid_geometry(geometry) and geometry != self.current.get("answer_geometry"):
            self.current["answer_geometry"] = geometry
            self.save_state()
        return True

    def scroll_to_bottom(self):
        adjustment = self.answer_scroller.get_vadjustment()
        adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
        return False

    def add_escape_handler(self, window):
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self.handle_key, window)
        window.add_controller(controller)

    def handle_key(self, _controller, keyval, _keycode, _state, window):
        if keyval == Gdk.KEY_Escape:
            return self.hide_window(window)
        return False

    def show_prompt(self):
        self.prompt_entry.set_text("")
        self.prompt_window.present()
        self.prompt_entry.grab_focus()

    def submit_prompt(self, _entry):
        question = self.prompt_entry.get_text().strip()
        if question:
            self.hide_window(self.prompt_window)
            self.ask(question)

    def set_voice_enabled(self, toggle):
        self.voice_enabled = toggle.get_active()
        self.refresh_prompt()
        self.save_state()

    def set_brief_enabled(self, toggle):
        self.brief_enabled = toggle.get_active()
        self.refresh_prompt()
        self.save_state()

    @staticmethod
    def style_button(button, label, tooltip, css_class):
        for name in ("icon-toggle", "icon-on", "icon-off", "icon-action",
                     "icon-recording", "icon-attached"):
            button.remove_css_class(name)
        button.set_label(label)
        button.set_tooltip_text(tooltip)
        button.add_css_class(css_class)

    def refresh_prompt(self):
        """Re-apply the prompt row: button labels, tooltips, and the entry hint."""
        self.prompt_entry.set_placeholder_text(
            "Screenshot attached — ask about it"
            if self.pending_image
            else "Ask askr…"
        )
        self.style_button(
            self.voice_toggle,
            "🔊" if self.voice_enabled else "🔇",
            f"Voice replies: {'on' if self.voice_enabled else 'off'}",
            "icon-on" if self.voice_enabled else "icon-off",
        )
        self.voice_toggle.add_css_class("icon-toggle")
        self.style_button(
            self.brief_toggle,
            "⚡",
            f"Brief replies: {'on' if self.brief_enabled else 'off'}",
            "icon-on" if self.brief_enabled else "icon-off",
        )
        self.brief_toggle.add_css_class("icon-toggle")
        self.style_button(
            self.mic_button,
            "■" if self.recording else "🎙",
            "Stop voice input" if self.recording else "Start voice input",
            "icon-recording" if self.recording else "icon-action",
        )
        self.style_button(
            self.camera_button,
            "📷",
            "Screenshot attached to the next question" if self.pending_image
            else "Capture this monitor for the next question",
            "icon-attached" if self.pending_image else "icon-action",
        )

    def toggle_voice_input(self, _button):
        action = "stop" if self.recording else "start"
        self.recording = not self.recording
        self.refresh_prompt()
        self.prompt_entry.grab_focus()
        GLib.timeout_add(120, self.run_voice_control, action)

    def run_voice_control(self, action):
        threading.Thread(target=self.control_voice_input, args=(action,), daemon=True).start()
        return False

    def control_voice_input(self, action):
        result = subprocess.run(
            ["voxtype", "record", action],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0 or action == "stop":
            GLib.idle_add(self.reset_voice_input)

    def reset_voice_input(self):
        self.recording = False
        self.refresh_prompt()

    def capture_screenshot(self, _button):
        # Attaching a screenshot must not cost the question already typed:
        # show_prompt() clears the entry when the prompt comes back.
        self.draft = self.prompt_entry.get_text()
        self.hide_window(self.prompt_window)
        threading.Thread(target=self.take_screenshot, daemon=True).start()

    def take_screenshot(self):
        # The agent keeps every attachment in the conversation and re-sends them
        # on each later turn, so capture size is paid again on every question.
        # A full-resolution JPEG is ~4x smaller than PNG and still legible.
        settings = self.config.get("screenshot") or {}
        image_format = settings.get("format", "jpeg")
        scale = settings.get("scale")
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        suffix = ".jpg" if image_format == "jpeg" else ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, dir=CAPTURE_DIR, delete=False) as image:
            image_path = Path(image.name)
        try:
            monitors = json.loads(subprocess.check_output(["hyprctl", "monitors", "-j"], text=True))
            monitor = next((item["name"] for item in monitors if item.get("focused")), None)
            command = ["grim"]
            if monitor:
                command += ["-o", monitor]
            if scale:
                command += ["-s", str(scale)]
            if image_format == "jpeg":
                command += ["-t", "jpeg", "-q", str(settings.get("quality", 90))]
            command.append(str(image_path))
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode:
                raise RuntimeError("Screenshot capture failed.")
            # Sounded on its own thread so the shutter does not delay the prompt
            # coming back.
            threading.Thread(target=self.play_shutter, daemon=True).start()
            GLib.idle_add(self.finish_screenshot, image_path)
        except Exception as error:
            image_path.unlink(missing_ok=True)
            GLib.idle_add(self.finish_screenshot, None, str(error))

    def finish_screenshot(self, image_path, error=None):
        if error:
            self.append_error(f"[Error: {error}]")
            self.present_answer()
            self.reopen_prompt()
            return
        if self.pending_image:
            self.pending_image.unlink(missing_ok=True)
        self.pending_image = image_path
        self.reopen_prompt()

    def reopen_prompt(self):
        """Bring the prompt back with whatever the user had already typed."""
        draft, self.draft = self.draft, ""
        self.refresh_prompt()
        self.show_prompt()
        if draft:
            self.prompt_entry.set_text(draft)
            self.prompt_entry.set_position(-1)

    def submit_reply(self, _entry):
        question = self.reply_entry.get_text().strip()
        if question:
            self.reply_entry.set_text("")
            self.ask(question)

    def start_new(self):
        if self.running:
            return
        self.archive_conversation()
        # Reset in place: rebuilding the dict from a literal silently dropped any
        # state key not listed here.
        self.current["thread_id"] = None
        self.current["messages"] = []
        self.current["agent"] = self.agent["name"]
        self.answer_buffer.set_text("")
        self.save_state()
        self.present_answer()
        self.reply_entry.grab_focus()

    def ask(self, question):
        if self.running:
            return
        if question == "/new":
            self.start_new()
            return
        self.running = True
        self.present_answer()
        self.append_question(question)
        self.begin_waiting()
        image_path = self.pending_image
        self.pending_image = None
        self.refresh_prompt()
        threading.Thread(
            target=self.run_agent,
            args=(question, self.voice_enabled, self.brief_enabled, image_path),
            daemon=True,
        ).start()

    def resolve_workdir(self):
        """Where the agent runs. Configurable; ~/Work when it exists, else $HOME."""
        configured = self.config.get("workdir")
        if configured:
            return expand(configured)
        work = Path.home() / "Work"
        return work if work.is_dir() else Path.home()

    def build_command(self, request, thread_id, image_path):
        """Assemble the agent's argv from its recipe and the configured settings."""
        recipe = self.agent
        settings = self.config.get("agent") or {}
        fields = {
            "workdir": str(self.workdir),
            "thread_id": thread_id or "",
            "model": self.setting_for("models", "model") or "",
            "effort": self.setting_for("effort", "reasoning_effort") or "",
            "image": str(image_path) if image_path else "",
        }

        if recipe.get("session") and not thread_id:
            # This agent will create the conversation from an id we choose, so
            # continuity does not depend on parsing anything back out of it.
            thread_id = str(uuid.uuid4())
            self.current["thread_id"] = thread_id
            fields["thread_id"] = thread_id

        if thread_id and recipe.get("resume"):
            template = recipe["resume"]
        elif self.current.get("messages") and recipe.get("continue"):
            template = recipe["continue"]
        else:
            template = recipe.get("start")
        if not template:
            raise RuntimeError(
                f"askr does not know how to run {recipe['name']!r}. "
                f"Describe it under [agent.command] in {CONFIG_FILE}."
            )
        command = list(template)
        if recipe.get("session"):
            command += recipe["session"]
        # Optional fragments are appended only when their setting has a value, so
        # an agent keeps its own defaults when askr has nothing to say.
        for key in ("model", "effort"):
            if fields[key] and recipe.get(key):
                command += recipe[key]
        if image_path and recipe.get("image"):
            command += recipe["image"]
        elif image_path:
            # No image flag for this agent, so name the file in the prompt instead.
            request = f"{request}\n\n[Attached screenshot: {image_path}]"
        command += list(settings.get("args") or [])
        # Agents that take the prompt as a flag value need that flag last.
        command += list(recipe.get("prompt") or [])
        # Substitute only the known placeholders. str.format would choke on any
        # literal brace an agent needs in an argument, such as a JSON value.
        command = [fill(part, fields) for part in command]
        command.append(request)
        return command

    def setting_for(self, table, flat_key):
        """Look up a per-agent setting, falling back to the bare key.

        A bare `model` names no agent, so it is only trusted when the config
        pins one with `name`. Otherwise askr says nothing and the agent keeps
        its own default, rather than being handed another agent's model.
        """
        settings = self.config.get("agent") or {}
        name = self.agent["name"]
        chosen = (settings.get(table) or {}).get(name)
        if chosen:
            return chosen
        if settings.get("name") == name:
            return settings.get(flat_key)
        return None

    def stream_answer(self, process, answer_parts):
        """Read the agent's stdout, appending answer text as it arrives."""
        recipe = self.agent
        if recipe.get("format") == "text":
            for line in process.stdout:
                answer_parts.append(line)
                GLib.idle_add(self.append_answer, line)
            return
        thread_path = recipe.get("thread")
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if thread_path and not self.current.get("thread_id"):
                found = dig(event, thread_path)
                if found and isinstance(found[0], str):
                    self.current["thread_id"] = found[0]
            text = self.event_text(event)
            if text:
                answer_parts.append(text)
                GLib.idle_add(self.append_answer, text)

    def run_agent(self, question, speak_reply, brief_reply, image_path):
        request = question
        if brief_reply:
            request += "\n\nAnswer in no more than three concise sentences."
        answer_parts = []
        try:
            command = self.build_command(request, self.current.get("thread_id"), image_path)
            self.debug("running:", command)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.workdir),
            )
            # stderr must be drained while stdout is read; an agent that fills
            # the stderr pipe would otherwise block forever.
            errors = []
            drain = threading.Thread(
                target=lambda: errors.append(process.stderr.read()), daemon=True
            )
            drain.start()
            self.stream_answer(process, answer_parts)
            returncode = process.wait()
            drain.join(timeout=5)
            stderr = ("".join(errors)).strip()
            if returncode != 0 and not answer_parts:
                raise RuntimeError(stderr or f"{self.agent['name']} did not return an answer.")
            answer = "".join(answer_parts).strip()
            if answer:
                self.current.setdefault("messages", []).append({"question": question, "answer": answer})
                self.save_state()
            GLib.idle_add(self.finish_answer, answer, speak_reply)
        except FileNotFoundError:
            GLib.idle_add(
                self.append_error,
                f"[Error: {self.agent['name']} is not installed. "
                f"Set one with: omarchy default agent <name>]",
            )
            GLib.idle_add(self.finish_answer, "", False)
        except Exception as error:
            GLib.idle_add(self.append_error, f"[Error: {error}]")
            GLib.idle_add(self.finish_answer, "", False)
        finally:
            if image_path:
                image_path.unlink(missing_ok=True)

    def event_text(self, event):
        """Pull answer text out of one JSON line using the agent's recipe."""
        recipe = self.agent
        for path, expected in (recipe.get("text_when") or {}).items():
            found = dig(event, path)
            if not found or found[0] not in expected:
                return ""
        for path in recipe.get("text") or []:
            values = [value for value in dig(event, path) if isinstance(value, str)]
            if values:
                return "".join(values)
        return ""

    def begin_waiting(self):
        """Show a live placeholder: agents answer in one burst, so without this
        the panel sits empty for the whole turn with no sign of progress."""
        self.end_waiting()
        self.waiting_since = time.monotonic()
        self.waiting_mark = self.answer_buffer.create_mark(
            None, self.answer_buffer.get_end_iter(), True
        )
        self.draw_waiting()
        self.waiting_timer = GLib.timeout_add(500, self.draw_waiting)

    def draw_waiting(self):
        if self.waiting_mark is None:
            return False
        start = self.answer_buffer.get_iter_at_mark(self.waiting_mark)
        self.answer_buffer.delete(start, self.answer_buffer.get_end_iter())
        elapsed = time.monotonic() - self.waiting_since
        dots = "." * (1 + int(elapsed * 2) % 3)
        self.append_text(f"{self.agent['name']} is thinking{dots} {elapsed:.0f}s", "waiting")
        GLib.idle_add(self.scroll_to_bottom)
        return True

    def end_waiting(self):
        if self.waiting_timer is not None:
            GLib.source_remove(self.waiting_timer)
            self.waiting_timer = None
        if self.waiting_mark is None:
            return
        start = self.answer_buffer.get_iter_at_mark(self.waiting_mark)
        self.answer_buffer.delete(start, self.answer_buffer.get_end_iter())
        self.answer_buffer.delete_mark(self.waiting_mark)
        self.waiting_mark = None

    def append_text(self, text, tag_name=None):
        # Deliberately does not scroll: markdown rendering splits one chunk into
        # many inserts, and scrolling per insert queues an idle callback each
        # time. Callers scroll once when the chunk is done.
        if not text:
            return
        end = self.answer_buffer.get_end_iter()
        if isinstance(tag_name, (tuple, list)):
            self.answer_buffer.insert_with_tags_by_name(end, text, *tag_name)
        elif tag_name:
            self.answer_buffer.insert_with_tags_by_name(end, text, tag_name)
        else:
            self.answer_buffer.insert(end, text)

    def append_question(self, text):
        if self.answer_buffer.get_char_count():
            self.append_text("\n\n")
        self.append_text(f"{text}\n\n", "question")
        GLib.idle_add(self.scroll_to_bottom)

    def append_answer(self, text):
        self.end_waiting()
        self.append_markdown(text)
        GLib.idle_add(self.scroll_to_bottom)

    def append_markdown(self, text):
        in_code_block = False
        for raw_line in text.splitlines(keepends=True):
            line = raw_line.rstrip("\n")
            newline = "\n" if raw_line.endswith("\n") else ""
            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                self.append_text(raw_line, ("answer", "markdown-code"))
                continue

            heading = re.match(r"^(#{1,6})\s+(.*)$", line)
            bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
            numbered = re.match(r"^(\s*\d+\.\s+)(.*)$", line)
            quote = re.match(r"^>\s?(.*)$", line)
            if heading:
                self.append_inline(heading.group(2) + newline, ("answer", "markdown-heading"))
            elif bullet:
                self.append_text(bullet.group(1) + "• ", "answer")
                self.append_inline(bullet.group(2) + newline, ("answer",))
            elif numbered:
                self.append_text(numbered.group(1), "answer")
                self.append_inline(numbered.group(2) + newline, ("answer",))
            elif quote:
                self.append_text("│ ", "answer")
                self.append_inline(quote.group(1) + newline, ("answer", "markdown-italic"))
            else:
                self.append_inline(raw_line, ("answer",))

    def append_inline(self, text, base_tags):
        token_pattern = re.compile(
            r"(\*\*[^*]+\*\*|`[^`]+`|\[[^]]+\]\([^)]+\)|(?<!\*)\*[^*]+\*(?!\*))"
        )
        position = 0
        for match in token_pattern.finditer(text):
            self.append_text(text[position:match.start()], base_tags)
            token = match.group(0)
            if token.startswith("**"):
                self.append_text(token[2:-2], (*base_tags, "markdown-bold"))
            elif token.startswith("`"):
                self.append_text(token[1:-1], (*base_tags, "markdown-code"))
            elif token.startswith("["):
                self.append_text(token[1:token.index("]")], (*base_tags, "markdown-link"))
            else:
                self.append_text(token[1:-1], (*base_tags, "markdown-italic"))
            position = match.end()
        self.append_text(text[position:], base_tags)

    def append_error(self, text):
        self.end_waiting()
        self.append_text(text, "error")
        GLib.idle_add(self.scroll_to_bottom)

    def finish_answer(self, answer, speak_reply):
        self.end_waiting()
        self.running = False
        if answer:
            threading.Thread(target=self.play_response_notification, daemon=True).start()
        if answer and speak_reply:
            threading.Thread(target=self.speak, args=(answer,), daemon=True).start()

    def play_sound(self, sound, players=None):
        """Play one file with a decoder that can actually handle its format."""
        if not sound.exists():
            self.debug("sound file missing:", sound)
            return False
        if not players:
            players = MP3_PLAYERS if sound.suffix.lower() == ".mp3" else PCM_PLAYERS
        for player in players:
            if not shutil.which(player[0]):
                continue
            result = subprocess.run(
                [*player, str(sound)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode == 0:
                return True
            self.debug("player failed:", player[0], result.returncode)
        self.debug("no player could handle", sound)
        return False

    def sound_enabled(self):
        return (self.config.get("sound") or {}).get("enabled", True)

    def play_response_notification(self):
        # Herdr's CLI notifications can be disabled independently of its
        # response sounds. Play its bundled completion MP3 locally instead.
        settings = self.config.get("sound") or {}
        if not self.sound_enabled():
            return
        sound = expand(settings["file"]) if settings.get("file") else ASSET_DIR / "herdr-done.mp3"
        self.play_sound(sound, settings.get("players"))

    def play_shutter(self):
        """Confirm a capture audibly, using the desktop's own shutter sound."""
        settings = self.config.get("sound") or {}
        if not self.sound_enabled():
            return
        configured = settings.get("shutter")
        if configured == "":
            return
        if configured:
            self.play_sound(expand(configured))
            return
        if shutil.which("canberra-gtk-play"):
            result = subprocess.run(
                ["canberra-gtk-play", "-i", SHUTTER_SOUND_ID],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode == 0:
                return
            self.debug("canberra shutter failed:", result.returncode)
        self.play_sound(SHUTTER_FALLBACK)

    def speak(self, text):
        spoken = re.sub(r"```.*?```", " code omitted ", text, flags=re.DOTALL)
        spoken = re.sub(r"`([^`]*)`", r"\1", spoken)
        spoken = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", spoken)
        spoken = re.sub(r"[*_#>|~-]+", " ", spoken)
        settings = self.config.get("voice") or {}
        piper = expand(settings.get("piper") or Path.home() / ".local" / "bin" / "piper")
        model = expand(
            settings.get("model") or DATA_DIR / "voices" / "en_US-ljspeech-high.onnx"
        )
        if not piper.exists() or not model.exists():
            self.debug("voice unavailable:", piper, model)
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as audio:
            audio_path = Path(audio.name)
        try:
            synthesis = subprocess.run(
                [str(piper), "-m", str(model), "-f", str(audio_path),
                 "--sentence-silence", "0.1"],
                input=spoken,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if synthesis.returncode == 0:
                subprocess.run(
                    ["pw-play", str(audio_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        finally:
            audio_path.unlink(missing_ok=True)

    STATE_DEFAULTS = {
        "agent": None,
        "thread_id": None,
        "messages": [],
        "archived_conversations": [],
        "voice_enabled": False,
        "brief_enabled": False,
        "answer_geometry": None,
    }

    def load_state(self):
        try:
            state = json.loads(STATE_FILE.read_text())
        except FileNotFoundError:
            state = {}
        except (OSError, json.JSONDecodeError) as error:
            # Keep the unreadable file: saving over it would destroy the only
            # copy of the conversation it still holds.
            spoiled = STATE_FILE.with_suffix(".corrupt")
            try:
                STATE_FILE.replace(spoiled)
                print(f"[askr] {STATE_FILE} unreadable ({error}); kept as {spoiled}",
                      file=sys.stderr, flush=True)
            except OSError:
                pass
            state = {}
        if not isinstance(state, dict):
            state = {}
        for key, default in self.STATE_DEFAULTS.items():
            state.setdefault(key, copy.deepcopy(default))
        return state

    def save_state(self):
        """Write history atomically, so a crash cannot leave a half-written file."""
        self.current["voice_enabled"] = self.voice_enabled
        self.current["brief_enabled"] = self.brief_enabled
        # The agent thread and the geometry timer both save, so serialise them.
        with self.state_lock:
            payload = json.dumps(self.current, ensure_ascii=False, indent=2)
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                temporary = STATE_FILE.with_suffix(".tmp")
                temporary.write_text(payload)
                temporary.replace(STATE_FILE)
            except OSError as error:
                self.debug("could not save state:", error)

    def restore_visible_history(self):
        if self.previous_agent:
            self.append_error(
                f"[Agent changed from {self.previous_agent} to {self.agent['name']}. "
                f"The previous conversation was archived and a new one started.]\n"
            )
            self.previous_agent = None
        messages = self.current.get("messages", [])
        if not messages:
            return
        for message in messages:
            self.append_question(message["question"])
            self.append_answer(message["answer"])


if __name__ == "__main__":
    Askr().run(sys.argv)
