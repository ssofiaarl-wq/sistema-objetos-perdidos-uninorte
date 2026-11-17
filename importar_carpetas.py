"""
importar_carpetas.py: carga tus carpetas de prueba en la base de datos (se hace una sola vez).

    imagenes/catalogo/<objeto>/   -> un objeto encontrado por subcarpeta, con todas sus fotos
    imagenes/reportes/<foto>      -> un reporte por foto suelta

Uso:
    python importar_carpetas.py
    python importar_carpetas.py --forzar     (agrega aunque la base ya tenga registros)

Para la prueba de 30 objetos usa una carpeta de datos propia, para no mezclarla con la base real:
    PowerShell:  $env:DATOS_DIR = "datos_prueba30"; python importar_carpetas.py
"""

import argparse
import os
from pathlib import Path

import almacen
import motor
from almacen import ErrorDatos
from motor import EXTENSIONES_IMAGEN, ErrorImagen

# carpeta del catálogo: (nombre que se mostrará, categoría)
# Los nombres son únicos: verdad.csv los usa para saber cuál es el objeto correcto de cada reporte.
CATALOGO = {
    # Electrónica
    "audifonos_001": ("Audífonos 001", "Electrónica"),
    "audifonos_002": ("Audífonos 002", "Electrónica"),
    "cargador_001": ("Cargador 001", "Electrónica"),
    "cargador_002": ("Cargador 002", "Electrónica"),
    "mouse_001": ("Mouse 001", "Electrónica"),
    "mouse_002": ("Mouse 002", "Electrónica"),
    "memoria_usb_001": ("Memoria USB 001", "Electrónica"),
    "powerbank_001": ("Power bank 001", "Electrónica"),
    "teclado_001": ("Teclado 001", "Electrónica"),
    # Accesorios
    "botella_001": ("Botella 001", "Accesorios"),
    "termo_002": ("Termo negro 002", "Accesorios"),
    "termo_003": ("Termo negro 003", "Accesorios"),
    "gafas_001": ("Gafas 001", "Accesorios"),
    "billetera_001": ("Billetera 001", "Accesorios"),
    "reloj_001": ("Reloj 001", "Accesorios"),
    "cartera_001": ("Cartera 001", "Accesorios"),
    "estuche_gafas_001": ("Estuche de gafas 001", "Accesorios"),
    # Útiles
    "cartuchera_001": ("Cartuchera 001", "Útiles"),
    "marcadornegro_001": ("Marcador negro 001", "Útiles"),
    "marcadorverde_001": ("Marcador verde 001", "Útiles"),
    "regla_001": ("Regla 001", "Útiles"),
    "resaltador_001": ("Resaltador 001", "Útiles"),
    "lapicero_001": ("Lapicero 001", "Útiles"),
    "lapicero_002": ("Lapicero 002", "Útiles"),
    "block_001": ("Block 001", "Útiles"),
    "calculadora_001": ("Calculadora 001", "Útiles"),
    "lápiz_001": ("Lápiz 001", "Útiles"),
    # Otro tipo
    "llaves_001": ("Llaves 001", "Otro tipo"),
    "taza_001": ("Taza 001", "Otro tipo"),
    "taza_002": ("Taza 002", "Otro tipo"),
}
UBICACION_DEFECTO = "Sin especificar"
CONTACTO_PRUEBA = "prueba@ejemplo.com"


def _fotos_de(carpeta):
    return [f for f in sorted(carpeta.iterdir()) if f.is_file() and f.suffix.lower() in EXTENSIONES_IMAGEN]


def importar(carpeta_catalogo, carpeta_reportes, modelo, forzar=False):
    """Importa objetos y reportes. Devuelve (n_objetos, n_reportes, avisos)."""
    existentes = (almacen.cargar_objetos(solo_disponibles=False)
                  + almacen.cargar_reportes(solo_pendientes=False))
    if existentes and not forzar:
        raise RuntimeError("La base de datos ya tiene registros. Usa --forzar si quieres agregar de todos modos.")

    avisos, n_objetos, n_reportes = [], 0, 0

    for sub in sorted(Path(carpeta_catalogo).iterdir()):
        if not sub.is_dir():
            continue
        if sub.name not in CATALOGO:
            avisos.append(f"{sub.name}: no está en CATALOGO; se importa con el nombre de la carpeta y categoría "
                          "«Otro tipo» (verdad.csv no lo reconocerá).")
        nombre, categoria = CATALOGO.get(sub.name, (sub.name, "Otro tipo"))
        try:
            _, errores = almacen.registrar_objeto(modelo, nombre, categoria, UBICACION_DEFECTO, _fotos_de(sub))
            n_objetos += 1
            avisos.extend(f"{sub.name}/{n}: {m}" for n, m in errores)
        except (ErrorDatos, ErrorImagen) as error:
            avisos.append(f"{sub.name}: {error}")

    for foto in _fotos_de(Path(carpeta_reportes)):
        try:
            almacen.registrar_reporte(modelo, foto.stem, foto, contacto=CONTACTO_PRUEBA)
            n_reportes += 1
        except (ErrorDatos, ErrorImagen) as error:
            avisos.append(f"{foto.name}: {error}")

    return n_objetos, n_reportes, avisos


def main():
    base = Path(__file__).parent / "imagenes"
    parser = argparse.ArgumentParser(description="Importa las carpetas de prueba a la base de datos.")
    parser.add_argument("--catalogo", default=str(base / "catalogo"))
    parser.add_argument("--reportes", default=str(base / "reportes"))
    parser.add_argument("--forzar", action="store_true")
    args = parser.parse_args()

    for ruta in (args.catalogo, args.reportes):
        if not Path(ruta).is_dir():
            parser.error(f"No existe la carpeta: {ruta}")

    almacen.iniciar(os.environ.get("DATOS_DIR"))
    print("Cargando el modelo...")
    try:
        n_obj, n_rep, avisos = importar(args.catalogo, args.reportes, motor.obtener_modelo(), args.forzar)
    except RuntimeError as error:
        parser.error(str(error))
    for aviso in avisos:
        print(f"Aviso: {aviso}")
    print(f"Importados: {n_obj} objetos y {n_rep} reportes en {almacen.carpeta_datos()}")


if __name__ == "__main__":
    main()
