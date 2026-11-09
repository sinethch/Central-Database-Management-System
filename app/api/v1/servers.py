from urllib.parse import quote
import ipaddress
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import verify_api_key
from app.models.server import ServerListResponse, ServerRecord, ServerRegisterRequest, ServerHostKeyRequest, ServerNameUpdateRequest, ServerPathsUpdateRequest, ServerHealthResponse, ContainerCreateRequest, ContainerCreateResponse
from app.services.database_registry import database_registry
from app.services.server_service import server_service

router = APIRouter(prefix="/servers", tags=["Remote Database Servers"])


@router.post("/host-key", dependencies=[Depends(verify_api_key)])
async def inspect_server_host_key(payload: ServerHostKeyRequest):
    try:
        return await asyncio.to_thread(server_service.inspect_host_key, payload.host, payload.ssh_port)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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


@router.get("/{server_id}/ports/{host_port}", dependencies=[Depends(verify_api_key)])
async def check_database_port(server_id: str, host_port: int):
    if host_port < 1024 or host_port > 65535:
        raise HTTPException(status_code=400, detail="Choose a port from 1024 to 65535.")
    try:
        return await asyncio.to_thread(server_service.check_database_port, server_id, host_port)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not check server port: {exc}")


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


@router.post("/{server_id}/containers/{engine}", response_model=ContainerCreateResponse, status_code=201, dependencies=[Depends(verify_api_key)])
async def create_database_container(server_id: str, engine: str, payload: ContainerCreateRequest):
    if engine not in {"postgres", "mongodb", "mysql", "redis"}:
        raise HTTPException(status_code=400, detail="Unsupported database engine.")
    if engine != payload.engine:
        raise HTTPException(status_code=400, detail="Path engine and payload engine do not match.")
    if payload.server_id != server_id:
        raise HTTPException(status_code=400, detail="Path server_id and payload server_id do not match.")
    server = server_service.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server was not found.")
    try:
        created = await asyncio.to_thread(
            server_service.create_database_container, server_id, name=payload.name,
            container_name=payload.container_name,
            database_name=payload.database_name, database_user=payload.database_user,
            database_password=payload.database_password, host_port=payload.host_port, image=payload.image, engine=engine,
        )
        try:
            host = server["host"]
            try:
                ip = ipaddress.ip_address(host)
                if ip.version == 6:
                    host = f"[{host}]"
            except ValueError:
                pass
            user = quote(payload.database_user, safe=''); password = quote(payload.database_password, safe=''); dbname = quote(payload.database_name, safe='')
            connection_url = {
                "postgres": f"postgresql://{user}:{password}@{host}:{payload.host_port}/{dbname}",
                "mongodb": f"mongodb://{user}:{password}@{host}:{payload.host_port}/{dbname}?authSource=admin",
                "mysql": f"mysql://{user}:{password}@{host}:{payload.host_port}/{dbname}",
                "redis": f"redis://:{password}@{host}:{payload.host_port}/0",
            }[engine]
            registered = await database_registry.add_database(
                name=payload.name, engine=engine, connection_url=connection_url, verify=True,
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
            "message": f"A new isolated {engine} container was created. Existing server containers and databases were not touched.",
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Container provisioning failed: {exc}")


@router.post("/{server_id}/containers/{engine}/stream", dependencies=[Depends(verify_api_key)])
async def create_database_container_stream(server_id: str, engine: str, payload: ContainerCreateRequest):
    """Provision a database container and stream remote Docker output as SSE."""
    if engine not in {"postgres", "mongodb", "mysql", "redis"}:
        raise HTTPException(status_code=400, detail="Unsupported database engine.")
    if engine != payload.engine:
        raise HTTPException(status_code=400, detail="Path engine and payload engine do not match.")
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
                server_service.create_database_container, server_id,
                name=payload.name, container_name=payload.container_name, database_name=payload.database_name,
                database_user=payload.database_user, database_password=payload.database_password,
                host_port=payload.host_port, image=payload.image, progress=report, engine=engine,
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
            user = quote(payload.database_user, safe=''); password = quote(payload.database_password, safe=''); dbname = quote(payload.database_name, safe='')
            connection_url = {
                "postgres": f"postgresql://{user}:{password}@{host}:{payload.host_port}/{dbname}",
                "mongodb": f"mongodb://{user}:{password}@{host}:{payload.host_port}/{dbname}?authSource=admin",
                "mysql": f"mysql://{user}:{password}@{host}:{payload.host_port}/{dbname}",
                "redis": f"redis://:{password}@{host}:{payload.host_port}/0",
            }[engine]
            registered = await database_registry.add_database(
                name=payload.name, engine=engine, connection_url=connection_url, verify=True,
                server_id=server_id, container_id=created["container_name"], read_only_verified=False,
            )
            result = {
                "database_id": registered.id, "server_id": server_id,
                "container_name": created["container_name"], "database_name": payload.database_name,
                "connection_url": registered.connection_url, "data_directory": created["data_directory"],
                "status": registered.status,
                "message": f"A new isolated {engine} container was created. Existing server containers and databases were not touched.",
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
