# Dominios personalizados

Un dominio propio es opcional. La dirección gratuita
`https://turnobot.net.ar/b/<slug>` no depende de esta función y sigue siendo el
origen de reserva disponible para todos los negocios.

## Estados y flujo del panel

1. El owner agrega un hostname DNS, sin protocolo ni puerto.
2. TurnoBot muestra una sola vez el valor aleatorio del TXT
   `_turnobot-verification.<hostname>`. Se guarda únicamente su SHA-256 y vence
   a las 24 horas. Si se pierde, se puede rotar; la rotación desactiva el dominio.
3. La aplicación consulta TXT con timeout limitado. Solo el valor exacto del
   token vigente verifica la propiedad.
4. El equipo de Operaciones configura el DNS de ingreso y HTTPS para ese
   hostname. El propietario puede solicitar la comprobación TLS desde el panel.
   La comprobación sale únicamente a direcciones IP públicas, conecta al puerto
   443 con SNI y valida hostname y cadena de certificados con el trust store del
   sistema. No sigue redirects ni acepta certificados autofirmados.
5. El owner activa el dominio y, opcionalmente, lo elige como principal.

Solo los registros `verified`, `tls_status=verified`, activos y asociados a un
negocio activo pueden resolver un tenant. La comprobación TLS fallida inactiva
el registro y quita su condición de principal. Un hostname pendiente, revocado,
inactivo o desconocido no cae en el negocio `id=1`. Un dominio personalizado
responde en `/` únicamente por HTTPS. Si la ruta además incluye `/b/<slug>`,
ese slug debe ser el del tenant resuelto por hostname.

La tabla permite varios hostnames por negocio y aplica una unicidad global
case-insensitive y como máximo un principal por negocio. No se ofrece wildcard
ni resolución por sufijo. Los hostnames se convierten a IDNA/minúsculas y se
almacenan sin punto final.

## Migraciones

- SQLite: `migrations/025_business_domains.sql`, transaccional e incremental.
- PostgreSQL: `migrations_pg/003_business_domains.sql`, aplicada únicamente
  mediante el runner explícito `scripts/migrate_pg.py`.

Ambas migraciones crean la misma funcionalidad lógica pero usan tipos nativos:
SQLite `INTEGER` para flags y timestamps como texto; PostgreSQL `BOOLEAN`,
`BIGINT IDENTITY` y `TIMESTAMPTZ`. No editar migraciones históricas. La
migración es aditiva; no borra negocios ni datos existentes.

Antes de una migración PostgreSQL en un entorno real, seguir el proceso de
backup, dry-run, aplicación y verificación de
[DEPLOYMENT.md](../DEPLOYMENT.md). No aplicar la migración en producción como
parte del arranque de la aplicación.

## DNS, ingress y TLS

El panel no inventa ni publica una IP de destino. El operador de la plataforma
debe definir qué target soporta el ingress actual y comunicarlo a cada cliente.
El DNS del cliente apuntará el hostname indicado a ese target y publicará el TXT
de verificación por separado. El proveedor DNS del cliente puede requerir A,
AAAA, CNAME o una opción ALIAS/ANAME para el apex.

Responsabilidades:

- **Cliente:** configura los registros DNS indicados por Operaciones y mantiene
  el dominio registrado.
- **Ingress administrado por TurnoBot:** termina TLS por SNI, permite solo
  hostnames provisionados, conserva el Host original autorizado y reemplaza las
  cabeceras reenviadas del cliente.
- **Flask:** normaliza el hostname y resuelve el tenant en `business_domains`;
  el proxy nunca decide `business_id`.
- **Operaciones:** emite, renueva y revoca certificados y luego ejecuta la
  comprobación TLS desde el panel.

La aplicación todavía no automatiza la emisión/renovación de certificados ni
provisiona Nginx. El panel verifica un certificado ya instalado; no es una
integración de ACME. Por lo tanto, los dominios reales no deben activarse hasta
que exista y se valide un proceso operativo de certificados y de allowlist del
ingress.

En AWS, dos opciones a evaluar son un Application Load Balancer con ACM (requiere
evaluar costo del balanceador, DNS/validación y cambios de entrada) o Nginx en
EC2 con un proceso ACME operado por el equipo (requiere automatizar emisión,
renovación, reload y límites de emisión). No se fijan precios ni se asume una
IP: confirmar los costos vigentes y la arquitectura aprobada antes de contratar
servicios o cambiar infraestructura. El inventario de AWS es referencial; la
configuración activa de Nginx, TLS, Security Groups y DNS no fue inspeccionada
para este cambio.

`ProxyFix` solo debe habilitarse con `TRUSTED_PROXY_COUNT` igual al número real
de proxies controlados. Waitress debe seguir enlazado a loopback. `X-Forwarded-
Host` enviado por un cliente no debe llegar sin sobrescritura por un proxy
confiable. `PUBLIC_BASE_URL` sigue siendo el origen de la plataforma y nunca se
construyen enlaces absolutos a partir del Host entrante.

## Panel y aislamiento

La gestión se limita a owners y obtiene siempre `business_id` del contexto
resuelto, nunca de un formulario. Los registros y la auditoría se escriben en
transacciones. Desactivar o revocar hace que el resolver deje de servir ese
tenant inmediatamente. Los enlaces de reserva y de invitación pueden usar el
dominio principal solo si está activo y la comprobación TLS sigue válida; la
URL gratuita permanece disponible.

Las cookies de sesión son host-only, por lo que un login en un dominio no se
comparte con otro hostname. Para administración en el dominio propio, el owner
debe iniciar sesión allí.

## Rollback

La migración es aditiva y no se debe eliminar `business_domains` como rollback.
Para deshabilitar la función, desactivar los dominios desde el panel y volver a
la versión anterior de la aplicación; esa versión ignora la tabla nueva. En
PostgreSQL, no ejecutar el runner de migraciones con una versión de código que
desconozca la migración 003. Si se requiere restaurar esquema, debe hacerse
desde backup recuperable siguiendo el procedimiento de recuperación del
operador, no con `DROP TABLE`.

Antes de un despliegue: confirmar SHA candidato y de producción, CI, backup
verificado, estado de migraciones y procedimiento de rollback. Ningún paso de
este documento autoriza un despliegue, cambio de DNS, Nginx, Security Group,
certificado o base de producción.
