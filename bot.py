import os
import asyncio
import logging
import threading
import hashlib
import re
import time
import uuid
import datetime
from html import escape as html_escape
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from telegram.error import BadRequest
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
col_moderadores = db['moderadores_grupo']

usuarios_autorizados = set()
grupos_por_usuario = {}
estado_panel = {}   # {user_id: {"chat_id": int, "step": str, "pid": id del mensaje programado elegido}}

MAX_PROGRAMADOS = 20  # mensajes programados por grupo

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
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    ThreadingHTTPServer(('0.0.0.0', port), SimpleHandler).serve_forever()

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

# --- HELPERS MODERADORES POR GRUPO ---

def agregar_moderador(chat_id, mod_id, agregado_por):
    col_moderadores.update_one(
        {"chat_id": chat_id, "user_id": mod_id},
        {"$set": {
            "chat_id": chat_id,
            "user_id": mod_id,
            "agregado_por": agregado_por,
            "fecha": datetime.datetime.utcnow()
        }},
        upsert=True
    )

def quitar_moderador(chat_id, mod_id):
    col_moderadores.delete_one({"chat_id": chat_id, "user_id": mod_id})

def obtener_moderadores(chat_id):
    return list(col_moderadores.find({"chat_id": chat_id}).sort("fecha", -1))

def es_moderador_grupo(chat_id, user_id):
    return col_moderadores.find_one({"chat_id": chat_id, "user_id": user_id}) is not None

async def puede_moderar_grupo(context, chat_id, user_id):
    """Retorna True si el usuario es admin del grupo o moderador registrado en ese grupo."""
    if es_moderador_grupo(chat_id, user_id):
        return True
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admins_ids = {a.user.id for a in admins}
        return user_id in admins_ids
    except Exception:
        return False

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

def migrar_programados():
    """Los mensajes programados guardados con la versión anterior (uno por grupo) pasan a ser el #1."""
    for d in col_programado.find({"pid": {"$exists": False}}):
        col_programado.update_one({"_id": d["_id"]}, {"$set": {"pid": "p1", "num": 1}})

def listar_programados(chat_id):
    docs = list(col_programado.find({"chat_id": chat_id}))
    docs.sort(key=lambda d: d.get("num", 0))
    return docs

def get_programado(chat_id, pid):
    doc = col_programado.find_one({"chat_id": chat_id, "pid": pid})
    if not doc:
        return None
    doc.setdefault("activo", False)
    doc.setdefault("intervalo", None)
    doc.setdefault("ultimo_envio_ts", None)
    doc.setdefault("num", 1)
    return doc

def save_programado_campo(chat_id, pid, campo, valor):
    col_programado.update_one({"chat_id": chat_id, "pid": pid}, {"$set": {campo: valor}})

def crear_programado(chat_id):
    num = max([d.get("num", 0) for d in listar_programados(chat_id)] + [0]) + 1
    pid = uuid.uuid4().hex[:8]
    col_programado.insert_one({
        "chat_id": chat_id, "pid": pid, "num": num, "texto": None, "media_file_id": None,
        "media_type": None, "botones_raw": None, "activo": False, "intervalo": None,
        "ultimo_envio_ts": None,
    })
    return pid, num

def eliminar_programado(chat_id, pid):
    col_programado.delete_many({"chat_id": chat_id, "pid": pid})

def guardar_short_id(texto):
    short_id = hashlib.md5(texto.encode("utf-8")).hexdigest()[:16]
    col_botones_data.update_one({"_id": short_id}, {"$set": {"texto": texto}}, upsert=True)
    return short_id

def obtener_texto_por_id(short_id):
    doc = col_botones_data.find_one({"_id": short_id})
    return doc.get("texto") if doc else None

COLORES_BOTON = {"g": "success", "r": "danger", "p": "primary"}   # verde, rojo, azul
TAG_COLOR_RE = re.compile(r"^#([grp])\b\s*", re.I)
EMOJI_PREMIUM_RE = re.compile(r"^\[\[ce:(\d+)\]\]\s*")
EMOJI_PREMIUM_ANY_RE = re.compile(r"\[\[ce:\d+\]\]")

def tokenizar_emojis(msg):
    """Convierte los emojis premium (emoji personalizados) del texto en marcas [[ce:ID]] para poder
    guardarlos como texto. Respeta los offsets en UTF-16 que usa Telegram."""
    texto = msg.text or ""
    ents = sorted((e for e in (msg.entities or ()) if e.type == "custom_emoji"), key=lambda e: e.offset)
    if not ents:
        return texto
    data = texto.encode("utf-16-le")
    out, pos = [], 0
    for e in ents:
        out.append(data[pos * 2:e.offset * 2].decode("utf-16-le"))
        out.append(f"[[ce:{e.custom_emoji_id}]]")
        pos = e.offset + e.length
    out.append(data[pos * 2:].decode("utf-16-le"))
    return "".join(out)

def parsear_titulo(titulo):
    """'#g [[ce:123]] Título' -> ('Título', 'success', '123'). Solo un emoji premium inicial puede ir
    como icono del botón; los demás quedan como el emoji normal (Telegram no los admite dentro del texto)."""
    style = icon = None
    m = TAG_COLOR_RE.match(titulo)
    if m:
        style = COLORES_BOTON[m.group(1).lower()]
        titulo = titulo[m.end():]
    m = EMOJI_PREMIUM_RE.match(titulo)
    if m:
        icon = m.group(1)
        titulo = titulo[m.end():]
    titulo = EMOJI_PREMIUM_ANY_RE.sub("", titulo).strip()
    return titulo, style, icon

def _boton(titulo, style=None, icon=None, **kw):
    extra = {}
    if style:
        extra["style"] = style
    if icon:
        extra["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(titulo, api_kwargs=extra or None, **kw)

def construir_boton(titulo, resto, style=None, icon=None):
    resto_lower = resto.lower()
    if resto_lower.startswith("popup:") or resto_lower.startswith("alert:"):
        texto_popup = resto.split(":", 1)[1].strip()
        short_id = guardar_short_id(texto_popup)
        return _boton(titulo, style, icon, callback_data=f"wpopup_{short_id}")
    if resto_lower.startswith("share:"):
        texto_compartir = resto.split(":", 1)[1].strip()
        url = f"https://t.me/share/url?url=&text={quote(texto_compartir)}"
        return _boton(titulo, style, icon, url=url)
    if resto_lower.startswith("copy:"):
        texto_copiar = resto.split(":", 1)[1].strip()
        short_id = guardar_short_id(texto_copiar)
        return _boton(titulo, style, icon, callback_data=f"wcopy_{short_id}")
    if resto_lower.strip() == "rules":
        return _boton(titulo, style, icon, callback_data="wrules_none")
    url = resto.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return _boton(titulo, style, icon, url=url)

def parsear_botones_ext(texto_botones):
    if not texto_botones:
        return None, None
    completo, simple = [], []
    hay_extras = False
    for fila in texto_botones.strip().split("\n"):
        fila = fila.strip()
        if not fila:
            continue
        row, row_s = [], []
        for parte in fila.split("&&"):
            parte = parte.strip()
            if " - " not in parte:
                continue
            titulo, resto = parte.split(" - ", 1)
            titulo, style, icon = parsear_titulo(titulo.strip())
            resto = resto.strip()
            if not titulo or not resto:
                continue
            row.append(construir_boton(titulo, resto, style, icon))
            row_s.append(construir_boton(titulo, resto))
            hay_extras = hay_extras or bool(style or icon)
        if row:
            completo.append(row)
            simple.append(row_s)
    if not completo:
        return None, None
    return InlineKeyboardMarkup(completo), (InlineKeyboardMarkup(simple) if hay_extras else None)

def parsear_botones(texto_botones):
    return parsear_botones_ext(texto_botones)[0]

FORMATO_TYPES = {"custom_emoji", "bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre",
                 "text_link", "blockquote", "expandable_blockquote", "text_mention"}

def datos_texto(msg):
    plano = msg.text or ""
    rico = any(e.type in FORMATO_TYPES for e in (msg.entities or ()))
    return {"texto": plano, "texto_html": msg.text_html if rico else None}

def preparar_texto(conf, user=None):
    plano = conf.get("texto") or ""
    h = conf.get("texto_html")
    if h:
        if user is not None:
            h = h.replace("{MENTION}", f'<a href="tg://user?id={user.id}">{html_escape(user.first_name or "")}</a>')
            plano = plano.replace("{MENTION}", user.first_name or "")
        return h, "html", plano
    if user is not None:
        return (construir_texto_bienvenida(plano, user) if plano else None), None, None
    return (plano or None), None, None

def construir_texto_bienvenida(texto, user):
    if not texto:
        return None
    mention = f"[{user.first_name}](tg://user?id={user.id})"
    return texto.replace("{MENTION}", mention)

async def enviar_mensaje_seguro(bot, chat_id, texto, media_file_id, media_type, markup, **extra):
    fallback = extra.pop("markup_fallback", None)
    fmt = extra.pop("fmt", None)
    plano = extra.pop("plain", None)
    pm = "HTML" if fmt == "html" else "Markdown"
    texto_plano = plano if plano is not None else texto

    async def _send(parse_mode, mk, tx):
        if media_type == "photo" and media_file_id:
            return await bot.send_photo(
                chat_id=chat_id, photo=media_file_id, caption=tx,
                parse_mode=parse_mode, reply_markup=mk, **extra
            )
        elif media_type == "video" and media_file_id:
            return await bot.send_video(
                chat_id=chat_id, video=media_file_id, caption=tx,
                parse_mode=parse_mode, reply_markup=mk, **extra
            )
        else:
            return await bot.send_message(
                chat_id=chat_id, text=tx or "👋",
                parse_mode=parse_mode, reply_markup=mk, **extra
            )

    intentos = [(pm, markup, texto)]
    if fallback is not None:
        intentos.append((pm, fallback, texto))
    intentos.append((None, markup, texto_plano))
    if fallback is not None:
        intentos.append((None, fallback, texto_plano))

    ultimo, solo_plano = None, False
    for parse_mode, mk, tx in intentos:
        if solo_plano and parse_mode is not None:
            continue
        try:
            return await _send(parse_mode, mk, tx)
        except BadRequest as e:
            ultimo = e
            m = str(e).lower()
            if any(k in m for k in ("parse entities", "can't find end", "character", "entit")):
                solo_plano = True
            logging.warning(f"⚠️ Envío rechazado en {chat_id} ({e}); probando una versión más simple")
    raise ultimo

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

def menu_grupo_panel(chat_id=None):
    cant_mods = col_moderadores.count_documents({"chat_id": chat_id}) if chat_id else 0
    keyboard = [
        [InlineKeyboardButton("📨 Enviar Mensaje", callback_data="msg_enviar")],
        [InlineKeyboardButton("👋 Mensaje de Bienvenida", callback_data="bienv_inicio")],
        [InlineKeyboardButton("📌 Mensajes Programados", callback_data="prog_inicio")],
        [InlineKeyboardButton(f"🛡️ Moderadores ({cant_mods})", callback_data="mods_inicio")],
        [InlineKeyboardButton("🔄 Cambiar grupo", callback_data="sel_grupo")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def render_menu_moderadores(query_or_msg, chat_id, es_mensaje=False):
    mods = obtener_moderadores(chat_id)
    texto = (
        "🛡️ **Moderadores de este grupo**\n\n"
        "Los moderadores pueden usar `/send mensaje` (o responder a un mensaje con `/send`) "
        "dentro de este grupo para que el bot lo reenvíe y lo fije para todos.\n\n"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Añadir moderador por ID", callback_data="mods_add")]
    ]
    if mods:
        texto += "**Moderadores actuales:**\n"
        for m in mods:
            uid = m["user_id"]
            texto += f"• `{uid}`\n"
            keyboard.append([InlineKeyboardButton(f"❌ Quitar {uid}", callback_data=f"mods_del_{uid}")])
    else:
        texto += "_No hay moderadores añadidos en este grupo._"

    keyboard.append([InlineKeyboardButton("⬅️ Atrás", callback_data="back_main")])
    markup = InlineKeyboardMarkup(keyboard)

    if es_mensaje:
        await query_or_msg.reply_text(texto, parse_mode="Markdown", reply_markup=markup)
    else:
        await query_or_msg.edit_message_text(texto, parse_mode="Markdown", reply_markup=markup)

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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Eliminar", callback_data=cb_eliminar)],
        [InlineKeyboardButton("⬅️ Cancelar y Atrás", callback_data=cb_atras)],
    ])

def menu_lista_programados(chat_id):
    keyboard = []
    for d in listar_programados(chat_id):
        estado = "🟢" if d.get("activo") else "🔴"
        iv = d.get("intervalo") or "—"
        prev = (d.get("texto") or ("📷 multimedia" if d.get("media_file_id") else "vacío"))
        prev = EMOJI_PREMIUM_ANY_RE.sub("", prev).replace("\n", " ")[:18]
        keyboard.append([InlineKeyboardButton(f"{estado} #{d.get('num', 1)} · {iv} · {prev}",
                                              callback_data=f"prog_sel_{d['pid']}")])
    if len(keyboard) < MAX_PROGRAMADOS:
        keyboard.append([InlineKeyboardButton("➕ Nuevo mensaje programado", callback_data="prog_nuevo")])
    keyboard.append([InlineKeyboardButton("⬅️ Atrás", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)

def menu_programado(chat_id, pid):
    conf = get_programado(chat_id, pid)
    if not conf:
        return menu_lista_programados(chat_id)
    activo = conf.get("activo", False)
    intervalo_txt = INTERVALOS_LABEL.get(conf.get("intervalo"), "Sin elegir")
    estado_txt = "🟢 ENCENDIDO" if activo else "🔴 APAGADO"
    keyboard = [
        [InlineKeyboardButton("📝 Editar Texto", callback_data="prog_texto")],
        [InlineKeyboardButton("🖼️ Editar Multimedia", callback_data="prog_media")],
        [InlineKeyboardButton("🔘 Editar Botones", callback_data="prog_botones")],
        [InlineKeyboardButton("👀 Vista Previa", callback_data="prog_preview")],
        [InlineKeyboardButton(f"⏱️ Intervalo: {intervalo_txt}", callback_data="prog_intervalo")],
        [InlineKeyboardButton(f"Estado: {estado_txt}", callback_data="prog_toggle")],
        [InlineKeyboardButton("🗑️ Eliminar este mensaje", callback_data="prog_eliminar")],
        [InlineKeyboardButton("⬅️ Atrás (lista)", callback_data="prog_inicio")],
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_intervalo(chat_id, pid):
    conf = get_programado(chat_id, pid) or {}
    actual = conf.get("intervalo")
    keyboard = []
    for key, label in INTERVALOS_LABEL.items():
        prefix = "✅ " if key == actual else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"prog_setint_{key}")])
    keyboard.append([InlineKeyboardButton("⬅️ Atrás", callback_data="prog_item")])
    return InlineKeyboardMarkup(keyboard)

def boton_atras(destino):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar y Atrás", callback_data=destino)]])

# --- MANEJADORES DE COMANDOS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.effective_chat.type != "private":
        return
    if user_id in estado_panel:
        estado_panel[user_id]["step"] = "idle"
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

    thread_id = update.message.message_thread_id
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

async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando usable por administradores y moderadores del grupo:
    - `/send texto del mensaje`: reenvía ese texto al grupo y lo fija para todos.
    - Respondiendo a un mensaje con `/send`: copia ese mensaje al grupo y lo fija para todos.
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if chat.type not in ("group", "supergroup"):
        return

    if not await puede_moderar_grupo(context, chat.id, user.id):
        return

    thread_id = msg.message_thread_id
    envio_kwargs = {"message_thread_id": thread_id} if thread_id else {}

    nuevo_msg = None

    # Caso 1: Se usó /send respondiendo a otro mensaje (sirve para texto, fotos, videos, etc.)
    if msg.reply_to_message:
        try:
            nuevo_msg = await context.bot.copy_message(
                chat_id=chat.id,
                from_chat_id=chat.id,
                message_id=msg.reply_to_message.message_id,
                **envio_kwargs
            )
        except Exception as e:
            logging.warning(f"Error copiando mensaje con /send en {chat.id}: {e}")
            return
    else:
        # Caso 2: /send <mensaje> escrito directamente
        raw_text = msg.text or ""
        # Extraer todo lo que sigue después de /send o /send@NombreBot
        match = re.match(r"^/send(?:@\w+)?(?:\s+(.*))?$", raw_text, flags=re.DOTALL | re.IGNORECASE)
        contenido = (match.group(1) if match else "").strip()

        if not contenido:
            aviso = await msg.reply_text("⚠️ Uso: `/send tu mensaje` (o responde a un mensaje con `/send`).", parse_mode="Markdown")
            await asyncio.sleep(5)
            try:
                await aviso.delete()
                await msg.delete()
            except:
                pass
            return

        # Conservar formato HTML si el usuario usó negritas/enlaces/emojis premium, ajustando el offset de "/send "
        prefix_len = len(raw_text) - len(contenido)
        rico = any(e.type in FORMATO_TYPES for e in (msg.entities or ()))

        try:
            if rico:
                # Reconstruir texto HTML quitando el prefijo "/send "
                html_completo = msg.text_html or contenido
                html_limpio = re.sub(r"^/send(?:@\w+)?\s+", "", html_completo, count=1, flags=re.DOTALL | re.IGNORECASE)
                nuevo_msg = await context.bot.send_message(
                    chat_id=chat.id,
                    text=html_limpio,
                    parse_mode="HTML",
                    **envio_kwargs
                )
            else:
                nuevo_msg = await context.bot.send_message(
                    chat_id=chat.id,
                    text=contenido,
                    **envio_kwargs
                )
        except Exception as e:
            logging.warning(f"Error enviando mensaje con /send en {chat.id}: {e}")
            try:
                nuevo_msg = await context.bot.send_message(
                    chat_id=chat.id,
                    text=contenido,
                    **envio_kwargs
                )
            except Exception as e2:
                logging.error(f"Fallo total en /send en {chat.id}: {e2}")
                return

    # Fijar el mensaje enviado para todos (con notificación activada: disable_notification=False)
    if nuevo_msg:
        try:
            await context.bot.pin_chat_message(
                chat_id=chat.id,
                message_id=nuevo_msg.message_id,
                disable_notification=False
            )
        except Exception as e:
            logging.warning(f"No se pudo fijar el mensaje de /send en {chat.id}: {e}")

    # Borrar el comando "/send ..." del moderador/admin para dejar limpio el chat
    try:
        await msg.delete()
    except:
        pass

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
        return

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
    topic_principal = conf.get("topic_principal")

    if not texto and not media_file_id:
        return

    texto_final, fmt, plano = preparar_texto(conf, user) if texto else (None, None, None)
    markup, markup_simple = parsear_botones_ext(botones_raw)

    anterior = col_ultimo_msg.find_one({"chat_id": chat_id})
    if anterior:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=anterior["message_id"])
        except:
            pass

    envio_kwargs = {}
    if topic_principal:
        envio_kwargs["message_thread_id"] = topic_principal

    nuevo_msg = None
    try:
        nuevo_msg = await enviar_mensaje_seguro(
            context.bot, chat_id, texto_final, media_file_id, media_type, markup,
            markup_fallback=markup_simple, fmt=fmt, plain=plano, **envio_kwargs
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

# --- MENSAJES PROGRAMADOS (varios por grupo; cada uno se repite, se fija y borra su copia anterior) ---

def nombre_job(chat_id, pid):
    return f"programado_{chat_id}_{pid}"

async def enviar_programado(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    pid = context.job.data
    conf = get_programado(chat_id, pid)

    if not conf or not conf.get("activo"):
        return

    texto = conf.get("texto")
    media_file_id = conf.get("media_file_id")
    media_type = conf.get("media_type")
    botones_raw = conf.get("botones_raw")

    if not texto and not media_file_id:
        return

    markup, markup_simple = parsear_botones_ext(botones_raw)
    texto, fmt, plano = preparar_texto(conf)

    anterior = conf.get("ultimo_message_id")
    if anterior:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=anterior)
        except:
            pass

    nuevo_msg = None
    try:
        nuevo_msg = await enviar_mensaje_seguro(
            context.bot, chat_id, texto, media_file_id, media_type, markup,
            markup_fallback=markup_simple, fmt=fmt, plain=plano
        )
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=nuevo_msg.message_id, disable_notification=True)
        except Exception as e:
            logging.warning(f"No se pudo fijar el mensaje programado en {chat_id}: {e}")
    except Exception as e:
        logging.warning(f"Error enviando mensaje programado en {chat_id}: {e}")
        return

    if nuevo_msg:
        save_programado_campo(chat_id, pid, "ultimo_message_id", nuevo_msg.message_id)
        save_programado_campo(chat_id, pid, "ultimo_envio_ts", nuevo_msg.date.timestamp())

def programar_job_mensaje(app_or_context, chat_id, pid, intervalo_key, first_delay=0):
    job_queue = app_or_context.job_queue
    for j in job_queue.get_jobs_by_name(nombre_job(chat_id, pid)):
        j.schedule_removal()
    segundos = INTERVALOS.get(intervalo_key)
    if not segundos:
        return
    job_queue.run_repeating(
        enviar_programado,
        interval=segundos,
        first=max(first_delay, 0),
        chat_id=chat_id,
        name=nombre_job(chat_id, pid),
        data=pid,
    )

def cancelar_job_mensaje(app_or_context, chat_id, pid):
    for j in app_or_context.job_queue.get_jobs_by_name(nombre_job(chat_id, pid)):
        j.schedule_removal()

async def restaurar_jobs_programados(app):
    ahora = time.time()
    for conf in col_programado.find({"activo": True}):
        chat_id, pid = conf["chat_id"], conf.get("pid")
        segundos = INTERVALOS.get(conf.get("intervalo"))
        if not segundos or not pid:
            continue
        ultimo_ts = conf.get("ultimo_envio_ts")
        espera = max(segundos - (ahora - ultimo_ts), 0) if ultimo_ts else 0
        programar_job_mensaje(app, chat_id, pid, conf["intervalo"], first_delay=espera)

# --- MENSAJES DE TEXTO Y FLUJOS (PANEL PRIVADO) ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else None
    text = update.message.text or ""
    chat = update.effective_chat

    if chat.type != "private":
        return

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

    if step == "esperando_mod_id":
        id_txt = text.strip()
        if not id_txt.isdigit():
            await update.message.reply_text(
                "⚠️ El ID del moderador debe ser un número (ejemplo: `123456789`).\n"
                "Envía el ID numérico o pulsa Cancelar.",
                parse_mode="Markdown",
                reply_markup=boton_atras("mods_inicio")
            )
            return
        nuevo_mod_id = int(id_txt)
        agregar_moderador(chat_id_sel, nuevo_mod_id, user_id)
        estado_panel[user_id]["step"] = "idle"
        await update.message.reply_text(
            f"✅ Moderador `{nuevo_mod_id}` añadido correctamente a este grupo.",
            parse_mode="Markdown"
        )
        await render_menu_moderadores(update.message, chat_id_sel, es_mensaje=True)
        return

    if step == "esperando_mensaje_grupo":
        estado_panel[user_id]["step"] = "idle"
        try:
            await context.bot.copy_message(
                chat_id=chat_id_sel,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
            await update.message.reply_text("✅ Mensaje enviado al grupo.", reply_markup=menu_grupo_panel(chat_id_sel))
        except Exception as e:
            await update.message.reply_text(f"❌ No se pudo enviar: {e}", reply_markup=menu_grupo_panel(chat_id_sel))
        return

    if step == "esperando_bienvenida_texto":
        estado_panel[user_id]["step"] = "idle"
        d = datos_texto(update.message)
        save_bienvenida_campo(chat_id_sel, "texto", d["texto"])
        save_bienvenida_campo(chat_id_sel, "texto_html", d["texto_html"])
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
        save_bienvenida_campo(chat_id_sel, "botones_raw", tokenizar_emojis(update.message))
        await update.message.reply_text("✅ Botones guardados.", reply_markup=menu_bienvenida())
        return

    pid = estado_panel[user_id].get("pid")

    if step == "esperando_prog_texto":
        estado_panel[user_id]["step"] = "idle"
        d = datos_texto(update.message)
        save_programado_campo(chat_id_sel, pid, "texto", d["texto"])
        save_programado_campo(chat_id_sel, pid, "texto_html", d["texto_html"])
        await update.message.reply_text("✅ Texto del mensaje programado guardado.", reply_markup=menu_programado(chat_id_sel, pid))
        return

    if step == "esperando_prog_media":
        if update.message.photo:
            save_programado_campo(chat_id_sel, pid, "media_file_id", update.message.photo[-1].file_id)
            save_programado_campo(chat_id_sel, pid, "media_type", "photo")
        elif update.message.video:
            save_programado_campo(chat_id_sel, pid, "media_file_id", update.message.video.file_id)
            save_programado_campo(chat_id_sel, pid, "media_type", "video")
        else:
            await update.message.reply_text(
                "⚠️ Envía una foto o un video.",
                reply_markup=menu_editor_campo("prog_media_eliminar", "prog_item")
            )
            return
        estado_panel[user_id]["step"] = "idle"
        await update.message.reply_text("✅ Multimedia guardada.", reply_markup=menu_programado(chat_id_sel, pid))
        return

    if step == "esperando_prog_botones":
        estado_panel[user_id]["step"] = "idle"
        save_programado_campo(chat_id_sel, pid, "botones_raw", tokenizar_emojis(update.message))
        await update.message.reply_text("✅ Botones guardados.", reply_markup=menu_programado(chat_id_sel, pid))
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
        estado_panel[user_id]["step"] = "idle"
        await query.edit_message_text("👥 Selecciona el grupo a configurar:", reply_markup=menu_grupos(user_id))
        return

    if data.startswith("elegir_grupo_"):
        chat_id = int(data.split("_")[-1])
        estado_panel[user_id]["chat_id"] = chat_id
        estado_panel[user_id]["step"] = "idle"
        titulo = cargar_grupos_usuario(user_id).get(chat_id, str(chat_id))
        await query.edit_message_text(
            f"✅ Grupo: {titulo}\n\n📨 Panel de Mensajes:",
            reply_markup=menu_grupo_panel(chat_id)
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
        await query.edit_message_text("📨 Panel de Mensajes:", reply_markup=menu_grupo_panel(chat_id_sel))
        return

    # --- MODERADORES POR GRUPO ---
    if data == "mods_inicio":
        estado_panel[user_id]["step"] = "idle"
        await render_menu_moderadores(query, chat_id_sel)
        return

    if data == "mods_add":
        estado_panel[user_id]["step"] = "esperando_mod_id"
        await query.edit_message_text(
            "🛡️ **Añadir moderador**\n\n"
            "Envía ahora el **ID numérico** del usuario que quieres hacer moderador en este grupo "
            "(ejemplo: `123456789`).",
            parse_mode="Markdown",
            reply_markup=boton_atras("mods_inicio")
        )
        return

    if data.startswith("mods_del_"):
        mod_id = int(data.split("_")[-1])
        quitar_moderador(chat_id_sel, mod_id)
        await query.answer("✅ Moderador eliminado.", show_alert=True)
        await render_menu_moderadores(query, chat_id_sel)
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
            "Puedes usar `{MENTION}` para mencionar al usuario que se une.\n\n"
            "💎 También puedes usar *emojis premium* y el formato de Telegram (negrita, cursiva, enlaces...). "
            "Si los usas, no escribas asteriscos a mano: el formato se toma del propio mensaje.",
            parse_mode="Markdown",
            reply_markup=menu_editor_campo("bienv_texto_eliminar", "bienv_inicio")
        )
        return

    if data == "bienv_texto_eliminar":
        estado_panel[user_id]["step"] = "idle"
        save_bienvenida_campo(chat_id_sel, "texto", None)
        save_bienvenida_campo(chat_id_sel, "texto_html", None)
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
            "Colores: `#g` verde · `#r` rojo · `#p` azul, al inicio\n"
            "`#g Título - t.me/LinkEjemplo`\n"
            "Emoji premium como icono: ponlo justo antes del título\n"
            "`#r 🔥 Título - t.me/LinkEjemplo` (con tu emoji premium)\n\n"
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

        texto_preview, fmt, plano = preparar_texto(conf, query.from_user)
        markup, markup_simple = parsear_botones_ext(botones_raw)

        try:
            await enviar_mensaje_seguro(context.bot, user_id, texto_preview, media_file_id, media_type, markup,
                                        markup_fallback=markup_simple, fmt=fmt, plain=plano)
        except Exception as e:
            await context.bot.send_message(chat_id=user_id, text=f"❌ No se pudo enviar la vista previa: {e}")
        return

    # --- MENSAJES PROGRAMADOS (varios por grupo) ---

    est = estado_panel[user_id]

    if data == "prog_inicio":
        est["step"], est["pid"] = "idle", None
        await query.edit_message_text(
            "📌 Mensajes Programados\n\n"
            "Puedes crear varios. Cada uno se repite en su propio intervalo, se fija en el chat "
            "y borra su copia anterior.\n🟢 encendido · 🔴 apagado",
            reply_markup=menu_lista_programados(chat_id_sel)
        )
        return

    if data == "prog_nuevo":
        if len(listar_programados(chat_id_sel)) >= MAX_PROGRAMADOS:
            await query.answer(f"⚠️ Máximo {MAX_PROGRAMADOS} mensajes programados por grupo.", show_alert=True)
            return
        pid, num = crear_programado(chat_id_sel)
        est["step"], est["pid"] = "idle", pid
        await query.edit_message_text(
            f"📌 Mensaje programado #{num} creado.\nConfigura texto/multimedia, botones e intervalo, y enciéndelo.",
            reply_markup=menu_programado(chat_id_sel, pid)
        )
        return

    if data.startswith("prog_sel_"):
        pid = data[len("prog_sel_"):]
        conf = get_programado(chat_id_sel, pid)
        if not conf:
            await query.edit_message_text("⚠️ Ese mensaje ya no existe.", reply_markup=menu_lista_programados(chat_id_sel))
            return
        est["step"], est["pid"] = "idle", pid
        await query.edit_message_text(f"📌 Mensaje programado #{conf['num']}", reply_markup=menu_programado(chat_id_sel, pid))
        return

    if data.startswith("prog_"):
        pid = est.get("pid")
        conf = get_programado(chat_id_sel, pid) if pid else None
        if not conf:
            await query.edit_message_text("📌 Mensajes Programados", reply_markup=menu_lista_programados(chat_id_sel))
            return
        num = conf["num"]

        if data == "prog_item":
            est["step"] = "idle"
            await query.edit_message_text(f"📌 Mensaje programado #{num}", reply_markup=menu_programado(chat_id_sel, pid))
            return

        if data == "prog_texto":
            est["step"] = "esperando_prog_texto"
            await query.edit_message_text(
                f"📝 Envía el nuevo texto del mensaje programado #{num}.\n\n"
                "💎 Puedes usar emojis premium y el formato de Telegram (negrita, cursiva, enlaces...).",
                reply_markup=menu_editor_campo("prog_texto_eliminar", "prog_item")
            )
            return

        if data == "prog_texto_eliminar":
            est["step"] = "idle"
            save_programado_campo(chat_id_sel, pid, "texto", None)
            save_programado_campo(chat_id_sel, pid, "texto_html", None)
            await query.edit_message_text("🗑️ Texto eliminado.", reply_markup=menu_programado(chat_id_sel, pid))
            return

        if data == "prog_media":
            est["step"] = "esperando_prog_media"
            await query.edit_message_text(
                f"🖼️ Envía la foto o video del mensaje programado #{num}:",
                reply_markup=menu_editor_campo("prog_media_eliminar", "prog_item")
            )
            return

        if data == "prog_media_eliminar":
            est["step"] = "idle"
            save_programado_campo(chat_id_sel, pid, "media_file_id", None)
            save_programado_campo(chat_id_sel, pid, "media_type", None)
            await query.edit_message_text("🗑️ Multimedia eliminada.", reply_markup=menu_programado(chat_id_sel, pid))
            return

        if data == "prog_botones":
            est["step"] = "esperando_prog_botones"
            await query.edit_message_text(
                "🔘 Envía los botones con el formato:\n\n"
                "`Título del botón - t.me/LinkEjemplo`\n\n"
                "Colores: `#g` verde · `#r` rojo · `#p` azul, al inicio\n"
                "`#g Título - t.me/LinkEjemplo`\n"
                "Emoji premium como icono: ponlo justo antes del título\n"
                "`#r 🔥 Título - t.me/LinkEjemplo` (con tu emoji premium)\n\n"
                "Varios en una línea: separa con ` && `\n"
                "Varias filas: una por línea\n\n"
                "Especiales:\n"
                "`Título - popup:Texto emergente`\n"
                "`Título - share:Texto a compartir`\n"
                "`Título - copy:Texto a copiar`",
                parse_mode="Markdown",
                reply_markup=menu_editor_campo("prog_botones_eliminar", "prog_item")
            )
            return

        if data == "prog_botones_eliminar":
            est["step"] = "idle"
            save_programado_campo(chat_id_sel, pid, "botones_raw", None)
            await query.edit_message_text("🗑️ Botones eliminados.", reply_markup=menu_programado(chat_id_sel, pid))
            return

        if data == "prog_preview":
            if not conf.get("texto") and not conf.get("media_file_id"):
                await query.answer("⚠️ Este mensaje programado aún no tiene contenido.", show_alert=True)
                return
            markup, markup_simple = parsear_botones_ext(conf.get("botones_raw"))
            texto_preview, fmt, plano = preparar_texto(conf)
            try:
                await enviar_mensaje_seguro(context.bot, user_id, texto_preview, conf.get("media_file_id"),
                                            conf.get("media_type"), markup, markup_fallback=markup_simple,
                                            fmt=fmt, plain=plano)
            except Exception as e:
                await context.bot.send_message(chat_id=user_id, text=f"❌ No se pudo enviar la vista previa: {e}")
            return

        if data == "prog_intervalo":
            await query.edit_message_text("⏱️ ¿Cada cuánto se reenvía el mensaje?", reply_markup=menu_intervalo(chat_id_sel, pid))
            return

        if data.startswith("prog_setint_"):
            intervalo_key = data.split("prog_setint_", 1)[1]
            save_programado_campo(chat_id_sel, pid, "intervalo", intervalo_key)
            if conf.get("activo"):
                programar_job_mensaje(context, chat_id_sel, pid, intervalo_key, first_delay=0)
            await query.edit_message_text(
                f"✅ Intervalo actualizado a {INTERVALOS_LABEL[intervalo_key]}.",
                reply_markup=menu_programado(chat_id_sel, pid)
            )
            return

        if data == "prog_toggle":
            if not conf.get("activo"):
                if not conf.get("intervalo"):
                    await query.answer("⚠️ Elige primero un intervalo.", show_alert=True)
                    return
                if not conf.get("texto") and not conf.get("media_file_id"):
                    await query.answer("⚠️ Configura texto o multimedia primero.", show_alert=True)
                    return
                save_programado_campo(chat_id_sel, pid, "activo", True)
                programar_job_mensaje(context, chat_id_sel, pid, conf["intervalo"], first_delay=0)
            else:
                save_programado_campo(chat_id_sel, pid, "activo", False)
                cancelar_job_mensaje(context, chat_id_sel, pid)
            await query.edit_message_text(f"📌 Mensaje programado #{num}", reply_markup=menu_programado(chat_id_sel, pid))
            return

        if data == "prog_eliminar":
            await query.edit_message_text(
                f"🗑️ ¿Eliminar el mensaje programado #{num}? Se apaga y se borra su configuración.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Sí, eliminar", callback_data="prog_eliminar_si"),
                    InlineKeyboardButton("↩️ No", callback_data="prog_item"),
                ]])
            )
            return

        if data == "prog_eliminar_si":
            cancelar_job_mensaje(context, chat_id_sel, pid)
            eliminar_programado(chat_id_sel, pid)
            est["step"], est["pid"] = "idle", None
            await query.edit_message_text(f"🗑️ Mensaje programado #{num} eliminado.", reply_markup=menu_lista_programados(chat_id_sel))
            return

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    migrar_programados()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("principal", cmd_principal))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    app.job_queue.run_once(lambda ctx: asyncio.create_task(restaurar_jobs_programados(app)), when=1)

    print("Bot Mensajes Iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
