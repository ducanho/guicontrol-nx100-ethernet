#!/usr/bin/env python3
"""NX100 variable editor. Python 3.10+, standard library only.

Yaskawa HW0483202: section 3.1, SAVEV (3-12/13), LOADV (3-26/27).
Only variable read/write; no motion, servo, JOB, I/O or parameter commands.
Stop JOBs and isolate the cell before testing unused, reserved variables.
The program does not implement a safety interlock. Writes are real changes.
"""
from __future__ import annotations

import math
import queue
import re
import socket
import threading
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox

HOST, PORT = '192.168.255.1', 80
TYPE_CODES = {'B': 0, 'I': 1, 'D': 2, 'R': 3, 'P': 4}
LIMITS = {'B': (0, 255), 'I': (-32768, 32767),
          'D': (-2147483648, 2147483647)}


class NXError(Exception):
    pass


class ControllerError(NXError):
    pass


class WriteUnknown(NXError):
    pass


def variable(name: str) -> tuple[str, int, str]:
    match = re.fullmatch(r'([BIDRP])([0-9]{1,3})', name.strip().upper())
    if not match:
        raise ValueError('Nhập tên biến như I099, D010, R010, B099 hoặc P099.')
    kind, digits = match.groups()
    index = int(digits)
    # Standard allocation in the supplied Handling manual, not expanded options.
    maximum = 127 if kind == 'P' else 99
    if index > maximum:
        raise ValueError(f'Bản này hỗ trợ {kind}000..{kind}{maximum:03d}.')
    return kind, index, f'{kind}{index:03d}'


def integer(text: str, low: int, high: int) -> str:
    if len(text) > 40 or not re.fullmatch(r'[+-]?[0-9]+', text.strip()):
        raise ValueError('Giá trị phải là số nguyên.')
    value = int(text)
    if not low <= value <= high:
        raise ValueError(f'Giá trị phải trong [{low}, {high}].')
    return str(value)


def number(text: str) -> Decimal:
    if len(text) > 90 or not re.fullmatch(
            r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?', text.strip()):
        raise ValueError('Số không hợp lệ; dùng dấu chấm thập phân.')
    try:
        result = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError('Số không hợp lệ.') from exc
    if not result.is_finite() or abs(result) > Decimal('3.4e38'):
        raise ValueError('Giá trị ngoài phạm vi.')
    return result


def fixed(text: str, places: int) -> str:
    value = number(text)
    if abs(value) > Decimal('1e9'):
        raise ValueError('Tọa độ quá lớn cho công cụ này.')
    if value != value.quantize(Decimal(1).scaleb(-places)):
        raise ValueError(f'Chỉ nhập tối đa {places} chữ số thập phân.')
    return format(value if value else Decimal(0), f'.{places}f')


def normalize(kind: str, raw: str) -> str:
    """Validate a SAVEV-format value. The same value is appended to LOADV."""
    if kind in LIMITS:
        return integer(raw, *LIMITS[kind])
    if kind == 'R':
        value = number(raw)
        if value and abs(value) < Decimal('1.18e-38'):
            raise ValueError('Giá trị R khác 0 nhưng quá nhỏ.')
        return format(value if value else Decimal(0), 'f')
    if kind != 'P':
        raise ValueError('Chỉ hỗ trợ B, I, D, R, P.')
    fields = [part.strip() for part in raw.strip().rstrip(',').split(',')]
    if fields[0] == '1' and len(fields) == 10:  # Cartesian
        fields[1] = integer(fields[1], 0, 27)
        for pos in range(2, 8):
            fields[pos] = fixed(fields[pos], 3 if pos < 5 else 2)
        fields[8] = integer(fields[8], 0, 63)  # Posture bit mask, not an angle.
        fields[9] = integer(fields[9], 0, 23)
    elif fields[0] == '0' and len(fields) == 8:  # Six robot-axis pulses + tool.
        fields[1:7] = [integer(x, *LIMITS['D']) for x in fields[1:7]]
        fields[7] = integer(fields[7], 0, 23)
    else:
        raise ValueError('Dữ liệu P không đúng dạng 6 trục: ' + raw)
    return ','.join(fields)


def editor_data(kind: str, raw: str) -> tuple[list[str], list[str], str]:
    canonical = normalize(kind, raw)
    if kind != 'P':
        return ['Giá trị'], [canonical], kind + ' - biến số'
    parts = canonical.split(',')
    if parts[0] == '1':
        frame = int(parts[1])
        frame_name = {0: 'BASE', 1: 'ROBOT', 26: 'TOOL', 27: 'MASTER TOOL'}.get(frame, f'USER {frame - 1}')
        labels = ['X (mm)', 'Y (mm)', 'Z (mm)', 'Rx / TX (deg)', 'Ry / TY (deg)', 'Rz / TZ (deg)']
        info = f'Cartesian | {frame_name} | Tool {parts[9]} | Posture {parts[8]}'
        return labels, parts[2:8], info + ' - giữ nguyên cấu hình P'
    return ['S (pulse)', 'L (pulse)', 'U (pulse)', 'R (pulse)', 'B (pulse)', 'T (pulse)'], parts[1:7], f'PULSE | Tool {parts[7]}'


def edited_value(kind: str, before: str, values: list[str]) -> str:
    before = normalize(kind, before)
    if kind != 'P':
        if len(values) != 1:
            raise ValueError('Thiếu giá trị biến.')
        return normalize(kind, values[0])
    if len(values) != 6:
        raise ValueError('P cần 6 giá trị.')
    fields = before.split(',')
    if fields[0] == '1':
        fields[2:8] = values
    else:
        fields[1:7] = values
    return normalize(kind, ','.join(fields))


def matches(kind: str, expected: str, actual: str) -> bool:
    left, right = normalize(kind, expected), normalize(kind, actual)
    if kind == 'R':
        return math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=0.0)
    return left == right


class NX100:
    """One socket per command, avoiding persistent-session idle-timeout issues.

    connect() checks the TCP/protocol handshake; it does not start a robot JOB.
    Never automatically retry a write, including after timeout/disconnection.
    """
    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = 5.0):
        self.host, self.port, self.timeout = host, int(port), float(timeout)

    def connect(self) -> str:
        return self._exchange()

    def _exchange(self, command: str = '', data: str = '') -> str:
        if command not in ('', 'SAVEV', 'LOADV'):
            raise ValueError('Chỉ cho phép SAVEV/LOADV.')
        if any(ord(c) < 32 or ord(c) > 126 for c in data) or len(data) > 1024:
            raise ValueError('Dữ liệu giao thức không hợp lệ.')
        payload = (data + '\r').encode('ascii') if data else b''
        attempted = False
        phase = 'TCP'
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                buffer = bytearray()

                def receive(delimiter: bytes) -> str:
                    deadline = time.monotonic() + self.timeout
                    while True:
                        pos = buffer.find(delimiter)
                        if pos >= 0:
                            value = bytes(buffer[:pos])
                            del buffer[:pos + len(delimiter)]
                            return value.decode('ascii')
                        if len(buffer) > 65536:
                            raise NXError('Phản hồi quá dài.')
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Timeout')
                        sock.settimeout(remaining)
                        chunk = sock.recv(4096)
                        if not chunk:
                            raise ConnectionError('NX100 đóng kết nối trước khi trả đủ dữ liệu.')
                        buffer.extend(chunk)

                phase = 'CONNECT'
                sock.sendall(b'CONNECT Robot_access\r\n')
                banner = receive(b'\r\n')
                if not banner.startswith('OK: NX Information Server'):
                    raise ControllerError(banner)
                if not command:
                    return banner
                phase = command + ' header'
                sock.sendall(f'HOSTCTRL_REQUEST {command} {len(payload)}\r\n'.encode('ascii'))
                ack = receive(b'\r\n')
                if not re.fullmatch(r'OK:\s*' + command, ack):
                    raise ControllerError(ack)
                phase = command + ' data/reply'
                attempted = True
                sock.sendall(payload)
                answer = receive(b'\r')
                if answer.startswith(('ERROR:', 'NG:')):
                    receive(b'\n')
                    raise ControllerError(answer)
                if command == 'LOADV':
                    tail = receive(b'\n')
                    if answer != '0000' or tail:
                        raise NXError('LOADV phản hồi khác 0000<CR><LF>: ' + repr(answer + tail))
                return answer
        except ControllerError:
            raise
        except Exception as exc:
            detail = f'{phase}: {exc}'
            if command == 'LOADV' and attempted:
                raise WriteUnknown('CHƯA RÕ KẾT QUẢ GHI. Đọc lại biến trước khi thử ghi lại.\n' + detail) from exc
            raise NXError(detail) from exc

    def read(self, name: str) -> str:
        kind, index, _ = variable(name)
        raw = self._exchange('SAVEV', f'{TYPE_CODES[kind]},{index}')
        return normalize(kind, raw)

    def write(self, name: str, value: str) -> str:
        kind, index, _ = variable(name)
        value = normalize(kind, value)
        return self._exchange('LOADV', f'{TYPE_CODES[kind]},{index},{value}')

    def write_and_read(self, name: str, value: str) -> tuple[str, bool]:
        kind, _, _ = variable(name)
        self.write(name, value)
        try:
            actual = self.read(name)
        except Exception as exc:
            raise WriteUnknown('LOADV đã trả 0000, nhưng đọc lại bị lỗi. Không tự ghi lại.\n' + str(exc)) from exc
        return actual, matches(kind, value, actual)


class App:
    def __init__(self, root: tk.Tk, client: NX100 | None = None):
        self.root, self.client = root, client or NX100()
        self.events = queue.Queue()
        self.busy, self.ready = False, False
        self.loaded = None
        self.inputs = []
        root.title('NX100 - Đọc / ghi biến')
        root.geometry('770x610')
        root.minsize(740, 610)
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='NX100 | Đọc / ghi biến', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
        top = ttk.Frame(outer)
        top.pack(fill='x', pady=(12, 10))
        ttk.Label(top, text=f'IP: {self.client.host}     Port: {self.client.port}').pack(side='left')
        self.connect_button = ttk.Button(top, text='Kết nối', command=self.connect)
        self.connect_button.pack(side='right')
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text='Tên biến:').pack(side='left')
        self.name = tk.StringVar(value='I099')
        self.name_entry = ttk.Entry(row, textvariable=self.name, width=12)
        self.name_entry.pack(side='left', padx=8)
        self.read_button = ttk.Button(row, text='Đọc', command=self.read)
        self.read_button.pack(side='left')
        ttk.Label(row, text='B / I / D / R / P').pack(side='left', padx=12)
        self.info = tk.StringVar(value='Nhập tên biến, bấm Đọc.')
        ttk.Label(outer, textvariable=self.info, wraplength=700).pack(anchor='w', pady=7)
        self.table = ttk.Frame(outer)
        self.table.pack(fill='x')
        self.write_button = ttk.Button(outer, text='Ghi giá trị mới', command=self.write)
        self.write_button.pack(anchor='e', pady=10)
        self.result = tk.StringVar(value='Chưa kết nối.')
        ttk.Label(outer, textvariable=self.result, wraplength=700).pack(anchor='w', pady=8)
        ttk.Separator(outer).pack(fill='x', pady=(8, 6))
        warning = ('Chỉ thử biến đã dành riêng: dừng JOB, Servo OFF, cô lập ngoại vi. '
                   'Ghi biến có thể ảnh hưởng JOB/CIO. Phần mềm không kiểm tra liên động an toàn.')
        ttk.Label(outer, text=warning, wraplength=700).pack(anchor='w')
        self.name.trace_add('write', lambda *_: self.clear())
        self.refresh()
        self.timer = root.after(50, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def clear(self):
        self.loaded = None
        self.inputs.clear()
        for widget in self.table.winfo_children():
            widget.destroy()
        self.info.set('Bấm Đọc để lấy giá trị của biến đã chọn.')
        self.refresh()

    def refresh(self):
        self.connect_button.configure(state='disabled' if self.busy else 'normal')
        self.name_entry.configure(state='disabled' if self.busy else 'normal')
        self.read_button.configure(state='normal' if self.ready and not self.busy else 'disabled')
        self.write_button.configure(state='normal' if self.ready and self.loaded and not self.busy else 'disabled')
        for _, entry in self.inputs:
            entry.configure(state='disabled' if self.busy else 'normal')

    def run(self, action, success):
        if self.busy:
            return
        self.busy = True
        self.result.set('Đang chờ NX100...')
        self.refresh()

        def worker():
            try:
                self.events.put((success, action(), None))
            except Exception as exc:
                self.events.put((None, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def connect(self):
        self.clear()
        self.ready = False
        def success(banner):
            self.ready = True
            self.result.set('Đã kiểm tra kết nối. ' + banner)
        self.run(self.client.connect, success)

    def read(self):
        try:
            _, _, name = variable(self.name.get())
        except ValueError as exc:
            self.result.set(str(exc))
            return
        self.clear()
        self.run(lambda: self.client.read(name), lambda raw: self.show(name, raw))

    def show(self, name, raw):
        self.clear()
        kind, _, _ = variable(name)
        labels, values, info = editor_data(kind, raw)
        self.loaded = (name, raw)
        self.info.set(name + ' | ' + info)
        for column, text in enumerate(('Trường', 'Giá trị vừa đọc', 'Giá trị mới')):
            ttk.Label(self.table, text=text, font=('Segoe UI', 10, 'bold')).grid(row=0, column=column, sticky='w', padx=6, pady=4)
        for row, (label, value) in enumerate(zip(labels, values), 1):
            ttk.Label(self.table, text=label).grid(row=row, column=0, sticky='w', padx=6, pady=4)
            current = ttk.Entry(self.table, width=24)
            current.insert(0, value)
            current.configure(state='readonly')
            current.grid(row=row, column=1, sticky='ew', padx=6, pady=4)
            var = tk.StringVar(value=value)
            entry = ttk.Entry(self.table, textvariable=var, width=24)
            entry.grid(row=row, column=2, sticky='ew', padx=6, pady=4)
            self.inputs.append((var, entry))
        self.table.columnconfigure(1, weight=1)
        self.table.columnconfigure(2, weight=1)
        stamp = datetime.now().strftime('%H:%M:%S')
        self.result.set(f'Đã đọc {name} lúc {stamp}. Không tự cập nhật; bấm Đọc để lấy mới.')

    def write(self):
        if self.busy or not self.loaded:
            return
        name, before = self.loaded
        try:
            kind, _, selected = variable(self.name.get())
            if selected != name:
                raise ValueError('Tên biến đã đổi. Bấm Đọc lại.')
            desired = edited_value(kind, before, [v.get() for v, _ in self.inputs])
        except ValueError as exc:
            self.result.set(str(exc))
            return
        if not messagebox.askyesno('Xác nhận ghi', f'Ghi thật vào {name}?\n\nCũ: {before}\nMới: {desired}\n\nBiến phải đã được dành riêng để test.', parent=self.root, default='no'):
            return
        def success(result):
            actual, matched = result
            self.show(name, actual)
            if matched:
                self.result.set(f'GHI OK: {name} = {actual} | Đọc lại khớp.')
            else:
                self.result.set(f'LOADV=0000 nhưng đọc lại KHÔNG KHỚP: {actual}. Không tự ghi lại.')
        self.run(lambda: self.client.write_and_read(name, desired), success)

    def poll(self):
        try:
            while True:
                success, result, error = self.events.get_nowait()
                self.busy = False
                if error:
                    self.clear()
                    self.result.set(error)
                    messagebox.showerror('NX100', error, parent=self.root)
                else:
                    success(result)
                self.refresh()
        except queue.Empty:
            pass
        self.timer = self.root.after(50, self.poll)

    def close(self):
        if self.busy:
            self.result.set('Đang trao đổi dữ liệu; chờ kết quả trước khi đóng.')
            return
        self.root.after_cancel(self.timer)
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
