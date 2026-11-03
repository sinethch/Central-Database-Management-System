from fastapi import APIRouter, Depends, HTTPException, status
from app.api.deps import verify_api_key
from app.models.storage import StorageTargetCreate, StorageTargetList, StorageTarget, ReplicationResult
from app.services.storage_service import storage_service

router = APIRouter(prefix="/storage", tags=["Snapshot Storage"])


@router.get("/targets", response_model=StorageTargetList, dependencies=[Depends(verify_api_key)])
async def list_targets():
    targets = storage_service.list()
    return {"total": len(targets), "targets": targets}


@router.post("/targets", response_model=StorageTarget, status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_api_key)])
async def register_target(payload: StorageTargetCreate):
    try:
        return storage_service.register(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/replicate/{backup_id}/{repository_id}", response_model=ReplicationResult, dependencies=[Depends(verify_api_key)])
async def replicate(backup_id: str, repository_id: str):
    try:
        return storage_service.replicate(backup_id, repository_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (FileExistsError, IOError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
