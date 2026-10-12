"""Safe guest target for deterministic XTest or direct-human calibration.

Run the X11 recorder in one terminal and this target in another, both from the
guest bundle::

    python3 computer-use-agent-x11-recorder.pyz record --actor xtest ...
    python3 computer-use-agent-x11-recorder.pyz calibration-probe --mode xtest         --confirm-calibration

The target shows only aggregate input counts; it never stores or displays which
key was pressed. XTest mode injects a bounded move, click, and key press into
this dedicated window. Human mode asks the self-operator to do the same
manually. Tk (``python3-tk``) and python-xlib must be present in the guest
snapshot.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def run_probe(mode: str, duration_seconds: float) -> dict[str, Any]:
    try:
        import tkinter as tk
    except Exception as error:
        raise RuntimeError(f"Tk calibration target unavailable: {type(error).__name__}") from error
    root = tk.Tk()
    root.title("CUA Recorder Calibration")
    root.geometry("460x260")
    root.resizable(False, False)
    counts = {"pointer_motion": 0, "button": 0, "key": 0}
    instruction = (
        "Automated XTest probe in progress."
        if mode == "xtest"
        else "Move the pointer in this box, click once, and press any key.\n"
        "Only counts are shown; the key itself is not stored."
    )
    label = tk.Label(root, text=instruction, padx=24, pady=24, justify="center")
    label.pack(fill="both", expand=True)
    counter = tk.Label(root, text="motion=0  clicks=0  keys=0", font=("sans", 12, "bold"))
    counter.pack(pady=18)

    def update_counter() -> None:
        counter.configure(
            text=(
                f"motion={counts['pointer_motion']}  "
                f"clicks={counts['button']}  keys={counts['key']}"
            )
        )

    def on_motion(_event: object) -> None:
        counts["pointer_motion"] += 1
        update_counter()

    def on_button(_event: object) -> None:
        counts["button"] += 1
        update_counter()

    def on_key(_event: object) -> str:
        counts["key"] += 1
        update_counter()
        return "break"

    root.bind("<Motion>", on_motion)
    root.bind("<ButtonPress>", on_button)
    root.bind("<KeyPress>", on_key)
    root.update_idletasks()
    root.focus_force()

    if mode == "xtest":
        try:
            from Xlib import XK, X, display
            from Xlib.ext import xtest
        except Exception as error:
            root.destroy()
            raise RuntimeError(f"python-xlib/XTest unavailable: {type(error).__name__}") from error
        connection = display.Display()
        if not connection.has_extension("XTEST"):
            connection.close()
            root.destroy()
            raise RuntimeError("XTEST extension is unavailable")

        def inject() -> None:
            root.update_idletasks()
            x = root.winfo_rootx() + root.winfo_width() // 2
            y = root.winfo_rooty() + root.winfo_height() // 2
            xtest.fake_input(connection, X.MotionNotify, x=x - 20, y=y - 10)
            xtest.fake_input(connection, X.MotionNotify, x=x, y=y)
            xtest.fake_input(connection, X.ButtonPress, 1)
            xtest.fake_input(connection, X.ButtonRelease, 1)
            keycode = connection.keysym_to_keycode(XK.string_to_keysym("x"))
            xtest.fake_input(connection, X.KeyPress, keycode)
            xtest.fake_input(connection, X.KeyRelease, keycode)
            connection.sync()

        root.after(750, inject)
        root.after(2_000, root.destroy)
    else:
        root.after(int(duration_seconds * 1_000), root.destroy)
    root.mainloop()
    if mode == "xtest":
        connection.close()
    return {
        "mode": mode,
        "ui_counts": counts,
        "key_content_recorded": False,
        "claim_boundary": "UI-side counts only; recorder trace and post-stop controls are evaluated separately.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibration-probe",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=("xtest", "human"), required=True)
    parser.add_argument("--duration-seconds", type=float, default=15.0)
    parser.add_argument("--confirm-calibration", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_calibration:
        print("Calibration input requires --confirm-calibration", file=sys.stderr)
        return 2
    if not 2 <= args.duration_seconds <= 60:
        print("--duration-seconds must be in [2, 60]", file=sys.stderr)
        return 2
    report = run_probe(args.mode, args.duration_seconds)
    print(json.dumps(report, indent=2))
    expected = all(report["ui_counts"][name] > 0 for name in ("pointer_motion", "button", "key"))
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
