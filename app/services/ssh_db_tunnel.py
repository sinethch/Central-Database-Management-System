"""Short-lived, loopback-only SSH tunnels for local-only registered databases."""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from app.services.server_service import server_service


def _is_local_target(target: dict) -> bool:
    if target.get("access_scope") == "local":
        return True
    # Older local targets were saved before access_scope was added.
    url = urlsplit(target.get("connection_url", ""))
    return bool(
        target.get("server_id")
        and target.get("container_id")
        and url.hostname in {"127.0.0.1", "::1", "localhost"}
    )


def is_local_database_target(target: dict) -> bool:
    return _is_local_target(target)


@asynccontextmanager
async def database_url_through_ssh(target: dict):
    """Yield the original URL, or a temporary localhost-forwarded URL for local targets."""
    original_url = target.get("connection_url", "")
    if not _is_local_target(target):
        yield original_url
        return

    server_id = target.get("server_id")
    if not server_id:
        raise RuntimeError("The local database has no registered SSH server for tunneling.")
    server_row = server_service.get(server_id)
    if not server_row:
        raise RuntimeError("The SSH server for this local database is no longer registered.")

    parsed = urlsplit(original_url)
    remote_port = parsed.port
    if not parsed.hostname or not remote_port:
        raise RuntimeError("The local database connection URL has no valid host and port.")

    client = await asyncio.to_thread(server_service.connect, server_row)
    local_server = None

    async def forward(local_reader: asyncio.StreamReader, local_writer: asyncio.StreamWriter):
        channel = None
        try:
            transport = client.get_transport()
            if not transport or not transport.is_active():
                raise RuntimeError("The SSH connection closed before the database tunnel opened.")
            peer = local_writer.get_extra_info("peername") or ("127.0.0.1", 0)
            channel = await asyncio.to_thread(
                transport.open_channel,
                "direct-tcpip",
                ("127.0.0.1", remote_port),
                (peer[0], peer[1]),
                timeout=10,
            )
            if channel is None:
                raise RuntimeError("The SSH server refused the database tunnel.")

            async def send_to_remote():
                while data := await local_reader.read(65536):
                    await asyncio.to_thread(channel.sendall, data)
                try:
                    channel.shutdown_write()
                except Exception:
                    pass

            async def send_to_local():
                while data := await asyncio.to_thread(channel.recv, 65536):
                    local_writer.write(data)
                    await local_writer.drain()

            to_remote = asyncio.create_task(send_to_remote())
            to_local = asyncio.create_task(send_to_local())
            done, pending = await asyncio.wait((to_remote, to_local), return_when=asyncio.FIRST_COMPLETED)
            failed = any(task.cancelled() or task.exception() for task in done)
            if to_local in done or failed:
                for task in pending:
                    task.cancel()
                channel.close()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()  # Retrieve any forwarding exception.
        except Exception:
            # A client disconnect or a remote database restart should only end
            # this forwarded connection, not the API request handler.
            pass
        finally:
            if channel:
                channel.close()
            local_writer.close()
            try:
                await local_writer.wait_closed()
            except Exception:
                pass

    try:
        local_server = await asyncio.start_server(forward, host="127.0.0.1", port=0)
        local_port = local_server.sockets[0].getsockname()[1]
        userinfo, separator, _hostport = parsed.netloc.rpartition("@")
        authority = f"{userinfo}@" if separator else ""
        tunneled_url = urlunsplit((parsed.scheme, f"{authority}127.0.0.1:{local_port}", parsed.path, parsed.query, parsed.fragment))
        yield tunneled_url
    finally:
        if local_server:
            local_server.close()
            await local_server.wait_closed()
        await asyncio.to_thread(client.close)
