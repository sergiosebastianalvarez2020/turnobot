# Configuración Local de Rutas de Claves SSH (Plantilla)

Copiá este archivo a `SERVERS.local.md` para guardar las rutas locales de tus claves privadas sin subirlas al repositorio (este archivo `SERVERS.local.md` está ignorado en `.gitignore`).

> [!CAUTION]
> **NUNCA pegues el contenido de tu clave privada (.pem / .key) en este archivo.**
> Solo documentá la ruta absoluta o relativa en tu sistema de archivos local.

---

## Rutas Locales de Claves

### AWS EC2 (Producción)
- **Ruta de clave privada**: `C:\Users\TU_USUARIO\.ssh\tu_clave_aws.pem`
- **Comando SSH directo**:
  ```powershell
  ssh -i "C:\Users\TU_USUARIO\.ssh\tu_clave_aws.pem" ubuntu@18.191.0.23
  ```

### OCI (Servidor Anterior)
- **Ruta de clave privada**: `C:\Users\TU_USUARIO\.ssh\tu_clave_oci.key`
- **Comando SSH directo**:
  ```powershell
  ssh -i "C:\Users\TU_USUARIO\.ssh\tu_clave_oci.key" opc@161.153.202.182
  ```

