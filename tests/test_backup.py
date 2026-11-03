import os
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from cryptography.fernet import Fernet
from app.services.backup_service import backup_service
from app.config import settings


def test_encryption_key_derivation(monkeypatch):
    """Fernet key derived from secret is valid 32-byte base64."""
    monkeypatch.setattr(settings, "BACKUP_ENCRYPTION_KEY", "unit-test-backup-encryption-key-32-bytes")
    key = backup_service._get_encryption_key()
    fernet = Fernet(key)
    sample = b"SELECT * FROM confidential_table;"
    token = fernet.encrypt(sample)
    decrypted = fernet.decrypt(token)
    assert decrypted == sample


def test_retention_pruning():
    """Backups older than retention_days are pruned while newer ones are preserved."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        old_dir = settings.BACKUP_STORAGE_DIR
        old_metadata_file = backup_service.metadata_file
        old_retention_days = settings.BACKUP_RETENTION_DAYS
        settings.BACKUP_STORAGE_DIR = str(temp_path)
        backup_service.metadata_file = temp_path / "backup_metadata.json"

        # Create 1 expired file and 1 fresh file
        old_file = temp_path / "old_backup.dump"
        old_file.write_bytes(b"old data")

        fresh_file = temp_path / "fresh_backup.dump"
        fresh_file.write_bytes(b"fresh data")

        now = datetime.now(timezone.utc)
        records = [
            {
                "backup_id": "old001",
                "filename": "old_backup.dump",
                "file_path": str(old_file),
                "format": "custom",
                "size_bytes": 8,
                "size_human": "8 B",
                "compressed": True,
                "encrypted": False,
                "status": "completed",
                "created_at": (now - timedelta(days=20)).isoformat(),
                "completed_at": (now - timedelta(days=20)).isoformat(),
                "duration_seconds": 1.0,
                "age_days": 20.0,
                "description": "Old backup",
                "error": None,
                "checksum_sha256": "dummy"
            },
            {
                "backup_id": "fresh002",
                "filename": "fresh_backup.dump",
                "file_path": str(fresh_file),
                "format": "custom",
                "size_bytes": 10,
                "size_human": "10 B",
                "compressed": True,
                "encrypted": False,
                "status": "completed",
                "created_at": (now - timedelta(days=2)).isoformat(),
                "completed_at": (now - timedelta(days=2)).isoformat(),
                "duration_seconds": 1.0,
                "age_days": 2.0,
                "description": "Fresh backup",
                "error": None,
                "checksum_sha256": "dummy2"
            }
        ]
        backup_service._save_metadata(records)

        # Enforce retention with limit of 14 days
        settings.BACKUP_RETENTION_DAYS = 14
        deleted_count = backup_service.enforce_retention()

        assert deleted_count == 1
        assert not old_file.exists(), "Old backup file should have been deleted"
        assert fresh_file.exists(), "Fresh backup file must remain intact"

        remaining = backup_service.list_backups()
        assert len(remaining) == 1
        assert remaining[0].backup_id == "fresh002"

        # Restore setting
        settings.BACKUP_STORAGE_DIR = old_dir
        backup_service.metadata_file = old_metadata_file
        settings.BACKUP_RETENTION_DAYS = old_retention_days
