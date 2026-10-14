"""Ярлик у меню: записується при кожному запуску зібраної програми, тож іде слідом за текою."""
import os
import shutil
import subprocess
import sys

ASSETS = os.path.join(os.path.dirname(__file__), "assets")
ICON = os.path.join(ASSETS, "lasun.svg")
ICO = os.path.join(ASSETS, "lasun.ico")

ENTRY = """[Desktop Entry]
Type=Application
Name=Lasun
Name[uk]=Ласун
Name[ru]=Ласун
Comment=Tasty light picks from ATB, deals first
Comment[uk]=Смачне й легке з АТБ, спершу акції
Comment[ru]=Вкусное и лёгкое из АТБ, сначала акции
Exec="{exe}"
Icon=lasun
Categories=Utility;
Keywords=atb;food;calories;diet;snack;
Terminal=false
"""


def install_menu_entry():
    if not getattr(sys, "frozen", False):
        return  # з вихідного коду: нема на що вказувати
    try:
        if sys.platform.startswith("win"):
            _windows_shortcut()
        else:
            _linux_entry()
    except Exception:
        pass


def _linux_entry():
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    desktop_file = os.path.join(data, "applications", "lasun.desktop")
    icon_file = os.path.join(data, "icons", "hicolor", "scalable", "apps", "lasun.svg")
    os.makedirs(os.path.dirname(desktop_file), exist_ok=True)
    os.makedirs(os.path.dirname(icon_file), exist_ok=True)
    shutil.copyfile(ICON, icon_file)
    text = ENTRY.format(exe=sys.executable)
    if not os.path.exists(desktop_file) or open(desktop_file).read() != text:
        with open(desktop_file, "w") as f:
            f.write(text)


def _windows_shortcut():
    programs = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs")
    lnk = os.path.join(programs, "Lasun.lnk")
    stamp = os.path.join(os.environ.get("LOCALAPPDATA", programs), "lasun", "shortcut-target")
    exe = sys.executable
    if os.path.exists(lnk) and os.path.exists(stamp) and open(stamp).read() == exe:
        return
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          "$s.TargetPath='{exe}';$s.WorkingDirectory='{dir}';$s.IconLocation='{exe},0';"
          "$s.Save()").format(lnk=lnk.replace("'", "''"), exe=exe.replace("'", "''"),
                              dir=os.path.dirname(exe).replace("'", "''"))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=0x08000000, timeout=30, check=True)  # CREATE_NO_WINDOW
    os.makedirs(os.path.dirname(stamp), exist_ok=True)
    with open(stamp, "w") as f:
        f.write(exe)
