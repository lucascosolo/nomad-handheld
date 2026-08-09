"""`python -m nomad` — switch the device on, or ask it something.

Boots the composition root against whatever `nomad.toml` selects, which by
default is mocks the whole way down: no hardware, no network, no credentials
(D9). With no subcommand it then waits. Nomad is a session, not a command —
the process staying alive *is* the product.

Shutdown is a signal, because on the device it always will be: SIGTERM from
systemd, SIGINT from a terminal, and in both cases the session gets to close
its turns and the database gets closed cleanly rather than being killed
mid-write. A startup failure exits non-zero so a supervisor can see it.

The subcommands live in `nomad.cli`; this module is the two lines that make
`python -m nomad` and the `nomad` console script the same program.
"""

from __future__ import annotations

from nomad.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
