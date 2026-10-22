# Copilot Window Bridge

Discover open VS Code desktop windows and send a message to a chosen window's
Copilot Chat. Continue its selected conversation, or start a fresh Local chat.
Choose what to do when the conversation is already running: queue, steer, or
interrupt.

This extension uses the VS Code command API and Node's built-in HTTP server.
It does not import the Copilot SDK or invoke Copilot CLI. The included sender
uses only Python's standard library. Your existing Copilot sign-in and normal
VS Code permissions apply.

## Requirements and validation

- VS Code desktop **1.143.0 or later**, with working Copilot Chat.
- Python 3.9 or later for `send.py`. Other local programs can use the HTTP API.
- Run the sender on the same computer as the desktop extension host. The
  extension is declared `ui`, so a Remote SSH / WSL / container workspace does
  not automatically move the listener into the remote environment.

The command contracts were verified against VS Code's **1.143.0 source tag**
and current upstream source on 22 October 2026. The HTTP bridge and dispatch
logic were tested with a simulated VS Code command interface; a live Copilot
session was not available for end-to-end testing. Workbench command arguments
can change between versions. This is an inspectable reference implementation,
not a supported external Copilot session API.

## Install

Download the VSIX or packaged distribution ZIP from
[GitHub Releases](https://github.com/insight001/copilot-window-bridge/releases).

From a source checkout, build the extension without installing dependencies:

```sh
python build.py
```

The packaged distribution ZIP produced by `build.py` includes the built VSIX
under `dist/`. GitHub's automatic **Download ZIP** contains source only; run
`python build.py` after extracting it.

1. In VS Code, run **Extensions: Install from VSIX...** from the Command Palette
   and select `dist/copilot-window-bridge-0.2.0.vsix`.
2. In each window you want to control, run **Copilot Bridge: Start for This Window**.
3. Open/focus the conversation you want to continue in each window. A fresh
   conversation can instead be created with `--new` when sending.
4. In a terminal, run `python send.py list` to discover the active bridges.

Installing the extension does not automatically start a listener. You enable
it per window. The notification shows that window's instance ID prefix;
**Show Details** shows its full ID and connection file path. The status-bar
item copies the connection file path, and the Command Palette also has
**Copilot Bridge: Copy Connection File Path**.

Run the examples from the folder containing `send.py` and `connections.py`.
On macOS/Linux you may use `python3`; on Windows, `py -3` is another option.

## Multiple windows and connection files

Every started bridge creates its own random **instance ID, port, token, and
connection file**. Two windows showing the same workspace still have separate
instances. The sender selects an instance explicitly; it never guesses from
the foreground window, a process ID, or whichever file happens to be newest.

By default, descriptors are stored in:

```text
~/.copilot-window-bridge/connections/connection-<instance-id>.json
```

On Windows, `~` is your home directory, typically `%USERPROFILE%`. The extension
writes these files automatically. You do not need to construct or edit JSON.

List the active connections:

```sh
python send.py list
```

Include stopped/unreachable or malformed descriptors left after a crash:

```sh
python send.py list --all
```

Get machine-readable metadata and connection file paths, without access tokens:

```sh
python send.py list --all --json
```

The list probes each descriptor's authenticated `/health` endpoint and checks
that the reported instance ID matches. A stale file cannot silently select a
different instance that later reuses the port. Unreachable records are hidden
unless `--all` is given; listing does not delete files. The default probe timeout
is one second and can be changed with `--timeout`.

Use `--window` with a full instance ID or a unique prefix. Use `--workspace`
with an exact workspace name if it identifies exactly one live instance.
Duplicate names or ambiguous prefixes fail before a message is sent. Window
labels and folder paths are snapshots captured when you start the bridge.

You can also select a copied file directly with `--connection`, independently
of the discovery directory. Both discovery-based and file-based sends verify
the selected instance's health before posting. IDs and credentials change when
the bridge restarts; rerun `list` after a reload.

### Do I need to bring VS Code to the foreground?

No manual foreground activation or keyboard automation is required to contact
the selected window. The extension receives the local HTTP request inside that
window, regardless of which application you are using. The workbench commands
can reveal/focus chat UI inside VS Code; this does not promise that VS Code will
never raise a window or change its internal focus.

Window selection and conversation selection are separate: `--window` identifies
the VS Code window, while the default conversation is that window's last-focused
chat. See [Targeting an existing conversation](#targeting-an-existing-conversation).

### Choose a different discovery directory

Set `COPILOT_WINDOW_BRIDGE_DIR` to an **absolute directory path** in the environment
used to launch VS Code and the sender. It replaces the entire default registry
directory. The client also supports a per-command override:

```sh
python send.py list --directory "/absolute/path/to/connections"
python send.py send --directory "/absolute/path/to/connections" --window 12345678 "Hello."
```

An environment change may require fully quitting and reopening VS Code. On
POSIX systems, the extension creates the directory with mode `0700` and files
with mode `0600`, and refuses an existing registry directory that is not private
to the current user. On Windows, protect the directory with your user-profile
access controls. A registry directory itself cannot be a symbolic link.

## Send messages

The IDs below are placeholders; use a real ID or prefix from `list`. Check that
you have the intended window:

```sh
python send.py health --window 12345678
```

Continue the current conversation. This is the default; if busy, queue the
message until the current response finishes:

```sh
python send.py send --window 12345678 "Summarize our progress."
```

Or select the only live window with an exact workspace name:

```sh
python send.py send --workspace "my-project" "Summarize our progress."
```

Steer a running conversation at its next supported opportunity:

```sh
python send.py send --window 12345678 --behavior steer "Focus on fixing the failing test first."
```

Cancel the current request and submit the new instruction:

```sh
python send.py send --window 12345678 --behavior interrupt "Stop that task and explain what is failing."
```

Start a separate Local chat in a new editor tab in the same window:

```sh
python send.py send --window 12345678 --new "Explain this project's structure."
```

The new-chat sequence opens a separate editor before creating its Local
session. It avoids the `sendToNewChat` command, which cancels the current
request. Under ordinary use, the previous conversation can continue running.
The new conversation uses VS Code's mode/model selection for that new session.

For longer or multiline prompts:

```sh
python send.py send --window 12345678 --file prompt.txt
```

Or pipe text through standard input:

```sh
python send.py send --window 12345678 --stdin
```

With `--stdin`, provide input from a pipe or type it and signal end-of-file.
The client never retries a submission automatically. To use a copied connection
path instead of discovery:

```sh
python send.py send --connection "/absolute/path/to/connection-<id>.json" "Hello."
```

The older `send.py --connection PATH MESSAGE` and `send.py --connection PATH
--health` syntax is also accepted. Restart the bridge after upgrading from 0.1
so its connection file is written to the new shared discovery directory.

## Behavior

| Selection | When idle | When running |
|---|---|---|
| Current + `queue` (default) | Send through the normal request path | Add a follow-up for after the current response |
| Current + `steer` | Send through the normal request path | Request that the current work yield and process the new message |
| Current + `interrupt` | Send normally | Cancel the current request, then send |
| `--new` | Open a separate Local chat and send | Open a separate Local chat and send; leave the old chat separate |

For non-Local session providers, queue/steer semantics are implemented by
that provider. VS Code or the provider can still pause for approvals or
reject input when a session is read-only, loading, restoring, or blocked.
Interrupting does not undo completed file edits, terminal commands, or
external actions.

### Targeting an existing conversation

The connection file selects the **VS Code window**. Within that window,
`current` means the **last-focused chat widget**, including a chat editor tab;
VS Code falls back to the sidebar chat if necessary. It is not a persistent
conversation ID or an arbitrary saved-session selector. Focus the intended
conversation before sending. Avoid changing chats during a dispatch.

Do not use this bridge while editing an earlier message, restoring a
checkpoint, or preparing delegation to another provider: those states change
the meaning of the built-in submission command. Ordinary idle and running
conversations are the intended use.

### Preserve an unsent draft

By default the bridge sends the supplied message programmatically with
`preserveInput: true`. It preserves the current draft and its attachments,
excludes those attachments from the injected message, and disables automatic
attachment of incidental editor context. Existing conversation history and
the session's tools remain available. Prompt-file slash commands in injected
text are not expanded in this mode.

Use `--composer` if you intentionally want the exact composer submission path
(including prompt-file expansion and the UI's hidden-request steering fallback):

```sh
python send.py send --window 12345678 --composer --behavior steer "Please run the next step."
```

**Composer mode replaces any unsent draft**, consumes the composer's
attachments, and can include normal implicit context. It uses the native
`queueMessage` or `steerWithMessage` command. Both send normally when idle.

### What the response means

A successful HTTP response is `202` with `"status": "dispatched"`.
It means that the VS Code command handler returned, **not that Copilot
accepted, completed, or answered the request**. Check the target chat for its
pending message, approvals, errors, and response. The workbench handler does
not expose an end-to-end delivery acknowledgment through these commands.

If a network timeout occurs, delivery is uncertain. Check the chat before
resending. Do not use a successful HTTP response as proof that a code change
or task has completed. This bridge does not scrape or export assistant replies.

## HTTP API

Read `url` and `token` from the selected window's connection file. Both routes
require `Authorization: Bearer <token>`. Use the exact `127.0.0.1` URL in the
file; hostnames, browser-origin requests, and unauthenticated requests are
rejected.

```http
GET /health
Authorization: Bearer <token>
```

```http
POST /message
Authorization: Bearer <token>
Content-Type: application/json

{
  "message": "Investigate the failing test.",
  "target": "current",
  "behavior": "queue",
  "inputMode": "preserve"
}
```

`target`: `current` or `new`; default `current`.
`behavior`: `queue`, `steer`, or `interrupt`; default `queue`.
`inputMode`: `preserve` or `composer`; default `preserve`.
`message`: nonblank text, at most 64 KiB of UTF-8.

`GET /health` returns `status`, `instanceId`, workspace/folder metadata, version
information, start time, process ID, and `pendingDispatches`. It never returns
the bearer token. `POST /message` returns a request ID and instance ID together
with the dispatch acknowledgment.

The listener serializes bridge command sequences, allowing at most 32 pending
dispatches. It does not lock the user's UI focus or serialize whole Copilot
responses. VS Code owns the actual conversation queue.

### Local access

The listener binds only to `127.0.0.1`, requires a random bearer token on every
request, rejects browser `Origin` headers, and checks the `Host` header. The
Python client disables proxies and HTTP redirects. Neither component logs
message bodies or tokens by default; the client redacts the selected token
from any reflected response. Discovery reads only the specified registry
directory and ignores non-regular or symbolic-link connection files.

**A connection file grants control of that window's chat to anyone who can
read it.** Keep it local and private. An injected prompt has the capabilities
and approval requirements of the target Copilot session. Do not expose the
listener through a network tunnel. Runtime connection files are excluded from
Git and from the release archives.

## Stop and restart

Run **Copilot Bridge: Stop for This Window** to close its listener and remove
its connection file. A window reload also stops the extension. Start again
and rerun `list` after reloading or restarting. A crash may leave a stale file,
which `list --all` reports. Old connection files cannot control a new instance.
Stopping the bridge does not cancel work already running in Copilot. Uninstall
the extension to remove it.

## Development and packaging

No npm or pip dependencies are needed. From the project directory:

```sh
node --test test-bridge.cjs test-registry.cjs
python -m unittest discover -p 'test_client.py'
python build.py
```

Tests exercise dispatch mapping with a simulated VS Code interface, the actual
loopback HTTP transport, per-window descriptors, and Python discovery/selection.
They do not launch VS Code or invoke Copilot. GitHub Actions runs the same tests
and build with Node 24 and Python 3.11.

`build.py` produces a VSIX and a source ZIP under `dist/`. Both use explicit file
allowlists and fixed ZIP metadata, excluding runtime connections, credentials,
caches, Git metadata, and previous archives. The source ZIP includes the built
VSIX. Installing a local VSIX does not require a Marketplace publisher account.

## Implementation map and sources

- `extension.js`: window lifecycle, connection file, status bar.
- `registry.js`: shared connection directory and per-window descriptor files.
- `server.js`: authenticated loopback HTTP server and dispatch sequencing.
- `dispatch.js`: validation and VS Code command mapping.
- `send.py`: Python command-line interface for listing, health, and sending.
- `connections.py`: connection validation, discovery, health probes, selection.
- `test-bridge.cjs`: local bridge/dispatch tests with simulated VS Code commands.
- `test-registry.cjs` and `test_client.py`: registry and client tests.
- `build.py`: reproducible packaging into a VSIX and ZIP using Python stdlib.

Primary source references:

- [VS Code chat interaction and queue/steer behavior](https://code.visualstudio.com/docs/chat/chat-overview)
- [Executing VS Code commands from an extension](https://code.visualstudio.com/api/extension-guides/command)
- [1.143.0: chat command arguments and New Chat Editor](https://github.com/microsoft/vscode/blob/1.143.0/src/vs/workbench/contrib/chat/browser/actions/chatActions.ts)
- [1.143.0: submission command argument forwarding](https://github.com/microsoft/vscode/blob/1.143.0/src/vs/workbench/contrib/chat/browser/actions/chatExecuteActions.ts)
- [1.143.0: input options, including preserveInput](https://github.com/microsoft/vscode/blob/1.143.0/src/vs/workbench/contrib/chat/browser/chat.ts)
- [1.143.0: queue and steer commands](https://github.com/microsoft/vscode/blob/1.143.0/src/vs/workbench/contrib/chat/browser/actions/chatQueueActions.ts)
- [1.143.0: Local chat creation](https://github.com/microsoft/vscode/blob/1.143.0/src/vs/workbench/contrib/chat/browser/actions/chatNewActions.ts)
- [1.143.0: chat widget selection](https://github.com/microsoft/vscode/blob/1.143.0/src/vs/workbench/contrib/chat/browser/widget/chatWidgetService.ts)

Licensed under the [MIT License](LICENSE). This extension is not published to
the Visual Studio Marketplace and is not affiliated with Microsoft or GitHub.
