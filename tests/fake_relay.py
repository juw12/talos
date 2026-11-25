"""The relay's rules in memory, for tests on this computer.

They are the ones of talos/relay_local.py (the relay inside Talos), which follow the Pages Function
(relay/functions/c/[[path]].js): same paths, limits and answers, so `talos relay` and the phone page
meet here what they meet on Cloudflare. It also hands out the phone page (relay/site), so a real
browser can be pointed at it. Everything is kept in dictionaries and forgotten when the test ends.
"""
from __future__ import annotations

from pathlib import Path

from talos.relay_local import LocalRelay

SITE = Path(__file__).resolve().parent.parent / "relay" / "site"


class FakeRelay(LocalRelay):
    def __init__(self):
        super().__init__("127.0.0.1", 0, SITE)
        self.seen = []                            # every sealed body that passed through, for "it is sealed" checks
