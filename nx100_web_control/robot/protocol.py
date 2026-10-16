"""Import the proven workspace transport; never duplicate its wire protocol."""

import sys
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "nx100-remote-control-master"
    / "nx100-remote-control-master"
    / "src"
)
if not SOURCE.is_dir():
    raise RuntimeError(f"Existing NX100 library missing: {SOURCE}")
sys.path.insert(0, str(SOURCE))

from nx100_remote_control.module.Cartesian import Pose, build_move, read_pose  # noqa: E402
from nx100_remote_control.module.Protocol import Client, OutcomeUnknown, ProtocolError  # noqa: E402
from nx100_remote_control.objects.Status import Status  # noqa: E402

__all__ = ["Client", "ProtocolError", "OutcomeUnknown", "Pose", "build_move", "read_pose", "Status"]
