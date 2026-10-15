# SPDX-License-Identifier: MPL-2.0
"""Linux only: stop right away on Windows.

Standard library only, so it runs before the third-party dependencies are imported
(they may not even be installed).
"""
import os
import sys

WINDOWS_MESSAGE = "Why are you using this shitty OS? I'm not built to handle hopeless situations."


def is_windows() -> bool:
    return sys.platform in ("win32", "cygwin") or os.name == "nt"


def refuse_windows(gui: bool = False) -> None:
    if not is_windows():
        return
    print(WINDOWS_MESSAGE, file=sys.stderr)
    if gui:  # a double-click shows no console: also say it in a window
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Crimson Desert – Save Editor", WINDOWS_MESSAGE)
            root.destroy()
        except Exception:
            pass
    sys.exit(1)
