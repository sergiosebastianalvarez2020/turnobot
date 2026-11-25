"""Tests unitarios para scripts/upload_s3_backup.py.

Verifica la lógica de validación, hash SHA-256, sidecar atómico, retries,
subida y verificación remota, aislando completamente las llamadas a AWS mediante mocks.
"""

from __future__ import annotations

import hashlib
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
    mark_uploaded,
    marker_path_for,
    remote_key_for,
    retry_call,
    sha256_file,
    upload_object,
    validate_dump_path,
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

    valid_sha = "a" * 64
    marker = mark_uploaded(state_dir, sample_dump, valid_sha, "postgres/2026/11/dump.dump")

    assert marker == state_dir / f"{sample_dump.name}.uploaded"
    assert is_upload_done(state_dir, sample_dump) is True
    assert "s3://" in marker.read_text(encoding="utf-8")

    mark_uploaded(state_dir, sample_dump, valid_sha, "postgres/2026/11/dump.dump")
    assert is_upload_done(state_dir, sample_dump) is True


def test_is_upload_done_valid_marker(sample_dump: Path, tmp_path: Path):
    """Un marcador válido (sha256 + s3:// URI) retorna True."""
    state_dir = tmp_path / "state"
    valid_sha = "b" * 64
    marker = mark_uploaded(state_dir, sample_dump, valid_sha, "postgres/2026/11/dump.dump")

    assert is_upload_done(state_dir, sample_dump) is True
    assert (
        marker.read_text(encoding="utf-8").strip()
        == f"{valid_sha}  s3://{BUCKET}/postgres/2026/11/dump.dump"
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
    with pytest.raises(ValueError, match="Checksum inv"):
        mark_uploaded(state_dir, sample_dump, "corto", "postgres/2026/11/dump.dump")

    with pytest.raises(ValueError, match="Checksum inv"):
        mark_uploaded(state_dir, sample_dump, "a" * 63, "postgres/2026/11/dump.dump")

    with pytest.raises(ValueError, match="Checksum inv"):
        mark_uploaded(state_dir, sample_dump, "a" * 65, "postgres/2026/11/dump.dump")

    # Longitud 64 debe pasar
    marker = mark_uploaded(state_dir, sample_dump, "a" * 64, "postgres/2026/11/dump.dump")
    assert marker.exists()
    assert is_upload_done(state_dir, sample_dump) is True


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
