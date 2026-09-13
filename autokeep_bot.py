"""
autokeep_bot.py
Bot ke-2 ("autokeep"): begitu tau ada username AVAILABLE -- baik dari relay
bot 1 (pesan di grup RELAY_CHAT_ID) ATAUPUN dari daftar prioritas /autokeep
sendiri -- bot ini langsung coba KLAIM username itu (bikin channel + assign
username, lewat akun pribadi/userbot).

LOGIN AKUN PRIBADI LEWAT BOT INI SENDIRI:
Gak perlu jalanin script terpisah di komputer lokal. Login dilakukan lewat
chat bot ini sendiri:
  /login +6281234567890   -> kirim kode OTP ke Telegram kamu
  /code 12345             -> masukin kode OTP yang masuk
  /password xxxxx         -> HANYA kalau akun kamu pakai 2FA, masukin password-nya
  /logout                 -> logout & hapus sesi tersimpan

Session yang berhasil login disimpan ke autokeep_storage.py (file JSON yang
sama dengan priority/claimed), jadi begitu file itu sudah dipindah ke
Railway Volume, sesi ini ikut persisten juga walau redeploy.

⚠️ Pakai akun kedua/cadangan, JANGAN akun Telegram utama kamu -- akun ini
   yang bakal dipakai bikin banyak channel otomatis (rawan kena rate-limit).

DUA JALUR DETEKSI:
1. Relay dari bot 1: dengerin pesan teks di grup RELAY_CHAT_ID, cari baris
   berpola "@username 🟢 Available", langsung trigger klaim.
2. Daftar prioritas sendiri (/autokeep): loop cepat independen, cek pakai
   MTProto resolveUsername sendiri (checker.UsernamePool, pakai BOT_TOKEN
   punya bot ini), gak perlu nunggu bot 1 sama sekali.

Env vars:
  BOT_TOKEN            -> token bot Telegram BARU khusus bot ini
  OWNER_CHAT_ID         -> chat id kamu (command & notif pribadi)
  RELAY_CHAT_ID         -> id grup relay tempat bot 1 kirim notif available
  API_ID, API_HASH      -> sama kayak punya bot 1 (my.telegram.org)
  AUTOKEEP_DATA_FILE    -> (opsional) path file JSON prioritas + sesi
  PRIORITY_CHECK_DELAY  -> (opsional) jeda antar-cek daftar prioritas, default 3 detik
"""

import asyncio
import logging
import os
import re

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

import autokeep_storage as astore
import checker
import keeper
from checker import Status, UsernamePool, Worker

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
RELAY_CHAT_ID = int(os.environ["RELAY_CHAT_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PRIORITY_CHECK_DELAY = float(os.getenv("PRIORITY_CHECK_DELAY", "3"))

# Baca baris relay dari bot 1, format persis: "@username 🟢 Available"
AVAILABLE_LINE_RE = re.compile(r"@(\w{5,32})\s*🟢\s*Available", re.IGNORECASE)

CLAIM_ERROR_LABEL = {
    "channel_limit_reached": "akun sudah kena limit maksimal jumlah channel/grup",
    "username_occupied": "kalah cepat, ada yang ambil duluan",
    "username_invalid": "Telegram bilang invalid pas mau di-assign",
}

pool: UsernamePool | None = None          # buat resolveUsername daftar prioritas
user_client: TelegramClient | None = None  # userbot akun pribadi, buat klaim

# State login sementara (cuma dipakai owner, jadi cukup 1 slot in-memory,
# gak perlu per-user). None kalau lagi gak ada proses login berjalan.
_pending_login: dict | None = None


def _owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != OWNER_CHAT_ID:
            return
        return await func(update, context)
    return wrapper


def _claim_error_text(error: str) -> str:
    if error.startswith("flood_wait:"):
        seconds = error.split(":", 1)[1]
        return f"kena FloodWait, coba lagi setelah {seconds} detik"
    return CLAIM_ERROR_LABEL.get(error, error)


async def try_claim_and_notify(app: Application, username: str, source: str):
    """Satu pintu buat proses klaim + kirim notif, dipakai baik dari relay
    maupun dari priority_loop, biar gak ada logika ganda/duplikat."""
    username = username.lstrip("@").strip().lower()

    if astore.is_claimed(username):
        return  # sudah pernah diproses sebelumnya, gak usah diulang

    if not await user_client.is_user_authorized():
        await app.bot.send_message(
            OWNER_CHAT_ID,
            f"⚠️ [{source}] @{username} available, tapi akun belum login. Pakai /login dulu.",
        )
        return

    result = await keeper.claim_username(user_client, username)

    if result["success"]:
        astore.mark_claimed(username, result["channel_id"])
        await app.bot.send_message(
            OWNER_CHAT_ID,
            f"✅ [{source}] @{username} berhasil di-keep! Channel dibikin -> https://t.me/{username}",
        )
    else:
        # Kalau channel sempat kebikin tapi assign-nya gagal karena kalah
        # cepat/invalid, tetap catat sebagai "sudah dicoba" biar gak diulang
        # terus-terusan setiap putaran priority_loop.
        permanent_fail = result["error"] in ("username_occupied", "username_invalid")
        if permanent_fail:
            astore.mark_claimed(username, result.get("channel_id"))
        await app.bot.send_message(
            OWNER_CHAT_ID,
            f"⚠️ [{source}] Gagal keep @{username}: {_claim_error_text(result['error'])}",
        )


@_owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot auto-keep aktif.\n\n"
        "/login +62xxx - login akun pribadi (sekali di awal)\n"
        "/code 12345 - masukin kode OTP\n"
        "/password xxxx - masukin password 2FA (kalau diminta)\n"
        "/logout - logout & hapus sesi\n\n"
        "/autokeep user1 user2 ... - tambah ke daftar prioritas (dicek cepat, independen)\n"
        "/autokeeplist - lihat daftar prioritas\n"
        "/hapus username - hapus dari daftar prioritas\n"
        "/riwayat - lihat username yang sudah diproses (sukses/gagal)"
    )


@_owner_only
async def cmd_autokeep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: /autokeep username1 username2")
        return
    added = [u.lstrip("@").lower() for u in context.args if astore.add_priority(u)]
    if added:
        await update.message.reply_text("Ditambahkan ke prioritas: " + ", ".join(added))
    else:
        await update.message.reply_text("Tidak ada yang baru (sudah ada di prioritas / sudah pernah diklaim).")


@_owner_only
async def cmd_hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: /hapus username1")
        return
    ok = astore.remove_priority(context.args[0])
    await update.message.reply_text("Dihapus dari prioritas." if ok else "Username tidak ada di daftar prioritas.")


@_owner_only
async def cmd_autokeeplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lst = astore.get_priority_list()
    if not lst:
        await update.message.reply_text("Daftar prioritas masih kosong.")
        return
    await update.message.reply_text("Prioritas:\n" + "\n".join(f"@{u}" for u in lst))


@_owner_only
async def cmd_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    claimed = astore.get_claimed()
    if not claimed:
        await update.message.reply_text("Belum ada riwayat.")
        return
    lines = []
    for u, info in claimed.items():
        cid = info.get("channel_id")
        lines.append(f"@{u} -> channel_id {cid}" if cid else f"@{u} -> gagal (tanpa channel)")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


@_owner_only
async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _pending_login
    if not context.args:
        await update.message.reply_text("Contoh: /login +6281234567890")
        return

    if await user_client.is_user_authorized():
        await update.message.reply_text("Sudah login. Pakai /logout dulu kalau mau ganti akun.")
        return

    phone = context.args[0].strip()
    try:
        sent = await user_client.send_code_request(phone)
    except Exception as e:
        await update.message.reply_text(f"Gagal kirim kode: {type(e).__name__}: {e}")
        return

    _pending_login = {"phone": phone, "phone_code_hash": sent.phone_code_hash, "step": "code"}
    await update.message.reply_text(
        "Kode OTP sudah dikirim ke akun Telegram kamu. Balas pakai:\n/code 12345"
    )
    try:
        await update.message.delete()  # nomor HP gak perlu nangkring di chat
    except Exception:
        pass


@_owner_only
async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _pending_login
    if not _pending_login or _pending_login.get("step") != "code":
        await update.message.reply_text("Gak ada proses login yang nunggu kode. Mulai dengan /login dulu.")
        return
    if not context.args:
        await update.message.reply_text("Contoh: /code 12345")
        return

    code = context.args[0].strip()
    try:
        await user_client.sign_in(
            phone=_pending_login["phone"],
            code=code,
            phone_code_hash=_pending_login["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        _pending_login["step"] = "password"
        await update.message.reply_text(
            "Akun ini pakai verifikasi 2 langkah. Balas pakai:\n/password password_kamu"
        )
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    except Exception as e:
        await update.message.reply_text(f"Gagal login: {type(e).__name__}: {e}")
        _pending_login = None
        return

    astore.save_session(user_client.session.save())
    me = await user_client.get_me()
    _pending_login = None
    await update.message.reply_text(f"✅ Berhasil login sebagai {me.first_name} (@{me.username}).")
    try:
        await update.message.delete()
    except Exception:
        pass


@_owner_only
async def cmd_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _pending_login
    if not _pending_login or _pending_login.get("step") != "password":
        await update.message.reply_text("Gak ada proses login yang nunggu password 2FA.")
        return
    if not context.args:
        await update.message.reply_text("Contoh: /password password_kamu")
        return

    password = " ".join(context.args)
    try:
        await user_client.sign_in(password=password)
    except Exception as e:
        await update.message.reply_text(f"Gagal login (password salah?): {type(e).__name__}: {e}")
        return

    astore.save_session(user_client.session.save())
    me = await user_client.get_me()
    _pending_login = None
    await update.message.reply_text(f"✅ Berhasil login sebagai {me.first_name} (@{me.username}).")
    try:
        await update.message.delete()  # hapus password dari chat
    except Exception:
        pass


@_owner_only
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _pending_login
    try:
        if await user_client.is_user_authorized():
            await user_client.log_out()
    except Exception as e:
        logger.exception(f"Gagal log_out ke Telegram: {e}")
    astore.clear_session()
    _pending_login = None
    await update.message.reply_text("Sudah logout & sesi dihapus. Pakai /login buat login akun lain.")


async def on_relay_message(event, app: Application):
    """Dengerin pesan di grup relay LEWAT USERBOT (Telethon MTProto), BUKAN
    lewat Bot API -- soalnya Telegram sengaja TIDAK ngirim update ke sebuah
    bot kalau pesannya berasal dari bot lain (biar gak infinite loop
    bot-ke-bot), walaupun privacy mode sudah dimatiin/jadi admin. Akun
    pribadi (userbot) gak kena batasan ini, jadi dia yang dipakai buat
    "dengerin" pesan dari bot 1 di grup relay.

    Setiap baris yang match pola '@user 🟢 Available' langsung dipicu buat
    diklaim, tanpa nunggu apa-apa."""
    text = event.raw_text or ""
    for match in AVAILABLE_LINE_RE.finditer(text):
        username = match.group(1)
        asyncio.create_task(try_claim_and_notify(app, username, "relay bot 1"))


async def priority_loop(app: Application):
    """Loop cepat, cuma muterin daftar /autokeep sendiri -- independen dari
    bot 1. Cek + langsung klaim di iterasi yang sama kalau ketemu available."""
    while True:
        lst = astore.get_priority_list()
        if not lst:
            await asyncio.sleep(10)
            continue

        for username in lst:
            if astore.is_claimed(username):
                continue
            try:
                raw_status = await pool.check(username)
                if raw_status == Status.AVAILABLE:
                    await try_claim_and_notify(app, username, "priority list")
            except Exception as e:
                logger.exception(f"Gagal cek prioritas @{username}: {e}")
            await asyncio.sleep(PRIORITY_CHECK_DELAY)


async def _post_init(app: Application):
    global pool, user_client

    # Bot session (Telethon + BOT_TOKEN) -- buat resolveUsername daftar prioritas.
    os.makedirs("sessions", exist_ok=True)
    bot_mtproto = TelegramClient("sessions/autokeep_worker", API_ID, API_HASH)
    await bot_mtproto.start(bot_token=BOT_TOKEN)
    pool = UsernamePool([Worker(client=bot_mtproto, name="autokeep_worker")], min_delay=1.3)

    # Userbot session (akun pribadi) -- load dari storage kalau sudah pernah
    # login sebelumnya lewat /login. Kalau belum pernah, connect tetap jalan
    # tapi statusnya "belum authorized" sampai /login dijalankan.
    saved_session = astore.get_session()
    user_client = TelegramClient(StringSession(saved_session), API_ID, API_HASH)
    await user_client.connect()

    if await user_client.is_user_authorized():
        me = await user_client.get_me()
        logger.info(f"Userbot sudah login sebagai: {me.first_name} (@{me.username})")
    else:
        logger.info("Userbot belum login. Pakai /login di chat buat login.")

    # Dengerin grup relay LEWAT USERBOT (bukan Bot API) -- lihat penjelasan
    # di on_relay_message soal kenapa harus lewat sini.
    user_client.add_event_handler(
        lambda event: on_relay_message(event, app),
        events.NewMessage(chats=RELAY_CHAT_ID),
    )

    await app.bot.set_my_commands([
        BotCommand("start", "Mulai / lihat daftar command"),
        BotCommand("login", "Login akun pribadi (buat klaim username)"),
        BotCommand("code", "Masukin kode OTP setelah /login"),
        BotCommand("password", "Masukin password 2FA (kalau diminta)"),
        BotCommand("logout", "Logout & hapus sesi akun pribadi"),
        BotCommand("autokeep", "Tambah username ke daftar prioritas"),
        BotCommand("autokeeplist", "Lihat daftar prioritas"),
        BotCommand("hapus", "Hapus username dari daftar prioritas"),
        BotCommand("riwayat", "Lihat riwayat klaim"),
    ])

    asyncio.create_task(priority_loop(app))
    await app.bot.send_message(OWNER_CHAT_ID, "✅ Autokeep bot siap, dengerin grup relay & priority list.")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("code", cmd_code))
    app.add_handler(CommandHandler("password", cmd_password))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("autokeep", cmd_autokeep))
    app.add_handler(CommandHandler("autokeeplist", cmd_autokeeplist))
    app.add_handler(CommandHandler("hapus", cmd_hapus))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))

    logger.info("Autokeep bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
