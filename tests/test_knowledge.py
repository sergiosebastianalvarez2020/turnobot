import tempfile
import unittest
from pathlib import Path

import database.database as database
from services.knowledge import (
    create_knowledge_scoped,
    delete_knowledge_scoped,
    get_knowledge_by_id_scoped,
    get_knowledge_scoped,
    search_knowledge_scoped,
    update_knowledge_scoped,
)


class TestKnowledge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        # Crear usuario owner para tests
        self.owner_id = database.create_user_scoped(
            "owner@test.com", database.generate_password_hash("testpass"), active=True
        )
        # Asignar rol owner al negocio 1
        connection = database.get_connection()
        try:
            owner_role = connection.execute("SELECT id FROM roles WHERE name = 'owner'").fetchone()
            connection.execute(
                "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
                (self.owner_id, 1, owner_role["id"]),
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_knowledge_isolation(self):
        """Negocio A no puede ver conocimiento de B."""
        # Crear negocio 2 en la base de datos de test
        connection = database.get_connection()
        try:
            connection.execute(
                'INSERT INTO businesses (id, name, slug) VALUES (2, "Business 2", "business-2")'
            )
            owner_role = connection.execute("SELECT id FROM roles WHERE name = 'owner'").fetchone()
            connection.execute(
                "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
                (self.owner_id, 2, owner_role["id"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        create_knowledge_scoped(1, "faq", "Pregunta A", "Respuesta A", "tag1", self.owner_id)
        create_knowledge_scoped(2, "faq", "Pregunta B", "Respuesta B", "tag2", self.owner_id)

        knowledge_a = get_knowledge_scoped(1)
        knowledge_b = get_knowledge_scoped(2)

        self.assertEqual(len(knowledge_a), 1)
        self.assertEqual(knowledge_a[0]["question"], "Pregunta A")
        self.assertEqual(len(knowledge_b), 1)
        self.assertEqual(knowledge_b[0]["question"], "Pregunta B")

        # Búsqueda en A no encuentra conocimiento de B
        results = search_knowledge_scoped(1, "Pregunta B", limit=5)
        self.assertEqual(len(results), 0)

    def test_knowledge_crud(self):
        """Crear, editar, activar/desactivar, eliminar."""
        # Crear
        kid = create_knowledge_scoped(1, "faq", "Pregunta", "Respuesta", "tag1", self.owner_id)
        self.assertIsNotNone(kid)

        # Leer
        knowledge = get_knowledge_scoped(1)
        self.assertEqual(len(knowledge), 1)
        self.assertEqual(knowledge[0]["question"], "Pregunta")
        self.assertEqual(knowledge[0]["answer"], "Respuesta")
        self.assertEqual(knowledge[0]["tags"], "tag1")
        self.assertEqual(knowledge[0]["active"], 1)

        # Actualizar
        result = update_knowledge_scoped(kid, 1, "instruction", "Nueva", "Nueva resp", "tag2", 0)
        self.assertTrue(result)

        knowledge = get_knowledge_scoped(1, active_only=False)
        self.assertEqual(len(knowledge), 1)
        self.assertEqual(knowledge[0]["type"], "instruction")
        self.assertEqual(knowledge[0]["question"], "Nueva")
        self.assertEqual(knowledge[0]["answer"], "Nueva resp")
        self.assertEqual(knowledge[0]["tags"], "tag2")
        self.assertEqual(knowledge[0]["active"], 0)

        # Eliminar
        result = delete_knowledge_scoped(kid, 1)
        self.assertTrue(result)
        self.assertEqual(len(get_knowledge_scoped(1, active_only=False)), 0)

    def test_knowledge_permissions(self):
        """Owner puede administrar (testea funciones scoped directamente)."""
        kid = create_knowledge_scoped(1, "faq", "Test", "Resp", "", self.owner_id)
        self.assertIsNotNone(kid)

        # get_by_id
        entry = get_knowledge_by_id_scoped(kid, 1)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["question"], "Test")

        # Cross-tenant no debe encontrar
        entry = get_knowledge_by_id_scoped(kid, 2)
        self.assertIsNone(entry)

    def test_knowledge_in_prompt(self):
        """Conocimiento relevante aparece en search_knowledge_scoped."""
        create_knowledge_scoped(
            1, "faq", "Aceptan transferencia", "Si, al alias MINE.NEGOCIO", "pagos", self.owner_id
        )

        # Verificar que la búsqueda encuentra el conocimiento
        results = search_knowledge_scoped(1, "transferencia", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question"], "Aceptan transferencia")
        self.assertIn("MINE.NEGOCIO", results[0]["answer"])

    def test_knowledge_cross_tenant_prompt(self):
        """Conocimiento de B jamas aparece en búsqueda scoped de A."""
        # Crear negocio 2 en la base de datos de test
        connection = database.get_connection()
        try:
            connection.execute(
                'INSERT INTO businesses (id, name, slug) VALUES (2, "Business 2", "business-2")'
            )
            owner_role = connection.execute("SELECT id FROM roles WHERE name = 'owner'").fetchone()
            connection.execute(
                "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
                (self.owner_id, 2, owner_role["id"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        create_knowledge_scoped(1, "faq", "Pregunta A", "Respuesta A", "tag1", self.owner_id)
        create_knowledge_scoped(
            2, "faq", "Pregunta B", "Respuesta B SOLO PARA B", "tag2", self.owner_id
        )

        # Búsqueda scoped por business_id=1 no debe devolver conocimiento de business_id=2
        results = search_knowledge_scoped(1, "Pregunta B", limit=5)
        self.assertEqual(len(results), 0)

        results = search_knowledge_scoped(2, "Pregunta A", limit=5)
        self.assertEqual(len(results), 0)

        # Búsqueda en el negocio correcto sí encuentra
        results = search_knowledge_scoped(1, "Pregunta A", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question"], "Pregunta A")

        results = search_knowledge_scoped(2, "Pregunta B", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question"], "Pregunta B")

    def test_inactive_not_in_prompt(self):
        """active=0 no se utiliza en search_knowledge_scoped."""
        # Crear entrada activa
        create_knowledge_scoped(1, "faq", "Activa", "Esta deberia aparecer", "tag", self.owner_id)
        # Crear entrada inactiva
        kid = create_knowledge_scoped(
            1, "faq", "Inactiva", "Esta no deberia aparecer", "tag", self.owner_id
        )
        update_knowledge_scoped(kid, 1, "faq", "Inactiva", "Esta no deberia aparecer", "tag", 0)

        # Solo la activa debe aparecer en la búsqueda
        results = search_knowledge_scoped(1, "activa", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question"], "Activa")

        results = search_knowledge_scoped(1, "inactiva", limit=5)
        self.assertEqual(len(results), 0)

        # get_knowledge_scoped con active_only=True no debe devolver la inactiva
        knowledge = get_knowledge_scoped(1, active_only=True)
        self.assertEqual(len(knowledge), 1)
        self.assertEqual(knowledge[0]["question"], "Activa")

    def test_fts_search(self):
        """Busqueda por palabras clave devuelve la entrada correcta."""
        create_knowledge_scoped(
            1,
            "faq",
            "Aceptan transferencia bancaria",
            "Si, aceptamos transferencias",
            "pagos transferencia",
            self.owner_id,
        )
        create_knowledge_scoped(
            1,
            "faq",
            "Cuanto cuesta el corte",
            "El corte cuesta 5000",
            "precios corte",
            self.owner_id,
        )

        results = search_knowledge_scoped(1, "transferencia", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question"], "Aceptan transferencia bancaria")

        results = search_knowledge_scoped(1, "corte", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question"], "Cuanto cuesta el corte")

        results = search_knowledge_scoped(1, "pagos", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question"], "Aceptan transferencia bancaria")

    def test_no_knowledge_backward_compatibility(self):
        """Negocio sin KB - search_knowledge_scoped devuelve lista vacía."""
        results = search_knowledge_scoped(1, "cualquier cosa", limit=5)
        self.assertEqual(len(results), 0)

        knowledge = get_knowledge_scoped(1)
        self.assertEqual(len(knowledge), 0)


if __name__ == "__main__":
    unittest.main()
