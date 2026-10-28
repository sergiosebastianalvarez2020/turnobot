# Inventario Operativo de Servidores — AWS y OCI

Este documento centraliza los datos de infraestructura conocidos y los procedimientos de diagnóstico de conexión para que agentes de IA (Kilo Code, OpenCode, Antigravity, etc.) y desarrolladores puedan consultar rápidamente la información operativa sin depender de conversaciones previas.

> [!IMPORTANT]
> **REGLAS OBLIGATORIAS DE OPERACIÓN**
> 1. Las IPs listadas son **datos registrados en la documentación**, no una comprobación en tiempo real.
> 2. **NO revelar jamás** claves privadas, contraseñas, tokens de sesión ni credenciales SMTP.
> 3. **NO modificar producción, no detener servicios, no correr migraciones ni eliminar backups** sin autorización explícita del operador.
> 4. **NO enviar notificaciones reales** (emails/SMS) durante pruebas de diagnóstico.
> 5. Para cualquier prueba de restauración de base de datos, **usar siempre una base aislada/descartable** (ver `scripts/restore_postgresql.py`).

---

## 1. AWS EC2 — Producción Actual

Servidor principal activo donde corre TurnoBot en producción.

| Parámetro | Valor Registrado | Notas |
|---|---|---|
| **Proveedor / Servicio** | AWS EC2 | Producción actual |
| **IP Pública registrada** | `18.191.0.23` | IP pública registrada (sujeta a verificación si no es elástica) |
| **Usuario SSH** | `ubuntu` | Usuario por defecto de Ubuntu Server en AWS |
| **Ruta del Proyecto** | `/home/ubuntu/turnobot` | Directorio raíz de la aplicación |
| **Entorno Virtual Python** | `/home/ubuntu/turnobot/.venv312` | Python 3.12 venv |
| **Servicio systemd** | `turnobot.service` | Gestionado vía `systemctl status turnobot.service` |
| **Servidor WSGI interno** | Waitress en `127.0.0.1:5000` | Detrás de proxy reverso (Nginx/Caddy) |
| **Endpoint de salud** | `http://127.0.0.1:5000/health` | HTTP 200 `{"status":"ok"}` |
| **Directorio de Backups PG** | `/home/ubuntu/turnobot/backups_pg` | Dumps de PostgreSQL (`.dump`) |
| **Timer de Backups systemd** | `turnobot-backup.timer` | Timer de backup periódico en AWS |

### Comandos de Diagnóstico y Conexión (PowerShell)

**1. Prueba de conectividad TCP (puerto 22 SSH):**
```powershell
Test-NetConnection 18.191.0.23 -Port 22
```

**2. Conexión SSH con ruta de clave configurable:**
```powershell
ssh -i "$env:USERPROFILE\.ssh\RUTA_DE_LA_CLAVE" ubuntu@18.191.0.23
```

> [!WARNING]
> **Ruta de clave privada AWS**:
> Debe configurarse según el archivo existente en el equipo local. En conversaciones previas se mencionó una ruta histórica `H:\Descagas\Downloads\Turnobot1 (2).pem`, pero **no debe asumirse que sigue existiendo ni que es la clave correcta**. 
> Configurar la ruta real en un archivo local `docs/operations/SERVERS.local.md` (ver plantilla `SERVERS.local.example.md`).

---

## 2. OCI (Oracle Cloud Infrastructure) — Servidor Anterior

Servidor anterior de producción. Puede mantenerse encendido con timers auxiliares y no debe descartarse ni apagarse sin previa verificación de migraciones y resguardo de datos históricos.

| Parámetro | Valor Registrado | Notas |
|---|---|---|
| **Proveedor / Servicio** | Oracle Cloud Infrastructure (OCI) | Servidor anterior |
| **IP Pública registrada** | `161.153.202.182` | IP pública registrada en OCI |
| **Usuario SSH** | `opc` | Usuario por defecto de Oracle Linux / Ubuntu en OCI |
| **Ruta del Proyecto** | `/home/opc/proyecto/turnobot` | Ubicación del proyecto en OCI |

### Comandos de Diagnóstico y Conexión (PowerShell)

**1. Prueba de conectividad TCP (puerto 22 SSH):**
```powershell
Test-NetConnection 161.153.202.182 -Port 22
```

**2. Conexión SSH con ruta de clave configurable:**
```powershell
ssh -i "$env:USERPROFILE\.ssh\RUTA_DE_LA_CLAVE_OCI" opc@161.153.202.182
```

> [!NOTE]
> **Ruta de clave privada OCI**:
> La ruta vigente de la clave OCI está **pendiente de confirmar**. No inventarla ni registrarla en este documento compartido.
> **Estado de OCI**: OCI puede conservar timers auxiliares (ej. de backup o sincronización). No se debe desactivar ni modificar sin confirmación explícita de cutover y datos.

---

## 3. Guía de Solución de Problemas (Troubleshooting)

Al ejecutar diagnósticos de conexión o intentar acceder por SSH, distinguir con precisión la causa raíz del fallo:

| Síntoma | Diagnóstico (`Test-NetConnection` / `ssh -v`) | Causa Raíz Probable | Acción Correctiva |
|---|---|---|---|
| **Timeout TCP** | `TcpTestSucceeded : False` tras varios segundos de espera | 1. Security Group (AWS) o Security List (OCI) no autoriza la IP pública actual del operador.<br>2. Instancia detenida o apagada.<br>3. IP pública modificada (si no se asignó Elastic IP / Reserved IP). | Verificar estado de la instancia en la consola web de AWS/OCI. Revisar reglas de entrada (Inbound) del Security Group para el puerto 22 desde la IP pública del operador (`curl.exe ifconfig.me`). |
| **Conexión rechazada (Connection Refused)** | `TcpTestSucceeded : False` inmediato (RST packet) | La instancia está encendida pero el servicio `sshd` no está escuchando en el puerto 22 o el firewall del SO (`ufw` / `iptables`) rechaza el paquete. | Conectar vía AWS EC2 Instance Connect / OCI Cloud Shell / Serial Console para revisar `systemctl status sshd`. |
| **Error de autenticación / Clave (Permission denied)** | TCP conecta (`TcpTestSucceeded : True`), pero SSH responde: `Permission denied (publickey)` | 1. Ruta de clave incorrecta o archivo `.pem`/`.key` inexistente.<br>2. Clave pública no presente en `~/.ssh/authorized_keys` del servidor.<br>3. Usuario incorrecto (ej. usar `root` u `opc` en AWS en vez de `ubuntu`).<br>4. Permisos inseguros en la clave privada (en Linux/Mac requiere `chmod 400`, en Windows restringir ACL a usuario actual). | Verificar que el archivo de clave existe localmente, revisar permisos y confirmar usuario SSH (`ubuntu` para AWS, `opc` para OCI). Usar `ssh -vvv` para depurar qué clave se está ofreciendo. |
| **Host key verification failed** | Advertencia `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!` | La IP fue reasignada a otro servidor o se recreó la instancia SSH host key. | Si se recreó legítimamente el servidor, actualizar `known_hosts` con `ssh-keygen -R <IP>`. Si no fue intencional, investigar posible conflicto de IP. |

---

## 4. Referencias y Documentación Relacionada

- [AGENTS.md](../../AGENTS.md): Directrices generales para agentes de IA y normas de seguridad.
- [DEPLOYMENT.md](../../DEPLOYMENT.md): Guía de despliegue, configuración de entorno, systemd y base de datos.
- [CUTOVER_PROCEDURE.md](../../CUTOVER_PROCEDURE.md): Procedimiento de backup, migración y verificación PostgreSQL.
- [docs/operations/SERVERS.local.example.md](SERVERS.local.example.md): Plantilla para configuración de rutas de claves locales.

