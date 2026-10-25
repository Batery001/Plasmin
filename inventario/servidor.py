"""Reglas de stock de Plasma Store sobre pos.sqlite3.

kind "in" suma, kind "out" resta, y cada cambio queda en stock_movements,
igual que en el punto de venta.
"""

from __future__ import annotations

import os
import sqlite3
import unicodedata

DB_PATH = os.environ.get("DB_PATH", r"C:\Users\bater\Desktop\plasma store\data\pos.sqlite3")


def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def normalizar(texto: str) -> str:
    base = unicodedata.normalize("NFD", (texto or "").strip().lower())
    return "".join(c for c in base if unicodedata.category(c) != "Mn")


def formato_cantidad(valor: float) -> str:
    if float(valor).is_integer():
        return str(int(valor))
    return f"{valor:g}"


def listar_productos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, name, stock_qty
        FROM products
        WHERE active = 1
        ORDER BY name COLLATE NOCASE;
        """
    ).fetchall()


def buscar_producto(conn: sqlite3.Connection, texto: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    pedido = normalizar(texto)
    if not pedido:
        return None, []
    filas = listar_productos(conn)
    exactos = [fila for fila in filas if normalizar(fila["name"]) == pedido]
    if len(exactos) == 1:
        return exactos[0], []
    if len(exactos) > 1:
        return None, exactos
    contenidos = [fila for fila in filas if pedido in normalizar(fila["name"])]
    if len(contenidos) == 1:
        return contenidos[0], []
    if len(contenidos) > 1:
        return None, contenidos
    return None, []


def texto_opciones(filas: list[sqlite3.Row]) -> str:
    nombres = "\n".join(f"- {fila['name']}" for fila in filas[:8])
    extra = ""
    if len(filas) > 8:
        extra = f"\nY {len(filas) - 8} más."
    return f"Hay varios productos con ese nombre. ¿Cuál?\n{nombres}{extra}"


def aplicar_movimiento(conn: sqlite3.Connection, product_id: int, kind: str, cantidad: float, nota: str) -> float:
    if kind not in ("in", "out"):
        raise ValueError("kind debe ser entrada o salida.")
    delta = abs(float(cantidad))
    if kind == "out":
        delta = -delta
    conn.execute("BEGIN IMMEDIATE;")
    try:
        fila = conn.execute(
            "SELECT name, stock_qty FROM products WHERE id = ?;",
            (int(product_id),),
        ).fetchone()
        if fila is None:
            raise ValueError("Producto no encontrado.")
        nuevo = float(fila["stock_qty"]) + delta
        if nuevo < -1e-9:
            raise ValueError("insuficiente")
        conn.execute(
            "UPDATE products SET stock_qty = ? WHERE id = ?;",
            (nuevo, int(product_id)),
        )
        conn.execute(
            """
            INSERT INTO stock_movements(product_id, kind, qty_delta, stock_after, note, sale_id)
            VALUES (?, ?, ?, ?, ?, NULL);
            """,
            (int(product_id), kind, delta, nuevo, nota[:240]),
        )
        conn.commit()
        return nuevo
    except Exception:
        conn.rollback()
        raise


def cantidad_valida(valor) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if numero <= 0:
        return None
    return numero


def ejecutar(payload: dict) -> str:
    accion = str(payload.get("accion") or "desconocida").strip().lower()
    producto = payload.get("producto")
    producto = str(producto).strip() if producto else ""
    nota = str(payload.get("nota") or "").strip()
    cantidad = cantidad_valida(payload.get("cantidad"))

    conn = conectar()
    try:
        if accion == "listar":
            filas = listar_productos(conn)
            if not filas:
                return "El inventario no tiene productos activos."
            lineas = [f"- {fila['name']}: {formato_cantidad(fila['stock_qty'])}" for fila in filas]
            return "Inventario:\n" + "\n".join(lineas)

        if accion == "consulta":
            if not producto:
                return "¿De qué producto quieres saber el stock?"
            elegido, opciones = buscar_producto(conn, producto)
            if opciones:
                return texto_opciones(opciones)
            if elegido is None:
                return f"No encuentro «{producto}» en el catálogo."
            return f"Quedan {formato_cantidad(elegido['stock_qty'])} de {elegido['name']}."

        if accion in ("entrada", "salida"):
            if not producto or cantidad is None:
                ejemplo = "llegaron 5 Booster Black Bolt" if accion == "entrada" else "salieron 2 Booster Black Bolt"
                return f"Necesito el producto y la cantidad. Ejemplo: «{ejemplo}»."
            elegido, opciones = buscar_producto(conn, producto)
            if opciones:
                return texto_opciones(opciones)
            if elegido is None:
                return f"No encuentro «{producto}» en el catálogo. No anoté el movimiento."
            product_id = int(elegido["id"])
            nombre = str(elegido["name"])
        else:
            return (
                "Puedo anotar entradas, salidas y consultar el stock. "
                "Ejemplos: «llegaron 5 Booster Black Bolt» o «¿cuánto queda de fundas protectoras?»."
            )
    finally:
        conn.close()

    kind = "in" if accion == "entrada" else "out"
    escritura = conectar()
    try:
        try:
            nuevo = aplicar_movimiento(escritura, product_id, kind, cantidad, f"Telegram: {nota}")
        except ValueError as exc:
            if str(exc) != "insuficiente":
                raise
            actual = escritura.execute(
                "SELECT stock_qty FROM products WHERE id = ?;",
                (product_id,),
            ).fetchone()
            hay = formato_cantidad(actual["stock_qty"]) if actual else "?"
            return f"No alcanza. {nombre} tiene {hay}."
    finally:
        escritura.close()

    verbo = "entrada" if accion == "entrada" else "salida"
    return f"Anoté {verbo} de {formato_cantidad(cantidad)}. {nombre} queda en {formato_cantidad(nuevo)}."
