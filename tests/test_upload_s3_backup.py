"""Tests unitarios para scripts/upload_s3_backup.py.

Verifica la lógica de validación, hash SHA-256, sidecar atómico, retries,
subida y verificación remota, aislando completamente las llamadas a AWS mediante mocks.
"""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.exceptions import ConnectionError as BotocoreConnectionError

from scripts.upload_s3_backup import (
    BUCKET,
    EXIT_FILE_INVALID,
    EXIT_OK,
    EXIT_PG_RESTORE,
    EXIT_STATE,
    EXIT_UPLOAD,
    UPLOAD_CONFIG,
    download_object,
    head_object,
    is_retryable,
    is_upload_done,
    is_valid_sha256_hex,
    mark_uploaded,
    marker_path_for,
    remote_key_for,
    retry_call,
    sha256_file,
    upload_object,
    validate_dump_path,
    validate_marker_file,
    verify_remote,
    write_sidecar_atomic,
)
from scripts.upload_s3_backup import main as s3_backup_main


@pytest.fixture
def sample_dump(tmp_path: Path) -> Path:
    """Crea un archivo dump simulado válido para pruebas."""
    dump_file = tmp_path / "turnobot-pg-20261124-120000.dump"
    # Escribir más de 1024 bytes para cumplir con MIN_BACKUP_BYTES
    content = b"PGDMP_TEST_CONTENT_" * 100
    dump_file.write_bytes(content)
    return dump_file


# ============================================================
# Pruebas de hash y validación de archivos locales
# ============================================================


def test_sha256_file(sample_dump: Path):
    """Calcula correctamente el hash SHA-256 del archivo."""
    expected = hashlib.sha256(sample_dump.read_bytes()).hexdigest()
    assert sha256_file(sample_dump) == expected


def test_validate_dump_path_valid(sample_dump: Path):
    """Acepta un archivo dump con formato y tamaño válidos."""
    path = validate_dump_path(str(sample_dump))
    assert path == sample_dump


def test_validate_dump_path_not_found(tmp_path: Path):
    """Rechaza un archivo inexistente."""
    with pytest.raises(FileNotFoundError):
        validate_dump_path(str(tmp_path / "turnobot-pg-20261124-000000.dump"))


def test_validate_dump_path_invalid_name(tmp_path: Path):
    """Rechaza nombres que no coincidan con turnobot-pg-YYYYMMDD-HHMMSS.dump."""
    bad_file = tmp_path / "otro-backup.dump"
    bad_file.write_bytes(b"x" * 2000)
    with pytest.raises(ValueError, match="Nombre de dump inválido"):
        validate_dump_path(str(bad_file))


def test_validate_dump_path_too_small(tmp_path: Path):
    """Rechaza archivos menores a 1024 bytes."""
    small_file = tmp_path / "turnobot-pg-20261124-120000.dump"
    small_file.write_bytes(b"pequeno")
    with pytest.raises(ValueError, match="demasiado pequeño"):
        validate_dump_path(str(small_file))


def test_validate_dump_path_empty(tmp_path: Path):
    """Rechaza archivos vacíos."""
    empty_file = tmp_path / "turnobot-pg-20261124-120000.dump"
    empty_file.write_bytes(b"")
    with pytest.raises(ValueError, match="demasiado pequeño|vacío"):
        validate_dump_path(str(empty_file))


def test_write_sidecar_atomic(sample_dump: Path):
    """Crea el archivo sidecar .sha256 de forma atómica y valida su contenido."""
    local_sha = sha256_file(sample_dump)
    sidecar = write_sidecar_atomic(sample_dump, local_sha)
    assert sidecar.exists()
    assert sidecar.read_text(encoding="utf-8").strip() == f"{local_sha}  {sample_dump.name}"

    # Si se invoca nuevamente con el mismo hash, debe ser idempotente
    sidecar2 = write_sidecar_atomic(sample_dump, local_sha)
    assert sidecar2 == sidecar

    # Si se intenta escribir con un hash distinto al existente, debe fallar
    with pytest.raises(ValueError, match="Sidecar existente con checksum distinto"):
        write_sidecar_atomic(
            sample_dump, "distinto_hash_0000000000000000000000000000000000000000000000000000"
        )


def test_upload_marker_tracks_pending_backups(sample_dump: Path, tmp_path: Path):
    """El marcador solo existe tras marcar y es idempotente."""
    state_dir = tmp_path / "state"
    assert is_upload_done(state_dir, sample_dump) is False

    valid_sha = sha256_file(sample_dump)
    expected_key = remote_key_for(sample_dump)
    marker = mark_uploaded(state_dir, sample_dump, valid_sha, expected_key)

    assert marker == state_dir / f"{sample_dump.name}.uploaded"
    assert is_upload_done(state_dir, sample_dump) is True
    assert "s3://" in marker.read_text(encoding="utf-8")

    mark_uploaded(state_dir, sample_dump, valid_sha, expected_key)
    assert is_upload_done(state_dir, sample_dump) is True


def test_is_upload_done_valid_marker(sample_dump: Path, tmp_path: Path):
    """Un marcador válido (sha256 + s3:// URI) retorna True."""
    state_dir = tmp_path / "state"
    valid_sha = sha256_file(sample_dump)
    expected_key = remote_key_for(sample_dump)
    marker = mark_uploaded(state_dir, sample_dump, valid_sha, expected_key)

    assert is_upload_done(state_dir, sample_dump) is True
    assert (
        marker.read_text(encoding="utf-8").strip() == f"{valid_sha}  s3://{BUCKET}/{expected_key}"
    )


def test_is_upload_done_missing_marker(sample_dump: Path, tmp_path: Path):
    """Sin marcador, retorna False."""
    state_dir = tmp_path / "state"
    assert is_upload_done(state_dir, sample_dump) is False


def test_is_upload_done_corrupted_marker_wrong_format(sample_dump: Path, tmp_path: Path):
    """Marcador con formato incorrecto (más/fewer partes) retorna False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    marker.write_text("solo_una_parte", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    marker.write_text("parte1 parte2 parte3", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False


def test_is_upload_done_corrupted_marker_invalid_sha_length(sample_dump: Path, tmp_path: Path):
    """Marcador con SHA-256 de longitud incorrecta retorna False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    marker.write_text("abc  s3://bucket/key", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    marker.write_text("a" * 63 + "  s3://bucket/key", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False


def test_is_upload_done_corrupted_marker_invalid_uri(sample_dump: Path, tmp_path: Path):
    """Marcador con URI no s3:// retorna False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    marker.write_text("a" * 64 + "  http://bucket/key", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    marker.write_text("a" * 64 + "  gs://bucket/key", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False


def test_mark_uploaded_validates_checksum_length(sample_dump: Path, tmp_path: Path):
    """mark_uploaded rechaza checksums con longitud != 64."""
    state_dir = tmp_path / "state"
    expected_key = remote_key_for(sample_dump)
    with pytest.raises(ValueError, match="hexadecimal"):
        mark_uploaded(state_dir, sample_dump, "corto", expected_key)

    with pytest.raises(ValueError, match="hexadecimal"):
        mark_uploaded(state_dir, sample_dump, "a" * 63, expected_key)

    with pytest.raises(ValueError, match="hexadecimal"):
        mark_uploaded(state_dir, sample_dump, "a" * 65, expected_key)

    # Longitud 64 y hex válido debe pasar (usar hash real del archivo)
    real_sha = sha256_file(sample_dump)
    marker = mark_uploaded(state_dir, sample_dump, real_sha, expected_key)
    assert marker.exists()
    assert is_upload_done(state_dir, sample_dump) is True


# ============================================================
# Pruebas de validación estricta de SHA-256 hexadecimal
# ============================================================


def test_is_valid_sha256_hex_valid():
    """SHA-256 válido de 64 caracteres hexadecimales."""
    # Minúsculas
    assert is_valid_sha256_hex("a" * 64)
    # Mayúsculas
    assert is_valid_sha256_hex("A" * 64)
    # Mixto (exactamente 64 chars)
    assert is_valid_sha256_hex("aBcDeF" * 10 + "1234")
    # Hash real de ejemplo (SHA-256 de cadena vacía)
    assert is_valid_sha256_hex("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


def test_is_valid_sha256_hex_invalid_chars():
    """Rechaza caracteres no hexadecimales."""
    # Caracteres no hex (g-z)
    assert not is_valid_sha256_hex("g" * 64)
    # Caracteres especiales
    assert not is_valid_sha256_hex("!" * 64)
    # Espacios
    assert not is_valid_sha256_hex(" " * 64)
    # Mix válido + inválido
    assert not is_valid_sha256_hex("a" * 63 + "g")
    assert not is_valid_sha256_hex("a" * 32 + "g" * 32)


def test_is_valid_sha256_hex_wrong_length():
    """Rechaza longitudes distintas de 64."""
    assert not is_valid_sha256_hex("")
    assert not is_valid_sha256_hex("a")
    assert not is_valid_sha256_hex("a" * 63)
    assert not is_valid_sha256_hex("a" * 65)
    assert not is_valid_sha256_hex("a" * 128)


def test_is_upload_done_rejects_non_hex_sha256(sample_dump: Path, tmp_path: Path):
    """is_upload_done rechaza SHA-256 con caracteres no hexadecimales."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"

    # SHA-256 con 'g' (no hex)
    marker.write_text("g" * 64 + f"  {expected_uri}", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    # SHA-256 con caracteres especiales
    marker.write_text("!" * 64 + f"  {expected_uri}", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False


def test_validate_marker_file_rejects_non_hex_sha256(sample_dump: Path, tmp_path: Path):
    """validate_marker_file rechaza SHA-256 con caracteres no hexadecimales."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"

    marker.write_text("g" * 64 + f"  {expected_uri}", encoding="utf-8")
    assert validate_marker_file(marker, sample_dump) is False


def test_mark_uploaded_rejects_non_hex_sha256(sample_dump: Path, tmp_path: Path):
    """mark_uploaded rechaza checksums con caracteres no hexadecimales."""
    state_dir = tmp_path / "state"
    expected_key = remote_key_for(sample_dump)
    with pytest.raises(ValueError, match="hexadecimal"):
        mark_uploaded(state_dir, sample_dump, "g" * 64, expected_key)

    with pytest.raises(ValueError, match="hexadecimal"):
        mark_uploaded(state_dir, sample_dump, "!" * 64, expected_key)

    # Válido debe pasar
    marker = mark_uploaded(state_dir, sample_dump, "a" * 64, expected_key)
    assert marker.exists()


# ============================================================
# Pruebas de validación estricta de URI S3 (bucket y clave)
# ============================================================


def test_is_upload_done_rejects_wrong_bucket(sample_dump: Path, tmp_path: Path):
    """is_upload_done rechaza URI con bucket incorrecto."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    expected_key = remote_key_for(sample_dump)
    valid_sha = "a" * 64

    # Bucket incorrecto
    marker.write_text(f"{valid_sha}  s3://otro-bucket/{expected_key}", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    # Bucket con nombre similar pero distinto
    marker.write_text(f"{valid_sha}  s3://turnobot-backups-otro/{expected_key}", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False


def test_is_upload_done_rejects_wrong_key(sample_dump: Path, tmp_path: Path):
    """is_upload_done rechaza URI con clave incorrecta."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    valid_sha = "a" * 64

    # Clave incorrecta (otro año/mes)
    marker.write_text(f"{valid_sha}  s3://{BUCKET}/postgres/2025/12/otro.dump", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    # Clave con nombre diferente
    marker.write_text(
        f"{valid_sha}  s3://{BUCKET}/postgres/2026/11/turnobot-pg-20261124-130000.dump",
        encoding="utf-8",
    )
    assert is_upload_done(state_dir, sample_dump) is False

    # Solo bucket sin clave
    marker.write_text(f"{valid_sha}  s3://{BUCKET}/", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False


def test_is_upload_done_rejects_malformed_uri(sample_dump: Path, tmp_path: Path):
    """is_upload_done rechaza URIs malformadas."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    valid_sha = "a" * 64

    # Sin esquema s3://
    marker.write_text(f"{valid_sha}  http://{BUCKET}/postgres/2026/11/dump.dump", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    # URI vacía
    marker.write_text(f"{valid_sha}  ", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False

    # Sin bucket
    marker.write_text(f"{valid_sha}  s3:///postgres/2026/11/dump.dump", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is False


def test_validate_marker_file_rejects_wrong_bucket_and_key(sample_dump: Path, tmp_path: Path):
    """validate_marker_file rechaza bucket y clave incorrectos."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    real_sha = sha256_file(sample_dump)
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"

    # Válido
    marker.write_text(f"{real_sha}  {expected_uri}", encoding="utf-8")
    assert validate_marker_file(marker, sample_dump) is True

    # Bucket incorrecto
    marker.write_text(f"{real_sha}  s3://otro-bucket/{expected_key}", encoding="utf-8")
    assert validate_marker_file(marker, sample_dump) is False

    # Clave incorrecta
    marker.write_text(f"{real_sha}  s3://{BUCKET}/postgres/2025/12/otro.dump", encoding="utf-8")
    assert validate_marker_file(marker, sample_dump) is False


def test_marker_from_another_backup_never_valid(sample_dump: Path, tmp_path: Path):
    """Un marcador de otro backup nunca debe considerarse válido."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Crear dos dumps diferentes
    dump1 = sample_dump
    dump2 = tmp_path / "turnobot-pg-20261124-130000.dump"
    dump2.write_bytes(b"PGDMP_TEST_CONTENT_" * 100)

    sha1 = sha256_file(dump1)
    sha2 = sha256_file(dump2)

    # Crear marcador SOLO para dump1
    key1 = remote_key_for(dump1)
    marker1 = marker_path_for(state_dir, dump1)
    marker1.write_text(f"{sha1}  s3://{BUCKET}/{key1}", encoding="utf-8")

    # NO crear marcador para dump2

    # El marcador de dump1 no debe validarse para dump2
    assert is_upload_done(state_dir, dump2) is False
    assert validate_marker_file(marker1, dump2) is False

    # El marcador de dump1 SÍ debe validarse para dump1
    assert is_upload_done(state_dir, dump1) is True
    assert validate_marker_file(marker1, dump1) is True

    # Ahora crear marcador para dump2 con clave diferente
    key2 = remote_key_for(dump2)
    marker2 = marker_path_for(state_dir, dump2)
    marker2.write_text(f"{sha2}  s3://{BUCKET}/{key2}", encoding="utf-8")

    # El marcador de dump2 no debe validarse para dump1
    assert validate_marker_file(marker2, dump1) is False
    # Pero sí para dump2
    assert is_upload_done(state_dir, dump2) is True
    assert validate_marker_file(marker2, dump2) is True


# ============================================================
# Pruebas de rechazo de enlaces simbólicos (symlinks)
# ============================================================


@pytest.mark.skipif(
    platform.system() == "Windows" and not os.environ.get("PYTEST_ALLOW_SYMLINKS"),
    reason="Windows no soporta symlinks sin privilegios elevados; establecer PYTEST_ALLOW_SYMLINKS=1 para forzar",
)
@pytest.mark.skipif(
    platform.system() == "Windows" and not os.environ.get("PYTEST_ALLOW_SYMLINKS"),
    reason="Windows no soporta symlinks sin privilegios elevados; establecer PYTEST_ALLOW_SYMLINKS=1 para forzar",
)
def test_is_upload_done_rejects_symlink_to_valid_marker(sample_dump: Path, tmp_path: Path):
    """is_upload_done rechaza symlink aunque apunte a marcador válido."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    expected_key = remote_key_for(sample_dump)
    valid_sha = sha256_file(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"

    # Crear marcador válido
    marker.write_text(f"{valid_sha}  {expected_uri}", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is True

    # Reemplazar el archivo marcador con un symlink al mismo archivo
    marker.unlink()
    try:
        marker.symlink_to(
            "/tmp/valid_marker_for_test"
        )  # symlink a ruta inexistente pero el test verifica que se rechaza por ser symlink
        # En realidad, para probar que rechaza symlink a archivo válido, creamos un archivo válido
        # y luego un symlink a él
        target = tmp_path / "valid_target_marker"
        target.write_text(f"{valid_sha}  {expected_uri}", encoding="utf-8")
        marker.unlink(missing_ok=True)
        marker.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("No se pueden crear symlinks en este entorno")

    assert is_upload_done(state_dir, sample_dump) is False


@pytest.mark.skipif(
    platform.system() == "Windows" and not os.environ.get("PYTEST_ALLOW_SYMLINKS"),
    reason="Windows no soporta symlinks sin privilegios elevados; establecer PYTEST_ALLOW_SYMLINKS=1 para forzar",
)
def test_is_upload_done_rejects_symlink_to_nonexistent(sample_dump: Path, tmp_path: Path):
    """is_upload_done rechaza symlink a archivo inexistente."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)

    try:
        marker.symlink_to("/ruta/inexistente/uploaded")
    except (OSError, NotImplementedError):
        pytest.skip("No se pueden crear symlinks en este entorno")

    assert is_upload_done(state_dir, sample_dump) is False


@pytest.mark.skipif(
    platform.system() == "Windows" and not os.environ.get("PYTEST_ALLOW_SYMLINKS"),
    reason="Windows no soporta symlinks sin privilegios elevados; establecer PYTEST_ALLOW_SYMLINKS=1 para forzar",
)
def test_is_upload_done_rejects_symlink_to_directory(sample_dump: Path, tmp_path: Path):
    """is_upload_done rechaza symlink a directorio."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)

    try:
        marker.symlink_to(state_dir)
    except (OSError, NotImplementedError):
        pytest.skip("No se pueden crear symlinks en este entorno")

    assert is_upload_done(state_dir, sample_dump) is False


@pytest.mark.skipif(
    platform.system() == "Windows" and not os.environ.get("PYTEST_ALLOW_SYMLINKS"),
    reason="Windows no soporta symlinks sin privilegios elevados; establecer PYTEST_ALLOW_SYMLINKS=1 para forzar",
)
def test_validate_marker_file_rejects_symlinks(sample_dump: Path, tmp_path: Path):
    """validate_marker_file rechaza symlinks."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    expected_key = remote_key_for(sample_dump)
    valid_sha = sha256_file(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"

    # Marcador válido
    marker.write_text(f"{valid_sha}  {expected_uri}", encoding="utf-8")
    assert validate_marker_file(marker, sample_dump) is True

    # Reemplazar con symlink
    marker.unlink()
    try:
        marker.symlink_to("/tmp/algo")
    except (OSError, NotImplementedError):
        pytest.skip("No se pueden crear symlinks en este entorno")

    assert validate_marker_file(marker, sample_dump) is False


# ============================================================
# Pruebas de escritura segura del marcador
# ============================================================


def test_mark_uploaded_rejects_wrong_key(sample_dump: Path, tmp_path: Path):
    """mark_uploaded rechaza claves que no coinciden con el dump."""
    state_dir = tmp_path / "state"
    with pytest.raises(ValueError, match="Clave no coincide"):
        mark_uploaded(state_dir, sample_dump, "a" * 64, "postgres/2025/12/otro.dump")

    with pytest.raises(ValueError, match="Clave no coincide"):
        mark_uploaded(
            state_dir, sample_dump, "a" * 64, "postgres/2026/11/turnobot-pg-20261124-130000.dump"
        )


def test_mark_uploaded_generates_correct_uri(sample_dump: Path, tmp_path: Path):
    """mark_uploaded genera URI correcta con bucket y clave esperados."""
    state_dir = tmp_path / "state"
    marker = mark_uploaded(state_dir, sample_dump, "a" * 64, remote_key_for(sample_dump))

    content = marker.read_text(encoding="utf-8").strip()
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"
    assert content == "a" * 64 + f"  {expected_uri}"


def test_marker_written_can_be_validated(sample_dump: Path, tmp_path: Path):
    """El marcador escrito puede ser validado posteriormente por ambas funciones."""
    state_dir = tmp_path / "state"
    real_sha = sha256_file(sample_dump)
    marker = mark_uploaded(state_dir, sample_dump, real_sha, remote_key_for(sample_dump))

    assert is_upload_done(state_dir, sample_dump) is True
    assert validate_marker_file(marker, sample_dump) is True


def test_mark_uploaded_preserves_atomic_write_and_permissions(sample_dump: Path, tmp_path: Path):
    """mark_uploaded conserva escritura atómica y permisos restrictivos."""
    state_dir = tmp_path / "state"
    marker = mark_uploaded(state_dir, sample_dump, "c" * 64, remote_key_for(sample_dump))

    # El archivo final existe y no hay temporales残留
    assert marker.exists()
    tmp_files = list(state_dir.glob(".*.tmp"))
    assert len(tmp_files) == 0

    # Permisos 0o600 (solo owner read/write) - solo en Unix
    if platform.system() != "Windows":
        stat_info = marker.stat()
        assert stat_info.st_mode & 0o777 == 0o600


# ============================================================
# Pruebas de regresión
# ============================================================


def test_valid_marker_still_prevents_reupload(sample_dump: Path, tmp_path: Path):
    """Un marcador válido sigue evitando una nueva subida."""
    state_dir = tmp_path / "state"
    real_sha = sha256_file(sample_dump)
    marker = mark_uploaded(state_dir, sample_dump, real_sha, remote_key_for(sample_dump))

    assert is_upload_done(state_dir, sample_dump) is True
    assert validate_marker_file(marker, sample_dump) is True

    # Simular que el servicio intenta procesar el mismo dump
    # is_upload_done debe retornar True y el dump debe omitirse
    assert is_upload_done(state_dir, sample_dump) is True


def test_is_upload_done_compares_sha256_with_file_content(sample_dump: Path, tmp_path: Path):
    """is_upload_done compara el SHA-256 del marcador con el hash real del archivo."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"

    # Calcular hash real del archivo
    real_sha = sha256_file(sample_dump)
    marker = marker_path_for(state_dir, sample_dump)

    # Marcador con hash REAL del archivo: debe ser válido
    marker.write_text(f"{real_sha}  {expected_uri}", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is True

    # Modificar el archivo
    sample_dump.write_bytes(b"CONTENIDO_MODIFICADO_DISTINTO" * 100)

    # El mismo marcador ya no debe ser válido (hash cambió)
    assert is_upload_done(state_dir, sample_dump) is False


def test_validate_marker_file_compares_sha256_with_file_content(sample_dump: Path, tmp_path: Path):
    """validate_marker_file compara el SHA-256 del marcador con el hash real del archivo."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"

    real_sha = sha256_file(sample_dump)
    marker = marker_path_for(state_dir, sample_dump)

    # Marcador con hash REAL: válido
    marker.write_text(f"{real_sha}  {expected_uri}", encoding="utf-8")
    assert validate_marker_file(marker, sample_dump) is True

    # Modificar el archivo
    sample_dump.write_bytes(b"OTRO_CONTENIDO_MODIFICADO" * 100)

    # El mismo marcador ya no debe ser válido
    assert validate_marker_file(marker, sample_dump) is False


def test_marker_with_wrong_sha256_rejected(sample_dump: Path, tmp_path: Path):
    """Marcador con SHA-256 incorrecto (no coincide con el archivo) es rechazado."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"
    marker = marker_path_for(state_dir, sample_dump)

    # SHA-256 válido en formato pero INCORRECTO para este archivo
    wrong_sha = "b" * 64
    marker.write_text(f"{wrong_sha}  {expected_uri}", encoding="utf-8")

    assert is_upload_done(state_dir, sample_dump) is False
    assert validate_marker_file(marker, sample_dump) is False


def test_modified_backup_with_same_name_must_be_reuploaded(sample_dump: Path, tmp_path: Path):
    """Backup modificado con el mismo nombre debe volver a subirse (no considerado hecho)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    expected_key = remote_key_for(sample_dump)
    expected_uri = f"s3://{BUCKET}/{expected_key}"
    marker = marker_path_for(state_dir, sample_dump)

    # Marcador válido para el archivo original
    real_sha = sha256_file(sample_dump)
    marker.write_text(f"{real_sha}  {expected_uri}", encoding="utf-8")
    assert is_upload_done(state_dir, sample_dump) is True

    # Simular que el archivo se modifica pero mantiene el mismo nombre
    sample_dump.write_bytes(b"BACKUP_MODIFICADO_MISMO_NOMBRE" * 200)

    # is_upload_done debe retornar False (pendiente de subida)
    assert is_upload_done(state_dir, sample_dump) is False
    assert validate_marker_file(marker, sample_dump) is False


def test_invalid_marker_does_not_skip_pending_backup(sample_dump: Path, tmp_path: Path):
    """Un marcador inválido no debe hacer que se omita un backup pendiente."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)

    # Marcador con SHA-256 inválido (no hex)
    marker.write_text("g" * 64 + f"  s3://{BUCKET}/{remote_key_for(sample_dump)}", encoding="utf-8")

    # Debe considerarse pendiente (no subido)
    assert is_upload_done(state_dir, sample_dump) is False
    assert validate_marker_file(marker, sample_dump) is False


def test_marker_from_other_bucket_never_validated_as_good(sample_dump: Path, tmp_path: Path):
    """Un marcador de otro bucket nunca debe considerarse válido."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    valid_sha = "e" * 64

    # Marcador con bucket incorrecto
    marker.write_text(
        f"{valid_sha}  s3://bucket-inexistente/{remote_key_for(sample_dump)}", encoding="utf-8"
    )

    assert is_upload_done(state_dir, sample_dump) is False
    assert validate_marker_file(marker, sample_dump) is False


def test_marker_from_other_key_never_validated_as_good(sample_dump: Path, tmp_path: Path):
    """Un marcador de otra clave nunca debe considerarse válido."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path_for(state_dir, sample_dump)
    valid_sha = "f" * 64

    # Marcador con clave incorrecta
    marker.write_text(f"{valid_sha}  s3://{BUCKET}/postgres/2025/01/otro.dump", encoding="utf-8")

    assert is_upload_done(state_dir, sample_dump) is False
    assert validate_marker_file(marker, sample_dump) is False


def test_remote_key_for(sample_dump: Path):
    """Genera la clave remota con estructura postgres/YYYY/MM/<name>."""
    key = remote_key_for(sample_dump)
    assert key == "postgres/2026/11/turnobot-pg-20261124-120000.dump"


# ============================================================
# Pruebas de reintentos y clasificación de errores
# ============================================================


def test_is_retryable():
    """Detecta adecuadamente errores reintentables y no reintentables."""
    assert is_retryable(EndpointConnectionError(endpoint_url="http://mock"))

    # Error 503 ServiceUnavailable
    err_503 = ClientError(
        {
            "Error": {"Code": "ServiceUnavailable", "Message": "Busy"},
            "ResponseMetadata": {"HTTPStatusCode": 503},
        },
        "PutObject",
    )
    assert is_retryable(err_503)

    # Error 403 AccessDenied (no reintentable)
    err_403 = ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": "Denied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )
    assert not is_retryable(err_403)


def test_retry_call_success_first_attempt():
    """Ejecuta y retorna de inmediato si la función es exitosa."""
    fn = MagicMock(return_value="OK")
    res = retry_call("test", fn, 1, a=2)
    assert res == "OK"
    fn.assert_called_once_with(1, a=2)


def test_retry_call_retries_on_transient_error():
    """Reintenta cuando ocurre un error transitorio y tiene éxito en el siguiente intento."""
    transient = EndpointConnectionError(endpoint_url="http://mock")
    fn = MagicMock(side_effect=[transient, "OK"])

    with patch("time.sleep") as mock_sleep:
        res = retry_call("test", fn)

    assert res == "OK"
    assert fn.call_count == 2
    mock_sleep.assert_called_once()


def test_is_retryable_connect_timeout():
    """ConnectTimeoutError se clasifica como reintentable."""
    assert is_retryable(ConnectTimeoutError(endpoint_url="http://mock"))


def test_is_retryable_connection_closed():
    """ConnectionClosedError se clasifica como reintentable."""
    assert is_retryable(ConnectionClosedError(endpoint_url="http://mock"))


def test_is_retryable_read_timeout():
    """ReadTimeoutError se clasifica como reintentable."""
    assert is_retryable(ReadTimeoutError(endpoint_url="http://mock"))


def test_is_retryable_botocore_connection_error():
    """BotocoreConnectionError se clasifica como reintentable."""
    assert is_retryable(BotocoreConnectionError(error="connection failed"))


def test_retry_call_retries_on_botocore_connection_error():
    """Reintenta ante BotocoreConnectionError y tiene éxito en el siguiente intento."""
    transient = BotocoreConnectionError(error="connection failed")
    fn = MagicMock(side_effect=[transient, "OK"])

    with patch("time.sleep") as mock_sleep:
        res = retry_call("test", fn)

    assert res == "OK"
    assert fn.call_count == 2
    mock_sleep.assert_called_once()


def test_retry_call_retries_on_connect_timeout():
    """Reintenta ante ConnectTimeoutError y tiene éxito en el siguiente intento."""
    transient = ConnectTimeoutError(endpoint_url="http://mock")
    fn = MagicMock(side_effect=[transient, "OK"])

    with patch("time.sleep") as mock_sleep:
        res = retry_call("test", fn)

    assert res == "OK"
    assert fn.call_count == 2
    mock_sleep.assert_called_once()


def test_retry_call_retries_on_connection_closed():
    """Reintenta ante ConnectionClosedError y tiene éxito en el siguiente intento."""
    transient = ConnectionClosedError(endpoint_url="http://mock")
    fn = MagicMock(side_effect=[transient, "OK"])

    with patch("time.sleep") as mock_sleep:
        res = retry_call("test", fn)

    assert res == "OK"
    assert fn.call_count == 2
    mock_sleep.assert_called_once()


def test_retry_call_respects_max_attempts():
    """Después de MAX_ATTEMPTS fallos, propaga la excepción sin más reintentos."""
    transient = EndpointConnectionError(endpoint_url="http://mock")
    fn = MagicMock(side_effect=[transient, transient, transient, transient])

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(EndpointConnectionError):
            retry_call("test", fn)

    # MAX_ATTEMPTS = 3, así que se intenta 3 veces
    assert fn.call_count == 3
    assert mock_sleep.call_count == 2  # sleep entre intentos 1-2 y 2-3


def test_retry_call_no_retry_on_permanent_error():
    """Errores no transitorios (ej. AccessDenied) no se reintentan."""
    permanent = ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": "Denied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )
    fn = MagicMock(side_effect=permanent)

    with pytest.raises(ClientError):
        retry_call("test", fn)

    fn.assert_called_once()


def test_retry_call_no_retry_on_403():
    """ClientError con HTTP 403 no se reintenta."""
    err_403 = ClientError(
        {
            "Error": {"Code": "Forbidden", "Message": "Forbidden"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )
    fn = MagicMock(side_effect=err_403)

    with pytest.raises(ClientError):
        retry_call("test", fn)

    fn.assert_called_once()


# ============================================================
# Pruebas de subida y verificación remota (con mocks)
# ============================================================


def test_upload_object(sample_dump: Path):
    """Usa upload_file con TransferConfig, sin cargar el dump completo en memoria."""
    client = MagicMock()
    with patch.object(Path, "read_bytes", side_effect=AssertionError("lectura completa")):
        upload_object(
            client, sample_dump, "dest/dump.dump", {"sha256": "1234"}, "application/octet-stream"
        )

    client.upload_file.assert_called_once_with(
        str(sample_dump),
        BUCKET,
        "dest/dump.dump",
        ExtraArgs={"Metadata": {"sha256": "1234"}, "ContentType": "application/octet-stream"},
        Config=UPLOAD_CONFIG,
    )
    client.put_object.assert_not_called()


def test_upload_transfer_config_uses_multipart(sample_dump: Path):
    """La configuración de transferencia habilita multipart limitado para dumps grandes."""
    assert UPLOAD_CONFIG.multipart_threshold == 64 * 1024 * 1024
    assert UPLOAD_CONFIG.multipart_chunksize == 16 * 1024 * 1024
    assert UPLOAD_CONFIG.max_request_concurrency == 4
    assert UPLOAD_CONFIG.use_threads is True


def test_head_and_download_object(tmp_path: Path):
    """head_object y download_object llaman a las APIs correspondientes de boto3."""
    client = MagicMock()
    client.head_object.return_value = {"ContentLength": 100}

    res = head_object(client, "test_key")
    assert res["ContentLength"] == 100
    client.head_object.assert_called_once_with(Bucket=BUCKET, Key="test_key")

    dest = tmp_path / "downloaded.tmp"
    download_object(client, "test_key", dest)
    client.download_file.assert_called_once_with(BUCKET, "test_key", str(dest))


def test_verify_remote_success(sample_dump: Path, tmp_path: Path):
    """Valida exitosamente cuando las cabeceras, hash y pg_restore coinciden."""
    local_sha = sha256_file(sample_dump)
    local_size = sample_dump.stat().st_size
    sidecar_path = write_sidecar_atomic(sample_dump, local_sha)

    client = MagicMock()
    client.head_object.side_effect = [
        {"ContentLength": local_size, "Metadata": {"sha256": local_sha}},  # dump
        {"ContentLength": sidecar_path.stat().st_size},  # sidecar
    ]

    def mock_download(bucket, key, dest):
        if key.endswith(".sha256"):
            Path(dest).write_text(sidecar_path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            Path(dest).write_bytes(sample_dump.read_bytes())

    client.download_file.side_effect = mock_download

    with patch("scripts.upload_s3_backup.run_pg_restore_list") as mock_restore:
        verify_remote(
            client,
            sample_dump,
            "remote/dump.dump",
            sidecar_path,
            "remote/dump.dump.sha256",
            local_sha,
            local_size,
        )
        mock_restore.assert_called_once()


def test_verify_remote_fails_on_hash_mismatch(sample_dump: Path):
    """Lanza excepción si el hash del archivo descargado difiere."""
    local_sha = sha256_file(sample_dump)
    local_size = sample_dump.stat().st_size
    sidecar_path = write_sidecar_atomic(sample_dump, local_sha)

    client = MagicMock()
    client.head_object.side_effect = [
        {"ContentLength": local_size, "Metadata": {"sha256": local_sha}},
        {"ContentLength": sidecar_path.stat().st_size},
    ]

    def mock_download(bucket, key, dest):
        Path(dest).write_bytes(b"CONTENIDO_CORRUPTO_DISTINTO" * 50)

    client.download_file.side_effect = mock_download

    with patch("scripts.upload_s3_backup.run_pg_restore_list"):
        with pytest.raises(RuntimeError, match="verificación remota fallida"):
            verify_remote(
                client,
                sample_dump,
                "remote/dump.dump",
                sidecar_path,
                "remote/dump.dump.sha256",
                local_sha,
                local_size,
            )


# ============================================================
# Pruebas de la función principal CLI main()
# ============================================================


def test_main_cli_flow_success(sample_dump: Path, tmp_path: Path):
    """Flujo completo exitoso de main(), con marcador local aislado."""
    state_dir = tmp_path / "state"
    with (
        patch("scripts.upload_s3_backup.run_pg_restore_list"),
        patch("scripts.upload_s3_backup.s3_client") as mock_s3_client_factory,
        patch("scripts.upload_s3_backup.verify_remote"),
    ):
        mock_client = MagicMock()
        mock_s3_client_factory.return_value = mock_client

        exit_code = s3_backup_main([str(sample_dump), "--state-dir", str(state_dir)])
        assert exit_code == EXIT_OK
        assert mock_client.upload_file.call_count == 2  # dump + sidecar
        assert is_upload_done(state_dir, sample_dump) is True


def test_main_cli_invalid_path():
    """Retorna EXIT_FILE_INVALID ante un archivo inexistente."""
    exit_code = s3_backup_main(["archivo_inexistente.dump"])
    assert exit_code == EXIT_FILE_INVALID


def test_main_cli_restore_fails(sample_dump: Path):
    """Retorna EXIT_PG_RESTORE si pg_restore --list falla."""
    with patch(
        "scripts.upload_s3_backup.run_pg_restore_list", side_effect=RuntimeError("Corrupted dump")
    ):
        exit_code = s3_backup_main([str(sample_dump)])
        assert exit_code == EXIT_PG_RESTORE


def test_main_cli_upload_fails(sample_dump: Path):
    """Retorna EXIT_UPLOAD si la subida a S3 falla."""
    with (
        patch("scripts.upload_s3_backup.run_pg_restore_list"),
        patch("scripts.upload_s3_backup.s3_client") as mock_s3_client_factory,
    ):
        mock_client = MagicMock()
        mock_client.upload_file.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Denied"}}, "PutObject"
        )
        mock_s3_client_factory.return_value = mock_client

        exit_code = s3_backup_main([str(sample_dump)])
        assert exit_code == EXIT_UPLOAD


def test_main_cli_fails_if_marker_write_fails(sample_dump: Path, tmp_path: Path):
    """No reporta éxito si la verificación remota pasó pero el marcador local falla."""
    state_dir = tmp_path / "state"
    with (
        patch("scripts.upload_s3_backup.run_pg_restore_list"),
        patch("scripts.upload_s3_backup.s3_client") as mock_s3_client_factory,
        patch("scripts.upload_s3_backup.verify_remote"),
        patch("scripts.upload_s3_backup.mark_uploaded", side_effect=PermissionError("no write")),
    ):
        mock_client = MagicMock()
        mock_s3_client_factory.return_value = mock_client

        exit_code = s3_backup_main([str(sample_dump), "--state-dir", str(state_dir)])
        assert exit_code == EXIT_STATE
