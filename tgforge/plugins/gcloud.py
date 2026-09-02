"""gcloud plugin: `/gcloud [adc]` — a browserless gcloud sign-in.

Runs the no-launch-browser auth flow with the display unset, streams the sign-in
URL, takes the next message as the verification code (via the text prompt), then
offers to hand the result to the agent. Runs inline in the current window; no class
of its own.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from tgforge.base.kernel import Plugin, reap, universal


def _login_env() -> dict:
    return {
        **os.environ,
        "PATH": f"{Path.home()}/.local/bin:{Path.home()}/.cargo/bin:" + os.environ.get("PATH", ""),
    }


class Gcloud(Plugin):
    id = "gcloud"

    @universal("/gcloud", "browserless gcloud sign-in", icon="☁️")
    async def gcloud(self, ctx):
        if ctx.args:  # typed `adc` keeps working from the keyboard
            adc = ctx.args.lower() in ("adc", "application-default")
        else:
            choice = await ctx.menu(
                "☁️ gcloud sign-in",
                [("CLI credential", "cli"), ("+ ADC (application-default)", "adc")],
            )
            if choice is None:
                return
            adc = choice == "adc"
        cmd = "env -u DISPLAY -u WAYLAND_DISPLAY gcloud auth login --no-launch-browser"
        if adc:
            cmd += " --update-adc"
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_login_env(),
        )
        buf = ""
        try:
            while "verification code" not in buf.lower():
                chunk = await asyncio.wait_for(proc.stdout.read(2048), timeout=25)
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
        except TimeoutError:
            pass
        if "http" not in buf:
            await reap(proc)
            await ctx.send(f"gcloud produced no sign-in URL:\n{buf[-1200:]}")
            return
        code = await ctx.ask_text(
            f"{buf.strip()}\n\n↳ send the verification code as your next message",
            timeout=300,
        )
        if not code:
            await reap(proc)
            return
        try:
            proc.stdin.write((code.strip() + "\n").encode())
            await proc.stdin.drain()
            out = await asyncio.wait_for(proc.stdout.read(), timeout=60)
            rc = await asyncio.wait_for(proc.wait(), timeout=10)
        except (TimeoutError, BrokenPipeError, ConnectionResetError, OSError):
            await reap(proc)
            await ctx.send("gcloud: failed to complete sign-in")
            return
        text = out.decode(errors="replace").strip() or "(no output)"
        mid = await ctx.send(f"{text}\n[gcloud exit {rc}]")
        if mid is not None and ctx.has_service("agent.prompt"):
            await ctx.offer_handoff(
                mid,
                f"[gcloud result] {text}",
                service="agent.prompt",
                label="↪ hand to the agent",
            )
