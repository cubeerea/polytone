<div align="center">

# Polytone

**Write rough. Send it in your own voice.**

A macOS menu-bar app that turns rough notes into messages that sound like you wrote them, in any app.

[![CI](https://github.com/cubeerea/polytone/actions/workflows/ci.yml/badge.svg)](https://github.com/cubeerea/polytone/actions/workflows/ci.yml)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![Status: early preview](https://img.shields.io/badge/status-early%20preview-orange)

[Quick start](#quick-start) · [Using Polytone](#using-polytone) · [Privacy](#privacy) · [Troubleshooting](#troubleshooting) · [Docs](#documentation)

</div>

---

## What it does

Type something rough in Slack, Mail, Notes, a browser or anywhere else:

```
- tell dana launch on track, deck thurs
```

Press **⌘⇧P**. Polytone replaces it with a finished message written the way *you* write:

```
Hey Dana, launch is on track. I'll have the deck to you Thursday.
```

Polytone isn't a generic "make this sound professional" tool. It learns your sentence length,
punctuation, openers, casing and favourite words from text **you** wrote, and holds every draft
to those habits.

## Features

- **Works in any app.** One shortcut rewrites the text you're editing in place. Your clipboard is
  put back afterwards.
- **Learns only from you.** It learns from writing you paste in, your sent Gmail, Wispr Flow
  dictations, files you drop in a folder and, if you opt in, messages you type and send.
- **Never learns from AI text.** A built-in firewall rejects Polytone's own drafts and other
  AI-written text, so your style model never drifts toward "AI voice".
- **Personas.** Boss, Friend, Investor and Default ship built in, and you can add your own.
  Each one keeps its own register, so messages to your manager don't sound like texts to a friend.
- **Gets better as you use it.** When you edit a draft before sending, Polytone learns from what
  you changed. Recent writing counts more than old writing, and a weekly check rolls back changes
  that made drafts worse.
- **Shows you your style.** The Style tab maps the words, openers and punctuation that make your
  writing yours.
- **Private by design.** Everything runs on your Mac and everything you wrote is encrypted at rest.
  Only the text needed to write a draft goes to the model.

## How it works

```
  your own writing ──▶ firewall ──────────▶ style model (on your Mac)
  Gmail, Wispr, …      rejects AI text            │ style rules + a few excerpts
                                                  ▼
  rough note ──⌘⇧P──▶ Claude writes the draft ──▶ pasted in place
                                                  │ you edit before sending
  style model ◀──── learns from what you changed ◀┘
```

1. **Learn.** Polytone reads text you wrote and builds a model of your style, separately for each persona.
2. **Rewrite.** When you press the shortcut, it sends your note, your style rules and a few short
   excerpts of your own writing to Claude, and pastes the result back.
3. **Improve.** If you edit the draft before sending, Polytone learns from the difference.

The [architecture doc](docs/ARCHITECTURE.md) explains each step in detail.

## Requirements

- A recent version of macOS
- An [Anthropic API key](https://console.anthropic.com/) for real drafts. Without one, Polytone
  runs on an offline mock model, which is fine for trying it out.
- These tools, to build and run the app:

| Tool | Install |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Download from the site; it stores your style model |
| [uv](https://docs.astral.sh/uv/) | `brew install uv` |
| Node.js 20+ and [pnpm](https://pnpm.io/) | `brew install node pnpm` |
| [Rust](https://www.rust-lang.org/tools/install) (stable) | `curl https://sh.rustup.rs -sSf \| sh` |

> Polytone is an early preview and there's no signed download yet, so you build it from source.
> It takes one command.

## Quick start

```bash
git clone https://github.com/cubeerea/polytone.git
cd polytone
cp .env.example .env          # open .env and set ANTHROPIC_API_KEY=sk-ant-…
pnpm start                    # or: npm run tauri:dev
```

`pnpm start` installs anything missing, starts Docker and the style database, and opens Polytone.
The first build takes a few minutes. Polytone then lives in your menu bar. Closing the window
keeps it running, and opening the app again brings the window back.

<details>
<summary>Prefer to keep your API key out of files?</summary>

Store it in the macOS Keychain instead of `.env`:

```bash
security add-generic-password -s com.polytone.app -a anthropic-api-key -w sk-ant-…
```

If both are set, `.env` wins.
</details>

### 1. Allow Polytone to paste for you

To copy and paste in other apps, Polytone needs macOS's permission. On first launch it opens on
**Settings** with a short guide.

1. Open **System Settings → Privacy & Security → Accessibility** and turn on **Polytone**.
   *Settings → Open System Settings* takes you straight there. When you run from a terminal,
   enable that terminal app instead.
2. If the shortcut fires but nothing is copied or pasted, also turn it on under **Input Monitoring**.
3. Return to Polytone. It re-checks automatically, though some macOS versions need the app relaunched.

### 2. Teach it your style

Polytone writes like you once it has seen some of your writing. A few dozen messages per persona
is a good start, and more is better. Pick any of these on the **Style** tab under **Learns from**:

| Source | How to set it up |
|---|---|
| **Pasted writing** | **Add…**, paste emails, messages or notes you wrote, choose a persona and press ⌘↵. |
| **Gmail (sent mail)** | Run the setup once (see below), then press **Run** to import your sent mail. Inbound mail is never used. |
| **Wispr Flow** | Automatic if Wispr Flow is installed. Polytone learns each dictation from its raw transcript, never the AI-polished version. |
| **Watch folder** | Drop `.txt`, `.md`, `.mbox` or iMessage `chat.db` exports into `data/inbox`. Only messages you sent are kept. |
| **What you type** *(opt-in)* | **Settings → Learning → Learn from what I type.** Learns messages you send in the apps you allow: Slack, Messages, Mail, Notes and others. Off by default. Password fields are never read. |

<details>
<summary>Connecting Gmail</summary>

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create an OAuth
   client of type **Desktop app** with the Gmail API enabled, and download its JSON.
2. Save it as `credentials.json` in the repo folder.
3. Run this once and approve access in the browser:
   ```bash
   uv sync --extra gmail
   uv run python -m backend.mcp_servers.gmail_outbound auth
   ```
4. Press **Run** next to Gmail on the Style tab. Later runs import only new mail.

Polytone reads only mail in your Sent folder, from your own address.
</details>

## Using Polytone

1. Type or select a rough note in any text field.
2. Press **⌘⇧P**.
3. Polytone replaces the selection with a draft. If nothing is selected it uses the whole field,
   and if the field is empty it uses what's on your clipboard.

A draft takes a few seconds. If something goes wrong, you get a notification and your text is left as it was.

**Personas.** Choose the active persona from the menu-bar icon. Add your own under
**Settings → Personas**, each with a one-line tone ("warm, brief, no exclamation marks"). Deleting
a persona moves its samples to Default, so nothing you wrote is lost.

**Tabs.**

| Tab | What's there |
|---|---|
| **Drafts** | Every draft with what you typed, a 0–100 style-match score, and a **Learning** panel showing what your edits are teaching it. |
| **Style** | A map of your writing habits, plus the sources Polytone learns from. |
| **Settings** | Shortcut, insert mode, draft speed, personas, typing capture, privacy and permissions. |

**Settings you'll likely touch.**

- **Shortcut**: record a different key combo, or pause it. Pausing also stops typing capture.
- **Insert by**: *Paste* (default) or *Type*, for apps that block pasting.
- **Draft quality**: *Best* (Claude Sonnet, closest to what you wrote) or *Fast* (Claude Haiku, about 40% quicker).
- **Auto-lock after** and **Delete captured text after**: under Privacy & security.

Advanced options live in `.env` and are listed in the [configuration reference](docs/CONFIGURATION.md).

## Privacy

- **On your Mac.** Your writing samples, drafts and style model stay on your machine. Nothing is
  uploaded except what's needed to write a draft.
- **What goes to Anthropic.** Each draft request sends your rough note, your style rules and two or
  three short excerpts of your own writing. Gmail import talks to Google. All of it goes over TLS 1.3.
- **Encrypted at rest.** Everything you wrote is encrypted with a key kept in your macOS Keychain.
  The style database stores only scrambled lookups, never your words. **Back up the Keychain item
  along with `data/`**, because without it the data can't be read.
- **Locked.** Opening the app needs Touch ID or your Mac password, and it locks again when you're
  idle, when the screen locks and when the Mac sleeps.
- **You decide how long data is kept.** Set a retention period in Settings, or wipe everything at once.
- **Your data isn't used to train AI models.** Polytone only *reads* your writing to build its own
  local rules, and never learns from text a model wrote.

The full security model is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security-and-privacy).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Header says **offline** | The backend isn't running. Quit and reopen Polytone, or run `uv run polytone-api` to see the error. |
| Header says **memory graph** | Docker or the style database isn't running, so nothing learned is being saved. Start Docker Desktop, then run `docker compose up -d`. |
| **No API key** / "The model failed" | Add `ANTHROPIC_API_KEY` to `.env` and restart Polytone. |
| Shortcut does nothing | Check Accessibility and Input Monitoring (see above) and that the shortcut isn't paused. Another app may own ⌘⇧P, so try a different shortcut. |
| macOS keeps asking for permission again | Development builds change signature on every rebuild. Toggle Polytone off and on in Accessibility. |
| Text pasted but my clipboard changed | Polytone restores every clipboard format it can read. Very large items (over 64 MB) and some file promises can't be kept. |
| Gmail shows **needs credentials** | Follow the [Gmail setup](#2-teach-it-your-style). The row shows the exact command to run. |
| A draft's score is missing | Scores arrive a moment after the draft. |
| "rejected: matches a Polytone-generated draft" | Working as intended: Polytone won't learn from its own output. |

Still stuck? [Open an issue](https://github.com/cubeerea/polytone/issues) with the output of
`uv run polytone-api`, or `data/backend.log` when launched from Finder.

## FAQ

**Does it work on Windows or Linux?** Not yet. Capturing and pasting text relies on macOS accessibility APIs.

**Can I use a different model?** Drafts use Claude through the Anthropic API. Set `POLYTONE_MODEL`
to pick another Claude model. See the [configuration reference](docs/CONFIGURATION.md).

**How much does it cost?** Each draft is one short Claude request. Fast mode (Haiku) costs less.
The optional weekly quality check makes up to 18 short requests per persona.

**Will it make things up?** It's instructed to keep your content and change only the wording. Fast
mode may add a few words of its own, so use *Best* when exact meaning matters.

**How do I start over?** Settings → Privacy & security → **Delete all learned data**. This deletes all samples,
drafts and the style model.

## Documentation

| Doc | For |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | How Polytone works: components, the learning pipeline, the firewall, scoring, security |
| [Configuration](docs/CONFIGURATION.md) | Every environment variable and setting |
| [Development](docs/DEVELOPMENT.md) | Building, testing, benchmarks, release builds and contributing |
| [Agent loop](docs/AGENT_LOOP.md) | Setting up the always-on coding agent loop, in this or any other project |
| [Contracts](CONTRACTS.md) | The binding spec: HTTP API, hotkey loop, graph schema, module layout |
| [MCP servers](backend/mcp_servers/README.md) | Using Polytone's ingestion sources from an MCP client |

## Contributing

Bug reports and ideas are welcome in [issues](https://github.com/cubeerea/polytone/issues). To work
on the code, start with [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

No license has been chosen yet. Until one is added, all rights are reserved.
