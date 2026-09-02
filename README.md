# askr

A lightweight, local question overlay for [Omarchy](https://omarchy.org/) / Hyprland.

`Super + U` opens the question prompt. The answer appears in a floating, pinned
panel that keeps its position and size between openings. `Super + Shift + U`
opens or closes that panel from anywhere; `Escape` closes it from inside.

Type `/help` in either input to list the commands:

| | |
|---|---|
| `/new` | start a new conversation |
| `/brief` | three-sentence answers; `/brief on\|off` |
| `/voice` | spoken replies; `/voice on\|off` |
| `/opacity 0.8` | try a panel opacity |
| `/config` | show the settings in force |
| `/reload` | re-read `config.toml` |

`/brief` and `/voice` are the same two toggles as the ⚡ and 🔊 buttons, and
persist the same way. History lives in
`~/.local/share/askr/history.json`.

## Which agent it uses

askr follows the agent you already chose in Omarchy:

```bash
omarchy default agent          # prints the current one
omarchy default agent claude   # switch, and askr follows
```

Nothing to configure — change your Omarchy agent and askr changes with it. It
reads `~/.config/omarchy/defaults/agent`, and falls back to `codex` if unset.

Because a conversation belongs to the agent that produced it — thread ids and
models are not interchangeable — changing your default agent archives the
current conversation and starts a fresh one, rather than handing the new agent
someone else's session id. The panel says so when it happens, and the archived
conversation stays in `history.json`.

To pin askr to one agent regardless, or to teach it an agent it does not know,
copy `config.example.toml` to `~/.config/askr/config.toml`:

```toml
[agent]
name = "claude"

[agent.models]
claude = "claude-opus-5"
```

Key models by agent rather than setting a bare `model`: a bare model names no
agent, so askr only applies it when `name` pins one.

Built-in support: `codex`, `claude`, `grok`, `gemini`, `opencode`, `crush`,
`copilot`, `pi`, `omp`. Any other agent can be described under `[agent.command]`
— argv templates plus dotted paths into its JSON output. See the example config.

Conversations continue in one of three ways, depending on what the CLI offers:
`codex`, `claude` and `grok` hand back a session id that askr passes to
`--resume`; `pi` and `omp` are given a session id askr mints itself; the rest
use their own `--continue`, which picks up that agent's most recent session —
not necessarily askr's, if you also use the CLI directly.

`codex` and `claude` are verified end to end, including resume. The rest are
built from each CLI's documented headless flags; please open an issue if one
has drifted.

## Installing

Clone it and run it — there is no build step, it is a single script:

```bash
git clone https://github.com/zenyatara/askr.git ~/.local/share/askr-app
python3 ~/.local/share/askr-app/askr.py
```

An AUR package is prepared but not yet published, because AUR account
registration is paused. Once it lands, `omarchy pkg aur add askr` will install
it to `/usr/bin/askr`, with the sounds under `/usr/share/askr/assets` — askr
finds those on its own, and `ASKR_ASSETS` overrides the location if your
prefix differs.

Then add the keybinding from the next section — without it there is no
`Super+U`, which is the whole point. The window rules there are worth adding
too, though askr works without them.

## Hyprland setup

```lua
-- ~/.config/hypr/bindings.lua
-- Installed from a package:
o.bind("SUPER + U", "askr", "askr")
o.bind("SUPER + SHIFT + U", "Toggle askr answer", "askr --toggle-answer")

-- Or from a clone, matching the path used above:
-- o.bind("SUPER + U", "askr", "python3 $HOME/.local/share/askr-app/askr.py")
-- o.bind("SUPER + SHIFT + U", "Toggle askr answer",
--        "python3 $HOME/.local/share/askr-app/askr.py --toggle-answer")

-- ~/.config/hypr/hyprland.lua
o.window({ class = "^io\\.github\\.zenyatara\\.askr$", title = "^askr$" },
         { float = true, center = true, border_size = 0, size = { 560, 96 } })
o.window({ class = "^io\\.github\\.zenyatara\\.askr$", title = "^askr answer$" },
         { float = true, pin = true, border_size = 0 })
```

The keybinding is the part you need. The window rules are optional polish:
Hyprland already floats both windows on its own, because the prompt is
fixed-size and the answer panel is a dialog. What the rules add is `pin`, so
the panel stays with you when you switch workspaces, plus centring the prompt
and dropping the borders.

The answer panel's position is restored by askr itself, so its rule
deliberately carries no `move` or `size`.

## Requirements

- Hyprland 0.56+ — the panel is positioned through `hyprctl`'s Lua dispatchers
- GTK 4 with PyGObject, and Python 3.11+ (for `tomllib`)
- One of the coding agents above
- An MP3-capable player for the completion sound: `mpg123`, `ffplay` or `mpv`.
  Note that `pw-play` and `paplay` cannot decode MP3 reliably — they exit 0
  having played only part of the file.

Optional: `voxtype` for voice input, `piper` for spoken replies, `grim` to
attach a screenshot of the focused monitor to a question, and
`canberra-gtk-play` for the shutter sound.

## Attaching a screenshot

The 📷 button captures the focused monitor. askr hides itself first, so it never
photographs its own prompt. You get a shutter sound, the button turns green, and
the prompt's placeholder changes to say a screenshot is attached — it goes with
your next question, whatever you type. Anything already typed is kept.

Captures are full-resolution JPEG rather than PNG, because the agent keeps every
attachment in the conversation and re-sends them with each later question. A
handful of PNG screenshots can add tens of megabytes to every subsequent turn.
If replies start feeling slow in a long conversation with several screenshots,
`/new` starts a fresh one and speed returns immediately.

## Configuration

Everything is optional; askr runs with no config file. See
[`config.example.toml`](config.example.toml) for the full set: working
directory, agent and model, panel opacity, sounds, screenshots, and
text-to-speech.

Panel translucency is `[ui] opacity`, from `0.0` to `1.0` (default `0.5`):

```toml
[ui]
opacity = 0.8
```

To find a value you like, type `/opacity 0.8` in either input — it applies
immediately so you can judge it against your actual desktop. That is a preview
only; put the number in `config.toml` to keep it, and `/reload` restores
whatever the file says.

Only the backgrounds fade — text stays fully opaque, so the panel keeps its
contrast over a busy desktop. Use this rather than a Hyprland `opacity` window
rule, which would fade the text along with the background.

After editing the config, apply it with `/reload` in either input, or from
anywhere:

```bash
askr.py --reload   # re-read config.toml in the running instance
askr.py --quit     # exit cleanly, saving the panel's geometry
```

Reloading keeps the conversation and the panel's position. The one exception is
the agent: changing it archives the current conversation and starts a fresh one,
because thread ids are not interchangeable between agents.

`/config` reports the settings actually in force — which agent was resolved and
where from, the model it will be asked for, the working directory, and whether
optional tools like piper are present. That is usually the fastest answer to
"why is it not using my model", and the right thing to include in a bug report.
Note that agents cannot reliably name their own model variant, so asking one
directly is not a check.

Set `ASKR_DEBUG=1` to log the agent command, the Hyprland geometry dispatches
and sound playback to stderr. Note that the logged command includes your prompt
text, so avoid redirecting that output somewhere it would be kept.

## License

MIT — see [LICENSE](LICENSE).

## Credits

The completion sound (`assets/herdr-done.mp3`) and the alternate tone
(`assets/herdr-request.mp3`) are the default notification sounds from
[Herdr](https://github.com/herdrdev/herdr), extracted from its binary and
redistributed unmodified under the Apache License 2.0. If you would rather not
ship them, delete `assets/` and point `[sound] file` at a sound of your own —
askr degrades quietly when the file is missing.

The screenshot sound is not bundled: it comes from your desktop's own sound
theme via `canberra-gtk-play`.
