"""Plasmin: Telegram, ChatGPT y el inventario de Plasma Store.

Lee el mensaje, pide a ChatGPT la acción y consulta o anota el stock en pos.sqlite3.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from inventario.servidor import ejecutar, listar_productos, conectar

RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / "bot_estado.json"
CLAVE = RAIZ / "claves.env"

SISTEMA = """Eres el intérprete del inventario de Plasma Store. El catálogo ya existe.
Responde solo un JSON con esta forma:
{"accion":"entrada|salida|consulta|listar|desconocida","producto":null,"cantidad":null}
entrada = llegó mercadería. salida = se vendió o se sacó. consulta = pregunta el stock de un producto. listar = pide todo el inventario. desconocida = no es inventario.
Si hay un producto, copia el nombre EXACTO de una línea del catálogo. Si no estás seguro de cuál, usa el texto de la persona. cantidad es un número, o null si no hay número. No inventes cantidad."""

AYUDA = (
    "Puedo anotar entradas, salidas y consultar el stock.\n"
    "Ejemplos:\n"
    "- Llegaron 5 Booster Black Bolt\n"
    "- Vendí 2 Fundas protectoras 66x91 (100 u.)\n"
    "- ¿Cuánto queda de Booster Black Bolt?\n"
    "- Lista el inventario"
)


def cargar_claves() -> None:
    if not CLAVE.exists():
        return
    for linea in CLAVE.read_text(encoding="utf-8").splitlines():
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        nombre, valor = linea.split("=", 1)
        os.environ.setdefault(nombre.strip(), valor.strip())


def pedir(url: str, cuerpo: dict | None = None, headers: dict | None = None) -> dict:
    data = None if cuerpo is None else json.dumps(cuerpo).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if cuerpo is not None else "GET")
    if cuerpo is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def telegram(token: str, metodo: str, cuerpo: dict | None = None) -> dict:
    return pedir(f"https://api.telegram.org/bot{token}/{metodo}", cuerpo)


def interpretar(api_key: str, texto: str, nombres: list[str]) -> dict:
    cuerpo = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SISTEMA},
            {"role": "user", "content": "Catálogo:\n" + "\n".join(nombres) + "\n\nMensaje del local:\n" + texto},
        ],
    }
    data = pedir(
        "https://api.openai.com/v1/chat/completions",
        cuerpo,
        {"Authorization": f"Bearer {api_key}"},
    )
    return json.loads(data["choices"][0]["message"]["content"])


def leer_offset() -> int:
    if not ESTADO.exists():
        return 0
    try:
        return int(json.loads(ESTADO.read_text(encoding="utf-8")).get("offset", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def guardar_offset(offset: int) -> None:
    ESTADO.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def nombres_catalogo() -> list[str]:
    conn = conectar()
    try:
        return [fila["name"] for fila in listar_productos(conn)]
    finally:
        conn.close()


def responder_texto(token: str, chat_id: int, texto: str) -> str:
    if texto.strip().lower() in {"/start", "/ayuda", "ayuda", "help"}:
        return AYUDA
    api_key = os.environ["OPENAI_API_KEY"]
    try:
        datos = interpretar(api_key, texto, nombres_catalogo())
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
        print(f"[plasmin] chatgpt: {exc}", flush=True)
        return "No pude consultar a ChatGPT. Revisa la API key e inténtalo de nuevo."
    return ejecutar(
        {
            "accion": datos.get("accion"),
            "producto": datos.get("producto"),
            "cantidad": datos.get("cantidad"),
            "nota": texto,
        }
    )


def main() -> None:
    cargar_claves()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Faltan TELEGRAM_BOT_TOKEN y OPENAI_API_KEY.")
    telegram(token, "deleteWebhook", {"drop_pending_updates": False})
    print("[plasmin] escuchando. La base es el pos.sqlite3 de Plasma Store.", flush=True)
    offset = leer_offset()
    while True:
        try:
            data = telegram(token, "getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            print(f"[plasmin] telegram: {exc}", flush=True)
            time.sleep(5)
            continue
        for update in data.get("result", []):
            offset = int(update["update_id"]) + 1
            guardar_offset(offset)
            message = update.get("message") or {}
            texto = (message.get("text") or "").strip()
            chat = message.get("chat") or {}
            if not texto or "id" not in chat:
                continue
            respuesta = responder_texto(token, int(chat["id"]), texto)
            telegram(token, "sendMessage", {"chat_id": chat["id"], "text": respuesta})
            print(f"[plasmin] {texto[:80]!r} -> {respuesta.splitlines()[0][:80]}", flush=True)


if __name__ == "__main__":
    main()
