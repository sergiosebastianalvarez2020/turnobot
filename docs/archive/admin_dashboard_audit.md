# Auditoría Panel Admin — TurnoBot

## Estructura
- Rutas admin: routes/register_admin.py (100+ endpoints)
- Template principal: templates/admin.html (monolítico, ~1200 líneas)
- CSS: static/admin.css
- JS: inline en admin.html
- Otros templates admin: admin_metricas, admin_usuarios, admin_conocimiento, admin_fidelizacion, admin_conversaciones, etc.

## Reutilizable
- Endpoints existentes de turnos, clientes, servicios, usuarios, métricas
- Auth/roles/multi-tenant (business_id en sesión)
- Disponibilidad y reglas de solapamiento

## Problemas UX
- Sidebar sin colapso real en desktop
- Sin bottom nav móvil
- Sin tokens de diseño consistentes (paleta clara vs dark)
- Carga monolítica

## Plan fases
1. Auditoría ✓
2. Layout/sidebar/bottom nav + design tokens dark mode
3. Dashboard
4. Agenda/turnos
5. Clientes
6. Servicios
7. Empleados
8. Estadísticas
9. Notificaciones/config
10. Integración

## Riesgos
- IDOR si se confía en business_id del cliente
- Performance t3.micro
- No deploy/prod changes
