"""
Registers the everoi:// link for the current Windows user, so the report's
Update button can start the local server when it isn't running.

    python install_link.py            install (or repair after moving the folder)
    python install_link.py --remove   remove it again

Only touches HKEY_CURRENT_USER\\Software\\Classes\\everoi - no admin rights needed.
"""
import os
import sys
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = r"Software\Classes\everoi"


def install():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")  # no console window
    cmd = f'"{pythonw}" "{os.path.join(HERE, "server.py")}" "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:EVE Industry ROI")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
    print("everoi:// link installed ->", cmd)


def remove():
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY + sub)
        except FileNotFoundError:
            pass
    print("everoi:// link removed")


if __name__ == "__main__":
    remove() if "--remove" in sys.argv else install()
