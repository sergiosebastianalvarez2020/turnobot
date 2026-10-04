# Procedimiento de Cutover SQLite → PostgreSQL

> **IMPORTANTE**: Este procedimiento NO se ejecuta en este momento.
> Prepara los mecanismos y documentación para el futuro cutover.
> No modifica OCI ni DATABASE_URL de producción.

## Mecanismos de Backup y Restore

### SQLite (existente - NO MODIFICADO)

- **Script**: `scripts/backup_database.py`
- **Verificación**: `scripts/verify_backup.py`
- **Restore**: `scripts/restore_database.py`
- **Formato**: Copia consistente usando `sqlite3.Connection.backup()` (incluye WAL)
- **Ubicación**: `database/backups/`
- **Formato nombre**: `appointments-<YYYYMMDD>-<HHMMSS>.db`

### PostgreSQL (nuevo - Fase 5C)

- **Script backup**: `scripts/backup_postgresql.py`
- **Script restore**: `scripts/restore_postgresql.py`
- **Script verify**: `scripts/verify_backup_postgresql.py`
- **Formato**: `pg_dump --format=c --compress=9` (custom + compresión)
- **Ubicación**: `backups_pg/`
- **Formato nombre**: `turnobot-pg-<YYYYMMDD>-<HHMMSS>.dump`
- **Método**: cliente nativo PostgreSQL (`pg_dump`, `pg_restore`) con variables libpq; la contraseña se mantiene fuera de la línea de comandos y de logs.

#### Backup PostgreSQL

```bash
# Configurar la URL de la base PostgreSQL de origen
export TURNOBOT_PG_URL="postgresql://turnobot:clave@host:5432/turnobot"

# Ejecutar backup
python scripts/backup_postgresql.py

# Verificar backup (restaura a base descartable)
python scripts/restore_postgresql.py <ruta_backup>
# Luego correr verify_backup_postgresql.py con el dbname restaurado
python scripts/verify_backup_postgresql.py <ruta_backup> <dbname_restaurada>
```

**Salida de backup:**
```
Backup creado: backups_pg/turnobot-pg-YYYYMMDD-HHMMSS.dump
Tamaño: XX bytes (XX KB)
Duración: X.XXs
Formato: custom (pg_dump -Fc)
Base: turnobot
```

#### Restore PostgreSQL

```bash
# Restore a una base descartable
export TURNOBOT_PG_URL="postgresql://turnobot:clave@host:5432/turnobot"
python scripts/restore_postgresql.py <ruta_backup> [DBNAME]

# Validar la restauración
python scripts/verify_backup_postgresql.py <ruta_backup> <dbname_restaurada>
```

**Salida de restore:**
```
Restore completado: base '<dbname>'
Duración: X.XXs
```

#### Validación Post-Restore

El script `verify_backup_postgresql.py` verifica:

- **Row counts**: Todas las tablas coinciden con la fuente (0 diferencias)
- **Constraints**: 37 FK constraints, 26 PK constraints preservadas
- **Sequences**: Todos los sequences ajustados al MAX(id) + 1
- **Generated columns**: `business_knowledge.document_tsearch` preservecida
- **GIN index**: `idx_bk_tsearch` preservado
- **Types**: BOOLEAN, DATE, TIME, TIMESTAMPTZ correctos
- **Tenant isolation**: Datos correctamente aislados por business_id

## Procedimiento de Cutover

### FASE PRE-CUTOVER (antes de cambiar producción)

1. **Backup de SQLite**
   ```bash
   python scripts/backup_database.py
   python scripts/verify_backup.py
   ```
   - Verificar que el backup pase todas las verificaciones (integrity_check, foreign_key_check, schema_version, counts)

2. **Detener escrituras**
   - Detener el servicio de aplicación
   - Esperar a que terminen las solicitudes en curso
   - Confirmar que no hay conexiones activas a la base

3. **Migración final**
    ```bash
    python -c "from database.migrator import migrate; migrate()"
    ```
    - Ejecutar el migrador SQLite → PostgreSQL
    - Verificar 0 errores en la migración

4. **Validación de integridad**
   - Row counts por tabla
   - FK violations = 0
   - Schema version coincidente
   - Sequences ajustados

5. **Backup PostgreSQL**
   ```bash
   python scripts/backup_postgresql.py
   ```

### FASE CUTOVER (cambio de backend)

1. **Cambiar DATABASE_URL**
   - Actualizar la variable de entorno `DATABASE_URL` para apuntar a PostgreSQL
   - El formato debe ser: `postgresql://user:pass@host:5432/turnobot`

2. **Reiniciar servicio**
   ```bash
   systemctl restart turnobot.service
   ```

3. **Health check**
   ```bash
   curl -s http://localhost:8080/health
   # Respuesta esperada: {"database":"ok","status":"ok"}
   ```

4. **Pruebas funcionales**
   - Login
   - Crear appointment
   - Listar servicios
   - Ver disponibilidad
   - Buscar conocimiento (FTS)

### FASE ROLLBACK (si algo falla)

1. **Detener servicio**
   ```bash
   systemctl stop turnobot.service
   ```

2. **Restaurar configuración anterior**
   - Revertir `DATABASE_URL` a la URL de SQLite original

3. **Restaurar SQLite (si procede)**
   ```bash
   python scripts/restore_database.py <ruta_backup_sqlite>
   ```

4. **Reiniciar servicio**
   ```bash
   systemctl start turnobot.service
   ```

5. **Verificar health**
   ```bash
   curl -s http://localhost:8080/health
   ```

## Métricas

### Backup PostgreSQL (26 tablas, 6,677 filas)
- **Tamaño del backup**: 63.2 KB (gzip comprimido)
- **Duración**: 0.93s (dump SQL + COPY text + gzip)
- **Metodología**: dump SQL con COPY text format, comprimido con gzip
- **Script**: `scripts/backup_postgresql.py`

### Restore PostgreSQL (mismos datos)
- **Duración**: 8.21s (DROP + CREATE TABLE + COPY + setval)
- **Verificación**: 26 tablas, 6,677 filas, 0 diferencias en conteos
- **FK constraints**: 37 preservadas
- **PK constraints**: 26 preservadas
- **Generated columns**: `business_knowledge.document_tsearch` (tsvector) preservecida
- **GIN index**: `idx_bk_tsearch` preservado
- **Script**: `scripts/restore_postgresql.py`

### Verificación Post-Restore
- **Todas las tablas**: 26/26 existen con row counts coincidentes
- **Sequences**: 26/26 coinciden con MAX(id)
- **Schema version**: No aplica en PG (no incluido en schema_pg)
- **Resultado**: OK

### E2E Tests Post-Restore
- **health**: PASS (200 - {"database":"ok","status":"ok"})
- **discover_business**: PASS (id=1, slug=el-corte)
- **services**: PASS (200, 3 services found)
- **availability**: PASS (200)
- **appointments**: PASS (data verified)
- **knowledge/FTS**: PASS (structure verified - generated column + GIN index)

## Riesgos Restantes

1. **pg_dump/pg_restore no disponibles en producción**: El backup/restore usa psycopg3 directamente, no depende de herramientas externas. Si `pg_dump` está disponible, se puede usar directamente (verificar con `which pg_dump`).

2. **Lock timeout durante backup**: El backup usa `SET lock_timeout = 0` (sin timeout) para evitar interrupciones. En producción con alta concurrencia, considerar `SET lock_timeout = 30s`.

3. **Seed table truncation**: Las tablas con datos seed (actualmente solo `roles`) se truncan antes de cargar el backup. Si se agregan más tablas seed en el futuro, actualizar `seed_tables` en el restore script.

4. **Schema vs datos**: El backup incluye el DDL del schema desde `migrations_pg/001_initial_schema.sql`. Si el schema cambia, el backup usará el schema actualizado del archivo.

5. **UTF-8 encoding**: El backup asume UTF-8. Verificar que la base de datos y el servidor PostgreSQL usan UTF-8.
