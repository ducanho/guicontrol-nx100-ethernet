"""Explicit simulator. No sockets, pulse/angle conversion or real robot access."""

import time


class MockClient:
    def __init__(self, ip, port=80, timeout=3, trace=None):
        self.ip, self.port = ip, port
        self.trace = trace or (lambda *args: None)
        self.pose = [1000.0, 200.0, 500.0, 180.0, 0.0, 90.0]
        self.origin = self.pose[:]
        self.target = None
        self.started = 0
        self.duration = 3
        self.word1, self.word2 = 194, 64

    def _advance(self):
        if self.target is None:
            return
        fraction = min(1, (time.monotonic() - self.started) / self.duration)
        self.pose = [a + (b - a) * fraction for a, b in zip(self.origin, self.target, strict=True)]
        if fraction >= 1:
            self.target = None
            self.word1 &= ~8

    def execute(self, name, data=""):
        self._advance()
        self.trace("TX", f"MOCK {name} {data}".encode())
        if name == "RSTATS":
            result = f"{self.word1},{self.word2}"
        elif name == "RPOSC":
            result = ",".join(f"{v:.3f}" for v in self.pose) + ",1,0,0,0,0,0,0,0"
        elif name == "RPOSJ":
            # Independent sample pulse readings; no fictitious inverse kinematics.
            result = "12000,-18000,24000,0,6000,-3000,0,0,0,0,0,0"
        elif name in {"MOVL", "MOVJ"}:
            offset = 3 if name == "MOVL" else 2
            self.origin = self.pose[:]
            self.target = [float(v) for v in data.split(",")[offset : offset + 6]]
            self.started = time.monotonic()
            self.word1 |= 8
            result = "0000"
        elif (name, data) == ("HOLD", "1"):
            self.target = None
            self.word1 &= ~8
            self.word2 |= 8
            result = "0000"
        else:
            raise ValueError("Unsupported mock command")
        self.trace("RX", result.encode())
        return result
