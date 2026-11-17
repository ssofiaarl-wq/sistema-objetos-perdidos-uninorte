"""
motor.py: lógica del sistema de objetos perdidos, sin interfaz.

Las funciones reciben datos y devuelven datos: no imprimen ni terminan el programa, para que la
página (Gradio) pueda usarlas directamente. Los errores se lanzan como
excepciones (ErrorImagen) y la interfaz decide cómo mostrarlos.

Un "registro" (objeto encontrado o reporte) es un diccionario:
    {"id": str, "nombre": str, "embedding": np.ndarray, "fotos": [str, ...]}
"""

import argparse
import hashlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.optimize import linear_sum_assignment
from sklearn.metrics.pairwise import cosine_similarity

# ---------------------------------------------------------------- configuración

NOMBRE_MODELO = "facebook/dinov2-base"
EXTENSIONES_IMAGEN = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}  # extensiones de imagen válidas
TAMANO_MAX_BYTES = 15 * 1024 * 1024      # fotos de más de 15 MB se rechazan
LADO_MAX_PX = 1600                       # fotos más grandes se reducen antes de procesarlas
UMBRAL_DEFECTO = 0.45                    # inicialmente calibrado con 7 objetos, luego probado con 30
TOP_K_DEFECTO = 5

CARPETA_BASE = Path(__file__).parent / "imagenes"
CARPETA_CACHE = Path(__file__).parent / ".cache_embeddings"


VERSION_PREPROCESO = "exif-v1"


class ErrorImagen(Exception):
    """La imagen no existe, no es válida o es demasiado pesada."""


# ----------------------------------------------------------------------- modelo

class Extractor:
    """Agrupa el modelo, su procesador de imágenes y el dispositivo."""

    def __init__(self, nombre=NOMBRE_MODELO):
        # Imports locales: así el resto del módulo funciona aunque torch no esté instalado
        import torch
        from transformers import AutoImageProcessor, AutoModel

        self.torch = torch
        self.dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
        self.procesador = AutoImageProcessor.from_pretrained(nombre)
        self.modelo = AutoModel.from_pretrained(nombre).to(self.dispositivo).eval()


_MODELO = None


def obtener_modelo():
    """Carga el modelo una sola vez y lo reutiliza en las siguientes llamadas."""
    global _MODELO
    if _MODELO is None:
        _MODELO = Extractor()
    return _MODELO


# ------------------------------------------------------------------- embeddings

def validar_imagen(ruta):
    """Comprueba que sea un archivo de imagen admitido, no demasiado pesado y legible."""
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorImagen(f"No existe la imagen: {ruta.name}")
    if ruta.suffix.lower() not in EXTENSIONES_IMAGEN:
        raise ErrorImagen(f"Formato no admitido: {ruta.suffix or '(sin extensión)'}")
    if ruta.stat().st_size > TAMANO_MAX_BYTES:
        raise ErrorImagen(f"La imagen pesa más de {TAMANO_MAX_BYTES // (1024 * 1024)} MB: {ruta.name}")
    try:
        with Image.open(ruta) as imagen:
            imagen.verify()                 # detecta archivos corruptos o que no son imágenes
    except Exception as error:
        raise ErrorImagen(f"Imagen dañada o ilegible: {ruta.name}") from error


def _preparar(imagen):
    """Aplica la rotación del celular (EXIF), convierte a RGB y reduce fotos enormes."""
    imagen = ImageOps.exif_transpose(imagen)
    imagen = imagen.convert("RGB")
    imagen.thumbnail((LADO_MAX_PX, LADO_MAX_PX))
    return imagen


def extraer_embedding(modelo, imagen):
    """Convierte una foto (ruta o imagen PIL) en un vector normalizado de 768 dimensiones."""
    if isinstance(imagen, Image.Image):
        preparada = _preparar(imagen)
    else:
        validar_imagen(imagen)
        with Image.open(imagen) as abierta:
            preparada = _preparar(abierta)

    entradas = modelo.procesador(images=preparada, return_tensors="pt")
    entradas = {k: v.to(modelo.dispositivo) for k, v in entradas.items()}
    with modelo.torch.no_grad():
        salida = modelo.modelo(**entradas)
    vector = salida.pooler_output[0].cpu().numpy()
    norma = np.linalg.norm(vector)
    return vector / norma if norma > 0 else vector


def _clave_cache(ruta):
    """Identifica una foto por modelo, preprocesamiento, ruta, tamaño y fecha de modificación."""
    ruta = Path(ruta)
    info = ruta.stat()
    texto = f"{NOMBRE_MODELO}|{VERSION_PREPROCESO}|{ruta.resolve()}|{info.st_size}|{info.st_mtime_ns}"
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def extraer_embedding_cacheado(modelo, ruta):
    """Devuelve el embedding desde disco si existe; si no, lo calcula y lo guarda."""
    validar_imagen(ruta)
    CARPETA_CACHE.mkdir(exist_ok=True)
    archivo = CARPETA_CACHE / f"{_clave_cache(ruta)}.npy"
    if archivo.exists():
        try:
            return np.load(archivo)
        except Exception:
            pass                            # caché dañada: se recalcula
    embedding = extraer_embedding(modelo, ruta)
    np.save(archivo, embedding)
    return embedding


def embedding_de_fotos(modelo, rutas, usar_cache=True):
    """Promedia las fotos de un mismo objeto en un solo vector normalizado.

    Devuelve (embedding, rutas_usadas, errores). Las fotos que fallan se saltan
    y se reportan en 'errores' como lista de (nombre_archivo, mensaje).
    Si ninguna foto sirve, lanza ErrorImagen.
    """
    extraer = extraer_embedding_cacheado if usar_cache else extraer_embedding
    vectores, usadas, errores = [], [], []
    for ruta in rutas:
        try:
            vectores.append(extraer(modelo, ruta))
            usadas.append(str(ruta))
        except ErrorImagen as error:
            errores.append((Path(ruta).name, str(error)))
    if not vectores:
        detalle = "; ".join(m for _, m in errores) or "no se recibieron fotos"
        raise ErrorImagen(f"Ninguna foto es utilizable: {detalle}")
    promedio = np.mean(np.stack(vectores), axis=0)
    norma = np.linalg.norm(promedio)
    if norma > 0:
        promedio = promedio / norma
    return promedio, usadas, errores

# ----------------------------------------------------- grafo y emparejamiento

def matriz_similitud(reportes, objetos):
    """Similitud coseno entre cada reporte (filas) y cada objeto (columnas)."""
    if not reportes or not objetos:
        return np.zeros((len(reportes), len(objetos)))
    R = np.stack([r["embedding"] for r in reportes])
    O = np.stack([o["embedding"] for o in objetos])
    return cosine_similarity(R, O)


def emparejar(reportes, objetos, umbral=UMBRAL_DEFECTO, pares_rechazados=None, S=None):
    """Emparejamiento de peso máximo en el grafo bipartito reportes-objetos.

    pares_rechazados: conjunto de tuplas (id_reporte, id_objeto) que no se pueden proponer.
    S: matriz de similitud ya calculada (opcional, para no repetir el cálculo).
    Devuelve (pares, grafo). Cada par es (reporte, objeto, similitud), de mayor a menor.
    """
    pares_rechazados = pares_rechazados or set()

    # Grafo bipartito como lista de adyacencia; el prefijo evita choques de nombres
    grafo = {f"R:{r['id']}": {} for r in reportes}
    grafo.update({f"O:{o['id']}": {} for o in objetos})
    if not reportes or not objetos:
        return [], grafo

    if S is None:
        S = matriz_similitud(reportes, objetos)

    # Aristas válidas: sobre el umbral y no rechazadas
    valido = S >= umbral
    for i, r in enumerate(reportes):
        for j, o in enumerate(objetos):
            if (r["id"], o["id"]) in pares_rechazados:
                valido[i, j] = False

    for i, r in enumerate(reportes):
        for j, o in enumerate(objetos):
            if valido[i, j]:
                grafo[f"R:{r['id']}"][f"O:{o['id']}"] = float(S[i, j])
                grafo[f"O:{o['id']}"][f"R:{r['id']}"] = float(S[i, j])

    # Pesos de pares no válidos a cero: el algoritmo no puede "gastar" objetos en ellos
    pesos = np.where(valido, S, 0.0)
    filas, columnas = linear_sum_assignment(pesos, maximize=True)

    # El húngaro también asigna pares de peso cero; aquí se descartan
    pares = [
        (reportes[i], objetos[j], float(S[i, j]))
        for i, j in zip(filas, columnas)
        if valido[i, j]
    ]
    pares.sort(key=lambda par: par[2], reverse=True)
    return pares, grafo


def candidatos_de_reporte(embedding, objetos, umbral=UMBRAL_DEFECTO, top_k=TOP_K_DEFECTO,
                          excluir_ids=None):
    """Los top_k objetos más parecidos a un embedding, sin importar el emparejamiento.

    Sirve para la búsqueda inmediata de un reporte nuevo (sin recalcular todo).
    excluir_ids: ids de objetos que no deben aparecer (por ejemplo, ya entregados).
    """
    excluir_ids = excluir_ids or set()
    lista = [o for o in objetos if o["id"] not in excluir_ids]
    if not lista:
        return []
    O = np.stack([o["embedding"] for o in lista])
    similitudes = cosine_similarity(embedding.reshape(1, -1), O)[0]
    orden = np.argsort(similitudes)[::-1][:top_k]
    return [
        {
            "objeto": lista[j],
            "similitud": float(similitudes[j]),
            "sobre_umbral": bool(similitudes[j] >= umbral),
        }
        for j in orden
    ]


def candidatos_por_reporte(reportes, objetos, S, umbral=UMBRAL_DEFECTO, top_k=TOP_K_DEFECTO):
    """Para cada reporte, sus top_k objetos más parecidos. Devuelve {id_reporte: [candidatos]}."""
    resultado = {}
    for i, r in enumerate(reportes):
        orden = np.argsort(S[i])[::-1][:top_k]
        resultado[r["id"]] = [
            {
                "objeto": objetos[j],
                "similitud": float(S[i, j]),
                "sobre_umbral": bool(S[i, j] >= umbral),
            }
            for j in orden
        ]
    return resultado

# ---------------------------------------- carga desde carpetas (prototipo/pruebas)

def cargar_objetos_carpeta(carpeta, modelo):
    """Cada subcarpeta es un objeto físico con una o varias fotos.

    Devuelve (registros, avisos). Los avisos son textos con lo que se tuvo que saltar.
    """
    registros, avisos = [], []
    for sub in sorted(Path(carpeta).iterdir()):
        if not sub.is_dir():
            continue
        fotos = [f for f in sorted(sub.iterdir())
                 if f.is_file() and f.suffix.lower() in EXTENSIONES_IMAGEN]
        if not fotos:
            avisos.append(f"{sub.name}: sin fotos válidas, se omite")
            continue
        try:
            embedding, usadas, errores = embedding_de_fotos(modelo, fotos)
        except ErrorImagen as error:
            avisos.append(f"{sub.name}: {error}")
            continue
        avisos.extend(f"{sub.name}/{nombre}: {mensaje}" for nombre, mensaje in errores)
        registros.append({"id": sub.name, "nombre": sub.name,
                          "embedding": embedding, "fotos": usadas})
    return registros, avisos


def cargar_reportes_carpeta(carpeta, modelo):
    """Cada foto suelta de la carpeta es un reporte con una sola foto."""
    registros, avisos = [], []
    for foto in sorted(Path(carpeta).iterdir()):
        if not foto.is_file() or foto.suffix.lower() not in EXTENSIONES_IMAGEN:
            continue
        try:
            embedding = extraer_embedding_cacheado(modelo, foto)
        except ErrorImagen as error:
            avisos.append(f"{foto.name}: {error}")
            continue
        registros.append({"id": foto.stem, "nombre": foto.stem,
                          "embedding": embedding, "fotos": [str(foto)]})
    return registros, avisos


# ------------------------------------------------------------------- terminal

def _main():
    parser = argparse.ArgumentParser(description="Empareja reportes con objetos encontrados.")
    parser.add_argument("--catalogo", default=str(CARPETA_BASE / "catalogo"))
    parser.add_argument("--reportes", default=str(CARPETA_BASE / "reportes"))
    parser.add_argument("--umbral", type=float, default=UMBRAL_DEFECTO)
    parser.add_argument("--top_k", type=int, default=TOP_K_DEFECTO)
    parser.add_argument("--rechazados", nargs="*", default=[],
                        help="Pares rechazados con formato id_reporte:id_objeto.")
    parser.add_argument("--diagnostico", action="store_true",
                        help="Muestra la matriz de similitudes.")
    args = parser.parse_args()

    for ruta in (args.catalogo, args.reportes):
        if not Path(ruta).is_dir():
            parser.error(f"No existe la carpeta: {ruta}")
    if args.top_k < 1:
        parser.error("--top_k debe ser un entero positivo.")

    rechazados = set()
    for texto in args.rechazados:
        if ":" not in texto:
            parser.error(f"Formato inválido en --rechazados: {texto} (usa reporte:objeto)")
        rechazados.add(tuple(texto.split(":", 1)))

    modelo = obtener_modelo()
    objetos, avisos_o = cargar_objetos_carpeta(args.catalogo, modelo)
    reportes, avisos_r = cargar_reportes_carpeta(args.reportes, modelo)
    for aviso in avisos_o + avisos_r:
        print(f"Aviso: {aviso}")
    print(f"Objetos encontrados: {len(objetos)} | Reportes de pérdida: {len(reportes)}")
    if not objetos or not reportes:
        parser.error("Se necesita al menos un objeto y un reporte para comparar.")

    S = matriz_similitud(reportes, objetos)
    if args.diagnostico:
        print("\nMatriz de similitudes:")
        print(" " * 18 + "".join(f"{o['nombre'][:12]:>14}" for o in objetos))
        for i, r in enumerate(reportes):
            print(f"{r['nombre'][:16]:<18}" + "".join(f"{S[i, j]:>14.3f}" for j in range(len(objetos))))

    pares, grafo = emparejar(reportes, objetos, args.umbral, rechazados, S=S)
    aristas = sum(len(v) for k, v in grafo.items() if k.startswith("R:"))
    print(f"\nAristas del grafo bipartito: {aristas}")

    print("\nEmparejamiento óptimo:")
    if not pares:
        print("Ningún reporte alcanzó el umbral con un objeto.")
    for reporte, objeto, sim in pares:
        print(f"  {reporte['nombre']}  <->  {objeto['nombre']} | similitud: {sim:.4f}")

    asignado = {r["id"]: o["id"] for r, o, _ in pares}
    top = candidatos_por_reporte(reportes, objetos, S, args.umbral, args.top_k)
    print("\nCandidatos por reporte:")
    for r in reportes:
        print(f"\n{r['nombre']}  (foto: {r['fotos'][0]})")
        for k, c in enumerate(top[r["id"]], start=1):
            marca = " <- sugerido por el emparejamiento" if asignado.get(r["id"]) == c["objeto"]["id"] else ""
            aviso = "" if c["sobre_umbral"] else " (bajo el umbral)"
            print(f"  {k}. {c['objeto']['nombre']} | similitud: {c['similitud']:.4f}{aviso}{marca}")

    emp_r = {r["id"] for r, _, _ in pares}
    emp_o = {o["id"] for _, o, _ in pares}
    print(f"\nReportes sin objeto: {[r['nombre'] for r in reportes if r['id'] not in emp_r] or 'ninguno'}")
    print(f"Objetos sin reporte: {[o['nombre'] for o in objetos if o['id'] not in emp_o] or 'ninguno'}")


if __name__ == "__main__":
    _main()
