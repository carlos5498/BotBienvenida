import os
import asyncio
import logging
import threading
import hashlib
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
    ContextTypes
)
from pymongo import MongoClient

# --- CONFIGURACIÓN ---
logging.basicConfig(level=logging.INFO)
MY_ID = int(os.getenv("MY_ID", 0))
TOKEN = os.getenv("TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
PASSWORD = "Carlos13mar"

client = MongoClient(MONGO_URL)
db = client['bot_mensajes_db']
col_grupos = db['grupos_registrados']
col_bienvenida = db['bienvenida_config']
col_ultimo_msg = db['ultimo_mensaje_bienvenida']
col_botones_data = db['botones_callback_data']
col_programado = db['mensaje_programado']

usuarios_autorizados = set()
grupos_por_usuario = {}
estado_panel = {}   # {user_id: {"chat_id": int, "step": str}}

INTERVALOS = {
    "1h": 60 * 60,
    "3h": 3 * 60 * 60,
    "6h": 6 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
}
INTERVALOS_LABEL = {"1h": "1 hora", "3h": "3 horas", "6h": "6 horas", "12h": "12 horas", "1d": "1 día"}

# Texto recordatorio del comando /principal (grupos de temas / forum)
TIP_PRINCIPAL = (
    "\n\n💡 Si este es un grupo de TEMAS (forum): entra al tema donde quieres "
    "que se envíen las bienvenidas y envía ahí /principal. "
    "Si no lo haces, las bienvenidas se enviarán en el tema General."
)

# --- SERVIDOR WEB ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Bot Mensajes Activo")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(('0.0.0.0', port), SimpleHandler).serve_forever()

# --- HELPERS ---

def is_autorizado(user_id):
    return user_id == MY_ID or user_id in usuarios_autorizados

def get_chat_sel(user_id):
    return estado_panel.get(user_id, {}).get("chat_id")

def registrar_grupo(user_id, chat_id, title):
    if user_id not in grupos_por_usuario:
        grupos_por_usuario[user_id] = {}
    grupos_por_usuario[user_id][chat_id] = title
    col_grupos.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": {"title": title}},
        upsert=True
    )

def desregistrar_grupo(chat_id):
    """Quita un grupo caído/inaccesible de Mongo y del caché en memoria, para todos los usuarios."""
    col_grupos.delete_many({"chat_id": chat_id})
    for uid in list(grupos_por_usuario.keys()):
        grupos_por_usuario[uid].pop(chat_id, None)

async def cargar_grupos_usuario_validados(user_id, bot):
    """Igual que cargar_grupos_usuario, pero verifica con la API de Telegram que el
    bot siga siendo miembro de cada grupo, y quita en el momento los que ya no."""
    grupos = dict(cargar_grupos_usuario(user_id))
    for chat_id, title in list(grupos.items()):
        try:
            member = await bot.get_chat_member(chat_id, bot.id)
            if member.status in ("left", "kicked"):
                desregistrar_grupo(chat_id)
                grupos.pop(chat_id, None)
        except Exception:
            # El bot ya no tiene acceso a ese chat (lo sacaron, lo borraron, etc.)
            desregistrar_grupo(chat_id)
            grupos.pop(chat_id, None)
    return grupos

def cargar_grupos_usuario(user_id):
    if user_id in grupos_por_usuario and grupos_por_usuario[user_id]:
        return grupos_por_usuario[user_id]
    docs = list(col_grupos.find({"user_id": user_id}))
    grupos = {d["chat_id"]: d["title"] for d in docs}
    grupos_por_usuario[user_id] = grupos
    return grupos

def get_bienvenida(chat_id):
    doc = col_bienvenida.find_one({"chat_id": chat_id})
    if not doc:
        return {
            "chat_id": chat_id,
            "texto": None,
            "media_file_id": None,
            "media_type": None,
            "botones_raw": None,
            "topic_principal": None,
        }
    doc.setdefault("topic_principal", None)
    return doc

def save_bienvenida_campo(chat_id, campo, valor):
    col_bienvenida.update_one({"chat_id": chat_id}, {"$set": {campo: valor}}, upsert=True)

def get_programado(chat_id):
    doc = col_programado.find_one({"chat_id": chat_id})
    if not doc:
        return {
            "chat_id": chat_id,
            "texto": None,
            "media_file_id": None,
            "media_type": None,
            "botones_raw": None,
            "activo": False,
            "intervalo": None,
            "ultimo_envio_ts": None,
        }
    doc.setdefault("activo", False)
    doc.setdefault("intervalo", None)
    doc.setdefault("ultimo_envio_ts", None)
    return doc

def save_programado_campo(chat_id, campo, valor):
    col_programado.update_one({"chat_id": chat_id}, {"$set": {campo: valor}}, upsert=True)

def guardar_short_id(texto):
    short_id = hashlib.md5(texto.encode("utf-8")).hexdigest()[:16]
    col_botones_data.update_one({"_id": short_id}, {"$set": {"texto": texto}}, upsert=True)
    return short_id

def obtener_texto_por_id(short_id):
    doc = col_botones_data.find_one({"_id": short_id})
    return doc.get("texto") if doc else None

def construir_boton(titulo, resto):
    resto_lower = resto.lower()
    if resto_lower.startswith("popup:") or resto_lower.startswith("alert:"):
        texto_popup = resto.split(":", 1)[1].strip()
        short_id = guardar_short_id(texto_popup)
        return InlineKeyboardButton(titulo, callback_data=f"wpopup_{short_id}")
    if resto_lower.startswith("share:"):
        texto_compartir = resto.split(":", 1)[1].strip()
        url = f"https://t.me/share/url?url=&text={quote(texto_compartir)}"
        return InlineKeyboardButton(titulo, url=url)
    if resto_lower.startswith("copy:"):
        texto_copiar = resto.split(":", 1)[1].strip()
        short_id = guardar_short_id(texto_copiar)
        return InlineKeyboardButton(titulo, callback_data=f"wcopy_{short_id}")
    if resto_lower.strip() == "rules":
        return InlineKeyboardButton(titulo, callback_data="wrules_none")
    # Por defecto: botón de enlace (URL)
    url = resto.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return InlineKeyboardButton(titulo, url=url)

def parsear_botones(texto_botones):
    if not texto_botones:
        return None
    filas = texto_botones.strip().split("\n")
    keyboard = []
    for fila in filas:
        fila = fila.strip()
        if not fila:
            continue
        partes = fila.split("&&")
        row = []
        for parte in partes:
            parte = parte.strip()
            if " - " not in parte:
                continue
            titulo, resto = parte.split(" - ", 1)
            titulo = titulo.strip()
            resto = resto.strip()
            if not titulo or not resto:
                continue
            row.append(construir_boton(titulo, resto))
        if row:
            keyboard.append(row)
    return InlineKeyboardMarkup(keyboard) if keyboard else None

def construir_texto_bienvenida(texto, user):
    if not texto:
        return None
    mention = f"[{user.first_name}](tg://user?id={user.id})"
    return texto.replace("{MENTION}", mention)

# --- TECLADOS ---

def menu_grupos(user_id):
    grupos = cargar_grupos_usuario(user_id)
    chat_sel = get_chat_sel(user_id)
    keyboard = []
    for chat_id, title in grupos.items():
        prefix = "✅ " if chat_id == chat_sel else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{title}", callback_data=f"elegir_grupo_{chat_id}")])
    if not keyboard:
        keyboard.append([InlineKeyboardButton("⚠️ Sin grupos disponibles", callback_data="none")])
    return InlineKeyboardMarkup(keyboard)

def menu_grupo_panel():
    keyboard = [
        [InlineKeyboardButton("📨 Enviar Mensaje", callback_data="msg_enviar")],
        [InlineKeyboardButton("👋 Mensaje de Bienvenida", callback_data="bienv_inicio")],
        [InlineKeyboardButton("📌 Mensaje Programado", callback_data="prog_inicio")],
        [InlineKeyboardButton("🔄 Cambiar grupo", callback_data="sel_grupo")],
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_bienvenida():
    keyboard = [
        [InlineKeyboardButton("📝 Editar Texto", callback_data="bienv_texto")],
        [InlineKeyboardButton("🖼️ Editar Multimedia", callback_data="bienv_media")],
        [InlineKeyboardButton("🔘 Editar Botones", callback_data="bienv_botones")],
        [InlineKeyboardButton("👀 Vista Previa", callback_data="bienv_preview")],
        [InlineKeyboardButton("⬅️ Atrás", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_editor_campo(cb_eliminar, cb_atras):
    """Teclado que acompaña el prompt de edición de un campo (texto/media/botones):
    Eliminar ese campo específico, o Cancelar y volver atrás."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Eliminar", callback_data=cb_eliminar)],
        [InlineKeyboardButton("⬅️ Cancelar y Atrás", callback_data=cb_atras)],
    ])

def menu_programado(chat_id):
    conf = get_programado(chat_id)
    activo = conf.get("activo", False)
    intervalo_key = conf.get("intervalo")
    intervalo_txt = INTERVALOS_LABEL.get(intervalo_key, "Sin elegir")
    estado_txt = "🟢 ENCENDIDO" if activo else "🔴 APAGADO"
    keyboard = [
        [InlineKeyboardButton("📝 Editar Texto", callback_data="prog_texto")],
        [InlineKeyboardButton("🖼️ Editar Multimedia", callback_data="prog_media")],
        [InlineKeyboardButton("🔘 Editar Botones", callback_data="prog_botones")],
        [InlineKeyboardButton("👀 Vista Previa", callback_data="prog_preview")],
        [InlineKeyboardButton(f"⏱️ Intervalo: {intervalo_txt}", callback_data="prog_intervalo")],
        [InlineKeyboardButton(f"Estado: {estado_txt}", callback_data="prog_toggle")],
        [InlineKeyboardButton("⬅️ Atrás", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_intervalo(chat_id):
    conf = get_programado(chat_id)
    actual = conf.get("intervalo")
    keyboard = []
    for key, label in INTERVALOS_LABEL.items():
        prefix = "✅ " if key == actual else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"prog_setint_{key}")])
    keyboard.append([InlineKeyboardButton("⬅️ Atrás", callback_data="prog_inicio")])
    return InlineKeyboardMarkup(keyboard)

def boton_atras(destino):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar y Atrás", callback_data=destino)]])

# --- MANEJADORES DE COMANDOS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.effective_chat.type != "private":
        return
    if not is_autorizado(user_id):
        return await update.message.reply_text("🔐 Envía la contraseña para acceder.")
    await buscar_grupos(update, context)

async def buscar_grupos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg_espera = await update.message.reply_text("🔍 Buscando grupos donde eres administrador...")
    grupos = await cargar_grupos_usuario_validados(user_id, context.bot)
    try:
        await msg_espera.delete()
    except:
        pass
    if not grupos:
        await update.message.reply_text(
            "⚠️ No encontré grupos configurables.\n\n"
            "Para que aparezcan:\n"
            "• Añade el bot al grupo como admin\n"
            "• Envía /reload en el grupo\n"
            "• Vuelve a enviar /start aquí"
        )
        return
    await update.message.reply_text("👥 Selecciona el grupo a configurar:", reply_markup=menu_grupos(user_id))

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user_id = update.effective_user.id
    if chat.type not in ("group", "supergroup"):
        return
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admins_ids = {a.user.id for a in admins}
        bot_id = context.bot.id
    except:
        return
    if user_id not in admins_ids or bot_id not in admins_ids:
        return
    registrar_grupo(user_id, chat.id, chat.title)
    for uid in list(usuarios_autorizados):
        if uid in admins_ids:
            registrar_grupo(uid, chat.id, chat.title)
    try:
        await update.message.reply_text(
            "✅ Grupo registrado. Ahora ve al bot en privado y usa /start."
            + (TIP_PRINCIPAL if chat.is_forum else "")
        )
    except:
        pass

async def cmd_principal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Enviado dentro de un tema de un grupo de temas (forum), guarda ese tema
    como destino de los mensajes de bienvenida para ese grupo.
    Enviado en el tema General (sin thread_id), vuelve a mandar las
    bienvenidas al chat/tema General.
    """
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type not in ("group", "supergroup"):
        return

    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admins_ids = {a.user.id for a in admins}
    except:
        return

    if user_id not in admins_ids:
        return

    if not chat.is_forum:
        await update.message.reply_text(
            "ℹ️ Este grupo no tiene temas activados, así que no necesitas /principal.\n"
            "Las bienvenidas ya se envían normalmente en el chat."
        )
        return

    thread_id = update.message.message_thread_id  # None si es el tema General
    save_bienvenida_campo(chat.id, "topic_principal", thread_id)

    if thread_id:
        await update.message.reply_text(
            "✅ A partir de ahora las bienvenidas se enviarán en ESTE tema.",
            message_thread_id=thread_id
        )
    else:
        await update.message.reply_text(
            "✅ A partir de ahora las bienvenidas se enviarán en el tema General."
        )

# --- DETECCIÓN DE NUEVOS MIEMBROS (join directo o solicitud aceptada) ---

async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    fue_miembro = old_status in ("member", "administrator", "creator")
    es_miembro_ahora = new_status in ("member", "administrator", "creator")
    if fue_miembro or not es_miembro_ahora:
        return  # No es un ingreso nuevo

    chat_id = result.chat.id
    user = result.new_chat_member.user
    if user.is_bot:
        return

    await enviar_bienvenida(context, chat_id, user)

async def enviar_bienvenida(context, chat_id, user):
    conf = get_bienvenida(chat_id)
    texto = conf.get("texto")
    media_file_id = conf.get("media_file_id")
    media_type = conf.get("media_type")
    botones_raw = conf.get("botones_raw")
    topic_principal = conf.get("topic_principal")  # None = grupo normal o tema General

    if not texto and not media_file_id:
        return  # Sin bienvenida configurada

    texto_final = construir_texto_bienvenida(texto, user) if texto else None
    markup = parsear_botones(botones_raw) if botones_raw else None

    # Borrar mensaje de bienvenida anterior
    anterior = col_ultimo_msg.find_one({"chat_id": chat_id})
    if anterior:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=anterior["message_id"])
        except:
            pass

    # Solo se pasa message_thread_id si hay un tema configurado con /principal.
    # En grupos normales (sin temas) esto queda como None y se envía como siempre.
    envio_kwargs = {}
    if topic_principal:
        envio_kwargs["message_thread_id"] = topic_principal

    nuevo_msg = None
    try:
        if media_type == "photo" and media_file_id:
            nuevo_msg = await context.bot.send_photo(
                chat_id=chat_id, photo=media_file_id,
                caption=texto_final, parse_mode="Markdown", reply_markup=markup,
                **envio_kwargs
            )
        elif media_type == "video" and media_file_id:
            nuevo_msg = await context.bot.send_video(
                chat_id=chat_id, video=media_file_id,
                caption=texto_final, parse_mode="Markdown", reply_markup=markup,
                **envio_kwargs
            )
        else:
            nuevo_msg = await context.bot.send_message(
                chat_id=chat_id, text=texto_final or "👋",
                parse_mode="Markdown", reply_markup=markup,
                **envio_kwargs
            )
    except Exception as e:
        logging.warning(f"Error enviando bienvenida: {e}")
        return

    if nuevo_msg:
        col_ultimo_msg.update_one(
            {"chat_id": chat_id},
            {"$set": {"message_id": nuevo_msg.message_id}},
            upsert=True
        )

# --- MENSAJE PROGRAMADO (se repite cada intervalo, fijado, borra el anterior) ---

async def enviar_programado(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    conf = get_programado(chat_id)

    if not conf.get("activo"):
        return  # Se desactivó; el job se remueve aparte, esto es una salvaguarda extra

    texto = conf.get("texto")
    media_file_id = conf.get("media_file_id")
    media_type = conf.get("media_type")
    botones_raw = conf.get("botones_raw")

    if not texto and not media_file_id:
        return  # No hay contenido configurado, no hay nada que enviar

    markup = parsear_botones(botones_raw) if botones_raw else None

    # Borrar el mensaje programado anterior (igual que la bienvenida)
    anterior = conf.get("ultimo_message_id")
    if anterior:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=anterior)
        except:
            pass

    nuevo_msg = None
    try:
        if media_type == "photo" and media_file_id:
            nuevo_msg = await context.bot.send_photo(
                chat_id=chat_id, photo=media_file_id,
                caption=texto, parse_mode="Markdown", reply_markup=markup
            )
        elif media_type == "video" and media_file_id:
            nuevo_msg = await context.bot.send_video(
                chat_id=chat_id, video=media_file_id,
                caption=texto, parse_mode="Markdown", reply_markup=markup
            )
        else:
            nuevo_msg = await context.bot.send_message(
                chat_id=chat_id, text=texto,
                parse_mode="Markdown", reply_markup=markup
            )
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=nuevo_msg.message_id, disable_notification=True)
        except Exception as e:
            logging.warning(f"No se pudo fijar el mensaje programado en {chat_id}: {e}")
    except Exception as e:
        logging.warning(f"Error enviando mensaje programado en {chat_id}: {e}")
        return

    if nuevo_msg:
        save_programado_campo(chat_id, "ultimo_message_id", nuevo_msg.message_id)
        save_programado_campo(chat_id, "ultimo_envio_ts", nuevo_msg.date.timestamp())

def programar_job_mensaje(app_or_context, chat_id, intervalo_key, first_delay=0):
    """Registra (o reemplaza) el job repetitivo de mensaje programado para un chat."""
    job_queue = app_or_context.job_queue
    for j in job_queue.get_jobs_by_name(f"programado_{chat_id}"):
        j.schedule_removal()
    segundos = INTERVALOS.get(intervalo_key)
    if not segundos:
        return
    job_queue.run_repeating(
        enviar_programado,
        interval=segundos,
        first=max(first_delay, 0),
        chat_id=chat_id,
        name=f"programado_{chat_id}"
    )

def cancelar_job_mensaje(app_or_context, chat_id):
    job_queue = app_or_context.job_queue
    for j in job_queue.get_jobs_by_name(f"programado_{chat_id}"):
        j.schedule_removal()

async def restaurar_jobs_programados(app):
    """Al iniciar el bot, vuelve a armar los jobs de todos los mensajes programados
    activos, calculando cuánto falta según el último envío guardado en Mongo
    (para no perder el conteo ni reenviar de más al reiniciar el bot)."""
    import time
    ahora = time.time()
    for conf in col_programado.find({"activo": True}):
        chat_id = conf["chat_id"]
        intervalo_key = conf.get("intervalo")
        segundos = INTERVALOS.get(intervalo_key)
        if not segundos:
            continue
        ultimo_ts = conf.get("ultimo_envio_ts")
        if ultimo_ts:
            transcurrido = ahora - ultimo_ts
            espera = max(segundos - transcurrido, 0)
        else:
            espera = 0
        programar_job_mensaje(app, chat_id, intervalo_key, first_delay=espera)

# --- MENSAJES DE TEXTO Y FLUJOS (PANEL PRIVADO) ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id if update.effective_user else None
    text = update.message.text or ""
    chat = update.effective_chat

    if chat.type != "private":
        return  # Este bot no procesa mensajes de grupo (solo eventos de ingreso)

    if not is_autorizado(user_id):
        if text.strip() == PASSWORD:
            usuarios_autorizados.add(user_id)
            await update.message.reply_text("✅ Acceso concedido.")
            await buscar_grupos(update, context)
        return

    if user_id not in estado_panel:
        return

    step = estado_panel[user_id].get("step")
    chat_id_sel = estado_panel[user_id].get("chat_id")
    if not step or step == "idle" or not chat_id_sel:
        return

    if step == "esperando_mensaje_grupo":
        estado_panel[user_id]["step"] = "idle"
        try:
            await context.bot.copy_message(
                chat_id=chat_id_sel,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
            await update.message.reply_text("✅ Mensaje enviado al grupo.", reply_markup=menu_grupo_panel())
        except Exception as e:
            await update.message.reply_text(f"❌ No se pudo enviar: {e}", reply_markup=menu_grupo_panel())
        return

    if step == "esperando_bienvenida_texto":
        estado_panel[user_id]["step"] = "idle"
        save_bienvenida_campo(chat_id_sel, "texto", text)
        await update.message.reply_text("✅ Texto de bienvenida guardado.", reply_markup=menu_bienvenida())
        return

    if step == "esperando_bienvenida_media":
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            save_bienvenida_campo(chat_id_sel, "media_file_id", file_id)
            save_bienvenida_campo(chat_id_sel, "media_type", "photo")
        elif update.message.video:
            file_id = update.message.video.file_id
            save_bienvenida_campo(chat_id_sel, "media_file_id", file_id)
            save_bienvenida_campo(chat_id_sel, "media_type", "video")
        else:
            await update.message.reply_text(
                "⚠️ Envía una foto o un video.",
                reply_markup=menu_editor_campo("bienv_media_eliminar", "bienv_inicio")
            )
            return
        estado_panel[user_id]["step"] = "idle"
        await update.message.reply_text("✅ Multimedia guardada.", reply_markup=menu_bienvenida())
        return

    if step == "esperando_bienvenida_botones":
        estado_panel[user_id]["step"] = "idle"
        save_bienvenida_campo(chat_id_sel, "botones_raw", text)
        await update.message.reply_text("✅ Botones guardados.", reply_markup=menu_bienvenida())
        return

    if step == "esperando_prog_texto":
        estado_panel[user_id]["step"] = "idle"
        save_programado_campo(chat_id_sel, "texto", text)
        await update.message.reply_text("✅ Texto del mensaje programado guardado.", reply_markup=menu_programado(chat_id_sel))
        return

    if step == "esperando_prog_media":
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            save_programado_campo(chat_id_sel, "media_file_id", file_id)
            save_programado_campo(chat_id_sel, "media_type", "photo")
        elif update.message.video:
            file_id = update.message.video.file_id
            save_programado_campo(chat_id_sel, "media_file_id", file_id)
            save_programado_campo(chat_id_sel, "media_type", "video")
        else:
            await update.message.reply_text(
                "⚠️ Envía una foto o un video.",
                reply_markup=menu_editor_campo("prog_media_eliminar", "prog_inicio")
            )
            return
        estado_panel[user_id]["step"] = "idle"
        await update.message.reply_text("✅ Multimedia guardada.", reply_markup=menu_programado(chat_id_sel))
        return

    if step == "esperando_prog_botones":
        estado_panel[user_id]["step"] = "idle"
        save_programado_campo(chat_id_sel, "botones_raw", text)
        await update.message.reply_text("✅ Botones guardados.", reply_markup=menu_programado(chat_id_sel))
        return

# --- CALLBACKS DE BOTONES ESPECIALES DE BIENVENIDA (popup / copy / rules) ---

async def welcome_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data.startswith("wpopup_"):
        short_id = data.split("_", 1)[1]
        texto = obtener_texto_por_id(short_id) or "..."
        await query.answer(texto[:200], show_alert=True)
        return
    if data.startswith("wcopy_"):
        short_id = data.split("_", 1)[1]
        texto = obtener_texto_por_id(short_id) or "..."
        await query.answer(texto[:200], show_alert=True)
        return
    if data == "wrules_none":
        await query.answer("⚠️ Reglas no configuradas para este grupo.", show_alert=True)
        return

# --- MANEJA BOTONES DEL PANEL PRIVADO ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # Callbacks de botones de bienvenida (se disparan en el grupo, sin requerir autorización)
    if data.startswith("wpopup_") or data.startswith("wcopy_") or data == "wrules_none":
        await welcome_button_callback(update, context)
        return

    if not is_autorizado(user_id):
        await query.answer("⛔ Sin acceso.")
        return

    await query.answer()

    if data == "none":
        return

    if user_id not in estado_panel:
        estado_panel[user_id] = {"chat_id": None, "step": "idle"}

    chat_id_sel = get_chat_sel(user_id)

    if data == "sel_grupo":
        await query.edit_message_text("👥 Selecciona el grupo a configurar:", reply_markup=menu_grupos(user_id))
        return

    if data.startswith("elegir_grupo_"):
        chat_id = int(data.split("_")[-1])
        estado_panel[user_id]["chat_id"] = chat_id
        estado_panel[user_id]["step"] = "idle"
        titulo = cargar_grupos_usuario(user_id).get(chat_id, str(chat_id))
        await query.edit_message_text(
            f"✅ Grupo: {titulo}\n\n📨 Panel de Mensajes:",
            reply_markup=menu_grupo_panel()
        )
        return

    if not chat_id_sel:
        await query.edit_message_text(
            "⚠️ Primero selecciona un grupo.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👥 Seleccionar grupo", callback_data="sel_grupo")
            ]])
        )
        return

    if data == "back_main":
        estado_panel[user_id]["step"] = "idle"
        await query.edit_message_text("📨 Panel de Mensajes:", reply_markup=menu_grupo_panel())
        return

    if data == "msg_enviar":
        estado_panel[user_id]["step"] = "esperando_mensaje_grupo"
        await query.edit_message_text(
            "📨 Envía el mensaje que quieres copiar y enviar al grupo (texto, imagen, video, lo que sea):",
            reply_markup=boton_atras("back_main")
        )
        return

    if data == "bienv_inicio":
        estado_panel[user_id]["step"] = "idle"
        await query.edit_message_text(
            f"👋 Configuración de Mensaje de Bienvenida:{TIP_PRINCIPAL}",
            reply_markup=menu_bienvenida()
        )
        return

    if data == "bienv_texto":
        estado_panel[user_id]["step"] = "esperando_bienvenida_texto"
        await query.edit_message_text(
            "📝 Envía el nuevo texto de bienvenida.\n\n"
            "Puedes usar `{MENTION}` para mencionar al usuario que se une.",
            parse_mode="Markdown",
            reply_markup=menu_editor_campo("bienv_texto_eliminar", "bienv_inicio")
        )
        return

    if data == "bienv_texto_eliminar":
        estado_panel[user_id]["step"] = "idle"
        save_bienvenida_campo(chat_id_sel, "texto", None)
        await query.edit_message_text("🗑️ Texto de bienvenida eliminado.", reply_markup=menu_bienvenida())
        return

    if data == "bienv_media":
        estado_panel[user_id]["step"] = "esperando_bienvenida_media"
        await query.edit_message_text(
            "🖼️ Envía la foto o video que acompañará el mensaje de bienvenida:",
            reply_markup=menu_editor_campo("bienv_media_eliminar", "bienv_inicio")
        )
        return

    if data in ("bienv_media_quitar", "bienv_media_eliminar"):
        estado_panel[user_id]["step"] = "idle"
        save_bienvenida_campo(chat_id_sel, "media_file_id", None)
        save_bienvenida_campo(chat_id_sel, "media_type", None)
        await query.edit_message_text("🗑️ Multimedia eliminada de la bienvenida.", reply_markup=menu_bienvenida())
        return

    if data == "bienv_botones":
        estado_panel[user_id]["step"] = "esperando_bienvenida_botones"
        await query.edit_message_text(
            "🔘 Envía los botones con el formato:\n\n"
            "`Título del botón - t.me/LinkEjemplo`\n\n"
            "Varios en una línea: separa con ` && `\n"
            "Varias filas: una por línea\n\n"
            "Especiales:\n"
            "`Título - popup:Texto emergente`\n"
            "`Título - share:Texto a compartir`\n"
            "`Título - copy:Texto a copiar`\n"
            "`Título - rules`",
            parse_mode="Markdown",
            reply_markup=menu_editor_campo("bienv_botones_eliminar", "bienv_inicio")
        )
        return

    if data == "bienv_botones_eliminar":
        estado_panel[user_id]["step"] = "idle"
        save_bienvenida_campo(chat_id_sel, "botones_raw", None)
        await query.edit_message_text("🗑️ Botones de bienvenida eliminados.", reply_markup=menu_bienvenida())
        return

    if data == "bienv_preview":
        conf = get_bienvenida(chat_id_sel)
        texto = conf.get("texto")
        media_file_id = conf.get("media_file_id")
        media_type = conf.get("media_type")
        botones_raw = conf.get("botones_raw")

        if not texto and not media_file_id:
            await query.answer("⚠️ No hay bienvenida configurada aún.", show_alert=True)
            return

        texto_preview = (texto or "").replace(
            "{MENTION}", f"[{query.from_user.first_name}](tg://user?id={query.from_user.id})"
        )
        markup = parsear_botones(botones_raw) if botones_raw else None

        try:
            if media_type == "photo" and media_file_id:
                await context.bot.send_photo(chat_id=user_id, photo=media_file_id, caption=texto_preview, parse_mode="Markdown", reply_markup=markup)
            elif media_type == "video" and media_file_id:
                await context.bot.send_video(chat_id=user_id, video=media_file_id, caption=texto_preview, parse_mode="Markdown", reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=user_id, text=texto_preview or "👋", parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            await query.answer(f"❌ Error: {e}", show_alert=True)
        return

    # --- MENSAJE PROGRAMADO ---

    if data == "prog_inicio":
        estado_panel[user_id]["step"] = "idle"
        await query.edit_message_text(
            "📌 **Mensaje Programado**\n"
            "Se reenvía repitiendo cada intervalo elegido, fijado en el chat, "
            "borrando la copia anterior (igual que la bienvenida).",
            parse_mode="Markdown",
            reply_markup=menu_programado(chat_id_sel)
        )
        return

    if data == "prog_texto":
        estado_panel[user_id]["step"] = "esperando_prog_texto"
        await query.edit_message_text(
            "📝 Envía el nuevo texto del mensaje programado:",
            reply_markup=menu_editor_campo("prog_texto_eliminar", "prog_inicio")
        )
        return

    if data == "prog_texto_eliminar":
        estado_panel[user_id]["step"] = "idle"
        save_programado_campo(chat_id_sel, "texto", None)
        await query.edit_message_text("🗑️ Texto del mensaje programado eliminado.", reply_markup=menu_programado(chat_id_sel))
        return

    if data == "prog_media":
        estado_panel[user_id]["step"] = "esperando_prog_media"
        await query.edit_message_text(
            "🖼️ Envía la foto o video del mensaje programado:",
            reply_markup=menu_editor_campo("prog_media_eliminar", "prog_inicio")
        )
        return

    if data == "prog_media_eliminar":
        estado_panel[user_id]["step"] = "idle"
        save_programado_campo(chat_id_sel, "media_file_id", None)
        save_programado_campo(chat_id_sel, "media_type", None)
        await query.edit_message_text("🗑️ Multimedia del mensaje programado eliminada.", reply_markup=menu_programado(chat_id_sel))
        return

    if data == "prog_botones":
        estado_panel[user_id]["step"] = "esperando_prog_botones"
        await query.edit_message_text(
            "🔘 Envía los botones con el formato:\n\n"
            "`Título del botón - t.me/LinkEjemplo`\n\n"
            "Varios en una línea: separa con ` && `\n"
            "Varias filas: una por línea\n\n"
            "Especiales:\n"
            "`Título - popup:Texto emergente`\n"
            "`Título - share:Texto a compartir`\n"
            "`Título - copy:Texto a copiar`",
            parse_mode="Markdown",
            reply_markup=menu_editor_campo("prog_botones_eliminar", "prog_inicio")
        )
        return

    if data == "prog_botones_eliminar":
        estado_panel[user_id]["step"] = "idle"
        save_programado_campo(chat_id_sel, "botones_raw", None)
        await query.edit_message_text("🗑️ Botones del mensaje programado eliminados.", reply_markup=menu_programado(chat_id_sel))
        return

    if data == "prog_preview":
        conf = get_programado(chat_id_sel)
        texto = conf.get("texto")
        media_file_id = conf.get("media_file_id")
        media_type = conf.get("media_type")
        botones_raw = conf.get("botones_raw")

        if not texto and not media_file_id:
            await query.answer("⚠️ No hay mensaje programado configurado aún.", show_alert=True)
            return

        markup = parsear_botones(botones_raw) if botones_raw else None

        try:
            if media_type == "photo" and media_file_id:
                await context.bot.send_photo(chat_id=user_id, photo=media_file_id, caption=texto, parse_mode="Markdown", reply_markup=markup)
            elif media_type == "video" and media_file_id:
                await context.bot.send_video(chat_id=user_id, video=media_file_id, caption=texto, parse_mode="Markdown", reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=user_id, text=texto, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            await query.answer(f"❌ Error: {e}", show_alert=True)
        return

    if data == "prog_intervalo":
        await query.edit_message_text(
            "⏱️ ¿Cada cuánto se reenvía el mensaje?",
            reply_markup=menu_intervalo(chat_id_sel)
        )
        return

    if data.startswith("prog_setint_"):
        intervalo_key = data.split("prog_setint_", 1)[1]
        save_programado_campo(chat_id_sel, "intervalo", intervalo_key)
        conf = get_programado(chat_id_sel)
        if conf.get("activo"):
            # Si ya estaba encendido, reprogramar con el intervalo nuevo desde ahora
            programar_job_mensaje(context, chat_id_sel, intervalo_key, first_delay=0)
        await query.edit_message_text(
            f"✅ Intervalo actualizado a {INTERVALOS_LABEL[intervalo_key]}.",
            reply_markup=menu_programado(chat_id_sel)
        )
        return

    if data == "prog_toggle":
        conf = get_programado(chat_id_sel)
        nuevo_estado = not conf.get("activo", False)

        if nuevo_estado:
            if not conf.get("intervalo"):
                await query.answer("⚠️ Elige primero un intervalo.", show_alert=True)
                return
            if not conf.get("texto") and not conf.get("media_file_id"):
                await query.answer("⚠️ Configura texto o multimedia primero.", show_alert=True)
                return
            save_programado_campo(chat_id_sel, "activo", True)
            programar_job_mensaje(context, chat_id_sel, conf["intervalo"], first_delay=0)
        else:
            save_programado_campo(chat_id_sel, "activo", False)
            cancelar_job_mensaje(context, chat_id_sel)

        await query.edit_message_text(
            "📌 **Mensaje Programado**",
            parse_mode="Markdown",
            reply_markup=menu_programado(chat_id_sel)
        )
        return

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("principal", cmd_principal))
    app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL, handle_message))

    app.job_queue.run_once(lambda ctx: asyncio.create_task(restaurar_jobs_programados(app)), when=1)

    print("Bot Mensajes Iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
