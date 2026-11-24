"""Sube un backup PostgreSQL a S3 y verifica la copia remota.

Uso:
    /usr/bin/python3 scripts/upload_s3_backup.py /ruta/turnobot-pg-YYYYMMDD-HHMMSS.dump

Autenticación exclusivamente por instance profile IAM (sin claves estáticas).
Bucket y región fijos para DR: us-east-2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

BUCKET = "turnobot-backups-528120377808-us-east-2-an"
REGION = "us-east-2"
MIN_BACKUP_BYTES = 1024
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
DUMP_NAME_RE = re.compile(r"^turnobot-pg-\d{8}-\d{6}\.dump$")
CHUNK_SIZE = 1024 * 1024
SHA256_HEX_LEN = 64

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_FILE_INVALID = 3
EXIT_PG_RESTORE = 4
EXIT_CHECKSUM = 5
EXIT_UPLOAD = 6
EXIT_REMOTE_VERIFY = 7

RETRYABLE_ERROR_CODES = {
    "SlowDown",
    "ServiceUnavailable",
    "InternalError",
    "RequestTimeout",
    "Throttling",
    "ThrottlingException",
    "RequestThrottled",
    "TooManyRequests",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_dump_path(path_str: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {path}")
    if path.is_symlink():
        raise ValueError(f"Se rechaza archivo simbólico: {path}")
    if not path.is_file():
        raise ValueError(f"No es un archivo regular: {path}")
    if not DUMP_NAME_RE.match(path.name):
        raise ValueError(
            f"Nombre de dump inválido (se espera turnobot-pg-YYYYMMDD-HHMMSS.dump): {path.name}"
        )
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"Backup vacío: {path}")
    if size < MIN_BACKUP_BYTES:
        raise ValueError(f"Backup demasiado pequeño ({size} bytes): {path}")
    return path


def run_pg_restore_list(path: Path) -> None:
    if shutil.which("pg_restore") is None:
        raise FileNotFoundError("pg_restore no está disponible en PATH")
    result = subprocess.run(
        ["pg_restore", "--list", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "pg_restore --list falló").strip()
        raise RuntimeError(f"pg_restore --list falló: {stderr[:500]}")


def write_sidecar_atomic(dump_path: Path, checksum: str) -> Path:
    sidecar = Path(f"{dump_path}.sha256")
    expected_line = f"{checksum}  {dump_path.name}\n"
    if sidecar.exists():
        existing = sidecar.read_text(encoding="utf-8", errors="replace").strip()
        existing_hash = existing.split()[0].lower() if existing else ""
        if existing_hash != checksum:
            raise ValueError(
                f"Sidecar existente con checksum distinto y no se sobrescribe: {sidecar}"
            )
        return sidecar
    tmp = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.tmp")
    tmp.write_text(expected_line, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, sidecar)
    return sidecar


def remote_key_for(dump_path: Path) -> str:
    stamp = dt.datetime.strptime(dump_path.stem.split("turnobot-pg-", 1)[1], "%Y%m%d-%H%M%S")
    return f"postgres/{stamp:%Y}/{stamp:%m}/{dump_path.name}"


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (EndpointConnectionError, ReadTimeoutError)):
        return True
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in RETRYABLE_ERROR_CODES:
            return True
        if status in (429, 500, 502, 503, 504):
            return True
        return False
    return False


def retry_call(label: str, func, *args, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except (ClientError, EndpointConnectionError, ReadTimeoutError) as exc:
            last_exc = exc
            if not is_retryable(exc) or attempt >= MAX_ATTEMPTS:
                print(
                    f"S3_ERROR {label} intento={attempt}: {type(exc).__name__}: {exc}", flush=True
                )
                raise
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            print(
                f"S3_RETRY {label} intento={attempt}/{MAX_ATTEMPTS} espera={delay:.1f}s "
                f"error={type(exc).__name__}",
                flush=True,
            )
            time.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"retry_call sin ejecución: {label}")


def s3_client():
    config = Config(
        region_name=REGION,
        retries={"max_attempts": 1, "mode": "standard"},
        connect_timeout=10,
        read_timeout=60,
    )
    return boto3.client("s3", config=config)


def upload_object(
    client, local: Path, key: str, metadata: dict[str, str] | None, content_type: str
) -> None:
    extra: dict = {"Metadata": metadata or {}, "ContentType": content_type}
    retry_call(
        f"put:{key}", client.put_object, Bucket=BUCKET, Key=key, Body=local.read_bytes(), **extra
    )


def head_object(client, key: str) -> dict:
    return retry_call("head", client.head_object, Bucket=BUCKET, Key=key)


def download_object(client, key: str, dest: Path) -> None:
    def _dl():
        client.download_file(BUCKET, key, str(dest))

    retry_call(f"get:{key}", _dl)


def verify_remote(
    client,
    dump_path: Path,
    dump_key: str,
    sidecar_path: Path,
    sidecar_key: str,
    local_sha: str,
    local_size: int,
) -> None:
    errors: list[str] = []

    dump_head = head_object(client, dump_key)
    remote_dump_size = int(dump_head["ContentLength"])
    if remote_dump_size != local_size:
        errors.append(f"ContentLength dump remoto={remote_dump_size} local={local_size}")

    side_head = head_object(client, sidecar_key)
    remote_side_size = int(side_head["ContentLength"])
    local_side_size = sidecar_path.stat().st_size
    if remote_side_size != local_side_size:
        errors.append(f"ContentLength sidecar remoto={remote_side_size} local={local_side_size}")

    meta_sha = (dump_head.get("Metadata") or {}).get("sha256", "").lower()
    if meta_sha != local_sha:
        errors.append(f"metadata sha256 remota no coincide: {meta_sha!r}")

    with tempfile.TemporaryDirectory(prefix="s3_backup_verify_") as tmp:
        tmp_dir = Path(tmp)
        downloaded_dump = tmp_dir / dump_path.name
        downloaded_side = tmp_dir / f"{dump_path.name}.sha256"
        download_object(client, dump_key, downloaded_dump)
        download_object(client, sidecar_key, downloaded_side)

        if downloaded_dump.stat().st_size != local_size:
            errors.append("tamaño de dump descargado distinto")

        remote_sha = sha256_file(downloaded_dump)
        if remote_sha != local_sha:
            errors.append(f"SHA-256 dump descargado {remote_sha} != local {local_sha}")

        side_text = downloaded_side.read_text(encoding="utf-8", errors="replace").strip()
        side_hash = side_text.split()[0].lower() if side_text else ""
        if side_hash != local_sha:
            errors.append(f"sidecar remoto hash {side_hash!r} != {local_sha}")

        try:
            run_pg_restore_list(downloaded_dump)
        except Exception as exc:
            errors.append(f"pg_restore --list remoto: {exc}")

    if errors:
        for err in errors:
            print(f"REMOTE_VERIFY_FAIL {err}", flush=True)
        raise RuntimeError("verificación remota fallida")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sube un dump PostgreSQL a S3 y lo verifica.")
    parser.add_argument("dump", help="Ruta al archivo turnobot-pg-YYYYMMDD-HHMMSS.dump")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        dump_path = validate_dump_path(args.dump)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return EXIT_FILE_INVALID

    try:
        run_pg_restore_list(dump_path)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return EXIT_PG_RESTORE

    local_sha = sha256_file(dump_path)
    local_size = dump_path.stat().st_size

    try:
        sidecar_path = write_sidecar_atomic(dump_path, local_sha)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return EXIT_CHECKSUM

    dump_key = remote_key_for(dump_path)
    sidecar_key = f"{dump_key}.sha256"
    metadata = {"sha256": local_sha, "source": "turnobot-backup"}

    client = s3_client()

    try:
        upload_object(client, dump_path, dump_key, metadata, "application/octet-stream")
        print(f"UPLOAD_OK dump={dump_key} bytes={local_size} sha256={local_sha}", flush=True)
        upload_object(client, sidecar_path, sidecar_key, {"sha256": local_sha}, "text/plain")
        print(f"UPLOAD_OK sidecar={sidecar_key}", flush=True)
    except Exception as exc:
        print(f"ERROR: subida fallida: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_UPLOAD

    try:
        verify_remote(client, dump_path, dump_key, sidecar_path, sidecar_key, local_sha, local_size)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return EXIT_REMOTE_VERIFY

    final_sha = sha256_file(dump_path)
    if final_sha != local_sha:
        print(
            f"ERROR: el archivo local cambió durante la subida: {final_sha} != {local_sha}",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_CHECKSUM

    print(f"LOCAL_SHA256_OK {final_sha}", flush=True)
    print("REMOTE_VERIFICATION=OK", flush=True)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("ERROR: interrumpido", file=sys.stderr)
        sys.exit(EXIT_UNEXPECTED)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(EXIT_UNEXPECTED)
