# Plan de mejoras aplicables

## Diagnóstico actual

El proyecto es una recepcionista virtual para una barbería construida con Flask, SQLite y Gemini. Ya incorpora varias decisiones correctas: separación entre rutas, servicios y base de datos; transacciones `BEGIN IMMEDIATE`; servicios y horarios configurables desde SQLite; validación básica de datos; y protección contra doble reserva.

Las mejoras prioritarias no son rehacer la arquitectura, sino preparar el sistema para uso real y reducir riesgos de seguridad, errores operativos y mantenimiento.

## Mejoras prioritarias

### 1. Seguridad de producción

- Eliminar credenciales por defecto peligrosas:
  - `SECRET_KEY` no debe tener un valor fallback conocido.
  - `ADMIN_PASSWORD` no debe usar `admin123` como valor predeterminado.
- Validar las variables obligatorias al iniciar y detener el servidor con un mensaje claro si faltan.
- Almacenar la contraseña administrativa como hash usando Werkzeug, nunca comparar una contraseña plana desde configuración.
- Configurar cookies de sesión con `HttpOnly`, `SameSite` y `Secure` cuando se ejecute con HTTPS.
- Añadir protección CSRF para login y futuras operaciones administrativas que modifiquen datos.
- No confiar ciegamente en `request.access_route` para rate limiting si la aplicación no está detrás de un proxy confiable.
- Aplicar rate limiting también a endpoints sensibles como consulta, cancelación y reprogramación, idealmente por IP y teléfono.
- Evitar exponer información que permita enumerar turnos o confirmar datos personales sin controles adicionales.

### 2. Consistencia de reservas

- Validar explícitamente que el servicio exista y esté activo dentro de `create_appointment`, no solamente en la ruta HTTP o en el prompt de Gemini.
- Hacer que la duración real del servicio participe en la validación del horario reservado. Actualmente los slots se construyen usando el servicio más largo, pero la regla debe quedar expresada y testeada en la capa de dominio.
- Validar que una reserva no quede en el pasado cuando la fecha es hoy y la hora ya pasó en la zona horaria del negocio.
- Validar formato y rango de la hora antes de hacer consultas o comparaciones.
- Definir una política para teléfonos con formato internacional, espacios, guiones y prefijos. La regla actual de solo dígitos puede rechazar números legítimos.
- Añadir estados y reglas explícitas para `confirmed`, `cancelled` y, si se necesita, `completed` o `no_show`.
- Añadir una restricción o estrategia clara para evitar reservas solapadas si en el futuro los servicios tienen duraciones diferentes.

### 3. Panel administrativo

- Mostrar turnos filtrados por fecha y estado, con orden por fecha y hora.
- Permitir cancelar y reprogramar desde el panel con confirmación y protección CSRF.
- Añadir configuración de servicios, precios, duración, horarios semanales y datos del negocio.
- Separar métricas de total, confirmados, cancelados y próximos turnos. Actualmente las tarjetas de total y confirmados usan el mismo conteo.
- Añadir paginación o filtros cuando crezca la cantidad de turnos.
- Evitar que el logout dependa únicamente de un enlace GET; usar POST para acciones que cambian estado.
- Mostrar mensajes de error y éxito de forma clara sin recargar toda la página cuando sea posible.

### 4. Tests y calidad

- Ejecutar los tests con el intérprete del entorno virtual (`venv`) y documentar el comando oficial.
- Separar tests unitarios, tests de Flask y tests de integración con Gemini.
- Marcar el test de Gemini como integración opcional, condicionado a `GEMINI_API_KEY`, para que no rompa la suite local.
- Añadir tests para:
  - servicio inexistente o inactivo;
  - fecha de hoy con hora pasada;
  - formatos de teléfono aceptados y rechazados;
  - reprogramación a día cerrado o fecha pasada;
  - cancelación de un turno ya cancelado;
  - errores de tipos en IDs y JSON;
  - concurrencia real de dos reservas;
  - rutas protegidas del panel admin;
  - rate limiting;
  - cookies y sesión de administrador.
- Reemplazar `test_fixes_simple.py`, que imprime afirmaciones pero no verifica automáticamente el código, por tests ejecutables con aserciones.
- Añadir cobertura y ejecutar `pytest` o `unittest` de forma reproducible en CI.

### 5. Observabilidad y errores

- Sustituir `print()` de errores en Flask por logging estructurado con niveles y contexto.
- No registrar contraseñas, teléfonos completos, prompts privados ni respuestas sensibles de Gemini.
- Devolver respuestas de error consistentes con un código/reason estable para que el frontend pueda actuar correctamente.
- Registrar latencia, errores y cantidad de iteraciones de tool calling de Gemini.
- Añadir manejo específico para timeout, cuota excedida, autenticación inválida y respuesta malformada de Gemini.
- Añadir un endpoint o mecanismo de health check que compruebe la aplicación y la base de datos sin llamar a Gemini.

### 6. Gemini y control de costos

- Mover el modelo, zona horaria y límites a configuración validada por entorno.
- No inicializar el cliente de Gemini de forma que impida ejecutar tests o levantar rutas que no usan IA cuando falta la API key.
- Limitar tamaño total del historial, no solo cantidad de mensajes, y normalizar roles/contenido en el servidor.
- Añadir timeout y reintentos acotados para llamadas externas.
- Validar todas las respuestas de las herramientas y no permitir que el modelo confirme operaciones si la capa de dominio no devuelve éxito.
- Considerar un flujo determinista para reservas, cancelaciones y reprogramaciones, dejando Gemini como interfaz conversacional y no como autoridad de negocio.

### 7. Base de datos y despliegue

- Configurar `timeout`, `busy_timeout` y, si corresponde, WAL para SQLite.
- Asegurar que la carpeta de base de datos exista antes de conectar.
- Revisar las migraciones para que fallen de forma transaccional y puedan auditarse fácilmente.
- Añadir backups periódicos y una estrategia de restauración.
- Añadir índices para búsquedas por teléfono, fecha, estado y combinación fecha/hora.
- No guardar `appointments.db`, `.env` ni secretos en el repositorio.
- Añadir configuración de producción con servidor WSGI, HTTPS, variables de entorno y modo debug desactivado.

### 8. Frontend y experiencia de uso

- Persistir o reconstruir la conversación después de una recarga cuando sea útil, sin guardar datos sensibles innecesarios.
- Deshabilitar el envío mientras una petición está pendiente y permitir cancelar peticiones lentas.
- Diferenciar errores de validación, rate limit, caída del servidor y fallo de Gemini.
- Añadir accesibilidad: foco visible, navegación por teclado, etiquetas, `aria-live` para mensajes y contraste suficiente.
- Evitar depender de emojis como única señal visual en el panel administrativo.
- Mostrar los horarios y servicios desde la API, manteniendo una única fuente de verdad.
- Añadir estados de carga, vacío, error y éxito para cada operación directa.

## Orden recomendado de implementación

1. Seguridad de secretos, cookies, CSRF y configuración de producción.
2. Validaciones de dominio para servicios, horarios, fechas y estados.
3. Separación y ampliación de tests, incluyendo rutas protegidas y errores.
4. Manejo de errores, logging y control de Gemini.
5. Mejoras del panel administrativo.
6. Mejoras de SQLite, backups y despliegue.
7. Accesibilidad y refinamiento de la experiencia frontend.

## Resultado esperado

El sistema debería poder ejecutarse con una configuración explícita, rechazar estados inválidos desde cualquier entrada, mantener la integridad de las reservas bajo concurrencia, permitir operar el negocio desde el panel administrativo y ofrecer una suite de tests que no dependa accidentalmente de una API externa.
