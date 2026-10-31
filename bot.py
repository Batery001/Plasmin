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

from inventario.servidor import (
    conectar,
    ejecutar,
    listar_productos,
    normalizar,
    situacion_productos,
    texto_alertas,
    texto_producto_alerta,
)

RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / "bot_estado.json"
CLAVE = RAIZ / "claves.env"

SISTEMA = """Eres el intérprete del inventario de Plasma Store. El catálogo ya existe.
Responde solo un JSON con esta forma:
{"accion":"entrada|salida|consulta|listar|pronostico|compras|desconocida","producto":null,"cantidad":null}
entrada = llegó mercadería. salida = se vendió o se sacó. consulta = pregunta el stock de un producto. listar = pide todo el inventario. pronostico = pregunta cuándo se agota o cuánto comprar de un producto. compras = pide el listado de lo que hay que comprar o reponer. desconocida = no es inventario.
Si hay un producto, copia el nombre EXACTO de una línea del catálogo. Si no estás seguro de cuál, usa el texto de la persona. cantidad es un número, o null si no hay número. No inventes cantidad."""

AYUDA = (
    "Puedo anotar entradas, salidas y consultar el stock.\n"
    "Ejemplos:\n"
    "- Llegaron 5 Booster Black Bolt\n"
    "- Vendí 2 Fundas protectoras 66x91 (100 u.)\n"
    "- ¿Cuánto queda de Booster Black Bolt?\n"
    "- ¿Cuánto comprar de Booster Black Bolt?\n"
    "- Lista el inventario\n"
    "- Me das el listado de lo que hay que comprar\n"
    "Si una venta deja un producto en el mínimo o en el punto de pedido, te aviso solo de ese."
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


def leer_estado() -> dict:
    estado = {"offset": 0, "chats": [], "alertas": {}}
    if not ESTADO.exists():
        return estado
    try:
        data = json.loads(ESTADO.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return estado
    if not isinstance(data, dict):
        return estado
    try:
        estado["offset"] = int(data.get("offset") or 0)
    except (TypeError, ValueError):
        estado["offset"] = 0
    chats = []
    for chat in data.get("chats") or []:
        try:
            chats.append(int(chat))
        except (TypeError, ValueError):
            continue
    estado["chats"] = chats
    alertas = data.get("alertas") or {}
    estado["alertas"] = alertas if isinstance(alertas, dict) else {}
    return estado


def guardar_estado(estado: dict) -> None:
    ESTADO.write_text(json.dumps(estado), encoding="utf-8")


def registrar_chat(estado: dict, chat_id: int) -> None:
    if chat_id not in estado["chats"]:
        estado["chats"].append(chat_id)
        guardar_estado(estado)


def nombres_catalogo() -> list[str]:
    conn = conectar()
    try:
        return [fila["name"] for fila in listar_productos(conn)]
    finally:
        conn.close()


def texto_alerta_actual() -> str:
    conn = conectar()
    try:
        return texto_alertas(conn)
    finally:
        conn.close()


def pide_listado_compras(texto: str) -> bool:
    pedido = normalizar(texto)
    if pedido in {"/alertas", "alertas", "/compras", "compras"}:
        return True
    return any(
        frase in pedido
        for frase in (
            "lo que hay que comprar",
            "que hay que comprar",
            "listado de compras",
            "lista de compras",
            "hay que reponer",
            "listado de reposicion",
            "lista de reposicion",
        )
    )


def snapshot_de(items: list[dict]) -> dict:
    return {str(item["id"]): {"stock": item["stock"], "tipo": item["tipo"]} for item in items}


def enviar_alertas(token: str, estado: dict) -> None:
    conn = conectar()
    try:
        items = situacion_productos(conn)
    finally:
        conn.close()
    anterior = estado.get("snapshot") or {}
    nuevo = snapshot_de(items)
    if not anterior:
        estado["snapshot"] = nuevo
        guardar_estado(estado)
        print("[plasmin] inventario base guardado. Aviso solo el producto que baje en una venta.", flush=True)
        return
    if nuevo == anterior:
        return
    cambiaron = []
    for item in items:
        previo = anterior.get(str(item["id"])) or {}
        try:
            stock_previo = float(previo.get("stock"))
        except (TypeError, ValueError):
            continue
        if item["tipo"] and item["stock"] < stock_previo:
            cambiaron.append(item)
    snapshot = dict(nuevo)
    for item in cambiaron:
        mensaje = texto_producto_alerta(item)
        enviado = not estado["chats"]
        for chat_id in list(estado["chats"]):
            try:
                telegram(token, "sendMessage", {"chat_id": chat_id, "text": mensaje})
            except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError) as exc:
                print(f"[plasmin] alerta: {exc}", flush=True)
                enviado = False
                break
            enviado = True
            print(f"[plasmin] alerta de {item['nombre']}", flush=True)
        if not enviado:
            snapshot[str(item["id"])] = anterior[str(item["id"])]
    estado["snapshot"] = snapshot
    guardar_estado(estado)


def trozos(texto: str, limite: int = 4000) -> list[str]:
    if len(texto) <= limite:
        return [texto]
    partes = []
    actual: list[str] = []
    largo = 0
    for linea in texto.splitlines():
        extra = len(linea) + 1
        if actual and largo + extra > limite:
            partes.append("\n".join(actual))
            actual = [linea]
            largo = extra
        else:
            actual.append(linea)
            largo += extra
    if actual:
        partes.append("\n".join(actual))
    return partes


def responder_texto(token: str, chat_id: int, texto: str) -> str:
    pedido = texto.strip().lower()
    if pedido in {"/start", "/ayuda", "ayuda", "help"}:
        return AYUDA
    if pide_listado_compras(texto):
        return texto_alerta_actual() or "No hay productos bajo el mínimo ni en punto de pedido."
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
    estado = leer_estado()
    if estado["chats"]:
        enviar_alertas(token, estado)
    else:
        print("[plasmin] sin chat guardado. El primer mensaje registra a quién avisar.", flush=True)
    proxima_revision = time.time()
    while True:
        try:
            data = telegram(
                token,
                "getUpdates",
                {"offset": estado["offset"], "timeout": 25, "allowed_updates": ["message"]},
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            print(f"[plasmin] telegram: {exc}", flush=True)
            time.sleep(5)
            continue
        for update in data.get("result", []):
            estado["offset"] = int(update["update_id"]) + 1
            guardar_estado(estado)
            message = update.get("message") or {}
            texto = (message.get("text") or "").strip()
            chat = message.get("chat") or {}
            if not texto or "id" not in chat:
                continue
            chat_id = int(chat["id"])
            registrar_chat(estado, chat_id)
            respuesta = responder_texto(token, chat_id, texto)
            telegram(token, "sendMessage", {"chat_id": chat_id, "text": respuesta})
            print(f"[plasmin] {texto[:80]!r} -> {respuesta.splitlines()[0][:80]}", flush=True)
        if data.get("result") or time.time() >= proxima_revision:
            enviar_alertas(token, estado)
            proxima_revision = time.time() + 60


if __name__ == "__main__":
    main()
