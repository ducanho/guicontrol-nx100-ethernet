"""Absolute Cartesian targets in BASE; preserve full RPOSC pose metadata."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from nx100_remote_control.module.Protocol import encode_command, ProtocolError


@dataclass(frozen=True)
class Pose:
    # RPOSC 0,1: XYZ, TX/TY/TZ, posture type, tool, six external axes.
    fields: tuple

    @classmethod
    def parse(cls, raw):
        fields = tuple(item.strip() for item in raw.split(','))
        if len(fields) != 14:
            raise ProtocolError('RPOSC 0,1 cần đủ 14 giá trị gồm tư thế, tool và trục ngoài; nhận: ' + raw)
        try:
            encode_command('MOVL', ','.join(('0', '1', '0') + fields))
        except ValueError as exc:
            raise ProtocolError('Dữ liệu vị trí chưa hợp lệ để tạo lệnh: ' + str(exc)) from exc
        return cls(fields)

    def verify_unchanged(self, current):
        if self.fields[6:8] != current.fields[6:8]:
            raise ProtocolError('Tool hoặc tư thế robot đã thay đổi; lấy lại vị trí hiện tại')
        for index in list(range(6)) + list(range(8, 14)):
            tolerance = Decimal('0.05') if index < 6 else Decimal('0.001')
            if abs(Decimal(self.fields[index]) - Decimal(current.fields[index])) > tolerance:
                raise ProtocolError('Robot đã đổi vị trí kể từ lần lấy tọa độ; lấy lại vị trí trước khi gửi đích mới')


def coordinate(value):
    """Serialize a decimal value exactly, without silently rounding input."""
    try:
        number = Decimal(value.strip())
        if not number.is_finite() or abs(number) > Decimal('9999999.999'):
            raise ValueError('Tọa độ phải là số hữu hạn trong phạm vi biểu diễn')
        fixed = number.quantize(Decimal('0.001'))
        if fixed != number:
            raise ValueError('X/Y/Z chỉ nhận tối đa 3 chữ số sau dấu chấm (mm)')
        return format(fixed, '.3f')
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError('Tọa độ phải dùng số và dấu chấm thập phân') from exc


def build_move(command, speed, xyz, pose):
    if command not in {'MOVL', 'MOVJ'} or not isinstance(pose, Pose):
        raise ValueError('Chọn MOVL/MOVJ và lấy vị trí từ robot trước')
    if len(xyz) not in {3, 6}:
        raise ValueError('Cần nhập đủ XYZ hoặc XYZ/Rx/Ry/Rz')
    coordinates = tuple(coordinate(value) for value in xyz)
    target = coordinates + (pose.fields[len(coordinates):] if len(coordinates) == 3 else pose.fields[6:])
    # MOVL selection 0 = translational V in mm/s; MOVJ speed is percent.
    prefix = ('0', speed.strip(), '0') if command == 'MOVL' else (speed.strip(), '0')
    data = ','.join(prefix + target)
    encode_command(command, data)
    return data


def read_pose(client):
    return Pose.parse(client.execute('RPOSC', '0,1'))
