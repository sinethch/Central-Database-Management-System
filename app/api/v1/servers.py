from urllib.parse import quote
import ipaddress
import asyncio
import json

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import verify_api_key
from app.models.server import ServerListResponse, ServerRecord, ServerRegisterRequest, ServerNameUpdateRequest, ServerPathsUpdateRequest, ServerHealthResponse, ContainerCreateRequest, ContainerCreateResponse
from app.services.database_registry import database_registry
from app.services.server_service import server_service

router = APIRouter(prefix="/servers", tags=["Remote Database Servers"])
MAX_POSTGRES_IMAGE_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024


@router.post("/{server_id}/images/postgres", dependencies=[Depends(verify_api_key)])
async def load_postgres_image_archive(
    server_id: str,
    image: str = Query(..., pattern=r"^postgres:(1[4-9]|[2-9][0-9])(-alpine)?$"),
    archive: UploadFile = File(...),
):
    """Upload a Docker save archive and load its tagged PostgreSQL image on the server."""
    server = server_service.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server was not found.")
    filename = (archive.filename or "").lower()
    if not filename.endswith((".tar", ".tar.gz", ".tgz")):
        raise HTTPException(status_code=400, detail="Choose a Docker image archive (.tar, .tar.gz, or .tgz).")
    size = 0
    while chunk := await archive.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_POSTGRES_IMAGE_ARCHIVE_BYTES:
            await archive.close()
            raise HTTPException(status_code=413, detail="Docker image archive exceeds the 2 GiB upload limit.")
    if size == 0:
        await archive.close()
        raise HTTPException(status_code=400, detail="The selected image archive is empty.")
    await archive.seek(0)
    try:
        return await asyncio.to_thread(server_service.load_postgres_image_archive, server_id, archive.file, image)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        await archive.close()


@router.get("", response_model=ServerListResponse, dependencies=[Depends(verify_api_key)])
async def list_servers():
    servers = server_service.list()
    return {"total": len(servers), "servers": servers}


@router.delete("/{server_id}", dependencies=[Depends(verify_api_key)])
async def remove_server_registration(server_id: str):
    removed = await asyncio.to_thread(server_service.remove_registration, server_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Server registration was not found.")
    return {"server_id": server_id, "removed": True, "message": "Server registration removed from this app. The remote server and its data were not changed."}


@router.patch("/{server_id}/name", response_model=ServerRecord, dependencies=[Depends(verify_api_key)])
async def update_server_name(server_id: str, payload: ServerNameUpdateRequest):
    try:
        server = await asyncio.to_thread(server_service.update_name, server_id, payload.name)
        if not server:
            raise HTTPException(status_code=404, detail="Server registration was not found.")
        return server
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{server_id}/paths", response_model=ServerRecord, dependencies=[Depends(verify_api_key)])
async def update_server_paths(server_id: str, payload: ServerPathsUpdateRequest):
    try:
        server = await asyncio.to_thread(
            server_service.update_paths, server_id, payload.database_root, payload.backup_root,
        )
        if not server:
            raise HTTPException(status_code=404, detail="Server registration was not found.")
        return server
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Could not verify remote directories: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not update remote directories: {exc}")


@router.post("", response_model=ServerRecord, status_code=201, dependencies=[Depends(verify_api_key)])
async def register_server(payload: ServerRegisterRequest):
    try:
        return await asyncio.to_thread(server_service.register, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not verify remote server: {exc}")


@router.get("/{server_id}/health", response_model=ServerHealthResponse, dependencies=[Depends(verify_api_key)])
async def get_server_health(server_id: str):
    try:
        return await asyncio.to_thread(server_service.health, server_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{server_id}/containers", dependencies=[Depends(verify_api_key)])
async def list_managed_containers(server_id: str):
    try:
        containers = await asyncio.to_thread(server_service.list_managed_containers, server_id)
        return {"containers": containers}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not list managed containers: {exc}")


@router.delete("/{server_id}/containers/{container_name}", dependencies=[Depends(verify_api_key)])
async def delete_managed_container(server_id: str, container_name: str):
    try:
        removed = await asyncio.to_thread(server_service.delete_managed_container, server_id, container_name)
        if not removed:
            raise HTTPException(status_code=404, detail="Managed container was not found.")
        database_registry.remove_container_registration(server_id, container_name)
        return {"server_id": server_id, "container_name": container_name, "removed": True, "message": "Container removed. Its data directory and all other server containers were left untouched."}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not remove managed container: {exc}")


@router.post("/{server_id}/containers/postgres", response_model=ContainerCreateResponse, status_code=201, dependencies=[Depends(verify_api_key)])
async def create_postgres_container(server_id: str, payload: ContainerCreateRequest):
    if payload.server_id != server_id:
        raise HTTPException(status_code=400, detail="Path server_id and payload server_id do not match.")
    server = server_service.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server was not found.")
    try:
        created = await asyncio.to_thread(
            server_service.create_postgres_container, server_id, name=payload.name,
            container_name=payload.container_name,
            database_name=payload.database_name, database_user=payload.database_user,
            database_password=payload.database_password, host_port=payload.host_port, image=payload.image,
        )
        try:
            host = server["host"]
            try:
                ip = ipaddress.ip_address(host)
                if ip.version == 6:
                    host = f"[{host}]"
            except ValueError:
                pass
            connection_url = (
                f"postgresql://{quote(payload.database_user, safe='')}:{quote(payload.database_password, safe='')}"
                f"@{host}:{payload.host_port}/{quote(payload.database_name, safe='')}"
            )
            registered = await database_registry.add_database(
                name=payload.name, engine="postgres", connection_url=connection_url, verify=True,
                server_id=server_id, container_id=created["container_name"], read_only_verified=False,
            )
        except Exception:
            # Container remains isolated under its new name and data path; never remove it automatically.
            raise
        return {
            "database_id": registered.id,
            "server_id": server_id,
            "container_name": created["container_name"],
            "database_name": payload.database_name,
            "connection_url": registered.connection_url,
            "data_directory": created["data_directory"],
            "status": registered.status,
            "message": "A new isolated PostgreSQL container was created. Existing server containers and databases were not touched.",
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Container provisioning failed: {exc}")


@router.post("/{server_id}/containers/postgres/stream", dependencies=[Depends(verify_api_key)])
async def create_postgres_container_stream(server_id: str, payload: ContainerCreateRequest):
    """Provision a PostgreSQL container and stream remote Docker output as SSE."""
    if payload.server_id != server_id:
        raise HTTPException(status_code=400, detail="Path server_id and payload server_id do not match.")
    server = server_service.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server was not found.")

    async def event_stream():
        loop = asyncio.get_running_loop()
        events = asyncio.Queue()

        def report(message: str):
            loop.call_soon_threadsafe(events.put_nowait, {"type": "progress", "message": message})

        async def provision():
            return await asyncio.to_thread(
                server_service.create_postgres_container, server_id,
                name=payload.name, container_name=payload.container_name, database_name=payload.database_name,
                database_user=payload.database_user, database_password=payload.database_password,
                host_port=payload.host_port, image=payload.image, progress=report,
            )

        task = asyncio.create_task(provision())
        yield f"data: {json.dumps({'type': 'progress', 'message': 'Provisioning started'})}\n\n"
        while not task.done() or not events.empty():
            try:
                item = await asyncio.wait_for(events.get(), timeout=0.25)
                yield f"data: {json.dumps(item)}\n\n"
            except asyncio.TimeoutError:
                if task.done() and events.empty():
                    break
        try:
            created = task.result()
            yield f"data: {json.dumps({'type': 'progress', 'message': 'Registering the new database with DBOps'})}\n\n"
            host = server["host"]
            try:
                ip = ipaddress.ip_address(host)
                if ip.version == 6:
                    host = f"[{host}]"
            except ValueError:
                pass
            connection_url = (
                f"postgresql://{quote(payload.database_user, safe='')}:{quote(payload.database_password, safe='')}"
                f"@{host}:{payload.host_port}/{quote(payload.database_name, safe='')}"
            )
            registered = await database_registry.add_database(
                name=payload.name, engine="postgres", connection_url=connection_url, verify=True,
                server_id=server_id, container_id=created["container_name"], read_only_verified=False,
            )
            result = {
                "database_id": registered.id, "server_id": server_id,
                "container_name": created["container_name"], "database_name": payload.database_name,
                "connection_url": registered.connection_url, "data_directory": created["data_directory"],
                "status": registered.status,
                "message": "A new isolated PostgreSQL container was created. Existing server containers and databases were not touched.",
            }
            yield f"data: {json.dumps({'type': 'complete', 'result': result})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{server_id}/snapshots/{backup_id}", dependencies=[Depends(verify_api_key)])
async def copy_snapshot_to_server(server_id: str, backup_id: str):
    try:
        return await asyncio.to_thread(server_service.copy_snapshot, server_id, backup_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Snapshot copy failed: {exc}")
