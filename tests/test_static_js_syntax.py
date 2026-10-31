"""Tests de regresión para archivos JavaScript estáticos.

Verifica que no existan literales de cadena adyacentes sin operador de
concatenación (+), lo cual causa SyntaxError en JavaScript (a diferencia
de Python o C, JS no concatena implícitamente strings adyacentes).
"""

import re
import unittest
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Patrón: una línea termina con comilla doble de cierre (") y la siguiente
# comienza con comilla doble de apertura ("), sin que haya operador de
# concatenación (+) ni coma al final de la primera línea.
_ADJACENT_STRING_RE = re.compile(r'"\s*$')


def _find_adjacent_string_literals(js_path: Path):
    """Devuelve lista de (num_linea, linea, siguiente_linea) con strings adyacentes."""
    issues = []
    lines = js_path.read_text(encoding="utf-8").splitlines()
    for i in range(len(lines) - 1):
        current = lines[i].rstrip()
        nxt = lines[i + 1].lstrip()
        if (
            _ADJACENT_STRING_RE.search(current)
            and nxt.startswith('"')
            and not current.rstrip().endswith('",')
            and not current.rstrip().endswith('");')
            and not current.rstrip().endswith('" +')
            and not nxt.startswith('+ "')
        ):
            issues.append((i + 1, current, nxt))
    return issues


@pytest.mark.no_pg
class TestStaticJSNoAdjacentStrings(unittest.TestCase):
    """Los archivos JS estáticos no deben tener literales de cadena adyacentes."""

    def test_no_adjacent_string_literals(self):
        """Ningún archivo .js en static/ tiene strings concatenadas implícitamente."""
        js_files = sorted(STATIC_DIR.glob("*.js"))
        self.assertGreater(len(js_files), 0, "No se encontraron archivos .js en static/")

        for js_file in js_files:
            with self.subTest(file=js_file.name):
                issues = _find_adjacent_string_literals(js_file)
                self.assertEqual(
                    issues,
                    [],
                    f"Literales de cadena adyacentes sin operador '+' en {js_file.name}: "
                    + "; ".join(f"línea {n}: {c!r} -> {s!r}" for n, c, s in issues),
                )


if __name__ == "__main__":
    unittest.main()
