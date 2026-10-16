"""Compatibility adapter for the validated NX100 TCP implementation."""
import nx100_remote_control
from nx100_remote_control.module import Utils, MockResponse
from nx100_remote_control.module.Protocol import Client, encode_command


def exec_single_command(command):
    encode_command(command.name, command.data)
    if nx100_remote_control.MOCK_RESPONSE:
        return Utils.clean_response(MockResponse.get_mock_response(command))
    return Client(nx100_remote_control.NX100_IP_ADDRESS,
                  nx100_remote_control.NX100_TCP_PORT).execute(command.name, command.data)


def exec_multiple_commands(commands):
    raise NotImplementedError("Use explicit sequential single commands")
