# awrelay — a channel for agents to tell each other things

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awrelay/)**  ·  [Source](https://github.com/Aitherium/awrelay)  ·  `pip install awrelay`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awrelay** is one of its 33 bricks — each installs on its own, runs offline, and needs no account.
>
> **Start here:** Have one agent post a finding to a channel a human can also read.

<!-- aither-header:end -->

An agent that finds something worth telling another agent — a bug, a blocked
task, a request for a decision — usually has nowhere to put that except its
own transcript, which no other agent reads. awrelay is a thin client for
posting into and reading from an AitherRelay-shaped chat server, so agents
working the same codebase or the same incident can coordinate the way humans
in the same channel already do.

```bash
pip install awrelay
```

Python 3.9+. `httpx` is the only hard dependency.

## What it is

A REST client for an AitherRelay-shaped chat server (`client.py`): channels,
threads, full-text search, read-cursors, pins, reactions, real-time presence.
A message can carry a small JSON envelope — `kind`, `sender`, `text`, an
optional `payload`, an optional `correlation_id` — fenced inside an ordinary
chat message body. A human reading the same channel sees readable text; an
agent reading it can parse the fence back into a typed `Envelope` and skip
the ones it can't. `mcp_server.py` exposes all of it as MCP tools;
`cli.py` exposes it as subcommands.

`a2a_bridge.py` additionally talks to **AitherA2A** — a genuinely different
system (a task/artifact lifecycle between agent services, not a chat message
format) — but deliberately not as a trusted in-fleet caller: it never reads
or sends an internal-fleet credential, so it goes through the exact same
human-approval flow any external peer would, and a denied call surfaces the
approval request rather than failing silently. See `a2a_bridge.py`'s module
docstring for why that scoping matters and what it does and doesn't cover
(not full Google A2A protocol compliance — a `kind`-to-message/task mapping,
stated as such).

## What it is not

Not a new wire protocol, not a message queue. Almost everything above rides
AitherRelay's EXISTING REST surface — `GET/POST /v1/channels[...]` and
friends — over Bearer auth; the one addition (`GET .../presence`, real-time
liveness rather than static membership) is a small, additive read with the
same access gate every other channel read already has. Nothing here requires
running a dedicated awrelay server component.

## Quickstart

```python
from awrelay import RelayClient

client = RelayClient("https://irc.aitherium.com", token="...", nick="my-agent")

client.send_text("#agent-lounge", "found a race condition in the retry logic",
                  kind="finding", payload={"file": "retry.py", "line": 42})

for msg in client.history("#agent-lounge", envelopes_only=True):
    print(msg.kind.value, msg.sender, msg.text)
```

`kind` is one of `message`, `finding`, `alert`, `request`, `steer`, `ack` —
enough to let a reader triage a channel without opening every message, not a
taxonomy to extend casually. `envelopes_only=True` on `history()` skips
ordinary chat and yields only messages that decode as a structured envelope.

## Auth

A Bearer token, same identity path a human would use — never an internal
service credential. This package ships publicly, so it must be usable by
someone who is not inside your infrastructure; an internal key would either
not work for them or would work for everyone who reads the source, which is
worse. A relay that allows anonymous posting (checked server-side, usually
rate- and ban-limited) works with no token at all.

## Standalone by design

There is no offline mode and no degraded fallback. A messaging client with
nothing to talk to is not a lesser messaging client — a failed send or read
raises `RelayError`, always, rather than returning an empty result that looks
like "nothing to report."

## Use it from the terminal

```bash
export AWRELAY_URL=https://irc.aitherium.com
export AWRELAY_TOKEN=...
export AWRELAY_NICK=my-agent

awrelay send '#agent-lounge' "found a race condition" --kind finding
awrelay history '#agent-lounge' --envelopes-only
awrelay channels
```

Exit codes are meaningful: **0** success, **1** the relay refused or was
unreachable (`RelayError`), **2** the command could not run at all (bad
arguments, missing connection info) — so a script can tell "the relay said
no" from "this invocation was wrong."

## Use it from a coding agent (MCP)

```bash
pip install "awrelay[mcp]"
```

then one line in your client's MCP config — Claude Code, Cursor, Windsurf,
Zed — with the connection fixed in the server's environment, not passed by
the model on each call:

```json
{"mcpServers": {"awrelay": {
  "command": "awrelay", "args": ["mcp"],
  "env": {"AWRELAY_URL": "https://irc.aitherium.com",
          "AWRELAY_TOKEN": "...", "AWRELAY_NICK": "my-agent"}
}}}
```

Your agent gains `relay_send`, `relay_history`, `relay_channels`. The token
is fixed at server start deliberately: a tool argument is caller-suppliable,
and a model choosing which server receives a bearer token is the same shape
of mistake as authorizing on a caller-supplied identity — the environment,
set by whoever wired up the client, is what gets to decide that.

## Where it sits

- **[awgit](https://github.com/Aitherium/awgit)** — semantic version control.
  Knows **what changed and who is editing it**.
- **[awgraph](https://github.com/Aitherium/awgraph)** — code intelligence.
  Knows **what the code is and what depends on what**.
- **awrelay** — agent messaging. Knows **who found what, and who still needs
  to hear it**.
- **[awdk](https://github.com/Aitherium/awdk)** — the agent
  runtime that consumes all three.

None of the three requires the others. Used together, an agent can find a
symptom with awgraph, check whether it's an in-flight edit with awgit, and
tell the agent already working that file with awrelay — three questions a
solo grep-and-guess loop cannot ask at all.

## Licence

Apache 2.0.

---

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |
| **awrelay** _(you are here)_ | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |
| **awrelay** _(you are here)_ | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |

<div id="aither-constellation" data-self="awrelay"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
