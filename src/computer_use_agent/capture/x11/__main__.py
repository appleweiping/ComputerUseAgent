"""Command dispatcher for the X11 guest bundle.

The same commands work from a source checkout
(``python -m computer_use_agent.capture.x11 <command> ...``) and inside the
deployed zipapp (``python3 computer-use-agent-x11-recorder.pyz <command> ...``).

Commands:

``doctor``, ``start-recorder``, ``stop-recorder``
    the recorder-only guest controller (:mod:`.guest_controller`);
``record``
    the X11 recorder process itself (:mod:`.recorder`); ``record --doctor``
    checks DISPLAY, python-xlib, RECORD and XTEST without recording;
``calibration-probe``
    the Tk calibration target for an XTest or a manual probe (:mod:`.calibration_probe`);
``calibrate``
    evaluate two-phase calibration evidence into a certificate (:mod:`.calibration`).

Every module is imported lazily, so the dispatcher itself needs only the
standard library.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

CONTROLLER_COMMANDS = ("doctor", "start-recorder", "stop-recorder")
COMMANDS = (*CONTROLLER_COMMANDS, "record", "calibration-probe", "calibrate")
USAGE = (
    "usage: computer-use-agent-x11-recorder.pyz {" + ",".join(COMMANDS) + "} [options]\n"
    "Run '<command> --help' for the options of one command."
)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(USAGE, file=sys.stdout if args else sys.stderr)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command in CONTROLLER_COMMANDS:
        from .guest_controller import main as controller_main

        return controller_main([command, *rest])
    if command == "record":
        from .recorder import main as recorder_main

        return recorder_main(rest)
    if command == "calibration-probe":
        from .calibration_probe import main as probe_main

        return probe_main(rest)
    if command == "calibrate":
        from .calibration import main as calibration_main

        return calibration_main(rest)
    print(f"unknown command {command!r}\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
