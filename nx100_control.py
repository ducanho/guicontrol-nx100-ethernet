"""Simple NX100 Cartesian target sender. Run with Python 3.10+ and Tkinter."""
from datetime import datetime
from pathlib import Path
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

RUNTIME = Path(__file__).resolve().parent / '.runtime' / 'tcl'
for variable, folder in [('TCL_LIBRARY', 'tcl' + str(tk.TclVersion)),
                         ('TK_LIBRARY', 'tk' + str(tk.TkVersion))]:
    if (RUNTIME / folder).is_dir():
        os.environ.setdefault(variable, str(RUNTIME / folder))

SOURCE = Path(__file__).resolve().parent / 'nx100-remote-control-master/nx100-remote-control-master/src'
sys.path.insert(0, str(SOURCE))
from nx100_remote_control.module.Protocol import Client, ProtocolError, encode_command
from nx100_remote_control.objects.Status import Status
from nx100_remote_control.module.Cartesian import build_move, read_pose


def read_status(client):
    raw = client.execute('RSTATS')
    parts = raw.split(',')
    if len(parts) != 2 or any(not part.strip().isdigit() for part in parts):
        raise ProtocolError('RSTATS không đúng định dạng: ' + raw)
    return raw, Status(*(int(part) for part in parts))


def checked_control(client, command, data):
    encode_command(command, data)
    if command == 'START' and not data:
        raise ValueError('Phải nhập tên JOB; không tự chạy JOB hiện tại')
    if command in {'START', 'MOVL', 'MOVJ'} or (command, data) in {('SVON', '1'), ('HOLD', '0')}:
        _, status = read_status(client)
        if not status.is_command_remote():
            raise ProtocolError('NX100 chưa ở Remote mode; mã 2100 là Command remote not set')
        if status.is_error_occurring() or status.is_alarm_occurring():
            raise ProtocolError('NX100 đang có error/alarm')
        if status.is_external_hold() or status.is_programming_pendant_hold():
            raise ProtocolError('NX100 đang HOLD từ ngoài hoặc pendant')
        if command in {'START', 'MOVL', 'MOVJ'}:
            if not status.is_servo_on():
                raise ProtocolError('Servo chưa ON')
            if status.is_command_hold() or status.is_running():
                raise ProtocolError('NX100 đang HOLD hoặc đang chạy')
    return client.execute(command, data)


def checked_move(client, command, speed, coordinates, reference):
    data = build_move(command, speed, coordinates, reference)
    reference.verify_unchanged(read_pose(client))
    return checked_control(client, command, data)


class App:
    def __init__(self, root):
        self.root = root
        self.client = None
        self.busy = False
        self.events = queue.Queue()
        self.motion_pose = None
        root.title('NX100 | Gửi đích Cartesian')
        root.geometry('920x690')
        root.minsize(820, 600)

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='NX100 / Ethernet Server', font=('Segoe UI', 16, 'bold')).pack(anchor='w')

        connection = ttk.LabelFrame(outer, text='Kết nối', padding=10)
        connection.pack(fill='x', pady=(10, 8))
        self.ip = tk.StringVar()
        self.port = tk.StringVar(value='80')
        self.timeout = tk.StringVar(value='10')
        self.endpoint_entries = []
        for column, (label, variable, width) in enumerate([
            ('IP robot', self.ip, 17), ('Port', self.port, 7), ('Timeout (s)', self.timeout, 7)
        ]):
            field = ttk.Frame(connection)
            field.grid(row=0, column=column, padx=(0, 10), sticky='w')
            ttk.Label(field, text=label).pack(anchor='w')
            entry = ttk.Entry(field, textvariable=variable, width=width)
            entry.pack()
            self.endpoint_entries.append(entry)
        self.connect_button = ttk.Button(connection, text='Kết nối', command=self.connect)
        self.connect_button.grid(row=0, column=3, padx=4, sticky='s')
        self.disconnect_button = ttk.Button(connection, text='Ngắt', command=self.disconnect)
        self.disconnect_button.grid(row=0, column=4, padx=4, sticky='s')
        self.status_button = ttk.Button(connection, text='Đọc trạng thái', command=self.refresh_status)
        self.status_button.grid(row=0, column=5, padx=4, sticky='s')
        self.pose_button = ttk.Button(connection, text='Đọc pose BASE', command=self.load_pose)
        self.pose_button.grid(row=0, column=6, padx=4, sticky='s')
        self.connection_text = tk.StringVar(value='Chưa kết nối')
        ttk.Label(connection, textvariable=self.connection_text).grid(
            row=1, column=0, columnspan=7, sticky='w', pady=(8, 0)
        )

        status = ttk.LabelFrame(outer, text='Trạng thái NX100', padding=10)
        status.pack(fill='x', pady=(0, 8))
        self.status_text = tk.StringVar(value='Chưa đọc trạng thái')
        ttk.Label(status, textvariable=self.status_text, wraplength=860).pack(anchor='w')

        motion = ttk.LabelFrame(outer, text='Đích tuyệt đối trong hệ BASE', padding=10)
        motion.pack(fill='x')
        self.pose_text = tk.StringVar(value='Đọc pose hiện tại trước khi nhập đích')
        ttk.Label(motion, textvariable=self.pose_text).grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 8))
        self.motion_values = [tk.StringVar() for _ in range(6)]
        self.motion_entries = []
        labels = ['X (mm)', 'Y (mm)', 'Z (mm)', 'Rx / TX (°)', 'Ry / TY (°)', 'Rz / TZ (°)']
        for index, label in enumerate(labels):
            row, column = divmod(index, 3)
            field = ttk.Frame(motion)
            field.grid(row=row + 1, column=column, padx=(0, 16), pady=4, sticky='w')
            ttk.Label(field, text=label).pack(anchor='w')
            entry = ttk.Entry(field, textvariable=self.motion_values[index], width=16)
            entry.pack()
            self.motion_entries.append(entry)

        controls = ttk.Frame(motion)
        controls.grid(row=3, column=0, columnspan=6, sticky='w', pady=(8, 4))
        self.motion_kind = tk.StringVar(value='MOVL')
        self.motion_kind_box = ttk.Combobox(
            controls, textvariable=self.motion_kind, values=('MOVL', 'MOVJ'), width=7, state='readonly'
        )
        self.motion_kind_box.pack(side='left', padx=(0, 10))
        self.motion_kind_box.bind('<<ComboboxSelected>>', self.change_motion_kind)
        self.speed_label = ttk.Label(controls, text='Tốc độ (mm/s)')
        self.speed_label.pack(side='left')
        self.speed = tk.StringVar(value='1')
        self.speed_entry = ttk.Entry(controls, textvariable=self.speed, width=8)
        self.speed_entry.pack(side='left', padx=(8, 14))
        self.armed = tk.BooleanVar(value=False)
        self.enable = ttk.Checkbutton(
            controls, text='Cho phép gửi lệnh chuyển động', variable=self.armed, command=self.update_controls
        )
        self.enable.pack(side='left', padx=(0, 14))
        self.send_button = ttk.Button(controls, text='Gửi MOVL', command=self.send_move)
        self.send_button.pack(side='left')

        ttk.Label(
            motion,
            text='Lệnh là đích tuyệt đối. Rx/Ry/Rz thay đổi hướng tool; tool, posture và trục ngoài giữ theo pose đã đọc.',
            wraplength=860,
            foreground='#8a3b12'
        ).grid(row=4, column=0, columnspan=6, sticky='w', pady=(6, 0))

        self.result_text = tk.StringVar(value='Chưa gửi lệnh')
        ttk.Label(outer, textvariable=self.result_text, wraplength=880).pack(anchor='w', pady=8)
        log_frame = ttk.LabelFrame(outer, text='TX / RX', padding=5)
        log_frame.pack(fill='both', expand=True)
        self.log = ScrolledText(log_frame, height=8, font=('Consolas', 9), state='disabled', wrap='word')
        self.log.pack(fill='both', expand=True)
        self.log.tag_configure('TX', foreground='#235da8')
        self.log.tag_configure('RX', foreground='#167043')
        self.log.tag_configure('ERROR', foreground='#b42318')

        self.update_controls()
        self.timer = root.after(50, self.process_events)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def append_log(self, tag, message):
        self.log.configure(state='normal')
        self.log.insert('end', datetime.now().strftime('%H:%M:%S') + ' ' + tag + ' ' + message + '\n', tag)
        self.log.see('end')
        self.log.configure(state='disabled')

    def update_controls(self):
        connected = self.client is not None
        for entry in self.endpoint_entries:
            entry.configure(state='disabled' if connected or self.busy else 'normal')
        self.connect_button.configure(state='normal' if not connected and not self.busy else 'disabled')
        self.disconnect_button.configure(state='normal' if connected and not self.busy else 'disabled')
        for button in (self.status_button, self.pose_button):
            button.configure(state='normal' if connected and not self.busy else 'disabled')
        self.enable.configure(state='normal' if connected and not self.busy else 'disabled')
        self.motion_kind_box.configure(state='readonly' if not self.busy else 'disabled')
        pose_loaded = connected and self.motion_pose is not None and not self.busy
        for entry in self.motion_entries:
            entry.configure(state='normal' if pose_loaded else 'disabled')
        self.speed_entry.configure(state='normal' if connected and not self.busy else 'disabled')
        self.send_button.configure(state='normal' if pose_loaded and self.armed.get() else 'disabled')

    def run(self, action, success):
        if self.busy:
            return
        self.busy = True
        self.result_text.set('Đang chờ NX100 phản hồi...')
        self.update_controls()

        def work():
            try:
                self.events.put(('done', success, action()))
            except Exception as exc:
                self.events.put(('error', str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def trace(self, direction, wire):
        self.events.put(('trace', direction, repr(wire)))

    def connect(self):
        if self.busy or self.client is not None:
            return
        try:
            candidate = Client(
                self.ip.get().strip(), self.port.get().strip(), self.timeout.get().strip(), self.trace
            )
        except ValueError as exc:
            self.result_text.set('Cấu hình không hợp lệ: ' + str(exc))
            return
        self.connection_text.set('Đang kết nối tới %s:%s...' % (candidate.ip, candidate.port))

        def success(result):
            self.client = candidate
            self.armed.set(False)
            self.connection_text.set('Đã kết nối Ethernet Server %s:%s' % (candidate.ip, candidate.port))
            self.show_status(result)
            self.update_controls()

        self.run(lambda: read_status(candidate), success)

    def disconnect(self):
        self.client = None
        self.armed.set(False)
        self.clear_pose()
        self.connection_text.set('Đã ngắt kết nối')
        self.status_text.set('Chưa đọc trạng thái')
        self.update_controls()

    def refresh_status(self):
        if self.client is not None:
            self.run(lambda: read_status(self.client), self.show_status)

    def show_status(self, result):
        raw, status = result
        flags = [('REMOTE', status.is_command_remote()), ('PLAY', status.is_play()),
                 ('SERVO', status.is_servo_on()), ('RUNNING', status.is_running()),
                 ('ERROR', status.is_error_occurring()), ('ALARM', status.is_alarm_occurring()),
                 ('HOLD', status.is_command_hold() or status.is_external_hold() or status.is_programming_pendant_hold())]
        summary = ' | '.join(name + ': ' + ('ON' if enabled else 'OFF') for name, enabled in flags)
        self.status_text.set(summary + ' | RSTATS: ' + raw)
        self.result_text.set('Đã đọc trạng thái; dữ liệu chỉ đúng tại thời điểm đọc.')

    def load_pose(self):
        if self.client is None:
            return

        def success(pose):
            self.motion_pose = pose
            for variable, value in zip(self.motion_values, pose.fields[:6]):
                variable.set(value)
            self.pose_text.set(
                'Pose đã đọc | tool %s | posture %s | trục ngoài được giữ nguyên' %
                (pose.fields[7], pose.fields[6])
            )
            self.result_text.set('Sửa tọa độ đích rồi gửi MOVL ở tốc độ thấp để kiểm tra.')
            self.update_controls()

        self.run(lambda: read_pose(self.client), success)

    def clear_pose(self):
        self.motion_pose = None
        for variable in self.motion_values:
            variable.set('')
        self.pose_text.set('Đọc pose hiện tại trước khi nhập đích')

    def change_motion_kind(self, event=None):
        is_linear = self.motion_kind.get() == 'MOVL'
        self.speed_label.configure(text='Tốc độ (mm/s)' if is_linear else 'Tốc độ (%)')
        self.send_button.configure(text='Gửi ' + self.motion_kind.get())
        self.result_text.set(
            'MOVL đi thẳng tới đích.' if is_linear else 'MOVJ chạy theo khớp; quỹ đạo TCP không đi thẳng.'
        )

    def send_move(self):
        client, reference = self.client, self.motion_pose
        if client is None or reference is None or self.busy or not self.armed.get():
            return
        command = self.motion_kind.get()
        speed = self.speed.get().strip()
        coordinates = [variable.get() for variable in self.motion_values]
        try:
            build_move(command, speed, coordinates, reference)
        except ValueError as exc:
            self.result_text.set('Đích không hợp lệ: ' + str(exc))
            return

        def success(raw):
            self.clear_pose()
            self.status_text.set('Lệnh đã được NX100 nhận; đọc lại trạng thái/vị trí để xác nhận hoàn tất.')
            self.result_text.set(command + ' trả ' + raw + '; đã nhận lệnh, chưa xác nhận robot đã tới đích.')
            self.update_controls()

        self.run(lambda: checked_move(client, command, speed, coordinates, reference), success)

    def process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'trace':
                    self.append_log(event[1], event[2])
                elif event[0] == 'done':
                    self.busy = False
                    event[1](event[2])
                    self.update_controls()
                elif event[0] == 'error':
                    self.busy = False
                    self.handle_error(event[1])
        except queue.Empty:
            pass
        self.timer = self.root.after(50, self.process_events)

    def handle_error(self, message):
        self.append_log('ERROR', message)
        self.armed.set(False)
        self.clear_pose()
        self.status_text.set('Có lỗi; đọc lại trạng thái trước khi thao tác tiếp')
        self.result_text.set(message)
        self.update_controls()

    def close(self):
        if self.busy:
            self.result_text.set('Đang chờ phản hồi; dùng E-STOP tại tủ nếu cần dừng khẩn cấp.')
            return
        self.root.after_cancel(self.timer)
        self.root.destroy()


if __name__ == '__main__':
    window = tk.Tk()
    App(window)
    window.mainloop()