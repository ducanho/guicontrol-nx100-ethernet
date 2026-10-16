"""Offline wire-contract and failure tests. No real robot/socket is used."""
from pathlib import Path
import socket
import sys
import unittest
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nx100_control import Client, checked_control, read_status, checked_move
from nx100_remote_control.module.Cartesian import Pose, build_move
from nx100_remote_control.module.Protocol import encode_command, OutcomeUnknown, ProtocolError
from nx100_remote_control.objects.Status import Status
from nx100_remote_control.objects.Response import Response


GREETING = b'OK: NX Information Server (3.00).\r\n'


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        if not self.chunks:
            return b''
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        return chunk


class WireTests(unittest.TestCase):
    def client(self):
        return Client('127.0.0.1', 80)

    def test_tcp_timeout_reports_endpoint_and_no_robot_command(self):
        trace = []
        client = Client('192.168.225.1', 80, trace=lambda *event: trace.append(event))
        with patch('socket.create_connection', side_effect=socket.timeout('timed out')) as connect:
            with self.assertRaises(ProtocolError) as error:
                client.execute('RSTATS')
        self.assertIn('192.168.225.1:80', str(error.exception))
        self.assertIn('Chưa gửi lệnh NX100', str(error.exception))
        self.assertEqual(trace, [])
        connect.assert_called_once()

    def test_handshake_timeout_is_distinct_from_tcp_timeout(self):
        sock = FakeSocket([socket.timeout('timed out')])
        with patch('socket.create_connection', return_value=sock):
            with self.assertRaisesRegex(ProtocolError, 'bắt tay'):
                self.client().execute('RSTATS')
        self.assertEqual(sock.sent, [b'CONNECT Robot_access\r\n'])

    def test_partial_ack_is_logged_even_without_crlf(self):
        trace = []
        sock = FakeSocket([GREETING, b'OK: RSTATS\r', socket.timeout('timed out')])
        client = Client('127.0.0.1', trace=lambda direction, wire: trace.append((direction, wire)))
        with patch('socket.create_connection', return_value=sock):
            with self.assertRaises(ProtocolError) as error:
                client.execute('RSTATS')
        self.assertIn(('RX', b'OK: RSTATS\r'), trace)
        self.assertIn("pending RX=b'OK: RSTATS\\r'", str(error.exception))

    def test_stage_timeout_resets_without_accepting_incomplete_frames(self):
        from nx100_remote_control.module.Protocol import _Reader
        sock = FakeSocket([b'OK\r\n', b'194,64\r'])
        with patch('nx100_remote_control.module.Protocol.time.monotonic', side_effect=[0, 1, 10, 11]):
            reader = _Reader(sock, 5, lambda *args: None)
            self.assertEqual(reader.until(b'\r\n'), 'OK')
            self.assertEqual(reader.until(b'\r'), '194,64')

    def test_fragmented_status_and_coalesced_ack_answer(self):
        wire = GREETING + b'OK: RSTATS\r\n194,64\r'
        for chunks in ([wire], [wire[i:i + 1] for i in range(len(wire))]):
            with self.subTest(chunks=len(chunks)):
                sock = FakeSocket(chunks)
                with patch('socket.create_connection', return_value=sock):
                    self.assertEqual(self.client().execute('RSTATS'), '194,64')
                self.assertEqual(sock.sent, [b'CONNECT Robot_access\r\n', b'HOSTCTRL_REQUEST RSTATS 0\r\n'])
                self.assertTrue(sock.closed)

    def test_rposc_exact_length_includes_cr(self):
        sock = FakeSocket([GREETING, b'OK: RPOSC\r\n', b'1,2,3,4,5,6,0,0\r'])
        with patch('socket.create_connection', return_value=sock):
            self.client().execute('RPOSC', '0,0')
        self.assertEqual(sock.sent, [b'CONNECT Robot_access\r\n', b'HOSTCTRL_REQUEST RPOSC 4\r\n', b'0,0\r'])

    def test_servo_payload_is_not_sent_before_ack(self):
        sock = FakeSocket([GREETING, b'NG: Bad command\r\n'])
        with patch('socket.create_connection', return_value=sock):
            with self.assertRaisesRegex(ProtocolError, 'Command rejected'):
                self.client().execute('SVON', '1')
        self.assertEqual(sock.sent, [b'CONNECT Robot_access\r\n', b'HOSTCTRL_REQUEST SVON 2\r\n'])

    def test_control_success_requires_exact_reply_and_crlf(self):
        for answer in [b'0000\r\n', b'10000\r\n', b'0000\r']:
            sock = FakeSocket([GREETING, b'OK: HOLD\r\n', answer])
            with patch('socket.create_connection', return_value=sock):
                if answer == b'0000\r\n':
                    self.assertEqual(self.client().execute('HOLD', '1'), '0000')
                else:
                    with self.assertRaises(OutcomeUnknown):
                        self.client().execute('HOLD', '1')

    def test_ng_greeting_prevents_any_command(self):
        sock = FakeSocket([b'NG: HTTP Error Response\r\n'])
        with patch('socket.create_connection', return_value=sock):
            with self.assertRaises(ProtocolError):
                self.client().execute('START', 'TEST')
        self.assertEqual(sock.sent, [b'CONNECT Robot_access\r\n'])

    def test_zero_data_command_missing_ack_is_unknown_no_retry(self):
        sock = FakeSocket([GREETING, socket.timeout('timeout')])
        with patch('socket.create_connection', return_value=sock) as connect:
            with self.assertRaises(OutcomeUnknown):
                self.client().execute('RESET')
        connect.assert_called_once()
        self.assertTrue(sock.closed)

    def test_lost_reply_after_start_payload_is_unknown_no_retry(self):
        sock = FakeSocket([GREETING, b'OK: START\r\n', socket.timeout('timeout')])
        with patch('socket.create_connection', return_value=sock) as connect:
            with self.assertRaises(OutcomeUnknown):
                self.client().execute('START', 'TEST')
        connect.assert_called_once()
        self.assertEqual(sock.sent[-1], b'TEST\r')

    def test_controller_error_is_explicit_failure(self):
        sock = FakeSocket([GREETING, b'OK: SVON\r\n', b'ERROR: [SVON] is not successful (4090).\r\n'])
        with patch('socket.create_connection', return_value=sock):
            with self.assertRaises(ProtocolError) as error:
                self.client().execute('SVON', '1')
        self.assertNotIsInstance(error.exception, OutcomeUnknown)

    def test_invalid_input_opens_no_socket(self):
        invalid = [('SVON', ''), ('HOLD', '10'), ('RSTATS', '0'),
                   ('RPOSC', '26,0'), ('RPOSC', '0,2'), ('RPOSC', '0'),
                   ('START', 'TEST\r\nRESET'), ('START', 'TEST.JBI'),
                   ('START', 'robot_á'), ('GET', '/'), ('DELETE', '*'),
                   ('IOWRITE', '22010,16,1'), ('IOWRITE', '22010,8,256'),
                   ('IOWRITE', '10010,8,1'), ('IOWRITE', '23287,8,1'),
                   ('IOWRITE', '22019,8,1'),
                   ('MOVJ', 'nan,' * 15 + '0')]
        with patch('socket.create_connection') as connect:
            for name, data in invalid:
                with self.subTest(name=name, data=data), self.assertRaises(ValueError):
                    self.client().execute(name, data)
            connect.assert_not_called()

    def test_motion_fields_and_byte_count(self):
        for name, data in [('MOVJ', '1,0,1,2,3,4,5,6,0,0,0,0,0,0,0,0'),
                           ('MOVL', '0,1,0,1,2,3,4,5,6,0,0,0,0,0,0,0,0')]:
            header, payload = encode_command(name, data)
            self.assertEqual(payload, (data + '\r').encode('ascii'))
            self.assertEqual(header, ('HOSTCTRL_REQUEST %s %d\r\n' % (name, len(payload))).encode('ascii'))


class StateTests(unittest.TestCase):
    def test_every_status_bit_including_leading_zeroes(self):
        word1 = ['is_step', 'is_one_cycle', 'is_auto', 'is_running', 'is_safety_speed_operation', 'is_teach', 'is_play', 'is_command_remote']
        word2 = ['is_programming_pendant_hold', 'is_external_hold', 'is_command_hold', 'is_alarm_occurring', 'is_error_occurring', 'is_servo_on']
        for active in range(8):
            status = Status(bin(1 << active), '0b0')
            for bit, method in enumerate(word1):
                self.assertEqual(getattr(status, method)(), bit == active)
        for active in range(1, 7):
            status = Status(0, 1 << active)
            for bit, method in enumerate(word2, 1):
                self.assertEqual(getattr(status, method)(), bit == active)

    def test_success_substring_is_not_success(self):
        self.assertTrue(Response('0000').is_success())
        for raw in ['10000', 'ERROR: 0000', '', None, '0000x']:
            self.assertFalse(Response(raw).is_success())

    def test_status_preflight_blocks_start(self):
        from unittest.mock import Mock
        for state in ['34,64', '66,64', '194,0', '194,80', '194,96', '194,72', '202,64', '194,68', '194,66']:
            client = Mock()
            client.execute.return_value = state
            with self.subTest(state=state), self.assertRaises(ProtocolError):
                checked_control(client, 'START', 'TEST')
            client.execute.assert_called_once_with('RSTATS')

    def test_play_without_remote_status_bit_blocks_control(self):
        from unittest.mock import Mock, call
        client = Mock()
        client.execute.return_value = '66,64'
        with self.assertRaisesRegex(ProtocolError, 'chưa ở Remote mode'):
            checked_control(client, 'START', 'TEST')
        client.execute.assert_called_once_with('RSTATS')

    def test_remote_mode_without_play_bit_allows_control(self):
        from unittest.mock import Mock, call
        client = Mock()
        client.execute.side_effect = ['130,64', '0000']
        self.assertEqual(checked_control(client, 'START', 'TEST'), '0000')
        self.assertEqual(client.execute.call_args_list, [call('RSTATS'), call('START', 'TEST')])

    def test_ready_start_does_not_implicitly_enable_servo_or_release_hold(self):
        from unittest.mock import Mock, call
        client = Mock()
        client.execute.side_effect = ['194,64', '0000']
        self.assertEqual(checked_control(client, 'START', 'TEST'), '0000')
        self.assertEqual(client.execute.call_args_list, [call('RSTATS'), call('START', 'TEST')])

    def test_ui_rejects_empty_job_without_sending(self):
        from unittest.mock import Mock
        client = Mock()
        with self.assertRaises(ValueError):
            checked_control(client, 'START', '')
        client.execute.assert_not_called()

    def test_malformed_status_is_not_usable(self):
        from unittest.mock import Mock
        for raw in ['194', '194,64,0', '-1,64', 'x,64', '256,64']:
            client = Mock()
            client.execute.return_value = raw
            with self.assertRaises((ValueError, ProtocolError)):
                read_status(client)


POSE = '353.769,202.779,120.658,-1.34,35.78,27.84,1,2,10.5,0,100,200,-300,400'


class CartesianTests(unittest.TestCase):
    def test_movel_has_17_fields_and_preserves_orientation_tool_and_external_axes(self):
        pose = Pose.parse(POSE)
        data = build_move('MOVL', '1', ['354.769', '202.779', '120.658'], pose)
        self.assertEqual(data, '0,1,0,354.769,202.779,120.658,-1.34,35.78,27.84,1,2,10.5,0,100,200,-300,400')
        header, payload = encode_command('MOVL', data)
        self.assertEqual(len(data.split(',')), 17)
        self.assertEqual(header, ('HOSTCTRL_REQUEST MOVL %d\r\n' % len(payload)).encode('ascii'))

    def test_movel_accepts_explicit_orientation_and_preserves_other_pose_fields(self):
        pose = Pose.parse(POSE)
        data = build_move('MOVL', '1', ['354.769', '202.779', '120.658', '10', '20', '30'], pose)
        self.assertEqual(data, '0,1,0,354.769,202.779,120.658,10.000,20.000,30.000,1,2,10.5,0,100,200,-300,400')

    def test_movj_has_16_fields_and_percent_speed(self):
        pose = Pose.parse(POSE)
        data = build_move('MOVJ', '0.5', ['354.769', '202.779', '120.658'], pose)
        self.assertEqual(len(data.split(',')), 16)
        self.assertTrue(data.startswith('0.5,0,354.769,202.779,120.658,'))
        self.assertEqual(data.split(',')[5:], list(pose.fields[3:]))

    def test_invalid_targets_never_contact_robot(self):
        from unittest.mock import Mock
        client = Mock()
        for xyz, speed in [(['NaN', '0', '0'], '1'), (['1.0001', '0', '0'], '1'),
                           (['', '0', '0'], '1'), (['1\rRESET', '0', '0'], '1'),
                           (['1', '0', '0'], '0'), (['1', '0', '0'], '1\rRESET')]:
            with self.subTest(xyz=xyz, speed=speed), self.assertRaises(ValueError):
                checked_move(client, 'MOVL', speed, xyz, Pose.parse(POSE))
        client.execute.assert_not_called()

    def test_short_or_malformed_pose_is_rejected(self):
        for raw in [','.join(POSE.split(',')[:8]), POSE.replace('35.78', 'nan'), POSE.replace(',1,2,', ',64,2,')]:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                Pose.parse(raw)

    def test_changed_pose_or_tool_blocks_motion_before_status_or_write(self):
        from unittest.mock import Mock
        for index, replacement in [(0, '355'), (3, '0'), (6, '2'), (7, '3'), (8, '11.5')]:
            changed = POSE.split(',')
            changed[index] = replacement
            client = Mock()
            client.execute.return_value = ','.join(changed)
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                checked_move(client, 'MOVL', '1', ['354.769', '202.779', '120.658'], Pose.parse(POSE))
            client.execute.assert_called_once_with('RPOSC', '0,1')

    def test_every_not_ready_state_blocks_movel_and_movj(self):
        from unittest.mock import Mock, call
        for command in ['MOVL', 'MOVJ']:
            for status in ['34,0', '194,0', '194,80', '194,96', '194,72', '202,64', '194,68', '194,66']:
                client = Mock()
                client.execute.side_effect = [POSE, status]
                with self.subTest(command=command, status=status), self.assertRaises(ProtocolError):
                    checked_move(client, command, '1', ['354.769', '202.779', '120.658'], Pose.parse(POSE))
                self.assertEqual(client.execute.call_args_list, [call('RPOSC', '0,1'), call('RSTATS')])

    def test_ready_motion_sends_once_after_fresh_position_and_status(self):
        from unittest.mock import Mock, call
        pose = Pose.parse(POSE)
        for command in ['MOVL', 'MOVJ']:
            client = Mock()
            client.execute.side_effect = [POSE, '194,64', '0000']
            xyz = ['354.769', '202.779', '120.658']
            self.assertEqual(checked_move(client, command, '1', xyz, pose), '0000')
            self.assertEqual(client.execute.call_args_list,
                             [call('RPOSC', '0,1'), call('RSTATS'), call(command, build_move(command, '1', xyz, pose))])

    def test_wire_motion_preserves_full_payload_and_never_retries(self):
        for command in ['MOVL', 'MOVJ']:
            data = build_move(command, '1', ['354.769', '202.779', '120.658'], Pose.parse(POSE))
            sock = FakeSocket([GREETING, ('OK: ' + command + '\r\n').encode('ascii'), socket.timeout('lost result')])
            with patch('socket.create_connection', return_value=sock) as connect:
                with self.assertRaises(OutcomeUnknown):
                    Client('127.0.0.1').execute(command, data)
            connect.assert_called_once()
            header, payload = encode_command(command, data)
            self.assertEqual(sock.sent, [b'CONNECT Robot_access\r\n', header, payload])


class GuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from nx100_control import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)

    def tearDown(self):
        self.app.close()

    def finish(self):
        deadline = time.monotonic() + 2
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy)

    def test_startup_and_invalid_config_send_nothing(self):
        with patch('socket.create_connection') as connect:
            self.assertEqual(str(self.app.send_button['state']), 'disabled')
            self.app.connect()  # IP is initially empty.
            self.assertIsNone(self.app.client)
            connect.assert_not_called()

    def test_connect_refresh_and_disconnect(self):
        from unittest.mock import Mock, call
        client = Mock(ip='127.0.0.1', port=80)
        client.execute.side_effect = ['194,64', '194,64']
        with patch('nx100_control.Client', return_value=client):
            self.app.connect()
        self.finish()
        self.assertIs(self.app.client, client)
        self.assertFalse(self.app.armed.get())
        self.app.refresh_status()
        self.finish()
        self.assertEqual(client.execute.call_args_list, [call('RSTATS'), call('RSTATS')])
        self.app.disconnect()
        self.assertIsNone(self.app.client)
        self.assertFalse(self.app.armed.get())
        self.assertEqual(client.execute.call_count, 2)

    def test_unknown_move_outcome_disarms_without_retry(self):
        from unittest.mock import Mock
        client = Mock(ip='127.0.0.1', port=80)
        client.execute.side_effect = [POSE, '194,64', OutcomeUnknown('Chưa xác định kết quả')]
        self.app.client = client
        self.app.motion_pose = Pose.parse(POSE)
        for variable, value in zip(self.app.motion_values, POSE.split(',')[:6]):
            variable.set(value)
        self.app.armed.set(True)
        self.app.update_controls()
        self.app.send_move()
        self.finish()
        self.assertFalse(self.app.armed.get())
        self.assertEqual(client.execute.call_count, 3)
        self.assertEqual([call.args[0] for call in client.execute.call_args_list], ['RPOSC', 'RSTATS', 'MOVL'])

    def test_pose_load_and_six_axis_send_only_when_armed(self):
        from unittest.mock import Mock, call
        client = Mock()
        client.execute.side_effect = [POSE, POSE, '194,64', '0000']
        self.app.client = client
        self.app.load_pose()
        self.finish()
        self.assertEqual(self.app.motion_values[0].get(), '353.769')
        self.assertEqual(str(self.app.motion_entries[3]['state']), 'normal')
        self.assertEqual(str(self.app.send_button['state']), 'disabled')
        self.app.motion_values[0].set('354.769')
        self.app.motion_values[3].set('10')
        self.assertEqual(client.execute.call_args_list, [call('RPOSC', '0,1')])
        self.app.send_move()  # Not armed: do nothing.
        self.assertEqual(client.execute.call_count, 1)
        self.app.armed.set(True)
        self.app.update_controls()
        self.app.send_move()
        self.finish()
        self.assertEqual(client.execute.call_count, 4)
        self.assertEqual(client.execute.call_args_list[-1][0][0], 'MOVL')
        self.assertIsNone(self.app.motion_pose)
        self.assertEqual(str(self.app.send_button['state']), 'disabled')
        self.assertIn('chưa xác nhận robot đã tới đích', self.app.result_text.get())
        sent_data = client.execute.call_args_list[-1][0][1]
        self.assertIn(',354.769,202.779,120.658,10.000,35.780,27.840,', sent_data)

    def test_movj_selection_sends_joint_motion_with_percent_speed(self):
        from unittest.mock import Mock
        client = Mock()
        client.execute.side_effect = [POSE, '130,64', '0000']
        self.app.client = client
        self.app.motion_pose = Pose.parse(POSE)
        for variable, value in zip(self.app.motion_values, ['354.769', '202.779', '120.658', '10', '20', '30']):
            variable.set(value)
        self.app.motion_kind.set('MOVJ')
        self.app.change_motion_kind()
        self.app.speed.set('1')
        self.app.armed.set(True)
        self.app.update_controls()
        self.app.send_move()
        self.finish()
        self.assertEqual(client.execute.call_args_list[-1][0][0], 'MOVJ')
        self.assertTrue(client.execute.call_args_list[-1][0][1].startswith('1,0,'))
        self.assertEqual(self.app.speed_label.cget('text'), 'Tốc độ (%)')


if __name__ == '__main__':
    unittest.main()
