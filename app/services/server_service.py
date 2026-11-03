import base64
import hashlib
import ipaddress
import json
import posixpath
import shlex
import stat
import time
import uuid
from typing import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from cryptography.fernet import Fernet
import paramiko

from app.config import settings


def _safe_root(value: str) -> str:
    raw = (value or "").strip()
    if not raw.startswith("/") or raw.startswith("//") or "," in raw or "\x00" in raw or ".." in raw.split("/"):
        raise ValueError("Enter an absolute Linux path without '..' segments.")
    normalized = posixpath.normpath(raw)
    if normalized == "/":
        raise ValueError("The filesystem root cannot be used as a database or snapshot directory.")
    return normalized


class ServerService:
    def __init__(self):
        self.file = Path(settings.BACKUP_STORAGE_DIR) / "server_registry.json"
        self._ensure()

    def _ensure(self):
        settings.get_backup_path()
        if not self.file.exists():
            self.file.write_text("[]", encoding="utf-8")

    def _cipher(self) -> Fernet:
        secret = settings.SERVER_CREDENTIAL_ENCRYPTION_KEY
        if not secret or len(secret) < 32:
            raise RuntimeError("Set SERVER_CREDENTIAL_ENCRYPTION_KEY to a unique random secret of at least 32 characters before registering remote servers.")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(key)

    def _load(self) -> List[Dict[str, Any]]:
        self._ensure()
        try:
            rows = json.loads(self.file.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        self._ensure()
        temp = self.file.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        temp.replace(self.file)

    def public(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {k: row[k] for k in ("id", "name", "host", "ssh_port", "username", "database_root", "backup_root", "status", "created_at")}

    def list(self) -> List[Dict[str, Any]]:
        return [self.public(row) for row in self._load()]

    def get(self, server_id: str) -> Optional[Dict[str, Any]]:
        return next((row for row in self._load() if row["id"] == server_id), None)

    def remove_registration(self, server_id: str) -> bool:
        """Remove only the local server registry entry; do not connect to the remote host."""
        rows = self._load()
        remaining = [row for row in rows if row.get("id") != server_id]
        if len(remaining) == len(rows):
            return False
        self._save(remaining)
        return True

    def update_name(self, server_id: str, name: str) -> Optional[Dict[str, Any]]:
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ValueError("Server name cannot be empty.")
        rows = self._load()
        row = next((item for item in rows if item.get("id") == server_id), None)
        if not row:
            return None
        row["name"] = cleaned_name
        self._save(rows)
        return self.public(row)

    def update_paths(self, server_id: str, database_root: str, backup_root: str) -> Optional[Dict[str, Any]]:
        """Update configured remote roots after read-only SSH validation."""
        database_root = _safe_root(database_root)
        backup_root = _safe_root(backup_root)
        rows = self._load()
        row = next((item for item in rows if item.get("id") == server_id), None)
        if not row:
            return None
        client = self.connect(row)
        try:
            sftp = client.open_sftp()
            try:
                self._check_remote_root(sftp, database_root, create=False)
                self._check_remote_root(sftp, backup_root, create=False)
            finally:
                sftp.close()
        finally:
            client.close()
        row["database_root"] = database_root
        row["backup_root"] = backup_root
        self._save(rows)
        return self.public(row)

    def health(self, server_id: str) -> Dict[str, Any]:
        """Collect host and DBOps-container health using read-only SSH commands."""
        row = self.get(server_id)
        if not row:
            raise FileNotFoundError("Server was not found.")
        result: Dict[str, Any] = {
            "server_id": server_id,
            "status": "unreachable",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "ssh_connected": False,
            "load_average": [],
        }
        client = None
        try:
            client = self.connect(row)
            result["ssh_connected"] = True
            try:
                result["docker_version"] = self._exec(client, ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
            except Exception as exc:
                result["error"] = f"Docker is unavailable: {exc}"
            try:
                uptime = self._exec(client, ["cat", "/proc/uptime"]).split()
                result["uptime_seconds"] = float(uptime[0])
                result["load_average"] = [float(value) for value in self._exec(client, ["cat", "/proc/loadavg"]).split()[:3]]
                meminfo = self._exec(client, ["cat", "/proc/meminfo"])
                memory = {}
                for line in meminfo.splitlines():
                    key, _, value = line.partition(":")
                    if key in ("MemTotal", "MemAvailable"):
                        memory[key] = int(value.strip().split()[0]) * 1024
                result["memory_total_bytes"] = memory.get("MemTotal")
                result["memory_available_bytes"] = memory.get("MemAvailable")
                disk_path = row["database_root"]
                try:
                    self._exec(client, ["test", "-d", disk_path])
                except RuntimeError:
                    disk_path = posixpath.dirname(disk_path) or "/"
                disk = self._exec(client, ["df", "-Pk", disk_path]).splitlines()
                fields = disk[-1].split()
                if len(fields) >= 6:
                    result["disk_total_bytes"] = int(fields[1]) * 1024
                    result["disk_used_bytes"] = int(fields[2]) * 1024
                    result["disk_available_bytes"] = int(fields[3]) * 1024
                    result["disk_used_percent"] = int(fields[4].rstrip("%"))
            except Exception as exc:
                result["error"] = (result.get("error", "") + f" Host metrics unavailable: {exc}").strip()
            if result.get("docker_version"):
                try:
                    statuses = self._exec(client, ["docker", "ps", "-a", "--filter", "label=com.dbops.managed=true", "--format", "{{.Status}}"])
                    status_lines = statuses.splitlines() if statuses else []
                    result["managed_containers_running"] = sum(line.startswith("Up ") for line in status_lines)
                    result["managed_containers_stopped"] = len(status_lines) - result["managed_containers_running"]
                except Exception as exc:
                    result["error"] = (result.get("error", "") + f" Container health unavailable: {exc}").strip()
            degraded = (
                not result.get("docker_version")
                or (result.get("disk_used_percent") is not None and result["disk_used_percent"] >= 90)
                or (result.get("managed_containers_stopped") or 0) > 0
                or bool(result.get("error"))
            )
            result["status"] = "degraded" if degraded else "healthy"
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            if client:
                client.close()
        return result

    def connect(self, row: Dict[str, Any]) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        known_hosts = Path(settings.SSH_KNOWN_HOSTS_PATH).expanduser()
        if not known_hosts.is_file():
            raise RuntimeError(f"SSH known_hosts file not found: {known_hosts}. Add the host through trusted terminal SSH first, or configure SSH_KNOWN_HOSTS_PATH.")
        try:
            client.load_host_keys(str(known_hosts))
        except (OSError, paramiko.SSHException) as exc:
            raise RuntimeError(f"Could not read SSH known_hosts file: {known_hosts}") from exc
        # Reject unknown or changed host keys before authenticating with the password.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        password = self._cipher().decrypt(row["password_enc"].encode()).decode()
        try:
            client.connect(
                hostname=row["host"], port=int(row["ssh_port"]), username=row["username"], password=password,
                timeout=10, banner_timeout=10, auth_timeout=10, allow_agent=False, look_for_keys=False,
            )
            return client
        except Exception:
            client.close()
            raise

    @staticmethod
    def _exec(client: paramiko.SSHClient, args: List[str], timeout: int = 60) -> str:
        command = shlex.join(args)
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        code = stdout.channel.recv_exit_status()
        output = stdout.read().decode(errors="replace").strip()
        error = stderr.read().decode(errors="replace").strip()
        if code:
            raise RuntimeError(error or output or f"Remote operation failed with status {code}.")
        return output

    @staticmethod
    def _exec_with_progress(client: paramiko.SSHClient, args: List[str], progress: Callable[[str], None], timeout: int = 600) -> str:
        """Run a remote command while forwarding its merged output as progress lines."""
        _stdin, stdout, _stderr = client.exec_command(shlex.join(args), timeout=timeout)
        channel = stdout.channel
        channel.set_combine_stderr(True)
        started = time.monotonic()
        pending = ""
        output = []
        while True:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="replace")
                output.append(chunk)
                pending += chunk
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if line.strip():
                        progress(line.rstrip("\r"))
            elif channel.exit_status_ready():
                break
            elif time.monotonic() - started > timeout:
                channel.close()
                raise TimeoutError(f"Remote command timed out after {timeout} seconds.")
            else:
                time.sleep(0.1)
        if pending.strip():
            progress(pending.rstrip("\r"))
        status_code = channel.recv_exit_status()
        if status_code:
            raise RuntimeError("\n".join(output).strip() or f"Remote operation failed with status {status_code}.")
        return "".join(output).strip()

    def register(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            host_ip = ipaddress.ip_address(data["host"])
        except ValueError as exc:
            raise ValueError("Use the server IP address so new database ports can bind only to that interface.") from exc
        if host_ip.is_unspecified or host_ip.is_loopback:
            raise ValueError("Use a reachable server interface IP, not 0.0.0.0 or loopback.")
        data["host"] = str(host_ip)
        data["database_root"] = _safe_root(data["database_root"])
        data["backup_root"] = _safe_root(data["backup_root"])
        cipher = self._cipher()
        draft = {
            **{k: data[k] for k in ("name", "host", "ssh_port", "username", "database_root", "backup_root")},
            "id": "srv-" + uuid.uuid4().hex[:8],
            "password_enc": cipher.encrypt(data["password"].encode()).decode(),
            "status": "unknown",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        client = self.connect(draft)
        try:
            # Read-only preflight: inspect configured roots and Docker; no paths are created.
            sftp = client.open_sftp()
            try:
                self._check_remote_root(sftp, draft["database_root"], create=False)
                self._check_remote_root(sftp, draft["backup_root"], create=False)
            finally:
                sftp.close()
            self._exec(client, ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        finally:
            client.close()
        draft["status"] = "online"
        rows = self._load()
        rows.append(draft)
        self._save(rows)
        return self.public(draft)

    def create_database_container(self, server_id: str, *, name: str, container_name: str, database_name: str, database_user: str, database_password: str, host_port: int, image: Optional[str], progress: Optional[Callable[[str], None]] = None, engine: str = "postgres") -> Dict[str, Any]:
        """Provision an isolated database container with fresh data and a protected password file."""
        configs = {
            "postgres": (image or "postgres:16-alpine", "/var/lib/postgresql/data", "postgres_password", 5432),
            "mongodb": (image or "mongo:7", "/data/db", "mongo_password", 27017),
            "mysql": (image or "mysql:8", "/var/lib/mysql", "mysql_password", 3306),
            "redis": (image or "redis:7-alpine", "/data", "redis_password", 6379),
        }
        patterns = {
            "postgres": r"postgres:(1[4-9]|[2-9][0-9])(-alpine)?",
            "mongodb": r"mongo:(6|7|8)(\.\d+)?(-[a-z0-9.-]+)?",
            "mysql": r"(mysql:8(\.\d+)?|mariadb:10\.\d+|mariadb:11\.\d+)(-[a-z0-9.-]+)?",
            "redis": r"redis:([7-9]|[1-9][0-9])(\.\d+)?(-[a-z0-9.-]+)?",
        }
        import re
        if engine not in configs:
            raise ValueError("Choose PostgreSQL, MongoDB, MySQL/MariaDB, or Redis.")
        image, target_dir, secret_name, internal_port = configs[engine]
        if not re.fullmatch(patterns[engine], image):
            raise ValueError(f"Unsupported {engine} image tag: {image}")
        row = self.get(server_id)
        if not row:
            raise FileNotFoundError("Server was not found.")
        client = self.connect(row)
        data_dir = posixpath.join(row["database_root"], container_name)
        secret_file = posixpath.join(row["database_root"], ".secrets", container_name, secret_name)
        bind_host = f"[{row['host']}]" if ":" in row["host"] else row["host"]
        report = progress or (lambda _message: None)
        try:
            report(f"Connecting to {row['host']} over SSH")
            try:
                self._exec(client, ["docker", "image", "inspect", image], timeout=20)
                report(f"Image {image} is already available locally")
            except RuntimeError as exc:
                detail = str(exc).lower()
                if not any(marker in detail for marker in ("no such image", "not found", "manifest unknown")):
                    raise RuntimeError(f"Could not check image '{image}' on the remote server: {exc}") from exc
                report(f"Image {image} is missing; pulling from Docker Hub")
                self._exec_with_progress(client, ["docker", "pull", image], report, timeout=600)
            report("Preparing an isolated data directory and protected password file")
            sftp = client.open_sftp()
            try:
                base_data = data_dir
                for _ in range(100):
                    try: sftp.lstat(data_dir); data_exists = True
                    except OSError: data_exists = False
                    try: sftp.lstat(secret_file); secret_exists = True
                    except OSError: secret_exists = False
                    if not data_exists and not secret_exists: break
                    suffix = uuid.uuid4().hex[:8]
                    data_dir = f"{base_data}-{suffix}"
                    secret_file = posixpath.join(row["database_root"], ".secrets", f"{container_name}-{suffix}", secret_name)
                else:
                    raise RuntimeError("Could not allocate a unique remote data path after 100 attempts.")
                self._create_new_directory(sftp, data_dir, row["database_root"])
                self._mkdirs(sftp, posixpath.dirname(secret_file), row["database_root"])
                with sftp.open(secret_file, "wx") as password_file:
                    password_file.write(database_password.encode("utf-8"))
                sftp.chmod(secret_file, 0o600)
            finally:
                sftp.close()
            args = ["docker", "run", "-d", "--name", container_name,
                "--label", "com.dbops.managed=true", "--label", f"com.dbops.name={name}",
                "--label", f"com.dbops.engine={engine}", "--restart", "unless-stopped",
                "--mount", f"type=bind,source={data_dir},target={target_dir}",
                "--mount", f"type=bind,source={secret_file},target=/run/secrets/dbops_password,readonly",
                "--publish", f"{bind_host}:{host_port}:{internal_port}"]
            if engine == "postgres":
                args += ["--env", "POSTGRES_DB="+database_name, "--env", "POSTGRES_USER="+database_user, "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/dbops_password"]
                ready = ["pg_isready", "-U", database_user, "-d", database_name]
            elif engine == "mongodb":
                args += ["--env", "MONGO_INITDB_ROOT_USERNAME="+database_user, "--env", "MONGO_INITDB_ROOT_PASSWORD_FILE=/run/secrets/dbops_password", "--env", "MONGO_INITDB_DATABASE="+database_name]
                ready = ["mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"]
            elif engine == "mysql":
                args += ["--env", "MYSQL_DATABASE="+database_name, "--env", "MYSQL_USER="+database_user, "--env", "MYSQL_PASSWORD_FILE=/run/secrets/dbops_password", "--env", "MYSQL_ROOT_PASSWORD_FILE=/run/secrets/dbops_password"]
                ready = ["mysqladmin", "ping", "-h", "127.0.0.1"]
            else:
                args += ["--entrypoint", "sh", image, "-c", "exec redis-server --appendonly yes --requirepass \"$(cat /run/secrets/dbops_password)\""]
                ready = ["sh", "-c", "REDISCLI_AUTH=\"$(cat /run/secrets/dbops_password)\" redis-cli ping"]
            if engine != "redis":
                args.append(image)
            report(f"Starting {engine} container {container_name}")
            self._exec_with_progress(client, args, report, timeout=600)
            report(f"Waiting for {engine} readiness")
            for _ in range(45):
                try:
                    self._exec(client, ["docker", "exec", container_name, *ready], timeout=8)
                    report(f"{engine} is ready")
                    return {"container_name": container_name, "data_directory": data_dir, "status": "running", "engine": engine}
                except RuntimeError:
                    time.sleep(2)
            raise RuntimeError(f"The new {engine} container was created but did not become ready. Its isolated files were left in place for inspection.")
        finally:
            client.close()

    def list_managed_containers(self, server_id: str) -> List[Dict[str, str]]:
        row = self.get(server_id)
        if not row:
            raise FileNotFoundError("Server was not found.")
        client = self.connect(row)
        try:
            output = self._exec(client, ["docker", "ps", "-a", "--filter", "label=com.dbops.managed=true", "--format", "{{.Names}}\t{{.Status}}\t{{.Label \"com.dbops.name\"}}"])
            containers = []
            for line in output.splitlines():
                parts = line.split("\t", 2)
                if parts and parts[0]:
                    containers.append({"container_name": parts[0], "status": parts[1] if len(parts) > 1 else "unknown", "display_name": parts[2] if len(parts) > 2 else ""})
            return containers
        finally:
            client.close()

    def delete_managed_container(self, server_id: str, container_name: str) -> bool:
        row = self.get(server_id)
        if not row:
            raise FileNotFoundError("Server was not found.")
        client = self.connect(row)
        try:
            try:
                managed = self._exec(client, ["docker", "inspect", "--format", "{{ index .Config.Labels \"com.dbops.managed\" }}", container_name]).strip()
            except RuntimeError:
                return False
            if managed != "true":
                raise ValueError("This container was not created by DBOps and cannot be removed here.")
            self._exec(client, ["docker", "rm", "--force", container_name])
            return True
        finally:
            client.close()

    def load_postgres_image_archive(self, server_id: str, archive, image: str) -> Dict[str, str]:
        """Transfer a Docker save archive to a registered host and load its PostgreSQL image."""
        row = self.get(server_id)
        if not row:
            raise FileNotFoundError("Server was not found.")
        client = self.connect(row)
        remote_dir = posixpath.join(row["database_root"], ".images")
        remote_archive = posixpath.join(remote_dir, "postgres-" + uuid.uuid4().hex + ".tar")
        try:
            sftp = client.open_sftp()
            try:
                self._mkdirs(sftp, remote_dir, row["database_root"])
                archive.seek(0)
                with sftp.open(remote_archive, "wx") as remote_file:
                    while block := archive.read(1024 * 1024):
                        remote_file.write(block)
            finally:
                sftp.close()
            self._exec(client, ["docker", "load", "--input", remote_archive], timeout=600)
            self._exec(client, ["docker", "image", "inspect", image], timeout=20)
            return {"server_id": server_id, "image": image, "status": "loaded"}
        except RuntimeError as exc:
            raise RuntimeError(f"Could not load PostgreSQL image archive on the remote server: {exc}") from exc
        finally:
            try:
                sftp = client.open_sftp()
                try:
                    sftp.remove(remote_archive)
                except OSError:
                    pass
                finally:
                    sftp.close()
            except Exception:
                pass
            client.close()

    def copy_snapshot(self, server_id: str, backup_id: str) -> Dict[str, Any]:
        from app.services.backup_service import backup_service
        row = self.get(server_id)
        backup = backup_service.get_backup(backup_id)
        if not row or not backup or backup.status != "completed":
            raise FileNotFoundError("Server or completed snapshot was not found.")
        source = Path(backup.file_path)
        if not source.is_file():
            raise FileNotFoundError("Local snapshot archive is unavailable.")
        expected = backup_service._calculate_sha256(source)
        if backup.checksum_sha256 and expected != backup.checksum_sha256:
            raise RuntimeError("Local snapshot checksum does not match its metadata.")
        remote_dir = posixpath.join(row["backup_root"], backup_id)
        remote_file = posixpath.join(remote_dir, backup.filename)
        temp_file = remote_file + ".partial-" + uuid.uuid4().hex[:8]
        client = self.connect(row)
        try:
            sftp = client.open_sftp()
            try:
                # Make only the dedicated snapshot directory; never overwrite a prior copy.
                self._mkdirs(sftp, remote_dir, row["backup_root"])
                try:
                    existing_info = sftp.lstat(remote_file)
                    if stat.S_ISLNK(existing_info.st_mode) or not stat.S_ISREG(existing_info.st_mode):
                        raise ValueError("Existing remote snapshot path is not a regular file.")
                    existing = sftp.open(remote_file, "rb")
                except OSError:
                    existing = None
                if existing:
                    digest = hashlib.sha256()
                    with existing:
                        for block in iter(lambda: existing.read(1024 * 1024), b""):
                            digest.update(block)
                    if digest.hexdigest() != expected:
                        raise FileExistsError("An archive with different contents already exists at the remote snapshot path.")
                else:
                    digest = hashlib.sha256()
                    with source.open("rb") as src, sftp.open(temp_file, "wb") as dst:
                        while block := src.read(1024 * 1024):
                            dst.write(block)
                            digest.update(block)
                    if digest.hexdigest() != expected:
                        raise RuntimeError("Transferred snapshot checksum verification failed.")
                    remote_digest = hashlib.sha256()
                    with sftp.open(temp_file, "rb") as uploaded:
                        for block in iter(lambda: uploaded.read(1024 * 1024), b""):
                            remote_digest.update(block)
                    if remote_digest.hexdigest() != expected:
                        raise RuntimeError("Remote snapshot read-back checksum verification failed; the copy was not recorded as available.")
                    sftp.rename(temp_file, remote_file)
            finally:
                sftp.close()
        finally:
            client.close()
        metadata = backup_service._load_metadata()
        for item in metadata:
            if item.get("backup_id") == backup_id:
                replicas = item.setdefault("remote_copies", [])
                if not any(copy.get("server_id") == server_id for copy in replicas):
                    replicas.append({"server_id": server_id, "path": remote_file, "checksum_sha256": expected})
        backup_service._save_metadata(metadata)
        return {"backup_id": backup_id, "server_id": server_id, "path": remote_file, "checksum_sha256": expected, "status": "verified"}

    def download_snapshot(self, server_id: str, remote_path: str, destination: Path, expected_sha256: str) -> Path:
        """Read a registered snapshot copy into a new local staging file; never write remotely."""
        row = self.get(server_id)
        if not row:
            raise FileNotFoundError("Remote snapshot server was not found.")
        normalized = posixpath.normpath(remote_path)
        if not normalized.startswith(row["backup_root"] + "/"):
            raise ValueError("Remote snapshot path is outside the registered snapshot directory.")
        client = self.connect(row)
        try:
            sftp = client.open_sftp()
            try:
                parts = normalized.split("/")
                current = ""
                for part in parts[:-1]:
                    if not part:
                        continue
                    current += "/" + part
                    item = sftp.lstat(current)
                    if stat.S_ISLNK(item.st_mode):
                        raise ValueError("Remote snapshot path cannot traverse symlinks.")
                item = sftp.lstat(normalized)
                if not stat.S_ISREG(item.st_mode):
                    raise ValueError("Remote snapshot path is not a regular file.")
                digest = hashlib.sha256()
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with sftp.open(normalized, "rb") as source, destination.open("xb") as target:
                        while block := source.read(1024 * 1024):
                            target.write(block)
                            digest.update(block)
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                if digest.hexdigest() != expected_sha256:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError("Remote snapshot checksum verification failed; restore was stopped.")
                return destination
            finally:
                sftp.close()
        finally:
            client.close()

    @staticmethod
    def _check_remote_root(sftp, root: str, *, create: bool) -> None:
        """Check every configured path component without following symlinks."""
        parts = [part for part in root.split("/") if part]
        current = ""
        for index, part in enumerate(parts):
            current += "/" + part
            try:
                item = sftp.lstat(current)
            except OSError as exc:
                if index == len(parts) - 1 and create:
                    sftp.mkdir(current, 0o700)
                    continue
                if index == len(parts) - 1:
                    return  # The configured root may be created later; its parent exists.
                raise RuntimeError(f"Parent directory for configured path '{root}' must already exist.") from exc
            if stat.S_ISLNK(item.st_mode):
                raise ValueError(f"Configured path '{root}' cannot traverse symlinks.")
            if not stat.S_ISDIR(item.st_mode):
                raise ValueError(f"Configured path component '{current}' is not a directory.")

    @classmethod
    def _mkdirs(cls, sftp, path: str, root: str):
        normalized_root = posixpath.normpath(root)
        normalized_path = posixpath.normpath(path)
        if normalized_path != normalized_root and not normalized_path.startswith(normalized_root + "/"):
            raise ValueError("Remote path is outside the configured directory.")
        cls._check_remote_root(sftp, normalized_root, create=True)
        current = normalized_root
        for part in [segment for segment in normalized_path[len(normalized_root):].split("/") if segment]:
            current = posixpath.join(current, part)
            try:
                item = sftp.lstat(current)
                if stat.S_ISLNK(item.st_mode):
                    raise ValueError("Configured remote paths must not traverse symlinks.")
                if not stat.S_ISDIR(item.st_mode):
                    raise ValueError("A remote path component exists and is not a directory.")
            except OSError:
                sftp.mkdir(current, 0o700)

    @classmethod
    def _create_new_directory(cls, sftp, path: str, root: str):
        normalized_root = posixpath.normpath(root)
        normalized_path = posixpath.normpath(path)
        if not normalized_path.startswith(normalized_root + "/"):
            raise ValueError("New remote directories must be below the configured directory.")
        cls._mkdirs(sftp, posixpath.dirname(normalized_path), normalized_root)
        try:
            sftp.lstat(normalized_path)
        except OSError:
            sftp.mkdir(normalized_path, 0o700)
            return
        raise FileExistsError("The generated remote directory already exists; no existing path was changed.")


server_service = ServerService()
