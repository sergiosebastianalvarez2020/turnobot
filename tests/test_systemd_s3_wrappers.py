"""Regresión de los ExecStart de subida S3 en systemd.

El lock exclusivo ya no se adquiere en un wrapper `flock` de systemd sino
dentro de `upload_s3_backup.py` (fcntl.flock), para que funcione también en
ejecución directa y para evitar una doble adquisición del mismo lock.
Estos tests comprueban que los ExecStart no envuelven con flock, que la
sintaxis bash es válida y que el código 75 (lock ocupado) se omite sin fallo.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd-aws-proposal"
SERVICES = ("turnobot-backup-s3.service", "turnobot-backup-s3-recovery.service")


def _find_bash() -> str:
    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates += [
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        ]
    else:
        candidates += [Path("/bin/bash"), Path("/usr/bin/bash")]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("bash")
    if found:
        return found
    pytest.skip("bash no disponible para validar los ExecStart")


def _exec_start(service: str) -> str:
    text = (SERVICE_DIR / service).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart=") :]
    raise AssertionError(f"{service} no define ExecStart")


@pytest.mark.parametrize("service", SERVICES)
def test_exec_start_has_no_outer_flock(service: str) -> None:
    exec_start = _exec_start(service)
    assert "set -euo pipefail" in exec_start
    assert "/usr/bin/flock" not in exec_start, "no debe haber un segundo flock fuera de Python"
    assert "flock_exit=$?" not in exec_start
    assert "scripts/upload_s3_backup.py" in exec_start


@pytest.mark.parametrize("service", SERVICES)
def test_exec_start_syntax_ok(service: str) -> None:
    bash = _find_bash()
    exec_start = _exec_start(service)
    script = exec_start[exec_start.index("'") + 1 : exec_start.rindex("'")]
    for snippet in (exec_start, script):
        proc = subprocess.run(
            [bash, "-n", "-c", snippet], capture_output=True, text=True, encoding="utf-8"
        )
        assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("service", SERVICES)
def test_marker_check_python_is_valid(service: str) -> None:
    exec_start = _exec_start(service)
    match = re.search(r'-c "(import sys.*?)" "\$marker"', exec_start)
    assert match is not None, "falta la comprobación validate_marker_file"
    snippet = match.group(1).replace(r"\\\"", '"')
    compile(snippet, "<ExecStart>", "exec")


@pytest.mark.parametrize("service", SERVICES)
def test_busy_exit_code_is_skipped_as_success(service: str) -> None:
    exec_start = _exec_start(service)
    assert '[ "$rc" -eq 75 ]' in exec_start, "el código 75 debe omitirse sin marcar fallo"


def _run_exec_start_with_stub(
    service: str, tmp_path: Path, upload_exit: int
) -> subprocess.CompletedProcess[str]:
    """Ejecuta el ExecStart real con un stub que simula el código de subida."""
    bash = _find_bash()
    work = str(tmp_path).replace("\\", "/")
    dumps = tmp_path / "backups_pg"
    dumps.mkdir(exist_ok=True)
    (dumps / "turnobot-pg-20261124-120000.dump").write_bytes(b"x" * 2000)
    stub = tmp_path / "stub_upload.py"
    stub.write_text(f"import sys\nsys.exit({upload_exit})\n", encoding="utf-8")

    exec_start = _exec_start(service)
    script = exec_start[exec_start.index("'") + 1 : exec_start.rindex("'")]
    script = script.replace("dir=/home/ubuntu/turnobot/backups_pg", f"dir={work}/backups_pg")
    script = script.replace(
        "state=/home/ubuntu/turnobot/state/s3_backup_uploads", f"state={work}/state"
    )
    script = script.replace("py=/home/ubuntu/turnobot/.venv312/bin/python", "py=python")
    script = script.replace(
        "/home/ubuntu/turnobot/scripts/upload_s3_backup.py", f"{work}/stub_upload.py"
    )
    return subprocess.run([bash, "-c", script], capture_output=True, text=True, encoding="utf-8")


@pytest.mark.parametrize("service", SERVICES)
def test_exec_start_skips_busy_without_failure(service: str, tmp_path: Path) -> None:
    proc = _run_exec_start_with_stub(service, tmp_path, upload_exit=75)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "SKIPPED_BUSY" in proc.stdout


@pytest.mark.parametrize("service", SERVICES)
def test_exec_start_propagates_real_upload_error(service: str, tmp_path: Path) -> None:
    proc = _run_exec_start_with_stub(service, tmp_path, upload_exit=7)
    assert proc.returncode != 0, proc.stdout
    assert "S3_ERROR" in proc.stderr
