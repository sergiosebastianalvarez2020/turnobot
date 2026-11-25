"""Regresión de los wrappers bash de subida S3 en systemd.

Bug original: con `set -euo pipefail`, ejecutar `flock` como comando suelto y
leer `$?` después no sirve, porque un 75 (lock ocupado) o cualquier fallo
aborta el bash antes de la conversión a 0. El wrapper correcto invoca `flock`
dentro de un `if`, que desactiva `errexit` solo para ese comando.

Estos tests extraen el `ExecStart` real de los `.service` y lo ejecutan con un
`flock` simulado para reproducir los tres escenarios: éxito, lock ocupado y
error real.
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

FLOCK_CMD = "/usr/bin/flock"
LOCK_PATH = "/run/lock/turnobot-s3-upload.lock"

# (nombre, modo del stub, comando interno que reemplaza la subida real)
CASES: list[tuple[str, str, str]] = [
    ("success", "run", ": > ${RAN}; exit 0"),
    ("busy", "busy", ": > ${RAN}; exit 0"),
    ("flock_error", "error", ": > ${RAN}; exit 0"),
    ("upload_error", "run", ": > ${RAN}; exit 7"),
]

# Código esperado del wrapper y si la subida llegó a ejecutarse.
EXPECTED: dict[str, tuple[int, bool]] = {
    "success": (0, True),
    "busy": (0, False),
    "flock_error": (2, False),
    "upload_error": (7, True),
}

STUB_FLOCK = """#!/usr/bin/env bash
set -u
mode="${STUB_FLOCK_MODE:-run}"
case "$mode" in
  busy) exit 75 ;;
  error) exit 2 ;;
esac
while [ "$#" -gt 0 ]; do
  case "$1" in
    -n) shift ;;
    -E) shift 2 ;;
    --) shift; break ;;
    -*) shift ;;
    *) break ;;
  esac
done
lock="$1"; shift
exec "$@"
"""


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
    pytest.skip("bash no disponible para ejecutar los wrappers")


def _exec_start(service: str) -> str:
    text = (SERVICE_DIR / service).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart=") :]
    raise AssertionError(f"{service} no define ExecStart")


def _wrapper_parts(exec_start: str) -> tuple[str, str, str]:
    # Solo hay dos comillas dobles sin escapar: abren y cierran el payload de
    # `/bin/bash -c "..."`. Las comillas internas van escapadas (`\"`).
    parts = re.split(r'(?<!\\)"', exec_start)
    if len(parts) != 3:
        raise AssertionError(f"estructura de wrapper inesperada: {exec_start}")
    prefix = parts[0].split("'", 1)[1]
    suffix = parts[2].rsplit("'", 1)[0]
    return prefix, parts[1], suffix


def _script_body(exec_start: str, inner: str) -> str:
    prefix, _original_inner, suffix = _wrapper_parts(exec_start)
    prefix = prefix.replace(FLOCK_CMD, "${STUB_FLOCK}")
    body = prefix + '"' + inner + '"' + suffix
    return body.replace(LOCK_PATH, "${LOCK_PATH}")


def _build_driver(exec_start: str) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        'WORK="$(mktemp -d)"',
        "trap 'rm -rf \"$WORK\"' EXIT",
        'STUB_FLOCK="$WORK/flock"',
        'LOCK_PATH="$WORK/upload.lock"',
        "export STUB_FLOCK LOCK_PATH",
        "cat > \"$STUB_FLOCK\" <<'__STUB_FLOCK__'",
        STUB_FLOCK,
        "__STUB_FLOCK__",
        'chmod +x "$STUB_FLOCK"',
        "",
        "run_case() {",
        '  name="$1"; mode="$2"',
        '  export STUB_FLOCK_MODE="$mode"',
        '  export RAN="$WORK/ran.$name"',
        '  bash "$WORK/body.$name.sh" >"$WORK/out.$name" 2>&1',
        "  code=$?",
        '  if [ -f "$RAN" ]; then ran=yes; else ran=no; fi',
        '  echo "CASE=$name EXIT=$code RAN=$ran"',
        "}",
        "",
    ]
    for name, _mode, inner in CASES:
        body = _script_body(exec_start, inner)
        lines += [f"cat > \"$WORK/body.{name}.sh\" <<'__BODY_{name}__'", body, f"__BODY_{name}__"]
    lines.append("")
    for name, mode, _inner in CASES:
        lines.append(f"run_case {name} {mode}")
    return "\n".join(lines) + "\n"


def _run_wrapper_cases(exec_start: str) -> dict[str, tuple[int, bool]]:
    bash = _find_bash()
    proc = subprocess.run(
        [bash, "-s"],
        input=_build_driver(exec_start),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise AssertionError(f"driver bash falló:\nstdout={proc.stdout}\nstderr={proc.stderr}")
    results: dict[str, tuple[int, bool]] = {}
    for line in proc.stdout.splitlines():
        match = re.fullmatch(r"CASE=(\S+) EXIT=(-?\d+) RAN=(yes|no)", line.strip())
        if match:
            results[match.group(1)] = (int(match.group(2)), match.group(3) == "yes")
    return results


@pytest.mark.parametrize("service", SERVICES)
def test_wrapper_uses_if_for_flock(service: str) -> None:
    exec_start = _exec_start(service)
    assert "set -euo pipefail" in exec_start
    assert f"if {FLOCK_CMD}" in exec_start, "flock debe ejecutarse dentro de un if"
    assert "-n -E 75" in exec_start
    assert LOCK_PATH in exec_start
    assert "flock_exit=$?" in exec_start


@pytest.mark.parametrize("service", SERVICES)
def test_exec_start_syntax_ok(service: str) -> None:
    bash = _find_bash()
    exec_start = _exec_start(service)
    # Script que systemd pasa a `/bin/bash -c` (contenido entre las comillas
    # simples externas), incluyendo el payload interno entre comillas dobles.
    script = exec_start[exec_start.index("'") + 1 : exec_start.rindex("'")]
    for snippet in (exec_start, script):
        proc = subprocess.run(
            [bash, "-n", "-c", snippet], capture_output=True, text=True, encoding="utf-8"
        )
        assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("service", SERVICES)
def test_wrapper_exit_codes(service: str) -> None:
    results = _run_wrapper_cases(_exec_start(service))
    assert results == EXPECTED
