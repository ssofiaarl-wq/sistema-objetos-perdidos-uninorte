"""
almacen.py: persistencia del sistema de objetos perdidos (SQLite + carpeta de fotos).

Guarda objetos encontrados y reportes de pérdida con su embedding, sus fotos, los
pares rechazados y las entregas. Devuelve registros en el mismo formato que usa
motor.py, así que se pueden pasar directo a motor.emparejar(...):

    {"id": "OBJ-0001", "nombre": ..., "embedding": np.ndarray, "fotos": [ruta, ...], ...}

Ciclo de vida (solo se guarda lo que cambia por una decisión humana):
    objeto:  disponible -> entregado
    reporte: pendiente  -> resuelto
La "sugerencia" del emparejamiento no se guarda: se recalcula con los registros
activos cada vez, así siempre refleja los rechazos y entregas más recientes.

Las fotos se guardan re-codificadas (sin metadatos EXIF, que pueden traer la
ubicación GPS del celular) y ya rotadas. El embedding se calcula desde esas copias,
por lo que reindexar() puede recalcularlo si algún día cambias de modelo.
"""

import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import motor
from motor import ErrorImagen

# Categorías fijas: clases de equivalencia disjuntas 
CATEGORIAS = ["Electrónica", "Accesorios", "Útiles", "Otro tipo"]

_DATOS = Path(__file__).parent / "datos"


class ErrorDatos(Exception):
    """Datos del formulario inválidos o registro inexistente."""


class ErrorEstado(Exception):
    """La operación no es válida para el estado actual del registro."""


_ESQUEMA = """
CREATE TABLE IF NOT EXISTS objetos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT NOT NULL,
    categoria   TEXT NOT NULL,
    ubicacion   TEXT NOT NULL,
    descripcion TEXT NOT NULL DEFAULT '',
    estado      TEXT NOT NULL DEFAULT 'disponible' CHECK (estado IN ('disponible', 'entregado')),
    modelo      TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    creado      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reportes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT NOT NULL,
    categoria   TEXT NOT NULL DEFAULT '',
    descripcion TEXT NOT NULL DEFAULT '',
    contacto    TEXT NOT NULL DEFAULT '',
    estado      TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'resuelto')),
    modelo      TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    creado      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fotos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo        TEXT NOT NULL CHECK (tipo IN ('objeto', 'reporte')),
    registro_id INTEGER NOT NULL,
    ruta        TEXT NOT NULL,
    orden       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fotos ON fotos (tipo, registro_id);
CREATE TABLE IF NOT EXISTS rechazos (
    reporte_id  INTEGER NOT NULL REFERENCES reportes (id),
    objeto_id   INTEGER NOT NULL REFERENCES objetos (id),
    creado      TEXT NOT NULL,
    PRIMARY KEY (reporte_id, objeto_id)
);
CREATE TABLE IF NOT EXISTS entregas (
    reporte_id  INTEGER PRIMARY KEY REFERENCES reportes (id),
    objeto_id   INTEGER NOT NULL UNIQUE REFERENCES objetos (id),
    similitud   REAL,
    creado      TEXT NOT NULL
);
"""


# ------------------------------------------------------------------ infraestructura

def iniciar(carpeta_datos=None):
    """Crea la carpeta de datos y las tablas si no existen. Llamar una vez al arrancar."""
    global _DATOS
    if carpeta_datos is not None:
        _DATOS = Path(carpeta_datos)
    _DATOS.mkdir(parents=True, exist_ok=True)
    with _conectar() as con:
        con.executescript(_ESQUEMA)


def carpeta_datos():
    """Carpeta donde viven la base de datos y las fotos (ruta absoluta)."""
    return _DATOS.resolve()


@contextmanager
def _conectar():
    """Una conexión por operación: es segura aunque la página atienda varias peticiones a la vez."""
    con = sqlite3.connect(_DATOS / "objetos_perdidos.db", timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def _transaccion():
    """Transacción con bloqueo de escritura: o se aplica todo, o no se aplica nada."""
    with _conectar() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")


def _ahora():
    return datetime.now().isoformat(timespec="seconds")


def _a_blob(embedding):
    return np.asarray(embedding, dtype=np.float32).tobytes()


def _de_blob(blob):
    return np.frombuffer(blob, dtype=np.float32).copy()


# ------------------------------------------------------------------------ códigos

def codigo_objeto(n):
    return f"OBJ-{n:04d}"


def codigo_reporte(n):
    return f"REP-{n:04d}"


def _numero(codigo, prefijo):
    partes = str(codigo).split("-", 1)
    if len(partes) != 2 or partes[0] != prefijo or not partes[1].isdigit():
        raise ErrorDatos(f"Código inválido: {codigo!r} (se esperaba {prefijo}-0001)")
    return int(partes[1])


# -------------------------------------------------------------------------- fotos

def _guardar_foto(origen, destino):
    """Guarda una copia limpia: rotada, en RGB, reducida y sin metadatos EXIF (ni GPS)."""
    motor.validar_imagen(origen)
    with Image.open(origen) as imagen:
        imagen = ImageOps.exif_transpose(imagen).convert("RGB")
        imagen.thumbnail((motor.LADO_MAX_PX, motor.LADO_MAX_PX))
        imagen.save(destino, "JPEG", quality=90)


def _preparar_fotos(fotos, tipo):
    """Copia las fotos válidas a una carpeta nueva. Devuelve (carpeta, rutas, errores)."""
    if isinstance(fotos, (str, Path)):
        fotos = [fotos]
    carpeta = _DATOS / "fotos" / ("objetos" if tipo == "objeto" else "reportes") / uuid.uuid4().hex
    carpeta.mkdir(parents=True)
    guardadas, errores = [], []
    for origen in fotos or []:
        destino = carpeta / f"{len(guardadas) + 1:02d}.jpg"
        try:
            _guardar_foto(origen, destino)
            guardadas.append(destino)
        except (ErrorImagen, OSError) as error:
            destino.unlink(missing_ok=True)
            errores.append((Path(str(origen)).name, str(error)))
    if not guardadas:
        shutil.rmtree(carpeta, ignore_errors=True)
        detalle = "; ".join(m for _, m in errores) or "no se recibieron fotos"
        raise ErrorImagen(f"Ninguna foto es utilizable: {detalle}")
    return carpeta, guardadas, errores


def _rutas_fotos(con, tipo, numero):
    filas = con.execute(
        "SELECT ruta FROM fotos WHERE tipo = ? AND registro_id = ? ORDER BY orden", (tipo, numero)
    ).fetchall()
    return [str(_DATOS / f["ruta"]) for f in filas]


def _fotos_por_registro(con, tipo):
    agrupadas = {}
    filas = con.execute(
        "SELECT registro_id, ruta FROM fotos WHERE tipo = ? ORDER BY registro_id, orden", (tipo,)
    ).fetchall()
    for f in filas:
        agrupadas.setdefault(f["registro_id"], []).append(str(_DATOS / f["ruta"]))
    return agrupadas


# ---------------------------------------------------------------------- registro

def _registrar(tipo, sql_insert, parametros, fotos, modelo):
    """Guarda fotos, calcula el embedding y inserta el registro en una sola operación.

    Si algo falla, no queda ni fila ni carpeta. Devuelve (numero, errores_de_fotos).
    """
    carpeta, guardadas, errores = _preparar_fotos(fotos, tipo)
    try:
        embedding, _, _ = motor.embedding_de_fotos(modelo, guardadas, usar_cache=False)
        with _transaccion() as con:
            cursor = con.execute(
                sql_insert, (*parametros, motor.NOMBRE_MODELO, _a_blob(embedding), _ahora())
            )
            numero = cursor.lastrowid
            for orden, foto in enumerate(guardadas, start=1):
                con.execute(
                    "INSERT INTO fotos (tipo, registro_id, ruta, orden) VALUES (?, ?, ?, ?)",
                    (tipo, numero, foto.relative_to(_DATOS).as_posix(), orden),
                )
        return numero, errores
    except BaseException:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise


def _texto_obligatorio(valor, campo):
    valor = (valor or "").strip()
    if not valor:
        raise ErrorDatos(f"{campo} es obligatorio.")
    return valor


def registrar_objeto(modelo, nombre, categoria, ubicacion, fotos, descripcion=""):
    """Registra un objeto encontrado con una o varias fotos. Devuelve (registro, errores_de_fotos)."""
    nombre = _texto_obligatorio(nombre, "El nombre")
    ubicacion = _texto_obligatorio(ubicacion, "La ubicación")
    if categoria not in CATEGORIAS:
        raise ErrorDatos(f"Categoría inválida: {categoria!r}. Opciones: {', '.join(CATEGORIAS)}")
    numero, errores = _registrar(
        "objeto",
        "INSERT INTO objetos (nombre, categoria, ubicacion, descripcion, modelo, embedding, creado) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (nombre, categoria, ubicacion, (descripcion or "").strip()),
        fotos,
        modelo,
    )
    return obtener_objeto(codigo_objeto(numero)), errores


def registrar_reporte(modelo, nombre, foto, categoria=None, descripcion="", contacto=""):
    """Registra un reporte de pérdida (normalmente con una sola foto). Devuelve (registro, errores)."""
    nombre = _texto_obligatorio(nombre, "El nombre")
    if categoria and categoria not in CATEGORIAS:
        raise ErrorDatos(f"Categoría inválida: {categoria!r}. Opciones: {', '.join(CATEGORIAS)}")
    numero, errores = _registrar(
        "reporte",
        "INSERT INTO reportes (nombre, categoria, descripcion, contacto, modelo, embedding, creado) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (nombre, categoria or "", (descripcion or "").strip(), (contacto or "").strip()),
        foto,
        modelo,
    )
    return obtener_reporte(codigo_reporte(numero)), errores


# ----------------------------------------------------------------------- lectura

def _registro_objeto(fila, fotos):
    return {
        "id": codigo_objeto(fila["id"]),
        "nombre": fila["nombre"],
        "categoria": fila["categoria"],
        "ubicacion": fila["ubicacion"],
        "descripcion": fila["descripcion"],
        "estado": fila["estado"],
        "modelo": fila["modelo"],
        "embedding": _de_blob(fila["embedding"]),
        "fotos": fotos,
        "creado": fila["creado"],
    }


def _registro_reporte(fila, fotos):
    return {
        "id": codigo_reporte(fila["id"]),
        "nombre": fila["nombre"],
        "categoria": fila["categoria"],
        "descripcion": fila["descripcion"],
        "contacto": fila["contacto"],
        "estado": fila["estado"],
        "modelo": fila["modelo"],
        "embedding": _de_blob(fila["embedding"]),
        "fotos": fotos,
        "creado": fila["creado"],
    }


def obtener_objeto(codigo):
    """Devuelve el objeto (con embedding) o None si no existe."""
    numero = _numero(codigo, "OBJ")
    with _conectar() as con:
        fila = con.execute("SELECT * FROM objetos WHERE id = ?", (numero,)).fetchone()
        if fila is None:
            return None
        return _registro_objeto(fila, _rutas_fotos(con, "objeto", numero))


def obtener_reporte(codigo):
    """Devuelve el reporte (con embedding) o None si no existe."""
    numero = _numero(codigo, "REP")
    with _conectar() as con:
        fila = con.execute("SELECT * FROM reportes WHERE id = ?", (numero,)).fetchone()
        if fila is None:
            return None
        return _registro_reporte(fila, _rutas_fotos(con, "reporte", numero))


def cargar_objetos(categoria=None, solo_disponibles=True):
    """Objetos listos para comparar. Solo incluye los calculados con el modelo actual."""
    sql, parametros = "SELECT * FROM objetos WHERE modelo = ?", [motor.NOMBRE_MODELO]
    if solo_disponibles:
        sql += " AND estado = 'disponible'"
    if categoria:
        sql += " AND categoria = ?"
        parametros.append(categoria)
    with _conectar() as con:
        fotos = _fotos_por_registro(con, "objeto")
        filas = con.execute(sql + " ORDER BY id", parametros).fetchall()
    return [_registro_objeto(f, fotos.get(f["id"], [])) for f in filas]


def cargar_reportes(solo_pendientes=True):
    """Reportes listos para comparar. Solo incluye los calculados con el modelo actual."""
    sql, parametros = "SELECT * FROM reportes WHERE modelo = ?", [motor.NOMBRE_MODELO]
    if solo_pendientes:
        sql += " AND estado = 'pendiente'"
    with _conectar() as con:
        fotos = _fotos_por_registro(con, "reporte")
        filas = con.execute(sql + " ORDER BY id", parametros).fetchall()
    return [_registro_reporte(f, fotos.get(f["id"], [])) for f in filas]


def vista_publica(objeto):
    """Lo que puede ver cualquier persona: sin ubicación ni descripción, que verifica el revisor."""
    return {
        "id": objeto["id"],
        "nombre": objeto["nombre"],
        "categoria": objeto["categoria"],
        "fotos": list(objeto["fotos"]),
    }


# ---------------------------------------------------- rechazos y entregas (revisor)

def _existen(con, numero_reporte, numero_objeto):
    reporte = con.execute("SELECT estado FROM reportes WHERE id = ?", (numero_reporte,)).fetchone()
    objeto = con.execute("SELECT estado FROM objetos WHERE id = ?", (numero_objeto,)).fetchone()
    if reporte is None:
        raise ErrorDatos(f"No existe el reporte {codigo_reporte(numero_reporte)}")
    if objeto is None:
        raise ErrorDatos(f"No existe el objeto {codigo_objeto(numero_objeto)}")
    return reporte, objeto


def rechazar(cod_reporte, cod_objeto):
    """El revisor descarta este par: el emparejamiento no volverá a proponerlo."""
    r, o = _numero(cod_reporte, "REP"), _numero(cod_objeto, "OBJ")
    with _transaccion() as con:
        _existen(con, r, o)
        con.execute(
            "INSERT OR IGNORE INTO rechazos (reporte_id, objeto_id, creado) VALUES (?, ?, ?)",
            (r, o, _ahora()),
        )


def pares_rechazados():
    """Conjunto de tuplas (codigo_reporte, codigo_objeto), listo para motor.emparejar."""
    with _conectar() as con:
        filas = con.execute("SELECT reporte_id, objeto_id FROM rechazos").fetchall()
    return {(codigo_reporte(f["reporte_id"]), codigo_objeto(f["objeto_id"])) for f in filas}


def confirmar_entrega(cod_reporte, cod_objeto, similitud=None):
    """El revisor confirma que el objeto es del reporte: ambos salen de los registros activos."""
    r, o = _numero(cod_reporte, "REP"), _numero(cod_objeto, "OBJ")
    with _transaccion() as con:
        reporte, objeto = _existen(con, r, o)
        if reporte["estado"] != "pendiente":
            raise ErrorEstado(f"El reporte {cod_reporte} ya está resuelto.")
        if objeto["estado"] != "disponible":
            raise ErrorEstado(f"El objeto {cod_objeto} ya fue entregado.")
        con.execute("UPDATE reportes SET estado = 'resuelto' WHERE id = ?", (r,))
        con.execute("UPDATE objetos SET estado = 'entregado' WHERE id = ?", (o,))
        con.execute(
            "INSERT INTO entregas (reporte_id, objeto_id, similitud, creado) VALUES (?, ?, ?, ?)",
            (r, o, similitud, _ahora()),
        )


# -------------------------------------------------------------- cambio de modelo

def contar_obsoletos():
    """Cuántos registros se calcularon con un modelo distinto al actual (no se comparan)."""
    with _conectar() as con:
        total = 0
        for tabla in ("objetos", "reportes"):
            total += con.execute(
                f"SELECT COUNT(*) FROM {tabla} WHERE modelo != ?", (motor.NOMBRE_MODELO,)
            ).fetchone()[0]
    return total

