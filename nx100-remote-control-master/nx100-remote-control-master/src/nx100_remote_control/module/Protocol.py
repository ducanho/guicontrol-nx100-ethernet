"""NX100 Ethernet Server, HW0483202 sections 3.1 and 5.4.

One connection per command; no retries after sending. Importing sends nothing.
Only explicitly validated commands are accepted. No HTTP requests or YERC UDP.
"""
import ipaddress
import math
import re
import socket
import threading
import time


class ProtocolError(RuntimeError):
    pass


class OutcomeUnknown(ProtocolError):
    """The command may have executed. Never automatically retry it."""


_WIRE_LOCK = threading.Lock()
_NO_DATA = {"RSTATS", "RPOSJ", "RALARM", "RJSEQ", "RESET", "CANCEL"}
_CONTROL = {"HOLD", "SVON", "RESET", "CANCEL", "START", "SETMJ", "MOVJ", "MOVL", "IOWRITE"}


def _integer(value, lower, upper):
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("Expected an unsigned decimal integer")
    number = int(value)
    if not lower <= number <= upper:
        raise ValueError("Integer outside allowed range")
    return number


def _number(value):
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value):
        raise ValueError("Expected a finite decimal number (no exponent)")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite number")
    return result


def encode_command(name, data=""):
    """Validate everything before opening a socket, then return exact wire bytes."""
    if not isinstance(name, str) or not isinstance(data, str):
        raise ValueError("Command and data must be strings")
    if any(ord(c) < 32 or ord(c) > 126 for c in name + data):
        raise ValueError("Only printable ASCII is accepted; CR/LF are added internally")
    fields = [item.strip() for item in data.split(",")]
    if name in _NO_DATA:
        if data:
            raise ValueError(name + " takes no data")
    elif name in {"HOLD", "SVON"}:
        if data not in {"0", "1"}:
            raise ValueError(name + " requires exactly 0 or 1")
    elif name == "RPOSC":
        if len(fields) != 2:
            raise ValueError("RPOSC requires coordinate system and external-axis flag")
        _integer(fields[0], 0, 25)
        _integer(fields[1], 0, 1)
        data = ",".join(fields)
    elif name in {"START", "SETMJ"}:
        if name == "START" and data == "":
            pass  # Manual: resume current execution job, at current line.
        elif not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", data):
            raise ValueError("JOB: 1..32 ASCII letters, digits, underscore or hyphen; no .JBI")
    elif name in {"MOVJ", "MOVL"}:
        linear = name == "MOVL"
        if len(fields) != (17 if linear else 16):
            raise ValueError("Wrong number of motion fields")
        offset = int(linear)
        if linear:
            _integer(fields[0], 0, 1)
        speed = _number(fields[offset])
        if (linear and speed < 0.1) or (not linear and not 0.01 <= speed <= 100):
            raise ValueError("Motion speed outside manual range")
        _integer(fields[1 + offset], 0, 25)
        for item in fields[2 + offset:8 + offset]:
            _number(item)
        _integer(fields[8 + offset], 0, 63)  # posture bits
        _integer(fields[9 + offset], 0, 23)  # tool
        for item in fields[10 + offset:]:
            _number(item)
        data = ",".join(fields)
    elif name in {"IOREAD", "IOWRITE"}:
        if len(fields) < 2:
            raise ValueError("I/O requires start contact and bit count")
        start = _integer(fields[0], 0, 99999)
        count = _integer(fields[1], 8, 256)
        if start % 10 > 7:
            raise ValueError("Contact numbers end in bit index 0..7")
        if count % 8 or len(fields) != (2 if name == "IOREAD" else 2 + count // 8):
            raise ValueError("I/O count must be whole bytes with matching byte values")
        if name == 'IOWRITE':
            last_bit = (start // 10) * 8 + start % 10 + count - 1
            if start < 22010 or last_bit > (2328 * 8 + 7):
                raise ValueError("IOWRITE supports only network inputs #22010..#23287")
        for item in fields[2:]:
            _integer(item, 0, 255)
        data = ",".join(fields)
    else:
        raise ValueError("Unsupported/unverified command: " + name)
    payload = (data + "\r").encode("ascii") if data else b""
    if len(payload) > 256:
        raise ValueError("NX100 command data exceeds 256 bytes")
    header = ("HOSTCTRL_REQUEST %s %d\r\n" % (name, len(payload))).encode("ascii")
    return header, payload


class _Reader:
    def __init__(self, sock, timeout, trace):
        self.sock = sock
        self.buffer = bytearray()
        self.timeout = timeout
        self.trace = trace

    def until(self, ending):
        deadline = time.monotonic() + self.timeout
        while True:
            end = self.buffer.find(ending)
            if end >= 0:
                stop = end + len(ending)
                result = bytes(self.buffer[:stop])
                del self.buffer[:stop]
                return result[:-len(ending)].decode("ascii")
            if len(self.buffer) > 65536:
                raise ProtocolError("Response exceeds receive limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Response deadline expired; pending RX=" + repr(bytes(self.buffer)))
            self.sock.settimeout(remaining)
            try:
                block = self.sock.recv(4096)
            except TimeoutError as exc:
                raise TimeoutError("Receive timed out; pending RX=" + repr(bytes(self.buffer))) from exc
            if not block:
                raise ProtocolError("Connection closed before complete response; pending RX=" + repr(bytes(self.buffer)))
            # Log immediately, including incomplete frames; do not hide a partial ACK.
            self.trace("RX", block)
            self.buffer.extend(block)


class Client:
    def __init__(self, ip, port=80, timeout=5.0, trace=None):
        self.ip = str(ipaddress.IPv4Address(ip))
        if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Port must be 1..65535")
        self.port = int(port)
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 60:
            raise ValueError("Timeout must be > 0 and <= 60 seconds")
        self.trace = trace or (lambda direction, wire: None)

    def execute(self, name, data=""):
        header, payload = encode_command(name, data)
        attempted = False
        stage = 'TCP'
        with _WIRE_LOCK:
            try:
                with socket.create_connection((self.ip, self.port), self.timeout) as sock:
                    stage = 'CONNECT Robot_access (bắt tay)'
                    reader = _Reader(sock, self.timeout, self.trace)

                    def send(wire):
                        self.trace("TX", wire)
                        sock.sendall(wire)

                    send(b"CONNECT Robot_access\r\n")
                    greeting = reader.until(b"\r\n")
                    if not re.fullmatch(r"OK: NX Information Server\s*\([^\r\n]+\)\.", greeting):
                        raise ProtocolError("Handshake rejected: " + greeting)
                    # A zero-data command can execute as soon as this header is received.
                    stage = 'HOSTCTRL_REQUEST / ACK ' + name
                    attempted = not payload
                    send(header)
                    ack = reader.until(b"\r\n")
                    if ack != "OK: " + name:
                        if ack.startswith(('NG:', 'ERROR:')):
                            attempted = False  # Explicit rejection; do not send payload.
                        raise ProtocolError("Command rejected: " + ack)
                    if payload:
                        stage = 'gửi dữ liệu ' + name
                        attempted = True
                        send(payload)
                    stage = 'đọc kết quả ' + name
                    result = reader.until(b"\r")
                    if result.startswith(("ERROR:", "NG:")):
                        attempted = False
                        raise ProtocolError(result)
                    if name in _CONTROL:
                        if reader.until(b"\n") != "":
                            raise ProtocolError("Control answer must end with CRLF")
                        if result != "0000":
                            raise ProtocolError("Unexpected control answer: " + result)
                    return result
            except (OSError, UnicodeError, ProtocolError) as exc:
                if stage == 'TCP':
                    raise ProtocolError(
                        'Không kết nối được TCP tới %s:%d (timeout cấu hình %g giây). '
                        'Chưa gửi lệnh NX100. Kiểm tra IP trên pendant, subnet của cổng Ethernet '
                        'máy tính, dây LAN và port/Ethernet Server. Chi tiết: %s'
                        % (self.ip, self.port, self.timeout, exc)
                    ) from exc
                if attempted and name in _CONTROL:
                    raise OutcomeUnknown(
                        name + ": chưa xác định lệnh đã thực thi hay chưa. "
                        "Không tự gửi lại; kiểm tra robot/trạng thái. Chi tiết: " + str(exc)
                    ) from exc
                raise ProtocolError('%s:%d — %s: %s' % (self.ip, self.port, stage, exc)) from exc
