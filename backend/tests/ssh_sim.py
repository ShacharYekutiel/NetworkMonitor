"""[3] A small asyncssh server that behaves like a network device CLI (tests of the config
backup session engine and runner). Records every command it receives (`commands`)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import asyncssh

COMWARE_CONFIG = (
    "#\n version 7.1.070, Release 6635P07\n#\n sysname sw1\n#\n"
    " snmp-agent community read cipher $c$3$xyz\n#\n local-user admin class manage\n"
    "  password hash $h$6$secret==\n#\nreturn"
)
CISCO_CONFIG = (
    "Building configuration...\n\nCurrent configuration : 1234 bytes\n!\n"
    "! Last configuration change at 10:00:00 UTC Sun Nov 8 2026\nhostname r1\n"
    "enable secret 9 $9$abc\n!\nend"
)
MIKROTIK_CONFIG = (
    "# 2026-11-08 02:47:03 by RouterOS 7.16\n# software id = ABCD-1234\n"
    "/system identity\nset name=mt1"
)


@dataclass
class Sim:
    mode: str = "comware"  # comware | cisco | aruba | exec
    username: str = "netmon"
    password: str = "pw"
    enable_password: str = "en"
    config: str = COMWARE_CONFIG
    host_key: Any = None
    commands: list[str] = field(default_factory=list)
    hang: bool = False  # never send a prompt
    exec_fail: set[str] = field(default_factory=set)  # exec commands that exit 1
    server: Any = None
    port: int = 0

    async def start(self) -> Sim:
        if self.host_key is None:
            self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        sim = self

        class _Server(asyncssh.SSHServer):
            def begin_auth(self, username: str) -> bool:
                return True

            def password_auth_supported(self) -> bool:
                return True

            def validate_password(self, username: str, password: str) -> bool:
                return username == sim.username and password == sim.password

        self.server = await asyncssh.create_server(
            _Server,
            "127.0.0.1",
            0,
            server_host_keys=[self.host_key],
            process_factory=self._handle,
            encoding="utf-8",
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.close()

    def new_host_key(self) -> None:
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.server.update(server_host_keys=[self.host_key])

    # ------------------------------------------------------------------ CLI
    async def _handle(self, process: asyncssh.SSHServerProcess) -> None:
        try:
            if process.command is not None:
                await self._exec(process, process.command)
            else:
                await self._shell(process)
        except (asyncssh.BreakReceived, asyncssh.TerminalSizeChanged, ConnectionError):
            pass
        finally:
            process.exit(0)

    async def _exec(self, process: Any, cmd: str) -> None:
        self.commands.append(cmd)
        if cmd in self.exec_fail:
            process.stderr.write("bad command name\n")
            process.exit(1)
            return
        if cmd in ("/export", "show configuration", 'clish -c "show configuration"'):
            process.stdout.write(self.config.replace("\n", "\r\n") + "\r\n")
        else:
            process.stdout.write("bad command name\r\n")

    async def _shell(self, process: Any) -> None:
        if self.hang:
            await asyncio.sleep(3600)
            return
        enabled = self.mode != "cisco"
        if self.mode == "aruba":
            process.stdout.write("\x1b[2J\x1b[24;1HPress any key to continue")
            await process.stdin.read(1)
        host = {"comware": "<sw1>", "cisco": "r1", "aruba": "sw2#"}.get(self.mode, "x#")

        def prompt() -> str:
            if self.mode == "cisco":
                return host + ("#" if enabled else ">")
            if self.mode == "aruba":
                return "\x1b[2K" + host + " "
            return host

        process.stdout.write("\r\nWelcome\r\n" + prompt())
        while True:
            line = await process.stdin.readline()
            if not line:
                return
            cmd = line.strip()
            if cmd == "":
                process.stdout.write("\r\n" + prompt())
                continue
            self.commands.append(cmd)
            out = ""
            if cmd in ("exit", "quit", "logout"):
                return
            if self.mode == "cisco" and cmd == "enable":
                process.stdout.write(cmd + "\r\nPassword: ")
                pw = (await process.stdin.readline()).strip()
                if pw == self.enable_password:
                    enabled = True
                    process.stdout.write("\r\n" + prompt())
                else:
                    process.stdout.write("\r\n% Access denied\r\n" + prompt())
                continue
            if cmd in (
                "screen-length disable",
                "no page",
                "terminal length 0",
                "terminal width 0",
            ):
                out = ""
            elif cmd in ("display current-configuration", "show running-config"):
                out = self.config
            else:
                out = "  % Unrecognized command found at '^' position."
            # the server's line editor already echoed the command
            body = out.replace("\n", "\r\n") + "\r\n" if out else ""
            process.stdout.write(body + prompt())
