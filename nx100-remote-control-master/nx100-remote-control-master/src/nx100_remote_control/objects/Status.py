class Status:
    """RSTATS: fixed bit positions, independent of leading zeroes."""
    def __init__(self, data_1, data_2):
        def word(value, maximum):
            result = int(value, 2) if isinstance(value, str) and value.startswith('0b') else int(value)
            if not 0 <= result <= maximum:
                raise ValueError('Invalid RSTATS word')
            return result
        self.data_1 = word(data_1, 255)
        self.data_2 = word(data_2, 127)

    def is_step(self):
        return bool(self.data_1 & (1 << 0))

    def is_one_cycle(self):
        return bool(self.data_1 & (1 << 1))

    def is_auto(self):
        return bool(self.data_1 & (1 << 2))

    def is_running(self):
        return bool(self.data_1 & (1 << 3))

    def is_safety_speed_operation(self):
        return bool(self.data_1 & (1 << 4))

    def is_teach(self):
        return bool(self.data_1 & (1 << 5))

    def is_play(self):
        return bool(self.data_1 & (1 << 6))

    def is_command_remote(self):
        return bool(self.data_1 & (1 << 7))

    def is_programming_pendant_hold(self):
        return bool(self.data_2 & (1 << 1))

    def is_external_hold(self):
        return bool(self.data_2 & (1 << 2))

    def is_command_hold(self):
        return bool(self.data_2 & (1 << 3))

    def is_alarm_occurring(self):
        return bool(self.data_2 & (1 << 4))

    def is_error_occurring(self):
        return bool(self.data_2 & (1 << 5))

    def is_servo_on(self):
        return bool(self.data_2 & (1 << 6))
