import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings


class StorageService:
    """Replicates completed archives to administrator-mounted storage paths.

    Remote NFS/SMB/PVC paths must be mounted into this process by the deployment.
    Copies are write-once from this service: it never removes repository files.
    """

    def __init__(self) -> None:
        self.file = Path(settings.BACKUP_STORAGE_DIR) / "storage_targets.json"
        self._ensure()

    def _ensure(self) -> None:
        settings.get_backup_path()
        if not self.file.exists():
            self.file.write_text("[]", encoding="utf-8")

    def _load(self) -> List[Dict[str, Any]]:
        self._ensure()
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("storage target root must be a list")
            return data
        except (OSError, ValueError) as exc:
            raise RuntimeError("Could not read storage target registry. Repair the registry file before continuing.") from exc

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        self._ensure()
        temp = self.file.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        temp.replace(self.file)

    def list(self) -> List[Dict[str, Any]]:
        rows = self._load()
        for row in rows:
            row["available"] = Path(row["path"]).is_dir()
        return rows

    def register(self, target: Dict[str, Any]) -> Dict[str, Any]:
        rows = self._load()
        if any(r["id"] == target["id"] for r in rows):
            raise ValueError("A storage target with this ID already exists.")
        path = Path(target["path"]).expanduser()
        if not path.is_dir():
            raise ValueError("Storage path must already exist and be mounted before registration.")
        target["path"] = str(path.resolve())
        if target.get("primary"):
            for row in rows:
                row["primary"] = False
        rows.append(target)
        self._save(rows)
        return {**target, "available": True}

    def get(self, target_id: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self._load() if r["id"] == target_id), None)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def replicate(self, backup_id: str, target_id: str) -> Dict[str, Any]:
        from app.services.backup_service import backup_service

        record = backup_service.get_backup(backup_id)
        target = self.get(target_id)
        if not record or record.status != "completed":
            raise FileNotFoundError("Completed backup was not found.")
        if not target:
            raise FileNotFoundError("Storage target was not found.")
        source = Path(record.file_path)
        root = Path(target["path"])
        if not source.is_file() or not root.is_dir():
            raise FileNotFoundError("Backup archive or registered storage path is unavailable.")
        expected = record.checksum_sha256 or self._sha256(source)
        destination_dir = root / "dbops-backups" / backup_id
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / record.filename
        if destination.exists():
            if self._sha256(destination) != expected:
                raise FileExistsError("A different archive already exists at this immutable backup path.")
        else:
            temp = destination.with_name(destination.name + ".partial")
            try:
                shutil.copyfile(source, temp)
                if self._sha256(temp) != expected:
                    raise IOError("Replica checksum verification failed.")
                temp.replace(destination)
            finally:
                temp.unlink(missing_ok=True)
        rows = backup_service._load_metadata()
        for row in rows:
            if row.get("backup_id") == backup_id:
                replicas = row.setdefault("replicas", [])
                if not any(r.get("repository_id") == target_id for r in replicas):
                    replicas.append({"repository_id": target_id, "file_path": str(destination), "checksum_sha256": expected})
        backup_service._save_metadata(rows)
        return {"backup_id": backup_id, "repository_id": target_id, "status": "verified", "checksum_sha256": expected, "error": None}

    def resolve_replica(self, backup_record: Any, target_id: Optional[str]) -> Path:
        replicas = getattr(backup_record, "replicas", None) or []
        if target_id:
            replicas = [r for r in replicas if r.get("repository_id") == target_id]
        for replica in replicas:
            path = Path(replica.get("file_path", ""))
            if path.is_file() and self._sha256(path) == replica.get("checksum_sha256"):
                return path
        if target_id:
            raise FileNotFoundError(f"No verified copy is available in repository '{target_id}'.")
        local = Path(backup_record.file_path)
        expected_local = getattr(backup_record, "checksum_sha256", None)
        if local.is_file() and (not expected_local or self._sha256(local) == expected_local):
            return local
        for replica in getattr(backup_record, "replicas", None) or []:
            path = Path(replica.get("file_path", ""))
            if path.is_file() and self._sha256(path) == replica.get("checksum_sha256"):
                return path
        raise FileNotFoundError("No available verified copy of this snapshot was found.")


storage_service = StorageService()
