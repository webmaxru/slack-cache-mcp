"""Windows Task Scheduler install/uninstall via schtasks + generated XML."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

TASK_NAME = "slackcache-daemon"


def _slackcache_command() -> list[str]:
    """Resolve argv prefix to invoke slackcache (frozen exe or python -m)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    # Prefer console script next to the interpreter when available
    exe = shutil.which("slackcache")
    if exe:
        return [exe]
    return [sys.executable, "-m", "slack_cache_mcp.cli"]


def generate_task_xml(*, command: list[str] | None = None, data_dir: Path | None = None) -> str:
    """Generate Task Scheduler XML (At log on + 5-minute repetition watchdog)."""
    cmd = command or _slackcache_command()
    # schtasks Action: Command + Arguments
    exe = cmd[0]
    args = list(cmd[1:]) + ["daemon", "--ensure-single"]
    if data_dir is not None:
        args.extend(["--data-dir", str(data_dir)])
    args_s = " ".join(f'"{a}"' if " " in a else a for a in args)
    # Working directory = parent of exe when possible
    work = str(Path(exe).resolve().parent)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>slack-cache-mcp continuous offline archive daemon</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(args_s)}</Arguments>
      <WorkingDirectory>{escape(work)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_task(*, data_dir: Path | None = None, command: list[str] | None = None) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "error": "windows_only"}
    xml = generate_task_xml(command=command, data_dir=data_dir)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16"
    ) as fh:
        fh.write(xml)
        xml_path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "task": TASK_NAME,
        }
    finally:
        xml_path.unlink(missing_ok=True)


def uninstall_task() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "error": "windows_only"}
    proc = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "task": TASK_NAME,
    }


def task_status() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "error": "windows_only", "installed": False}
    proc = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "ok": proc.returncode == 0,
        "installed": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "task": TASK_NAME,
    }
