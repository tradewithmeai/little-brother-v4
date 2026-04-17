"""
Little Brother - one-shot setup script.

Registers the watchdog with Task Scheduler (restarts on crash) and adds the
tray companion to the Windows autostart registry key for the current user.

Run: python tools/install.py
"""

import os
import subprocess
import sys
import winreg
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent.parent
PYTHONW = ROOT / "venv" / "Scripts" / "pythonw.exe"
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"

WATCHDOG_TASK_NAME = "LittleBrotherWatchdog"
TRAY_AUTOSTART_NAME = "LittleBrotherTray"
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _check_prereqs():
    errors = []
    if not PYTHONW.exists():
        errors.append(f"pythonw.exe not found at {PYTHONW}\n  Run: python -m venv venv && venv\\Scripts\\pip install -r requirements.txt")
    if not (ROOT / "tools" / "watchdog.py").exists():
        errors.append("tools/watchdog.py not found")
    if not (ROOT / "tools" / "tray.py").exists():
        errors.append("tools/tray.py not found")
    if errors:
        for e in errors:
            print(f"  ERROR: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Task Scheduler - watchdog
# ---------------------------------------------------------------------------

def _build_task_xml() -> str:
    pythonw = str(PYTHONW)
    watchdog = str(ROOT / "tools" / "watchdog.py")
    workdir = str(ROOT)
    username = os.environ.get("USERNAME", os.environ.get("USER", ""))

    return dedent(f"""\
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo>
            <Description>Control plane for Little Brother monitoring (watchdog + LB process)</Description>
          </RegistrationInfo>
          <Triggers>
            <LogonTrigger>
              <Enabled>true</Enabled>
              <UserId>{username}</UserId>
            </LogonTrigger>
          </Triggers>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <RestartOnFailure>
              <Interval>PT1M</Interval>
              <Count>3</Count>
            </RestartOnFailure>
            <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{pythonw}</Command>
              <Arguments>"{watchdog}"</Arguments>
              <WorkingDirectory>{workdir}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
    """)


def _install_watchdog_task():
    print("\n[1/3] Registering watchdog with Task Scheduler...")

    xml_path = ROOT / "tools" / "_watchdog_task.xml"
    xml_path.write_text(_build_task_xml(), encoding="utf-16")

    try:
        result = subprocess.run(
            ["schtasks", "/create",
             "/tn", WATCHDOG_TASK_NAME,
             "/xml", str(xml_path),
             "/f"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  OK Task '{WATCHDOG_TASK_NAME}' registered")
        else:
            print(f"  FAIL schtasks failed: {result.stderr.strip()}")
            print("  Tip: Run this script from an elevated (admin) prompt if Task Scheduler requires it.")
    finally:
        xml_path.unlink(missing_ok=True)


def _start_watchdog_task():
    print("\n[2/3] Starting watchdog task now...")
    result = subprocess.run(
        ["schtasks", "/run", "/tn", WATCHDOG_TASK_NAME],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  OK Task started")
    else:
        print(f"  FAIL Could not start task: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Registry autostart - tray
# ---------------------------------------------------------------------------

def _install_tray_autostart():
    print("\n[3/3] Adding tray to Windows autostart...")
    pythonw = str(PYTHONW)
    tray = str(ROOT / "tools" / "tray.py")
    value = f'"{pythonw}" "{tray}"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY,
                            access=winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, TRAY_AUTOSTART_NAME, 0, winreg.REG_SZ, value)
        print(f"  OK '{TRAY_AUTOSTART_NAME}' added to HKCU\\...\\Run")
    except Exception as exc:
        print(f"  FAIL Registry write failed: {exc}")


def _start_tray_now():
    tray = str(ROOT / "tools" / "tray.py")
    subprocess.Popen(
        [str(PYTHONW), tray],
        cwd=str(ROOT),
        creationflags=0x00000008,  # DETACHED_PROCESS
    )
    print("  OK Tray started")


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def uninstall():
    print("Removing Little Brother autostart entries...")
    subprocess.run(["schtasks", "/delete", "/tn", WATCHDOG_TASK_NAME, "/f"],
                   capture_output=True)
    print(f"  OK Task '{WATCHDOG_TASK_NAME}' removed (if it existed)")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY,
                            access=winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, TRAY_AUTOSTART_NAME)
        print(f"  OK '{TRAY_AUTOSTART_NAME}' removed from autostart")
    except FileNotFoundError:
        print(f"  - '{TRAY_AUTOSTART_NAME}' was not registered")
    print("\nDone. Watchdog and tray will no longer start automatically.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def install():
    print("=" * 60)
    print("  Little Brother - Setup")
    print("=" * 60)
    print(f"  Project root : {ROOT}")
    print(f"  Python       : {PYTHONW}")

    _check_prereqs()
    _install_watchdog_task()
    _start_watchdog_task()
    _install_tray_autostart()

    start_tray = input("\nStart tray companion now? [Y/n] ").strip().lower()
    if start_tray in ("", "y", "yes"):
        _start_tray_now()

    print("\n" + "=" * 60)
    print("  Setup complete.")
    print(f"  Watchdog : http://localhost:5001/health")
    print(f"  Dashboard: http://localhost:5000")
    print()
    print("  To enable LB auto-start on watchdog launch:")
    print('    Set "auto_start_app": true in little_brother/config.json')
    print()
    print("  To uninstall: python tools/install.py --uninstall")
    print("=" * 60)


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()
