import unittest
import nx100_remote_control
from nx100_remote_control.module import Gripper


class MyTestCase(unittest.TestCase):
    nx100_remote_control.MOCK_RESPONSE = True  # only possible to test with mock responses

    def test_write_gripper_close(self):
        # Legacy custom-hardware example has an unverified four-bit I/O payload.
        # It must be rejected rather than transmitted to a different robot.
        with self.assertRaises(ValueError):
            Gripper.write_gripper_close()

    def test_write_gripper_open(self):
        with self.assertRaises(ValueError):
            Gripper.write_gripper_open()


if __name__ == '__main__':
    unittest.main()
