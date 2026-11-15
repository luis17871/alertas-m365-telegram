#!/usr/bin/env python3
"""Monitoreo de incidencias de Microsoft 365 y Azure con alertas a Telegram.

Lee la pagina oficial https://status.cloud.microsoft/ con un navegador
headless (la pagina es JavaScript) y publica en el grupo de Telegram cuando:
  - aparece un incidente nuevo o cambia (nuevo estado / titulo),
  - un incidente activo SIGUE activo (recordatorio periodico, cada
    RECORDAR_MIN minutos; por defecto 60),
  - un incidente se restablece (pasa a sin incidentes).
Sin novedad y sin recordatorio pendiente, no publica nada.

Reglas (pedidas por Luis):
- Solo alertas de Microsoft 365 y Azure, tono formal, detalladas y graficas.
- Se envian TODAS las alertas criticas y medias. Al inicio del mensaje va
  un circulo de color con la severidad: 🔴 critica, 🟡 media,
  🟢 restablecido / sin incidentes.
- Todas las horas en hora de Ecuador (UTC-5), etiquetadas.
- El estado se guarda en state.json; la primera corrida establece la base
  sin publicar, salvo que el estatus o el titulo ya hayan cambiado respecto
  a la semilla (en ese caso si publica).

Deteccion (corregida 14/11/2026): el estado por defecto es "sin incidentes".
Solo se marca incidente cuando la pagina muestra los banners oficiales
"Service interruption" o "Service degradation". El texto de ayuda fijo del
panel de Azure ("Go to Azure Service Health... RSS") ya NO se confunde con un
incidente.

Variables de entorno:
- TELEGRAM_BOT_TOKEN (secreto del repositorio)
- TELEGRAM_CHAT_ID (por defecto -5566320217, grupo "Alertas 365")
- TEST_POST=true: envia un mensaje de prueba sin tocar state.json
- RECORDAR_MIN: minutos entre recordatorios de un incidente activo (def. 60)
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

STATUS_URL = "https://status.cloud.microsoft/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
ECUADOR = ZoneInfo("America/Guayaquil")

TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-5566320217")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TEST_POST = os.environ.get("TEST_POST", "false").lower() == "true"
RECORDAR_MIN = int(os.environ.get("RECORDAR_MIN", "60"))

ACTIVE_STATUSES = ("interruption", "degradation", "incident")

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}


def load_state():
    with open(STATE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def fetch_status_page():
    """Devuelve el texto visible de la pagina oficial de estado."""
    last_error = None
    for attempt in range(2):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(STATUS_URL, timeout=60000)
                page.wait_for_selector("text=Service Health Status", timeout=60000)
                page.wait_for_timeout(5000)
                try:
                    btn = page.get_by_text("Show details", exact=False).first
                    if btn and btn.is_visible(timeout=5000):
                        btn.click(timeout=5000)
                        page.wait_for_timeout(3000)
                except Exception:
                    pass  # sin detalles expandidos igual se lee el estado
                body = page.inner_text("body")
                browser.close()
                return body
        except Exception as exc:  # noqa: BLE001 - reintento simple
            last_error = exc
            print(f"Intento {attempt + 1} fallido: {exc}", flush=True)
    raise RuntimeError(f"No se pudo leer la pagina oficial: {last_error}")


def section(body, start, ends):
    i = body.find(start)
    if i < 0:
        return ""
    j = len(body)
    for marker in ends:
        k = body.find(marker, i + len(start))
        if 0 < k < j:
            j = k
    return body[i:j]


def parse_utc_times(text):
    """Extrae horas en UTC del texto y las devuelve como datetimes UTC."""
    found = []
    pattern = re.compile(
        r"(?:(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),\s+(\d{4})\s+)?"
        r"(\d{1,2}):(\d{2})\s*(AM|PM)?\s*UTC",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        month_name, day, year, hour, minute, ampm = m.groups()
        hour = int(hour)
        minute = int(minute)
        if ampm:
            ampm = ampm.upper()
            if ampm == "PM" and hour != 12:
                hour += 12
            if ampm == "AM" and hour == 12:
                hour = 0
        now = datetime.now(timezone.utc)
        year = int(year) if year else now.year
        month = MONTHS.get((month_name or "").capitalize(), now.month)
        day = int(day) if day else now.day
        try:
            found.append(datetime(year, month, day, hour, minute, tzinfo=timezone.utc))
        except ValueError:
            continue
    return found


def to_ecuador(dt_utc):
    return dt_utc.astimezone(ECUADOR).strftime("%H:%M")


def find_next_update(text):
    m = re.search(
        r"[Nn]ext update[^.]{0,120}?(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*UTC)",
        text,
    )
    if not m:
        m = re.search(
            r"[Uu]pdate by[^.]{0,120}?(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*UTC)",
            text,
        )
    if not m:
        return None
    times = parse_utc_times(m.group(0))
    return to_ecuador(times[0]) if times else None


def detect_status(text):
    """Estado del servicio a partir de los banners oficiales.

    Por defecto 'ok'. Solo marca incidente con las frases oficiales
    'Service interruption' / 'Service degradation'. El texto de ayuda fijo
    del panel (enlaces a Service Health, RSS) nunca cuenta como incidente.
    """
    if "Service interruption" in text:
        return "interruption"
    if "Service degradation" in text:
        return "degradation"
    return "ok"


def extract_title(text, status):
    """Descripcion corta del incidente; vacio si esta sano o es boilerplate."""
    if status == "ok":
        return ""
    m = re.search(r"Users may (?:be unable to|be experiencing|experience)[^.]*", text)
    if not m:
        return ""
    title = clean(m.group(0))
    # Evita capturar el texto de ayuda del panel.
    if "Service Health" in title or "RSS" in title:
        return ""
    return title


def parse_service(body, service):
    if service == "m365":
        text = section(
            body,
            "Microsoft 365 (Business or Enterprise)",
            ["Microsoft Azure", "Power Platform Admin Center"],
        )
    else:
        text = section(body, "Microsoft Azure", ["Power Platform Admin Center"])
    text = clean(text)
    status = detect_status(text)
    title = extract_title(text, status)
    times = parse_utc_times(text)
    last_update = to_ecuador(times[0]) if times else None
    next_update = find_next_update(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "status": status,
        "title": title,
        "last_update_ec": last_update,
        "next_update_ec": next_update,
        "details_hash": digest,
        "excerpt": clean(text[:600]),
    }


STATUS_LABEL = {
    "degradation": "DEGRADACIÓN DEL SERVICIO",
    "interruption": "INTERRUPCIÓN DEL SERVICIO",
    "incident": "INCIDENCIA ACTIVA",
    "restored": "RESTABLECIDO",
    "ok": "SIN INCIDENTES",
}
# Severidad (pedida por Luis el 14/11/2026): se envian TODAS las alertas
# criticas y medias. El circulo de color va al inicio del mensaje e indica
# la severidad: 🔴 critica (interrupcion del servicio / incidencia),
# 🟡 media (degradacion del servicio), 🟢 restablecido / sin incidentes.
SEVERITY = {
    "degradation": ("🟡", "MEDIA"),
    "interruption": ("🔴", "CRÍTICA"),
    "incident": ("🔴", "CRÍTICA"),
    "restored": ("🟢", None),
    "ok": ("🟢", None),
}


def compose(service, effective_status, info, prev, reminder=False):
    name = "MICROSOFT 365" if service == "m365" else "MICROSOFT AZURE"
    now_ec = datetime.now(ECUADOR).strftime("%H:%M")
    icon, severity = SEVERITY[effective_status]
    if reminder and severity:
        headline = (
            f"{icon} EL INCIDENTE CONTINÚA — {name} — {STATUS_LABEL[effective_status]}"
        )
    elif severity:
        headline = f"{icon} ALERTA {severity} — {name} — {STATUS_LABEL[effective_status]}"
    else:
        headline = f"{icon} {STATUS_LABEL[effective_status]} — {name}"
    lines = [
        headline,
        f"🕐 Hora Ecuador: {now_ec} (UTC-5)",
    ]
    ids = ""
    if prev.get("title") and prev.get("title") == info["title"]:
        ids = prev.get("incident_ids", "")
    if ids:
        lines.append(f"📌 Incidente: {ids}")
    lines.append(f"📋 Corresponde a: {info['title'] or 'revise el detalle en la página oficial.'}")
    if info["last_update_ec"]:
        summary = info["excerpt"][:300]
        lines.append(
            f"🕔 Última actualización oficial: {info['last_update_ec']} hora Ecuador (UTC-5) — {summary}"
        )
    else:
        lines.append("🕔 Última actualización oficial: ver detalle en la página oficial.")
    lines.append(
        f"⏭ Próxima actualización prevista: "
        f"{info['next_update_ec'] + ' hora Ecuador (UTC-5)' if info['next_update_ec'] else 'no indicada'}"
    )
    lines.append("🔗 Fuente: https://status.cloud.microsoft/")
    return "\n".join(lines)


def decide(info, prev, now_epoch):
    """Decide la accion para un servicio.

    Devuelve (accion, effective, reminder):
      accion   : 'base' (fija base sin publicar), 'send' o 'none'
      effective: estado a mostrar ('restored' si se restablecio)
      reminder : True si es recordatorio de incidente que continua
    """
    baseline = "details_hash" not in prev
    status_changed = info["status"] != prev.get("status")
    title_changed = info["title"] != prev.get("title", "")
    active = info["status"] in ACTIVE_STATUSES

    if baseline and not status_changed and not title_changed:
        return "base", info["status"], False

    # Cambio real de estado o de titulo -> publicar.
    if status_changed or title_changed:
        effective = info["status"]
        if prev.get("status") in ACTIVE_STATUSES and info["status"] == "ok":
            effective = "restored"
        return "send", effective, False

    # Sin cambios, pero sigue activo: recordatorio periodico.
    if active:
        last = prev.get("last_alert_epoch", 0)
        if now_epoch - last >= RECORDAR_MIN * 60:
            return "send", info["status"], True

    return "none", info["status"], False


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: falta el secreto TELEGRAM_BOT_TOKEN en el repositorio.", file=sys.stderr)
        sys.exit(1)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram devolvió un error: {data}")
    print(f"Mensaje enviado a Telegram (message_id {data['result']['message_id']}).")
    return data


def main():
    state = load_state()
    if TEST_POST:
        now_ec = datetime.now(ECUADOR).strftime("%H:%M")
        send_telegram(
            "🔧 PRUEBA DEL MONITOREO — MICROSOFT 365 / AZURE\n"
            f"🕐 Hora Ecuador: {now_ec} (UTC-5)\n"
            "Este es un mensaje de prueba del monitoreo automático "
            "(GitHub Actions). Las alertas reales llegan solo cuando hay "
            "una novedad en Microsoft 365 o Azure."
        )
        print("Prueba enviada. state.json no se modificó.")
        return 0

    body = fetch_status_page()
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    changed = False
    for service in ("m365", "azure"):
        info = parse_service(body, service)
        prev = state.get(service, {})
        action, effective, reminder = decide(info, prev, now_epoch)

        if action == "base":
            print(f"{service}: base inicial establecida, sin publicar.")
            state[service] = {**prev, **info}
            changed = True
            continue

        if action == "send":
            message = compose(service, effective, info, prev, reminder=reminder)
            tag = "recordatorio" if reminder else "novedad"
            print(f"{service}: {tag} detectada, publicando alerta.")
            print(message)
            send_telegram(message)
            stored = {**prev, **info}
            stored["status"] = info["status"]
            stored["last_alert_epoch"] = now_epoch
            if effective == "restored":
                stored["title"] = ""
            state[service] = stored
            changed = True
        else:
            print(f"{service}: sin novedad.")
            # Mantiene hash/estado al dia sin publicar.
            merged = {**prev, **info}
            merged["status"] = info["status"]
            if merged != prev:
                state[service] = merged
                changed = True

    if changed:
        save_state(state)
        print("state.json actualizado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
