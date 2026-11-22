#!/usr/bin/env python3
"""Monitoreo de incidencias de Microsoft 365 y Azure con alertas a Telegram.

Fuentes (cada alerta se cataloga por CATEGORIA y SEVERIDAD):

1) Pagina oficial https://status.cloud.microsoft/ (scraping headless). Mantiene
   el estado actual de Microsoft 365 y Azure como maquina de estados:
   alerta al aparecer, recordatorio "EL INCIDENTE CONTINUA" cada RECORDAR_MIN
   minutos mientras siga, y "RESTABLECIDO" al resolverse.

2) RSS oficial de Azure Status  -> capta caidas publicas de Azure que la pagina
   agregada a veces no refleja.

3) RSS del Centro de mensajes/estado de Microsoft 365 (admin center).

4) (Opcional, se activa solo con credenciales) Microsoft Graph Service
   Communications API -> incidentes de TU tenant y avisos del Centro de
   mensajes / seguridad (p.ej. una brecha tipo Entra Connect Health). Requiere
   registrar una app en Entra con permisos de aplicacion ServiceHealth.Read.All
   y ServiceMessage.Read.All, y cargar GRAPH_TENANT_ID / GRAPH_CLIENT_ID /
   GRAPH_CLIENT_SECRET como secretos del repositorio. Sin esas variables, este
   bloque no se ejecuta.

Catalogacion:
- Categoria: 🔧 Servicio  |  🔐 Seguridad
- Severidad: 🔴 critica  |  🟡 media  |  🟢 restablecido  |  ℹ️ informativa

Reglas (pedidas por Luis): solo Microsoft 365 y Azure, tono formal, horas en
hora de Ecuador (UTC-5) etiquetadas, se envian todas las alertas criticas y
medias. Cada fuente de eventos (RSS/Graph) deduplica por id para no repetir.

Variables de entorno:
- TELEGRAM_BOT_TOKEN (secreto del repositorio)
- TELEGRAM_CHAT_ID (por defecto -5566320217, grupo "Alertas 365")
- TEST_POST=true: envia un mensaje de prueba sin tocar state.json
- RECORDAR_MIN: minutos entre recordatorios de incidente activo (def. 60)
- GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET: activan el bloque 4
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

try:
    import feedparser
except Exception:  # noqa: BLE001
    feedparser = None

STATUS_URL = "https://status.cloud.microsoft/"
# RSS oficiales (verificados nov-2026). Se intentan en orden hasta que uno responda.
AZURE_RSS = [
    "https://rssfeed.azure.status.microsoft/en-us/status/feed/",
    "https://azure.status.microsoft/en-us/status/feed/",
    "https://azure.microsoft.com/en-us/status/feed/",
]
M365_RSS = [
    "https://status.cloud.microsoft/api/feed/mac",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
ECUADOR = ZoneInfo("America/Guayaquil")

TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-5566320217")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TEST_POST = os.environ.get("TEST_POST", "false").lower() == "true"
RECORDAR_MIN = int(os.environ.get("RECORDAR_MIN", "60"))

GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")

ACTIVE_STATUSES = ("interruption", "degradation", "incident")
SEEN_MAX = 300  # ids recordados para no repetir alertas de eventos (RSS/Graph)

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}

STATUS_LABEL = {
    "degradation": "DEGRADACIÓN DEL SERVICIO",
    "interruption": "INTERRUPCIÓN DEL SERVICIO",
    "incident": "INCIDENCIA ACTIVA",
    "restored": "RESTABLECIDO",
    "ok": "SIN INCIDENTES",
    "info": "AVISO",
}
# 🔴 critica (interrupcion/incidencia), 🟡 media (degradacion),
# 🟢 restablecido/sin incidentes, ℹ️ informativa.
SEVERITY = {
    "degradation": ("🟡", "MEDIA"),
    "interruption": ("🔴", "CRÍTICA"),
    "incident": ("🔴", "CRÍTICA"),
    "restored": ("🟢", None),
    "ok": ("🟢", None),
    "info": ("ℹ️", "INFORMATIVA"),
}
CATEGORY_LABEL = {
    "servicio": "🔧 Servicio",
    "seguridad": "🔐 Seguridad",
}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("seen", [])
    return data


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    return clean(re.sub(r"<[^>]+>", " ", text))


def now_ec():
    return datetime.now(ECUADOR).strftime("%H:%M")


def fetch_page_text(url, limit=700):
    """Lee la pagina publica del incidente y devuelve un resumen legible.

    Prefiere la meta-descripcion; si no, toma el texto visible del body.
    Best-effort: ante cualquier fallo devuelve cadena vacia (no rompe la alerta).
    """
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "alertas-m365"})
        r.raise_for_status()
        html = r.text
        m = re.search(
            r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']+)',
            html, re.IGNORECASE,
        )
        if m and len(clean(m.group(1))) >= 40:
            return clean(m.group(1))[:limit]
        body = re.search(r"(?is)<body[^>]*>(.*)</body>", html)
        text = strip_html(body.group(1) if body else html)
        return text[:limit]
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo leer la pagina del incidente {url}: {exc}", flush=True)
        return ""


# --------------------------------------------------------------------------- #
# Fuente 1: scraping de la pagina oficial (maquina de estados)                 #
# --------------------------------------------------------------------------- #

def fetch_status_page():
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
                    pass
                body = page.inner_text("body")
                browser.close()
                return body
        except Exception as exc:  # noqa: BLE001
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
    m = re.search(r"[Nn]ext update[^.]{0,120}?(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*UTC)", text)
    if not m:
        m = re.search(r"[Uu]pdate by[^.]{0,120}?(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*UTC)", text)
    if not m:
        return None
    times = parse_utc_times(m.group(0))
    return to_ecuador(times[0]) if times else None


def detect_status(text):
    if "Service interruption" in text:
        return "interruption"
    if "Service degradation" in text:
        return "degradation"
    return "ok"


def extract_title(text, status):
    if status == "ok":
        return ""
    m = re.search(r"Users may (?:be unable to|be experiencing|experience)[^.]*", text)
    if not m:
        return ""
    title = clean(m.group(0))
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


def compose(service, effective_status, info, prev, reminder=False, category="servicio"):
    name = "MICROSOFT 365" if service == "m365" else "MICROSOFT AZURE"
    icon, severity = SEVERITY[effective_status]
    if reminder and severity:
        headline = f"{icon} EL INCIDENTE CONTINÚA — {name} — {STATUS_LABEL[effective_status]}"
    elif severity:
        headline = f"{icon} ALERTA {severity} — {name} — {STATUS_LABEL[effective_status]}"
    else:
        headline = f"{icon} {STATUS_LABEL[effective_status]} — {name}"
    lines = [
        headline,
        f"🏷 Categoría: {CATEGORY_LABEL[category]}",
        f"🕐 Hora Ecuador: {now_ec()} (UTC-5)",
    ]
    lines.append(f"📋 Corresponde a: {info['title'] or 'revise el detalle en la página oficial.'}")
    if info.get("excerpt"):
        lines.append(f"📝 De qué se trata: {info['excerpt'][:600]}")
    if info["last_update_ec"]:
        lines.append(f"🕔 Última actualización oficial: {info['last_update_ec']} hora Ecuador (UTC-5)")
    else:
        lines.append("🕔 Última actualización oficial: ver detalle en la página oficial.")
    nxt = info["next_update_ec"]
    lines.append(f"⏭ Próxima actualización prevista: {nxt + ' hora Ecuador (UTC-5)' if nxt else 'no indicada'}")
    lines.append("🔗 Fuente: https://status.cloud.microsoft/")
    return "\n".join(lines)


def decide(info, prev, now_epoch):
    baseline = "details_hash" not in prev
    status_changed = info["status"] != prev.get("status")
    title_changed = info["title"] != prev.get("title", "")
    active = info["status"] in ACTIVE_STATUSES
    if baseline and not status_changed and not title_changed:
        return "base", info["status"], False
    if status_changed or title_changed:
        effective = info["status"]
        if prev.get("status") in ACTIVE_STATUSES and info["status"] == "ok":
            effective = "restored"
        return "send", effective, False
    if active:
        last = prev.get("last_alert_epoch", 0)
        if now_epoch - last >= RECORDAR_MIN * 60:
            return "send", info["status"], True
    return "none", info["status"], False


# --------------------------------------------------------------------------- #
# Fuentes 2 y 3: feeds RSS                                                     #
# --------------------------------------------------------------------------- #

def classify_event(title, summary):
    """Devuelve (severidad, categoria) a partir del texto de un item de feed."""
    t = f"{title} {summary}".lower()
    category = "seguridad" if any(k in t for k in (
        "security", "breach", "vulnerab", "advisory", "compromis", "exposed", "exploit",
    )) else "servicio"
    if any(k in t for k in ("resolved", "mitigated", "post incident", "pir ", "recovered",
                            "has been restored", "restored", "healthy")):
        return "restored", category
    if any(k in t for k in ("interruption", "unavailable", "outage", " down", "not able",
                            "unable to", "failure", "failing", "error rate")):
        return "interruption", category
    if any(k in t for k in ("degrad", "latency", "intermittent", "slow", "delays", "impacted",
                            "investigating", "experiencing")):
        return "degradation", category
    return "info", category


def fetch_feed(urls):
    """Primer feed de la lista que responda; devuelve objeto feedparser o None."""
    if feedparser is None:
        print("feedparser no disponible; se omiten los feeds RSS.", flush=True)
        return None
    # Navegadores reales reciben el feed; un User-Agent tipo navegador evita
    # respuestas vacias o HTML de algunos CDN de Microsoft.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    for url in urls:
        try:
            resp = requests.get(url, timeout=30, headers=headers)
            status = resp.status_code
            ctype = resp.headers.get("content-type", "")
            parsed = feedparser.parse(resp.content)
            feed_title = parsed.feed.get("title", "") if getattr(parsed, "feed", None) else ""
            print(
                f"Feed {url}: HTTP {status}, {len(resp.content)} bytes, "
                f"ctype={ctype!r}, version={getattr(parsed, 'version', '')!r}, "
                f"bozo={getattr(parsed, 'bozo', '')}, entradas={len(parsed.entries)}, "
                f"title={feed_title!r}",
                flush=True,
            )
            resp.raise_for_status()
            if parsed.entries:
                return parsed
            # Feed valido pero vacio (sin incidente activo): no probar mas URLs.
            if feed_title or getattr(parsed, "version", ""):
                return None
        except Exception as exc:  # noqa: BLE001
            print(f"Feed fallido {url}: {exc}", flush=True)
    return None


def entry_id(entry):
    raw = entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def entry_time_ec(entry):
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    dt = datetime(*tm[:6], tzinfo=timezone.utc)
    return dt.astimezone(ECUADOR).strftime("%Y-%m-%d %H:%M")


def compose_feed(name, severity, category, title, summary, link, when_ec):
    icon, sev = SEVERITY[severity]
    if sev:
        headline = f"{icon} ALERTA {sev} — {name} — {STATUS_LABEL[severity]}"
    else:
        headline = f"{icon} {STATUS_LABEL[severity]} — {name}"
    lines = [
        headline,
        f"🏷 Categoría: {CATEGORY_LABEL[category]}",
        f"🕐 Hora Ecuador: {now_ec()} (UTC-5)",
        f"📋 {clean(title)}",
    ]
    if summary:
        lines.append(f"📝 De qué se trata: {clean(summary)[:700]}")
    else:
        lines.append("📝 De qué se trata: ver el detalle en la fuente.")
    if when_ec:
        lines.append(f"🕔 Publicado: {when_ec} hora Ecuador (UTC-5)")
    lines.append(f"🔗 Fuente: {link}")
    return "\n".join(lines)


def process_feed(urls, name, state, send):
    parsed = fetch_feed(urls)
    if not parsed:
        return
    seen = state["seen"]
    # Procesa de mas antiguo a mas nuevo para respetar el orden cronologico.
    for entry in reversed(parsed.entries[:30]):
        eid = entry_id(entry)
        if eid in seen:
            continue
        title = entry.get("title", "")
        summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "") or urls[0]
        # Si la descripcion del feed viene corta, lee la pagina del incidente.
        if len(summary) < 80 and link.startswith("http"):
            page_text = fetch_page_text(link)
            if len(page_text) > len(summary):
                summary = page_text
        severity, category = classify_event(title, summary)
        when_ec = entry_time_ec(entry)
        msg = compose_feed(name, severity, category, title, summary, link, when_ec)
        print(f"{name} [feed]: nuevo item, publicando. ({severity}/{category})")
        send(msg)
        seen.append(eid)
    # Limita el tamano del historial de ids.
    if len(seen) > SEEN_MAX:
        del seen[:-SEEN_MAX]


# --------------------------------------------------------------------------- #
# Fuente 4 (opcional): Microsoft Graph Service Communications API              #
# --------------------------------------------------------------------------- #

def graph_enabled():
    return bool(GRAPH_TENANT_ID and GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET)


def graph_token():
    url = f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": GRAPH_CLIENT_ID,
        "client_secret": GRAPH_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def process_graph(state, send):
    """Incidentes de servicio y avisos del Centro de mensajes del tenant.

    Se activa solo si estan GRAPH_TENANT_ID/CLIENT_ID/CLIENT_SECRET. Requiere
    permisos de aplicacion ServiceHealth.Read.All y ServiceMessage.Read.All.
    """
    if not graph_enabled():
        return
    try:
        token = graph_token()
    except Exception as exc:  # noqa: BLE001
        print(f"Graph: no se pudo obtener token: {exc}", flush=True)
        return
    headers = {"Authorization": f"Bearer {token}"}
    seen = state["seen"]
    endpoints = [
        # (url, nombre, categoria por defecto)
        ("https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/issues", "MICROSOFT 365 / AZURE", "servicio"),
        ("https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/messages", "CENTRO DE MENSAJES", "seguridad"),
    ]
    for url, name, default_cat in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            items = r.json().get("value", [])
        except Exception as exc:  # noqa: BLE001
            print(f"Graph {url}: {exc}", flush=True)
            continue
        for item in items[:30]:
            gid = "graph:" + str(item.get("id", ""))
            key = hashlib.sha256(gid.encode("utf-8")).hexdigest()[:16]
            if key in seen:
                continue
            title = item.get("title", "")
            body = item.get("body", {}).get("content", "") if isinstance(item.get("body"), dict) else ""
            summary = clean(re.sub(r"<[^>]+>", " ", body))
            status = item.get("status", "")
            severity, category = classify_event(title + " " + status, summary)
            if default_cat == "seguridad":
                category = "seguridad"
            link = item.get("webUrl", "https://admin.microsoft.com/")
            msg = compose_feed(name, severity, category, title, summary, link, None)
            print(f"Graph [{name}]: nuevo item {gid} ({severity}/{category})")
            send(msg)
            seen.append(key)
    if len(seen) > SEEN_MAX:
        del seen[:-SEEN_MAX]


# --------------------------------------------------------------------------- #
# Telegram                                                                     #
# --------------------------------------------------------------------------- #

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: falta el secreto TELEGRAM_BOT_TOKEN en el repositorio.", file=sys.stderr)
        sys.exit(1)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=30)
        resp.raise_for_status()
    except requests.HTTPError:
        # No exponer la URL (contiene el token) en los logs publicos de Actions.
        raise RuntimeError(f"Telegram respondió HTTP {resp.status_code} al enviar el mensaje.")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram devolvió error: {data.get('description', data)}")
    print(f"Mensaje enviado a Telegram (message_id {data['result']['message_id']}).")
    return data


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main():
    state = load_state()

    if TEST_POST:
        send_telegram(
            "🔧 PRUEBA DEL MONITOREO — MICROSOFT 365 / AZURE\n"
            f"🕐 Hora Ecuador: {now_ec()} (UTC-5)\n"
            "Mensaje de prueba del monitoreo automático (GitHub Actions). "
            "Fuentes: página oficial, RSS de Azure Status, RSS de M365 admin "
            "center" + (" y Microsoft Graph (tenant)" if graph_enabled() else "") + ". "
            "Las alertas reales llegan solo cuando hay una novedad, con el detalle "
            "de qué se trata y el enlace a la fuente."
        )
        # Muestra, como ejemplo, el ultimo incidente real publicado en Azure.
        parsed = fetch_feed(AZURE_RSS)
        if parsed and parsed.entries:
            e = parsed.entries[0]
            title = e.get("title", "")
            summary = strip_html(e.get("summary", "") or e.get("description", ""))
            link = e.get("link", "") or AZURE_RSS[0]
            if len(summary) < 80 and link.startswith("http"):
                pt = fetch_page_text(link)
                if len(pt) > len(summary):
                    summary = pt
            sev, cat = classify_event(title, summary)
            sample = compose_feed("MICROSOFT AZURE (EJEMPLO)", sev, cat, title, summary, link, entry_time_ec(e))
            send_telegram("🔎 Ejemplo del formato con el último incidente publicado en Azure Status:\n\n" + sample)
        print("Prueba enviada. state.json no se modificó.")
        return 0

    changed = False

    # Fuente 1: pagina oficial (maquina de estados). No es fatal si falla.
    try:
        body = fetch_status_page()
        now_epoch = int(datetime.now(timezone.utc).timestamp())
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
                msg = compose(service, effective, info, prev, reminder=reminder, category="servicio")
                print(f"{service}: {'recordatorio' if reminder else 'novedad'} detectada, publicando.")
                send_telegram(msg)
                stored = {**prev, **info}
                stored["status"] = info["status"]
                stored["last_alert_epoch"] = now_epoch
                if effective == "restored":
                    stored["title"] = ""
                state[service] = stored
                changed = True
            else:
                print(f"{service}: sin novedad.")
                merged = {**prev, **info}
                merged["status"] = info["status"]
                if merged != prev:
                    state[service] = merged
                    changed = True
    except Exception as exc:  # noqa: BLE001
        print(f"Fuente pagina oficial fallida (se continua con los feeds): {exc}", flush=True)

    before = len(state["seen"])

    # Fuentes 2 y 3: feeds RSS.
    process_feed(AZURE_RSS, "MICROSOFT AZURE", state, send_telegram)
    process_feed(M365_RSS, "MICROSOFT 365", state, send_telegram)

    # Fuente 4: Graph (solo si hay credenciales).
    process_graph(state, send_telegram)

    if len(state["seen"]) != before:
        changed = True

    if changed:
        save_state(state)
        print("state.json actualizado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
