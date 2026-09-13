"""
Penyimpanan sederhana berbasis file JSON untuk:
- "priority": daftar username yang ditambahkan lewat /autokeep, dicek dengan
  jeda ketat (independen dari watchlist bot 1).
- "claimed": riwayat username yang SUDAH berhasil diklaim (dibikinkan
  channel-nya), biar gak dicoba klaim ulang dan biar ada catatan channel_id
  masing-masing.

CATATAN soal persistensi: sama seperti storage.py di bot 1, file JSON ini
bisa hilang kalau di-redeploy di platform yang filesystem-nya ephemeral
(lihat catatan di storage.py). Kalau bot 1 sudah dipindah ke solusi
persisten (mis. backup ke Telegram), sebaiknya autokeep_storage.py ini
dipindah juga pakai pola yang sama -- ini file terpisah supaya gampang
diganti belakangan tanpa nyentuh storage.py punya bot 1.
"""

import json
import os
from threading import Lock

DATA_FILE = os.getenv("AUTOKEEP_DATA_FILE", "autokeep_data.json")
_lock = Lock()


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"priority": {}, "claimed": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {"priority": {}, "claimed": {}}
    data.setdefault("priority", {})
    data.setdefault("claimed", {})
    return data


def _save(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_priority(username: str) -> bool:
    """True kalau berhasil ditambahkan (belum ada di priority DAN belum pernah diklaim)."""
    username = username.strip().lstrip("@").lower()
    with _lock:
        data = _load()
        if username in data["priority"] or username in data["claimed"]:
            return False
        data["priority"][username] = {}
        _save(data)
        return True


def remove_priority(username: str) -> bool:
    username = username.strip().lstrip("@").lower()
    with _lock:
        data = _load()
        if username in data["priority"]:
            del data["priority"][username]
            _save(data)
            return True
        return False


def get_priority_list() -> list[str]:
    with _lock:
        return list(_load()["priority"].keys())


def is_claimed(username: str) -> bool:
    username = username.strip().lstrip("@").lower()
    with _lock:
        return username in _load()["claimed"]


def mark_claimed(username: str, channel_id: int) -> None:
    """Catat username sebagai sudah berhasil diklaim, sekaligus keluarkan
    dari daftar priority kalau dia ada di situ."""
    username = username.strip().lstrip("@").lower()
    with _lock:
        data = _load()
        data["claimed"][username] = {"channel_id": channel_id}
        data["priority"].pop(username, None)
        _save(data)


def get_claimed() -> dict:
    with _lock:
        return _load()["claimed"]


def save_session(session_string: str) -> None:
    """Simpan session string hasil login (lewat /login di bot) biar gak perlu
    login ulang tiap restart. Disimpan di file JSON yang sama dengan
    priority/claimed -- kalau file ini sudah dipindah ke Railway Volume,
    session ini ikut aman juga."""
    with _lock:
        data = _load()
        data["session_string"] = session_string
        _save(data)


def get_session() -> str:
    """Return session string tersimpan, atau string kosong kalau belum pernah login."""
    with _lock:
        return _load().get("session_string", "")


def clear_session() -> None:
    with _lock:
        data = _load()
        data.pop("session_string", None)
        _save(data)
