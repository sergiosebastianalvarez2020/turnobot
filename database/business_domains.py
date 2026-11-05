"""Persistence operations for tenant custom domains."""

from database.database import get_connection


def _record_audit(connection, business_id, actor_email, action, hostname, ip_address):
    connection.execute(
        "INSERT INTO audit_log "
        "(actor_email, business_id, action, detail, ip_address) "
        "VALUES (?, ?, ?, ?, ?)",
        (actor_email or "", business_id, action, hostname, ip_address or ""),
    )


def list_business_domains(business_id):
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, business_id, hostname, verification_status, is_active, is_primary, "
            "verification_expires_at, verified_at, tls_status, tls_checked_at, created_at "
            "FROM business_domains WHERE business_id = ? ORDER BY id",
            (business_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_business_domain(business_id, domain_id):
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT id, business_id, hostname, verification_status, is_active, is_primary, "
            "verification_token_hash, verification_expires_at, verified_at, tls_status, "
            "tls_checked_at FROM business_domains WHERE business_id = ? AND id = ?",
            (business_id, domain_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def resolve_business_domain(hostname, connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_connection()
    try:
        row = connection.execute(
            "SELECT b.id, b.name, b.slug FROM business_domains d "
            "JOIN businesses b ON b.id = d.business_id "
            "WHERE lower(d.hostname) = ? AND d.verification_status = 'verified' "
            "AND d.is_active = TRUE AND d.tls_status = 'verified' AND b.active = TRUE",
            (hostname,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_connection:
            connection.close()


def get_primary_business_domain(business_id):
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT hostname FROM business_domains "
            "WHERE business_id = ? AND is_primary = TRUE AND verification_status = 'verified' "
            "AND is_active = TRUE AND tls_status = 'verified'",
            (business_id,),
        ).fetchone()
        return row["hostname"] if row else None
    finally:
        connection.close()


def create_business_domain(business_id, hostname, token_hash, actor_email="", ip_address=""):
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO business_domains "
            "(business_id, hostname, verification_token_hash, verification_expires_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (business_id, hostname, token_hash, "+24 hours"),
        )
        _record_audit(
            connection, business_id, actor_email, "business_domain.created", hostname, ip_address
        )
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def rotate_business_domain_token(business_id, domain_id, token_hash, actor_email="", ip_address=""):
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT hostname FROM business_domains WHERE business_id = ? AND id = ?",
            (business_id, domain_id),
        ).fetchone()
        if not row:
            connection.rollback()
            return None
        hostname = row["hostname"]
        connection.execute(
            "UPDATE business_domains SET verification_status = 'pending', is_active = FALSE, "
            "is_primary = FALSE, verification_token_hash = ?, "
            "verification_expires_at = datetime('now', ?), verified_at = NULL, "
            "tls_status = 'pending', tls_checked_at = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE business_id = ? AND id = ?",
            (token_hash, "+24 hours", business_id, domain_id),
        )
        _record_audit(
            connection,
            business_id,
            actor_email,
            "business_domain.token_rotated",
            hostname,
            ip_address,
        )
        connection.commit()
        return hostname
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def verify_business_domain(business_id, domain_id, token_hash, actor_email="", ip_address=""):
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE business_domains SET verification_status = 'verified', "
            "verification_token_hash = NULL, verification_expires_at = NULL, "
            "verified_at = CURRENT_TIMESTAMP, tls_status = 'pending', tls_checked_at = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE business_id = ? AND id = ? "
            "AND verification_status = 'pending' AND verification_token_hash = ? "
            "AND verification_expires_at > CURRENT_TIMESTAMP",
            (business_id, domain_id, token_hash),
        )
        if cursor.rowcount:
            row = connection.execute(
                "SELECT hostname FROM business_domains WHERE business_id = ? AND id = ?",
                (business_id, domain_id),
            ).fetchone()
            _record_audit(
                connection,
                business_id,
                actor_email,
                "business_domain.verified",
                row["hostname"],
                ip_address,
            )
        connection.commit()
        return bool(cursor.rowcount)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_business_domain_tls_status(business_id, domain_id, status, actor_email="", ip_address=""):
    if status not in {"verified", "failed"}:
        raise ValueError("Estado TLS inválido.")
    connection = get_connection()
    try:
        if status == "failed":
            cursor = connection.execute(
                "UPDATE business_domains SET tls_status = ?, tls_checked_at = CURRENT_TIMESTAMP, "
                "is_active = FALSE, is_primary = FALSE, updated_at = CURRENT_TIMESTAMP "
                "WHERE business_id = ? AND id = ? AND verification_status = 'verified'",
                (status, business_id, domain_id),
            )
        else:
            cursor = connection.execute(
                "UPDATE business_domains SET tls_status = ?, tls_checked_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE business_id = ? AND id = ? "
                "AND verification_status = 'verified'",
                (status, business_id, domain_id),
            )
        if cursor.rowcount:
            row = connection.execute(
                "SELECT hostname FROM business_domains WHERE business_id = ? AND id = ?",
                (business_id, domain_id),
            ).fetchone()
            _record_audit(
                connection,
                business_id,
                actor_email,
                f"business_domain.tls_{status}",
                row["hostname"],
                ip_address,
            )
        connection.commit()
        return bool(cursor.rowcount)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_business_domain_active(business_id, domain_id, active, actor_email="", ip_address=""):
    connection = get_connection()
    try:
        if active:
            cursor = connection.execute(
                "UPDATE business_domains SET is_active = TRUE, updated_at = CURRENT_TIMESTAMP "
                "WHERE business_id = ? AND id = ? AND verification_status = 'verified' "
                "AND tls_status = 'verified'",
                (business_id, domain_id),
            )
        else:
            cursor = connection.execute(
                "UPDATE business_domains SET is_active = FALSE, is_primary = FALSE, "
                "updated_at = CURRENT_TIMESTAMP WHERE business_id = ? AND id = ?",
                (business_id, domain_id),
            )
        if cursor.rowcount:
            row = connection.execute(
                "SELECT hostname FROM business_domains WHERE business_id = ? AND id = ?",
                (business_id, domain_id),
            ).fetchone()
            _record_audit(
                connection,
                business_id,
                actor_email,
                "business_domain.activated" if active else "business_domain.deactivated",
                row["hostname"],
                ip_address,
            )
        connection.commit()
        return bool(cursor.rowcount)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def revoke_business_domain(business_id, domain_id, actor_email="", ip_address=""):
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT hostname FROM business_domains WHERE business_id = ? AND id = ?",
            (business_id, domain_id),
        ).fetchone()
        if not row:
            connection.rollback()
            return False
        connection.execute(
            "UPDATE business_domains SET verification_status = 'revoked', is_active = FALSE, "
            "is_primary = FALSE, verification_token_hash = NULL, verification_expires_at = NULL, "
            "tls_status = 'pending', tls_checked_at = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE business_id = ? AND id = ?",
            (business_id, domain_id),
        )
        _record_audit(
            connection,
            business_id,
            actor_email,
            "business_domain.revoked",
            row["hostname"],
            ip_address,
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_primary_business_domain(business_id, domain_id, actor_email="", ip_address=""):
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT hostname FROM business_domains WHERE business_id = ? AND id = ? "
            "AND verification_status = 'verified' AND is_active = TRUE "
            "AND tls_status = 'verified'",
            (business_id, domain_id),
        ).fetchone()
        if not row:
            connection.rollback()
            return False
        connection.execute(
            "UPDATE business_domains SET is_primary = FALSE, updated_at = CURRENT_TIMESTAMP "
            "WHERE business_id = ?",
            (business_id,),
        )
        connection.execute(
            "UPDATE business_domains SET is_primary = TRUE, updated_at = CURRENT_TIMESTAMP "
            "WHERE business_id = ? AND id = ?",
            (business_id, domain_id),
        )
        _record_audit(
            connection,
            business_id,
            actor_email,
            "business_domain.primary_changed",
            row["hostname"],
            ip_address,
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
