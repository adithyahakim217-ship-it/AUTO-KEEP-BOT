"""
keeper.py
Modul buat BENERAN "keep" username yang lagi AVAILABLE: bikin channel baru,
lalu nempelin username itu ke channel tersebut.

PENTING: fungsi di sini HARUS dipanggil pakai TelegramClient yang login
sebagai AKUN PRIBADI (lewat StringSession hasil generate_session.py), BUKAN
client yang login pakai bot token. `channels.createChannel` bisa saja
dipanggil bot, tapi assign username publik ke channel yang baru dibuat
sengaja kita pakai akun pribadi sesuai keputusan desain -- channel yang
dibikin jadi kepemilikan akun kamu sendiri, gampang dikelola/dipindah
kepemilikannya nanti kalau perlu.

Kenapa bikin channel (bukan ganti username akun sendiri):
Satu akun cuma bisa punya 1 username utama (+ beberapa username tambahan
lewat fitur "multiple usernames", tapi tetap terbatas). Channel bisa dibikin
banyak (walau ada limit jumlah per akun, lihat catatan ChannelsTooMuchError
di bawah), masing-masing punya slot username sendiri -- jadi ini cara paling
scalable buat "ngumpulin" banyak username sekaligus.
"""

from telethon import TelegramClient
from telethon.errors import (
    ChannelsTooMuchError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameOccupiedError,
)
from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest


async def claim_username(user_client: TelegramClient, username: str) -> dict:
    """
    Coba klaim satu username: bikin channel baru berjudul persis nama
    username-nya (tanpa bio), lalu assign username itu ke channel tersebut.

    Return:
        {"success": True, "channel_id": int, "channel": Channel, "error": None}   -- berhasil
        {"success": False, "channel_id": int|None, "channel": Channel|None, "error": "kode_error"} -- gagal

    Kode error yang mungkin muncul:
        "channel_limit_reached"     -- akun sudah kena limit jumlah channel/grup
        "flood_wait:<detik>"        -- kena rate limit, coba lagi setelah sekian detik
        "create_failed:<pesan>"     -- gagal pas bikin channel (jarang)
        "username_occupied"         -- KALAH CEPAT, ada yang ambil duluan
        "username_invalid"          -- Telegram bilang invalid pas assign (jarang,
                                        kemungkinan barusan kena banned juga)
        "assign_failed:<pesan>"     -- gagal assign karena sebab lain
    """
    try:
        result = await user_client(CreateChannelRequest(
            title=username,
            about="",
            megagroup=False,  # channel biasa (broadcast), bukan supergroup
        ))
        channel = result.chats[0]
    except ChannelsTooMuchError:
        return {"success": False, "channel_id": None, "channel": None, "error": "channel_limit_reached"}
    except FloodWaitError as e:
        return {"success": False, "channel_id": None, "channel": None, "error": f"flood_wait:{e.seconds}"}
    except Exception as e:
        return {"success": False, "channel_id": None, "channel": None, "error": f"create_failed:{type(e).__name__}: {e}"}

    try:
        await user_client(UpdateUsernameRequest(channel=channel, username=username))
    except UsernameOccupiedError:
        # Kalah cepat -- orang/bot lain sudah ambil duluan. Channel kosong
        # (tanpa username) ini tetap ada, biar bisa dihapus manual belakangan.
        return {"success": False, "channel_id": channel.id, "channel": channel, "error": "username_occupied"}
    except UsernameInvalidError:
        return {"success": False, "channel_id": channel.id, "channel": channel, "error": "username_invalid"}
    except FloodWaitError as e:
        return {"success": False, "channel_id": channel.id, "channel": channel, "error": f"flood_wait:{e.seconds}"}
    except Exception as e:
        return {"success": False, "channel_id": channel.id, "channel": channel, "error": f"assign_failed:{type(e).__name__}: {e}"}

    return {"success": True, "channel_id": channel.id, "channel": channel, "error": None}
