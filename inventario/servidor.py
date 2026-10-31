"""Reglas de stock de Plasma Store sobre pos.sqlite3.

kind "in" suma, kind "out" resta, y cada cambio queda en stock_movements,
igual que en el punto de venta.
"""

from __future__ import annotations

import math
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


def demanda_observada(conn: sqlite3.Connection, product_id: int) -> tuple[float, int, float]:
    """Unidades vendidas, días distintos con venta y venta promedio por día."""
    fila = conn.execute(
        """
        SELECT COALESCE(SUM(si.qty), 0) AS unidades,
               COUNT(DISTINCT date(s.created_at)) AS dias
        FROM sale_items si
        JOIN sales s ON s.id = si.sale_id
        WHERE si.product_id = ?;
        """,
        (int(product_id),),
    ).fetchone()
    unidades = float(fila["unidades"] or 0)
    dias = int(fila["dias"] or 0)
    if dias <= 0 or unidades <= 0:
        return 0.0, 0, 0.0
    return unidades, dias, unidades / dias


def plazo_entrega(lead_time_dias: int | None = None) -> int:
    entrega = lead_time_dias if lead_time_dias is not None else int(os.environ.get("LEAD_TIME_DIAS", "7"))
    return entrega if entrega >= 1 else 1


def calcular_reposicion(stock: float, seguridad: float, demanda: float, entrega: int) -> dict:
    ciclo = entrega
    punto_pedido = demanda * entrega + seguridad
    objetivo = demanda * (entrega + ciclo) + seguridad
    return {
        "dias_restantes": stock / demanda,
        "punto_pedido": punto_pedido,
        "pedir": max(0, math.ceil(objetivo - stock)),
        "riesgo": stock <= punto_pedido,
        "entrega": entrega,
        "ciclo": ciclo,
    }


def pronosticar(conn: sqlite3.Connection, product_id: int, lead_time_dias: int | None = None) -> str:
    """Pronóstico de quiebre y cantidad a pedir.

    La demanda diaria es el promedio de las ventas ya registradas.
    Con pocos días de historia el resultado queda marcado como provisional.
    """
    fila = conn.execute(
        "SELECT name, stock_qty, min_stock_qty FROM products WHERE id = ?;",
        (int(product_id),),
    ).fetchone()
    if fila is None:
        return "Producto no encontrado."

    nombre = str(fila["name"])
    stock = float(fila["stock_qty"])
    seguridad = max(0.0, float(fila["min_stock_qty"] or 0))
    entrega = plazo_entrega(lead_time_dias)
    unidades, dias, demanda = demanda_observada(conn, product_id)

    if demanda <= 0:
        return (
            f"{nombre}: stock {formato_cantidad(stock)}. "
            "No hay ventas registradas, así que no estimo una fecha de agotamiento ni una compra."
        )

    cuenta = calcular_reposicion(stock, seguridad, demanda, entrega)
    dias_restantes = cuenta["dias_restantes"]
    punto_pedido = cuenta["punto_pedido"]
    pedir = cuenta["pedir"]
    riesgo = cuenta["riesgo"]
    ciclo = cuenta["ciclo"]
    provisional = dias < 14

    estado = (
        f"Riesgo de quiebre: al ritmo observado se cruza el punto de pedido. "
        f"Quedan unos {formato_cantidad(round(dias_restantes, 1))} días de venta."
        if riesgo
        else (
            f"Sin riesgo inmediato. Al ritmo observado quedan unos "
            f"{formato_cantidad(round(dias_restantes, 1))} días de venta."
        )
    )
    aviso = ""
    if provisional:
        aviso = f" Este promedio sale de {dias} día(s) con venta y {formato_cantidad(unidades)} unidades. Es provisional."

    return (
        f"{nombre}\n"
        f"1. {estado}{aviso}\n"
        f"2. Punto de pedido: {formato_cantidad(round(punto_pedido, 1))} "
        f"({formato_cantidad(round(demanda, 2))} al día × {entrega} días de entrega + "
        f"{formato_cantidad(seguridad)} de stock mínimo).\n"
        f"3. Comprar {pedir} unidades para cubrir el próximo ciclo de {ciclo} días y reponer el mínimo. "
        f"Stock actual: {formato_cantidad(stock)}."
    )


def situacion_productos(conn: sqlite3.Connection) -> list[dict]:
    """Stock y tipo de aviso de cada producto activo."""
    entrega = plazo_entrega()
    filas = conn.execute(
        """
        SELECT id, name, stock_qty, min_stock_qty
        FROM products
        WHERE active = 1
        ORDER BY name COLLATE NOCASE;
        """
    ).fetchall()
    items = []
    for fila in filas:
        stock = float(fila["stock_qty"])
        seguridad = max(0.0, float(fila["min_stock_qty"] or 0))
        _unidades, dias, demanda = demanda_observada(conn, int(fila["id"]))
        tipo = ""
        pedir = 0
        dias_restantes = None
        provisional = False
        if demanda > 0:
            cuenta = calcular_reposicion(stock, seguridad, demanda, entrega)
            provisional = dias < 14
            if cuenta["riesgo"] and cuenta["pedir"] > 0:
                tipo = "comprar"
                pedir = cuenta["pedir"]
                dias_restantes = cuenta["dias_restantes"]
        if not tipo and seguridad > 0 and stock <= seguridad:
            tipo = "minimo"
        items.append(
            {
                "id": int(fila["id"]),
                "nombre": str(fila["name"]),
                "stock": stock,
                "minimo": seguridad,
                "tipo": tipo,
                "pedir": pedir,
                "dias": dias_restantes,
                "provisional": provisional,
            }
        )
    return items


def texto_producto_alerta(item: dict) -> str:
    """Aviso de un solo producto que bajó por una venta."""
    stock = formato_cantidad(item["stock"])
    nombre = item["nombre"]
    if item["tipo"] == "comprar":
        extra = ""
        if item["minimo"] > 0 and item["stock"] <= item["minimo"]:
            extra = f", mínimo {formato_cantidad(item['minimo'])}"
        aviso = " La cantidad es provisional." if item["provisional"] else ""
        return (
            f"Plasmin: se está acabando {nombre}.\n"
            f"Quedan {stock}{extra}, unos {formato_cantidad(round(item['dias'], 1))} días de venta. "
            f"Comprar {formato_cantidad(item['pedir'])}.{aviso}"
        )
    return (
        f"Plasmin: se está acabando {nombre}.\n"
        f"Quedan {stock}, mínimo {formato_cantidad(item['minimo'])}."
    )


def texto_alertas(conn: sqlite3.Connection) -> str:
    """Listado completo, solo cuando alguien lo pide."""
    items = situacion_productos(conn)
    comprar = [item for item in items if item["tipo"] == "comprar"]
    minimo = [item for item in items if item["tipo"] == "minimo"]
    if not comprar and not minimo:
        return ""

    lineas = ["Listado de lo que hay que comprar"]
    if comprar:
        lineas.append("Hay que comprar:")
        for item in sorted(comprar, key=lambda actual: (actual["dias"], actual["nombre"])):
            extra = ""
            if item["minimo"] > 0 and item["stock"] <= item["minimo"]:
                extra = f", mínimo {formato_cantidad(item['minimo'])}"
            lineas.append(
                f"- {item['nombre']}: quedan {formato_cantidad(item['stock'])}{extra}, "
                f"unos {formato_cantidad(round(item['dias'], 1))} días. Comprar {formato_cantidad(item['pedir'])}."
            )
    if minimo:
        lineas.append("En el mínimo, sin ventas para calcular la compra:")
        for item in sorted(minimo, key=lambda actual: (actual["stock"], actual["nombre"])):
            lineas.append(
                f"- {item['nombre']}: quedan {formato_cantidad(item['stock'])}, "
                f"mínimo {formato_cantidad(item['minimo'])}."
            )
    lineas.append(f"Plazo de entrega usado: {plazo_entrega()} días.")
    if any(item["provisional"] for item in comprar):
        lineas.append("La cantidad a comprar sale de pocos días de venta, así que es provisional.")
    return "\n".join(lineas)


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
        if accion == "compras":
            return texto_alertas(conn) or "No hay productos bajo el mínimo ni en punto de pedido."

        if accion == "listar":
            filas = listar_productos(conn)
            if not filas:
                return "El inventario no tiene productos activos."
            lineas = [f"- {fila['name']}: {formato_cantidad(fila['stock_qty'])}" for fila in filas]
            return "Inventario:\n" + "\n".join(lineas)

        if accion == "pronostico":
            if not producto:
                return "¿De qué producto quieres el pronóstico de reposición?"
            elegido, opciones = buscar_producto(conn, producto)
            if opciones:
                return texto_opciones(opciones)
            if elegido is None:
                return f"No encuentro «{producto}» en el catálogo."
            return pronosticar(conn, int(elegido["id"]))

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
