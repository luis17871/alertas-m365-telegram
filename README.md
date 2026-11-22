# Alertas Microsoft 365 y Azure a Telegram

Monitoreo automático de incidencias de **Microsoft 365** y **Azure** con
alertas al grupo de Telegram **Alertas 365**, **cada 15 minutos** con
GitHub Actions.

## Fuentes

1. **Página oficial** `https://status.cloud.microsoft/` (scraping headless):
   mantiene el estado actual como máquina de estados — alerta al aparecer,
   **EL INCIDENTE CONTINÚA** cada `RECORDAR_MIN` min (def. 60) mientras siga,
   y **RESTABLECIDO** al resolverse.
2. **RSS oficial de Azure Status** — capta caídas públicas de Azure que la
   página agregada a veces no refleja.
3. **RSS del admin center de Microsoft 365** (`status.cloud.microsoft/api/feed/mac`).
4. **(Opcional) Microsoft Graph Service Communications API** — incidentes del
   **tenant** y avisos del **Centro de mensajes / seguridad** (p. ej. una
   brecha tipo Entra Connect Health, que **no** aparece en páginas públicas).
   Se activa solo al cargar los secretos `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`
   y `GRAPH_CLIENT_SECRET` (ver abajo). Sin ellos, este bloque no corre.

Las fuentes de eventos (RSS/Graph) **deduplican por id**: cada incidente se
alerta una sola vez.

## Catalogación

Cada alerta lleva dos ejes:

- **Categoría**: 🔧 Servicio | 🔐 Seguridad
- **Severidad**: 🔴 crítica (interrupción) | 🟡 media (degradación) |
  🟢 restablecido / sin incidentes | ℹ️ informativa

Todas las horas en **hora de Ecuador (UTC-5)**. El historial ya comunicado se
guarda en `state.json`.

## Configuración

Secreto requerido en el repositorio
(Settings → Secrets and variables → Actions → New repository secret):

- `TELEGRAM_BOT_TOKEN`: token del bot `@M365_status_bot`.

El grupo de destino es el chat `-5566320217` ("Alertas 365") y está
definido en el flujo (`.github/workflows/m365-telegram-alerts.yml`).

### Activar las alertas del tenant (Graph, opcional)

Para captar incidentes de tu tenant y avisos de seguridad del Centro de
mensajes (lo que el propio aviso dice verificar por Azure Service Health):

1. En Entra ID, registra una aplicación y crea un *client secret*.
2. Concede **permisos de aplicación** `ServiceHealth.Read.All` y
   `ServiceMessage.Read.All`, y da *grant admin consent*.
3. Agrega como secretos del repositorio: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`
   y `GRAPH_CLIENT_SECRET`.

Con esos tres secretos cargados, el bloque Graph se activa automáticamente en
la siguiente corrida.

## Probar

Actions → "Alertas Microsoft 365 y Azure a Telegram" → Run workflow →
`test_post: true`. Envía un mensaje de prueba al grupo sin modificar
`state.json`.
