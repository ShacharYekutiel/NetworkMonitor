#!/usr/bin/env python3
"""[1.4.2] CI: SSH (config backup) works inside the hardened app container.

Run inside the netmon container (`docker compose exec -T netmon python - < this file`):
starts an asyncssh server on 127.0.0.1 with an in-memory host key, connects with asyncssh's
client using a password (as the config backup does), runs a command and checks the output.
Proves that the read-only root filesystem, the dropped capabilities and no-new-privileges
leave the SSH client (key exchange, ciphers, sockets) working. Standard library + asyncssh.
"""

from __future__ import annotations

import asyncio
import sys

import asyncssh

USER, PASSWORD = "ci", "ci-ssh-pass"


class Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD


async def handle(process: asyncssh.SSHServerProcess) -> None:
    process.stdout.write(f"running-config for {process.command}\n")
    process.exit(0)


async def main() -> int:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server, "127.0.0.1", 0, server_host_keys=[host_key], process_factory=handle
    )
    port = server.sockets[0].getsockname()[1]
    try:
        async with asyncssh.connect(
            "127.0.0.1", port, username=USER, password=PASSWORD, known_hosts=None
        ) as conn:
            res = await conn.run("show running-config", check=True)
    finally:
        server.close()
    out = str(res.stdout)
    print("ssh self-test:", out.strip())
    return 0 if "running-config for show running-config" in out else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
