"""A JSON-RPC 2.0 client over a process's stdio, one message per line: the Agent Client
Protocol transport `agy acp` serves. Requests carry ids and get their replies,
notifications carry none, and the agent's own requests get the client's reply.
"""

from __future__ import annotations

import asyncio
import json
import logging

LOGGER = logging.getLogger("tgforge")


class AcpError(RuntimeError):
    """An error reply to a request: its `code` and `message`."""

    def __init__(self, code, message: str):
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message


class AcpClient:
    """One agent connection. `run()` reads the agent's stdout until it ends and hands
    every message to `handler` in arrival order, awaited one at a time:
    `handler.on_reply(id, method, result, error)` for a reply to a request sent here
    (before the request's own future resolves), `handler.on_notification(method,
    params)`, `handler.on_request(id, method, params)` for the agent's requests."""

    def __init__(self, stdout, stdin, handler):
        self._stdout = stdout
        self._stdin = stdin
        self._handler = handler
        self._next_id = 0
        # id → (method, the reply's future; None when only `on_reply` takes it)
        self._waiting: dict[str, tuple[str, asyncio.Future | None]] = {}

    async def _write(self, message: dict) -> None:
        self._stdin.write((json.dumps(message) + "\n").encode())
        await self._stdin.drain()

    async def send_request(
        self, method: str, params: dict, wait: bool = True
    ) -> tuple[str, asyncio.Future | None]:
        """Send a request; returns its id (as text) and the future its reply resolves
        (the result, or an `AcpError`) — None with `wait=False`, when only
        `handler.on_reply` takes the reply."""
        self._next_id += 1
        request_id = self._next_id
        reply = asyncio.get_running_loop().create_future() if wait else None
        self._waiting[str(request_id)] = (method, reply)  # before the write: the reader may answer
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await self._write(message)
        except (BrokenPipeError, ConnectionResetError, OSError) as error:
            self._waiting.pop(str(request_id), None)
            raise AcpError(-32000, f"the agent process is gone: {error!r}") from error
        return str(request_id), reply

    async def request(self, method: str, params: dict):
        """Send a request and wait for its result; raises `AcpError` on an error reply."""
        _request_id, reply = await self.send_request(method, params)
        return await reply

    async def notify(self, method: str, params: dict) -> bool:
        """Send a notification; False when the agent process is gone."""
        try:
            await self._write({"jsonrpc": "2.0", "method": method, "params": params})
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False
        return True

    async def respond(self, request_id, result=None, error: dict | None = None) -> None:
        """Answer one of the agent's requests (its id unchanged)."""
        body = {"error": error} if error is not None else {"result": result}
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, **body})
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOGGER.warning("acp: reply to %r not sent, the agent process is gone", request_id)

    async def run(self) -> None:
        """Read until the agent's stdout ends; then fail every request still waiting."""
        try:
            async for raw in self._stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("acp: a line that is not JSON: %r", line[:200])
                    continue
                await self._dispatch(message)
        finally:
            waiting, self._waiting = self._waiting, {}
            for _method, reply in waiting.values():
                if reply is not None and not reply.done():
                    reply.set_exception(AcpError(-32000, "the agent process ended"))

    async def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:  # a reply
            request_id = str(message.get("id"))
            if request_id not in self._waiting:
                return
            sent_method, reply = self._waiting.pop(request_id)
            error = message.get("error")
            result = message.get("result")
            await self._handler.on_reply(request_id, sent_method, result, error)
            if reply is None or reply.done():
                return
            if error is not None:
                reply.set_exception(AcpError(error.get("code"), error.get("message", "")))
            else:
                reply.set_result(result)
        elif "id" in message:
            await self._handler.on_request(message["id"], method, message.get("params") or {})
        else:
            await self._handler.on_notification(method, message.get("params") or {})
