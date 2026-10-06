# awrelay for agents

Read this if you are an agent (or a human) editing this package. Short on
purpose: the commands, the traps that cost a session, and where the rest lives.
Nothing here is read at runtime — it is for you.

## What this is

PyPI distribution **`awrelay`** (version in `pyproject.toml`), import package
`awrelay`, Python >= 3.10. A channel for agents to tell each other things — the
client side of the relay: channels, structured envelopes, A2A bridge, doors.

This repository is a **synced mirror** of the AitherOS monorepo (lane
`.github/workflows/sync-awrelay.yml`). Hand edits made here are overwritten on
the next sync — change the source and let the lane publish.

## Build, test, verify

```bash
python -m pytest tests -q        # the suite: 126 tests, green at v0.5.0
pip install -e .                 # editable install for developing against it
```

The suite was run from a source checkout with no prior install. The publish
lane (`publish-brick.yml`) additionally builds the wheel, installs it and
imports it — a tree that tests green can still ship a broken wheel.

## Rules that keep this useful

- **A relay change is a protocol change.** The door handshake and first-party
  CLI identity are pinned by name (`test_door_client.py`,
  `test_cli_first_party_identity.py`, `test_hookgate.py`); keep new behaviour
  under its own named test, because two clients disagreeing about a handshake
  looks like the network misbehaving.
- **Envelopes are structured, and both ends read the same fields.** The
  bridge tests (`test_a2a_bridge.py`) exist to keep the wire honest — a field
  added on one side only is a silent drop, not an error.
- **The registry drives the public surface.** This repo's README header,
  `llms.txt` and `aither-manifest.json` are generated from the ecosystem
  registry (one yaml in the AitherOS monorepo) and rewritten on every sync.
  Change the registry; do not hand-edit the generated blocks.
- **The install line is a measured claim.** `check_ecosystem_install_lines`
  asserts the advertised `pip install` channel is real and ours. A rename or
  a move lands with the registry entry in the same change.

## Read next

- `llms.txt` — the install/use card written for an agent to execute
- `README.md` — the human front door
- `docs/` — the generated docs site source
