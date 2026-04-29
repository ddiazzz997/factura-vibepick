"""Bot de Telegram para generar facturas Vibepick en PDF (USD).

Uso desde Telegram:
- Envía texto con este formato:
    Nombre del cliente
    Dirección postal
    Servicio · 1234
    Otro servicio · 567
- Opcional: adjunta una foto del logo/avatar del cliente y pon el
  mismo formato como pie de foto. La imagen se usará como avatar
  circular del cliente en la factura.

El bot devuelve un PDF en una sola página.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Template
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

HERE = Path(__file__).parent
TEMPLATE_PATH = HERE / "factura.template.html"
ASSETS_DIR = HERE / "assets"
STATE_PATH = HERE / "state.json"
OUTPUT_DIR = HERE / "out"
OUTPUT_DIR.mkdir(exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

MESES_ES = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
]

logging.basicConfig(
    format="%(asctime)s · %(levelname)s · %(message)s",
    level=logging.INFO,
)
for noisy in ("httpx", "httpcore", "telegram.ext", "telegram.Bot", "telegram"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("vibepick-bot")


# ---------- Estado persistente ----------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"counter": 41, "owner_chat_id": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------- Assets ----------

def b64_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


LOGO_B64 = b64_file(ASSETS_DIR / "logo.png")
PROFILE_B64 = b64_file(ASSETS_DIR / "profile.jpg")


# ---------- Formato ----------

def fmt_usd(amount: float) -> str:
    return "$" + f"{amount:,.2f}"


def fmt_fecha(d: datetime) -> str:
    return f"{d.day} {MESES_ES[d.month - 1]} {d.year}"


def initial(name: str) -> str:
    name = name.strip()
    return name[0].upper() if name else "?"


# ---------- Parseo del mensaje ----------

PRICE_SEP = re.compile(r"\s*[·\-|:]\s*\$?\s*([\d\.,\s]+?)\s*(?:USD|usd|\$)?\s*$")


def parse_message(text: str) -> dict:
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if len(lines) < 3:
        raise ValueError(
            "Necesito al menos 3 líneas:\n"
            "1) cliente\n2) dirección\n3+) servicios con precio"
        )
    client_name = lines[0]
    client_address = lines[1]
    items = []
    for line in lines[2:]:
        m = PRICE_SEP.search(line)
        if not m:
            raise ValueError(
                f'No encuentro el precio en: "{line}"\n'
                'Formato: descripción · 1234'
            )
        raw = m.group(1).replace(" ", "").replace(",", "")
        try:
            price = float(raw)
        except ValueError:
            raise ValueError(f'Precio inválido en: "{line}"')
        desc = line[: m.start()].strip().rstrip("·-|:").strip()
        items.append({"desc": desc, "amount": price})
    return {
        "client_name": client_name,
        "client_address": client_address,
        "items": items,
    }


# ---------- Render PDF ----------

def render_pdf(parsed: dict, invoice_number: str, client_photo_b64: str | None = None) -> Path:
    today = datetime.now()
    due = today + timedelta(days=30)
    items = [
        {**it, "amount_fmt": fmt_usd(it["amount"])} for it in parsed["items"]
    ]
    total = sum(it["amount"] for it in parsed["items"])

    template = Template(TEMPLATE_PATH.read_text())
    html = template.render(
        invoice_number=invoice_number,
        date_emit=fmt_fecha(today),
        date_due=fmt_fecha(due),
        client_name=parsed["client_name"],
        client_initial=initial(parsed["client_name"]),
        client_address=parsed["client_address"].replace("\n", "<br/>"),
        client_photo_b64=client_photo_b64,
        items=items,
        total_fmt=fmt_usd(total),
        logo_b64=LOGO_B64,
        profile_b64=PROFILE_B64,
    )

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
        html_path = Path(f.name)

    pdf_path = OUTPUT_DIR / f"{invoice_number}.pdf"
    subprocess.run(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            "--no-margins",
            "--virtual-time-budget=4000",
            f"--print-to-pdf={pdf_path}",
            f"file://{html_path}",
        ],
        check=True,
        capture_output=True,
    )
    html_path.unlink(missing_ok=True)

    info = subprocess.run(
        ["pdfinfo", str(pdf_path)], capture_output=True, text=True, check=True
    )
    pages = next(
        (int(l.split()[1]) for l in info.stdout.splitlines() if l.startswith("Pages:")),
        0,
    )
    if pages != 1:
        pdf_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"El PDF resultó con {pages} páginas. Reduce los servicios o la longitud."
        )
    return pdf_path


# ---------- Telegram ----------

WELCOME = (
    "👋 Hola, soy *Vibepick Facturas*.\n\n"
    "Envíame un mensaje con este formato:\n"
    "```\n"
    "Nombre del cliente\n"
    "Dirección postal\n"
    "Servicio · 1234\n"
    "Otro servicio · 567\n"
    "```\n"
    "💡 Opcional: adjunta una foto/logo del cliente con el mismo texto "
    "como pie de foto y la usaré como avatar.\n\n"
    "Te devuelvo el PDF en una sola página, en USD."
)


def is_authorized(state: dict, chat_id: int) -> bool:
    return state.get("owner_chat_id") == chat_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    chat_id = update.effective_chat.id
    if state.get("owner_chat_id") is None:
        state["owner_chat_id"] = chat_id
        save_state(state)
        log.info("Owner registrado: chat_id=%s", chat_id)
        await update.message.reply_text(
            "✅ Te he registrado como propietario del bot.\n\n" + WELCOME,
            parse_mode="Markdown",
        )
        return
    if not is_authorized(state, chat_id):
        await update.message.reply_text("⛔️ No autorizado.")
        return
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def _process(
    update: Update,
    text: str,
    client_photo_b64: str | None,
) -> None:
    state = load_state()
    try:
        parsed = parse_message(text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    state["counter"] = state.get("counter", 41) + 1
    save_state(state)
    invoice_number = f"VBP-{datetime.now().year}-{state['counter']:04d}"

    await update.message.reply_text(f"⚙️ Generando *{invoice_number}*…", parse_mode="Markdown")
    try:
        pdf_path = await asyncio.to_thread(render_pdf, parsed, invoice_number, client_photo_b64)
    except Exception as e:
        log.exception("Error generando PDF")
        await update.message.reply_text(f"❌ Error: {e}")
        return

    with pdf_path.open("rb") as f:
        await update.message.reply_document(
            document=f,
            filename=pdf_path.name,
            caption=f"✅ {invoice_number} · {parsed['client_name']}",
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    if not is_authorized(state, update.effective_chat.id):
        await update.message.reply_text("⛔️ No autorizado. Envía /start si eres el propietario.")
        return
    await _process(update, update.message.text or "", None)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    if not is_authorized(state, update.effective_chat.id):
        await update.message.reply_text("⛔️ No autorizado.")
        return
    caption = update.message.caption or ""
    if not caption.strip():
        await update.message.reply_text(
            "⚠️ Adjunta el texto de la factura como pie de la foto."
        )
        return
    photo = update.message.photo[-1]
    file = await photo.get_file()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = Path(f.name)
    try:
        await file.download_to_drive(tmp)
        b64 = base64.b64encode(tmp.read_bytes()).decode()
    finally:
        tmp.unlink(missing_ok=True)
    await _process(update, caption, b64)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("✗ Falta TELEGRAM_BOT_TOKEN en el entorno")
    if not TEMPLATE_PATH.exists():
        raise SystemExit(f"✗ Falta plantilla: {TEMPLATE_PATH}")
    if not Path(CHROME).exists():
        raise SystemExit(f"✗ Chrome no encontrado en {CHROME}")

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot iniciado. Esperando mensajes…")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
