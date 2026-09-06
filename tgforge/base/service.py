"""Managed-service control for the bot: install/uninstall a user service, report
whether it's running, and a detached restart that never self-waits (a plain
restart from inside a bot-driven turn would deadlock against the graceful drain).

Two backends, picked by platform: systemd --user on Linux, launchd LaunchAgent on
macOS. Both grant the main process a drain window on stop (systemd
KillMode=mixed + TimeoutStopSec, launchd ExitTimeOut) and both wait, before even
issuing the restart, while any bot-driven `claude` is still alive — a live turn, a
/compact, or a subagent, in any window — so a restart never interrupts another
window's in-flight work. The wait is bounded (a 60-min cap), then it restarts anyway.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tgforge.base.kernel import IS_LINUX, IS_MACOS

_DRAIN_SECS = 150


def service_manager() -> str:
    """Human name of the per-user process manager backing the managed service."""
    if IS_LINUX:
        return "systemd user service"
    if IS_MACOS:
        return "launchd LaunchAgent"
    return "no managed service"


_UNIT = """[Unit]
Description=tgforge Telegram bot ({service})
After=network-online.target

[Service]
Type=simple
WorkingDirectory={home}
ExecStart={exec_start} run
Restart=on-failure
RestartSec=3
KillMode=mixed
TimeoutStopSec={drain}

[Install]
WantedBy=default.target
"""

_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{service}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exec_start}</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{home}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ExitTimeOut</key><integer>{drain}</integer>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""


def _exec_start() -> str:
    return shutil.which("tgforge") or str(Path.home() / ".local" / "bin" / "tgforge")


def _plist_path(service: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service}.plist"


def _unit_path(service: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{service}.service"


def install_unit(service: str, home: Path) -> Path:
    """Write, enable, and start a user service that runs the default app from
    `home` (where bot.json lives). Returns the service file path."""
    exec_start = _exec_start()
    if IS_MACOS:
        dest = _plist_path(service)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            _PLIST.format(service=service, home=home, exec_start=exec_start, drain=_DRAIN_SECS)
        )
        subprocess.run(["launchctl", "unload", str(dest)], check=False, stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "load", "-w", str(dest)], check=False)
        return dest
    dest = _unit_path(service)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        _UNIT.format(service=service, home=home, exec_start=exec_start, drain=_DRAIN_SECS)
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", service], check=False)
    return dest


def uninstall_unit(service: str) -> None:
    """Stop + disable the service and remove its file. Safe if it doesn't exist."""
    if IS_MACOS:
        dest = _plist_path(service)
        subprocess.run(
            ["launchctl", "unload", "-w", str(dest)], check=False, stderr=subprocess.DEVNULL
        )
        dest.unlink(missing_ok=True)
        return
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", service],
        check=False,
        stderr=subprocess.DEVNULL,
    )
    _unit_path(service).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def service_active(service: str) -> bool:
    """True if the service is currently running."""
    if IS_MACOS:
        r = subprocess.run(["launchctl", "list", service], capture_output=True, text=True)
        # a loaded+running job prints a dict with a numeric "PID" key
        return r.returncode == 0 and '"PID"' in r.stdout
    r = subprocess.run(
        ["systemctl", "--user", "is-active", service], capture_output=True, text=True
    )
    return r.stdout.strip() == "active"


# Wait while any bot-driven claude is alive, then restart — a live turn, a /compact,
# or a subagent, in ANY window — so a restart never interrupts another window's work.
# The scope is the bot's process subtree: on Linux that's exactly the unit's cgroup;
# on macOS there is no cgroup, so each claude's parent chain is walked up to the bot.
_LINUX_RESTART = """
cgroup="$(systemctl --user show {service} -p ControlGroup --value)"
cgroup_file="/sys/fs/cgroup$cgroup/cgroup.procs"
for _ in $(seq 1 720); do
    busy=0
    for pid in $(cat "$cgroup_file" 2>/dev/null); do
        if [ "$(cat /proc/$pid/comm 2>/dev/null)" = claude ]; then busy=1; break; fi
    done
    [ "$busy" -eq 0 ] && break
    sleep 5
done
systemctl --user restart {service}
"""

_MACOS_RESTART = """
lock="{home}/restart.lock"
mkdir "$lock" 2>/dev/null || exit 0
trap 'rmdir "$lock" 2>/dev/null' EXIT
bot="$(launchctl list {service} 2>/dev/null | sed -n 's/.*"PID" = \\([0-9][0-9]*\\).*/\\1/p')"
descends_from_bot() {{
    p="$1"
    for _ in $(seq 1 40); do
        [ -z "$p" ] && return 1
        [ "$p" = "$bot" ] && return 0
        [ "$p" = 1 ] && return 1
        p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
    done
    return 1
}}
for _ in $(seq 1 720); do
    [ -z "$bot" ] && break
    busy=0
    for pid in $(pgrep -x claude 2>/dev/null); do
        if descends_from_bot "$pid"; then busy=1; break; fi
    done
    [ "$busy" -eq 0 ] && break
    sleep 5
done
launchctl kickstart -k "gui/$(id -u)/{service}"
"""


def pop_restart_announce(home: Path) -> int | None:
    """Read + delete the thread id a restart asked to be pinged in, if any."""
    f = Path(home) / ".restart_announce"
    try:
        tid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    f.unlink(missing_ok=True)
    return tid


def detached_restart(
    service: str, home: Path | None = None, announce_thread: int | None = None
) -> None:
    """Restart the service from inside a bot-driven turn without self-waiting.
    Single-instance: a systemd transient unit / a macOS lock dir drops a second
    concurrent restart. `home` is required on macOS (the lock lives under it) and to
    carry `announce_thread` — the topic the fresh process pings when it comes back."""
    if announce_thread is not None and home is not None:
        try:
            (Path(home) / ".restart_announce").write_text(str(announce_thread))
        except OSError:
            pass
    if IS_MACOS:
        script = _MACOS_RESTART.format(service=service, home=home or Path.home())
        subprocess.Popen(  # noqa: S603 — detached waiter, survives our exit
            ["bash", "-c", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    script = _LINUX_RESTART.format(service=service)
    unit = f"{service}_restart"
    subprocess.run(
        ["systemctl", "--user", "stop", f"{unit}.service"],
        check=False,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["systemd-run", "--user", "--collect", f"--unit={unit}", "bash", "-c", script],
        check=False,
    )
