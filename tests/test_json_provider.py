"""Tests para PgAwareJSONProvider (JSON provider para PostgreSQL)."""

import datetime
import json
import unittest

from flask import Flask

from application.json_provider import PgAwareJSONProvider


class TestPgAwareJSONProvider(unittest.TestCase):
    """Tests unitarios del proveedor JSON que soporta datetime.date/time."""

    def setUp(self):
        self.app = Flask(__name__)
        self.provider = PgAwareJSONProvider(self.app)

    def test_datetime_date_serialization(self):
        """datetime.date se serializa a ISO format (YYYY-MM-DD)."""
        d = datetime.date(2026, 6, 15)
        result = self.provider.default(d)
        self.assertEqual(result, "2026-06-15")

    def test_datetime_time_serialization(self):
        """datetime.time se serializa a ISO format (HH:MM:SS)."""
        t = datetime.time(10, 30, 0)
        result = self.provider.default(t)
        self.assertEqual(result, "10:30:00")

    def test_datetime_time_with_microseconds(self):
        """datetime.time con microsegundos se serializa correctamente."""
        t = datetime.time(10, 30, 45, 123456)
        result = self.provider.default(t)
        self.assertEqual(result, "10:30:45.123456")

    def test_datetime_datetime_serialization(self):
        """datetime.datetime usa la serialización del padre (ISO con T)."""
        dt = datetime.datetime(2026, 6, 15, 10, 30, 0)
        result = self.provider.default(dt)
        self.assertEqual(result, "2026-06-15T10:30:00")

    def test_unknown_type_raises(self):
        """Tipos no soportados lanzan TypeError (comportamiento padre)."""

        class Unknown:
            pass

        with self.assertRaises(TypeError):
            self.provider.default(Unknown())

    def test_dumps_via_flask_json_provider(self):
        """json.dumps con el provider serializa date/time correctamente."""
        d = datetime.date(2026, 6, 15)
        t = datetime.time(10, 30, 0)
        data = {"date": d, "time": t}
        result = self.provider.dumps(data)
        parsed = json.loads(result)
        self.assertEqual(parsed["date"], "2026-06-15")
        self.assertEqual(parsed["time"], "10:30:00")

    def test_standard_types_through_dumps(self):
        """Tipos estándar (dict, list, str, int, float, bool, None) intactos via dumps."""
        test_cases = [
            ({"key": "value"}, {"key": "value"}),
            ([1, 2, 3], [1, 2, 3]),
            ("string", "string"),
            (42, 42),
            (3.14, 3.14),
            (True, True),
            (None, None),
        ]
        for input_val, expected in test_cases:
            with self.subTest(input_val=input_val):
                result = self.provider.dumps(input_val)
                parsed = json.loads(result)
                self.assertEqual(parsed, expected)


if __name__ == "__main__":
    unittest.main()
