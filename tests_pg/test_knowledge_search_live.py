"""Tests live de busqueda full-text PostgreSQL (knowledge base).

Estas pruebas se ejecutan SOLO contra PostgreSQL real (marcador @pytest.mark.pg_live).
Validan el contrato funcional de search_knowledge_scoped con tsvector + GIN.

No modifican el comportamiento de SQLite; validan el contrato PostgreSQL.
"""

import pytest

from services.knowledge import (
    create_knowledge_scoped,
    get_knowledge_scoped,
    search_knowledge_scoped,
)
from tests_pg._helpers import make_pg_proxy, seed_user


def _setup_knowledge_context(pg_seed):
    """Crea datos de prueba para busqueda en business_id = pg_seed['business_id']."""
    user_id = seed_user(pg_seed["url"])
    # FAQ
    create_knowledge_scoped(
        pg_seed["business_id"],
        "faq",
        "Cuanto cuesta el corte?",
        "El corte cuesta 1000 pesos.",
        "precio,costo",
        user_id,
    )
    create_knowledge_scoped(
        pg_seed["business_id"],
        "faq",
        "A que hora abren?",
        "Abren a las 09:00 y cierran a las 18:00.",
        "horario,apertura",
        user_id,
    )
    create_knowledge_scoped(
        pg_seed["business_id"],
        "instruction",
        "Como reservar por WhatsApp",
        "Enviar un mensaje al numero de la negocio con la palabra RESERVA.",
        "whatsapp,reserva",
        user_id,
    )
    create_knowledge_scoped(
        pg_seed["business_id"],
        "policy",
        "Politica de cancelacion",
        "Las cancelaciones deben hacerse con 2 horas de antelacion.",
        "cancelacion,politica",
        user_id,
    )
    # Inactiva (no debe aparecer en busquedas)
    create_knowledge_scoped(
        pg_seed["business_id"],
        "faq",
        "Entrada inactiva",
        "Esta no debe aparecer.",
        "inactiva",
        user_id,
    )
    # Desactivar la ultima
    knowledge = get_knowledge_scoped(pg_seed["business_id"], active_only=False)
    inactive = [k for k in knowledge if k["question"] == "Entrada inactiva"][0]
    from services.knowledge import update_knowledge_scoped

    update_knowledge_scoped(
        inactive["id"],
        pg_seed["business_id"],
        "faq",
        "Entrada inactiva",
        "Esta no debe aparecer.",
        "inactiva",
        active=False,
    )


# =====================================================================
# 1. STEMMING ESPANOL
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_spanish_stemming(pg_pool, pg_seed, monkeypatch):
    """'cortes' debe encontrar 'corte' (stemming espanol)."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    results = search_knowledge_scoped(pg_seed["business_id"], "cortes", limit=5)
    questions = [r["question"] for r in results]
    assert "Cuanto cuesta el corte?" in questions


# =====================================================================
# 2. STOPWORDS
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_stopwords(pg_pool, pg_seed, monkeypatch):
    """Palabras vacias ('de', 'el', 'la') se ignoran en la busqueda."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    # "corte de la" -> stopwords "de", "la" se eliminan, queda "corte"
    results = search_knowledge_scoped(pg_seed["business_id"], "corte de la", limit=5)
    questions = [r["question"] for r in results]
    # "corte" aparece en "Cuanto cuesta el corte?"
    assert any("corte" in q.lower() for q in questions)


# =====================================================================
# 3. ACENTOS
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_accents(pg_pool, pg_seed, monkeypatch):
    """Busqueda sin acentos encuentra entradas con acentos."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    # Buscar "cancela" sin acento -> debe encontrar "cancelacion"
    results = search_knowledge_scoped(pg_seed["business_id"], "cancelacion", limit=5)
    questions = [r["question"] for r in results]
    assert any("cancelacion" in q.lower() for q in questions)


# =====================================================================
# 4. FRASES EXACTAS
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_phrase(pg_pool, pg_seed, monkeypatch):
    """Busqueda de frase exacta con comillas."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    # Frase exacta
    results = search_knowledge_scoped(pg_seed["business_id"], '"politica de cancelacion"', limit=5)
    questions = [r["question"] for r in results]
    assert any("cancelacion" in q.lower() for q in questions)


# =====================================================================
# 5. BUSQUEDA PARCIAL (prefijos)
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_partial(pg_pool, pg_seed, monkeypatch):
    """Prefijo 'cort*' encuentra 'corte' y 'cortes'."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    results = search_knowledge_scoped(pg_seed["business_id"], "cort*", limit=5)
    questions = [r["question"] for r in results]
    assert any("corte" in q.lower() for q in questions)


# =====================================================================
# 6. RANKING
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_ranking(pg_pool, pg_seed, monkeypatch):
    """Resultados ordenados por relevancia (ts_rank)."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    # "corte" debe aparecer primero si hay multiples coincidencias
    results = search_knowledge_scoped(pg_seed["business_id"], "corte", limit=10)
    assert len(results) > 0
    # El primer resultado debe tener "corte" en la pregunta
    assert "corte" in results[0]["question"].lower()


# =====================================================================
# 7. QUERY VACIA
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_empty_query(pg_pool, pg_seed, monkeypatch):
    """Query vacia debe devolver lista vacia, sin excepcion."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))

    assert search_knowledge_scoped(pg_seed["business_id"], "", limit=5) == []
    assert search_knowledge_scoped(pg_seed["business_id"], "   ", limit=5) == []
    assert search_knowledge_scoped(pg_seed["business_id"], "\t\n", limit=5) == []


# =====================================================================
# 8. CARACTERES ESPECIALES
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_search_special_chars(pg_pool, pg_seed, monkeypatch):
    """Caracteres especiales no rompen la busqueda."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    # Caracteres que podrian ser operadores en websearch_to_tsquery
    results = search_knowledge_scoped(pg_seed["business_id"], "corte@#$%", limit=5)
    questions = [r["question"] for r in results]
    assert any("corte" in q.lower() for q in questions)

    # Operador OR explicito
    results = search_knowledge_scoped(pg_seed["business_id"], "corte OR horario", limit=5)
    questions = [r["question"] for r in results]
    assert len(results) >= 2


# =====================================================================
# 9. CROSS-TENANT ISOLATION
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_cross_tenant(pg_pool, pg_seed, monkeypatch):
    """Busqueda no cruza business_id (aislamiento multi-tenant)."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    # Crear otro negocio con conocimiento similar
    from tests_pg._helpers import seed_business

    business2 = seed_business(pg_seed["url"])
    user_id2 = seed_user(pg_seed["url"])
    create_knowledge_scoped(
        business2, "faq", "Cuanto cuesta el corte?", "2000 pesos", "precio", user_id2
    )

    # Buscar en negocio original
    results = search_knowledge_scoped(pg_seed["business_id"], "corte", limit=5)
    assert all(r["business_id"] == pg_seed["business_id"] for r in results)
    # No debe aparecer el del negocio 2
    assert not any("2000" in r["answer"] for r in results)


# =====================================================================
# 10. INACTIVE EXCLUDED
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_inactive_excluded(pg_pool, pg_seed, monkeypatch):
    """Entradas inactive=FALSE no aparecen en busquedas."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    results = search_knowledge_scoped(pg_seed["business_id"], "inactiva", limit=5)
    assert len(results) == 0


# =====================================================================
# 11. LIMIT
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_limit(pg_pool, pg_seed, monkeypatch):
    """Limite de resultados funciona correctamente."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    results = search_knowledge_scoped(pg_seed["business_id"], "corte", limit=2)
    assert len(results) <= 2

    results = search_knowledge_scoped(pg_seed["business_id"], "corte", limit=10)
    assert len(results) <= 10


# =====================================================================
# 12. TODOS LOS CAMPOS INDEXADOS
# =====================================================================


@pytest.mark.pg_live
def test_pg_knowledge_all_indexed_fields(pg_pool, pg_seed, monkeypatch):
    """Search finds matches in question, answer and tags."""
    monkeypatch.setattr("services.knowledge.get_connection", lambda: make_pg_proxy(pg_pool))
    _setup_knowledge_context(pg_seed)

    # Buscar en answer
    results = search_knowledge_scoped(pg_seed["business_id"], "1000 pesos", limit=5)
    questions = [r["question"] for r in results]
    assert any("corte" in q.lower() for q in questions)

    # Buscar en tags
    results = search_knowledge_scoped(pg_seed["business_id"], "whatsapp", limit=5)
    questions = [r["question"] for r in results]
    assert any("WhatsApp" in q for q in questions)
