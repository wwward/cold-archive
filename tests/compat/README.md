# Compatibility fixtures

Archives made by real past releases of Cold Archive, kept byte-for-byte.
Every build has to check and repair every one of them:
`tests/test_compat.py` runs them through the engine, and
`packaging/compat_gate.py` runs them through the built app's own command
line and each of its bundled par2 engines before a release is signed.
Drives handed out with an older version must stay repairable by every
newer one; if a change breaks one of these archives, the change is wrong,
not the fixture.

| Fixture | Made with | par2 engine | Made on | How |
|---|---|---|---|---|
| `archives/v0.2.1` | Cold Archive 0.2.1 (the release the first Shoebox drives were made with), from its DMG | par2cmdline 1.3.0 (bundled `par2-fast`) | 2026-10-19 | `make_fixture.py`, then `protect --no-tools --label "Compatibility fixture" --media hdd` |
| `archives/v0.2.2` | Cold Archive 0.2.2, the installed app | par2cmdline 1.3.0 (bundled `par2-fast`) | 2026-10-19 | same |

Add one for every release, made with that release's own app (see
`make_fixture.py`), never with a development checkout. Never regenerate or
edit an existing fixture: its value is that an old build made it.

The fixtures leave out the TOOLS folder (`--no-tools`) to stay small; TOOLS
is an ordinary folder with its own repair data, so the same code paths
cover it. git does not keep file dates, so the tests restore the dates each
archive recorded before using it.
