# Alertas Microsoft 365 y Azure a Telegram

Monitoreo automático de incidencias de **Microsoft 365** y **Azure** con
alertas al grupo de Telegram **Alertas 365**.

- Revisa la página oficial de estado (https://status.cloud.microsoft/)
  **cada 15 minutos** con GitHub Actions.
- Publica en el grupo **solo cuando hay una novedad**: incidente nuevo,
  actualización oficial nueva o cambio de estatus. Sin novedad, no publica nada.
- Cada alerta es formal, detallada y gráfica, con el estatus marcado y
  **todas las horas en hora de Ecuador (UTC-5)**.
- El estado ya comunicado se guarda en `state.json`; el flujo lo actualiza
  solo cuando publica una alerta.

## Configuración

Secreto requerido en el repositorio
(Settings → Secrets and variables → Actions → New repository secret):

- `TELEGRAM_BOT_TOKEN`: token del bot `@M365_status_bot`.

El grupo de destino es el chat `-5566320217` ("Alertas 365") y está
definido en el flujo (`.github/workflows/m365-telegram-alerts.yml`).

## Probar

Actions → "Alertas Microsoft 365 y Azure a Telegram" → Run workflow →
`test_post: true`. Envía un mensaje de prueba al grupo sin modificar
`state.json`.
