"""
app.py: página web del sistema de objetos perdidos (Gradio).

Usa motor.py (modelo, grafo bipartito, emparejamiento) y almacen.py (base de datos).
Pestañas:
    1. Reportar pérdida      (pública)   sube una foto y ve los 5 objetos más parecidos
    2. Registrar encontrado  (pública)   una o varias fotos por objeto
    3. Revisión              (con clave) confirma o rechaza las sugerencias del emparejamiento
    4. Inventario            (pública)   galería de objetos disponibles

Para ejecutar:
    pip install -U gradio
    python app.py
"""
import base64
import hmac
import inspect
import os
from datetime import datetime
from pathlib import Path

import numpy as np

import almacen
import motor
from almacen import CATEGORIAS, ErrorDatos, ErrorEstado
from motor import ErrorImagen

UMBRAL = motor.UMBRAL_DEFECTO
TOP_K = motor.TOP_K_DEFECTO
CLAVE_PREDETERMINADA = "cambiar-esta-clave"
CLAVE_REVISOR = os.environ.get("CLAVE_REVISOR", CLAVE_PREDETERMINADA)


class ErrorAcceso(Exception):
    """Clave de revisor incorrecta."""


ERRORES = (ErrorDatos, ErrorImagen, ErrorEstado, ErrorAcceso)


# ------------------------------------------------- utilidades de interfaz

def _dropdown(opciones, valor=None):
    """Actualiza un Dropdown. Aislado aquí para poder probar los manejadores sin Gradio."""
    import gradio as gr
    return gr.Dropdown(choices=opciones, value=valor)


def _sin_cambio():
    """Indica a Gradio que no modifique un componente."""
    import gradio as gr
    return gr.skip() if hasattr(gr, "skip") else gr.update()


def _a_rutas(archivos):
    """Convierte lo que entrega gr.File (rutas u objetos con .name) en una lista de rutas."""
    if not archivos:
        return []
    if isinstance(archivos, (str, Path)):
        archivos = [archivos]
    return [str(a) if isinstance(a, (str, Path)) else str(a.name) for a in archivos]

def _fecha(texto):
    """Convierte 2026-11-14T09:05:35 en 14/11/2026, 9:05 a. m."""
    try:
        f = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return texto or "—"
    return f"{f:%d/%m/%Y}, {(f.hour % 12) or 12}:{f:%M} {'a. m.' if f.hour < 12 else 'p. m.'}"

def _verificar_clave(clave):
    """La clave se comprueba en cada acción del servidor, no solo al mostrar la pestaña."""
    if not hmac.compare_digest((clave or "").encode("utf-8"), CLAVE_REVISOR.encode("utf-8")):
        raise ErrorAcceso("Clave incorrecta.")


def _leyenda_publica(candidato):
    visible = almacen.vista_publica(candidato["objeto"])
    return f"{visible['id']} · {visible['nombre']} · {visible['categoria']}"


# ------------------------------------------------- 1. reportar pérdida

def reportar(nombre, categoria, descripcion, contacto, foto, guardar):
    """Busca los objetos más parecidos a la foto y, si se pide, guarda el reporte."""
    try:
        if not foto:
            raise ErrorDatos("Sube una foto del objeto que perdiste.")
        categoria = categoria or None
        modelo = motor.obtener_modelo()
        aviso_guardado = ""
        if guardar:
            if not (contacto or "").strip():
                raise ErrorDatos("Para guardar tu reporte deja un contacto (correo o teléfono), "
                                 "o desmarca la casilla para solo buscar.")
            reporte, _ = almacen.registrar_reporte(modelo, nombre, foto, categoria, descripcion, contacto)
            embedding = reporte["embedding"]
            aviso_guardado = (f"Tu reporte quedó guardado con el código **{reporte['id']}**. "
                              "Si el objeto aparece después, el revisor podrá contactarte.\n\n")
        else:
            embedding = motor.extraer_embedding(modelo, foto)
    except ERRORES as error:
        return f"⚠️ {error}", []

    objetos = almacen.cargar_objetos(categoria=categoria)
    candidatos = motor.candidatos_de_reporte(embedding, objetos, UMBRAL, TOP_K)
    if not candidatos:
        donde = f"en la categoría {categoria}" if categoria else "todavía"
        return aviso_guardado + f"No hay objetos registrados {donde}.", []

    galeria = [(c["objeto"]["fotos"][0], _leyenda_publica(c)) for c in candidatos]
    if any(c["sobre_umbral"] for c in candidatos):
        mensaje = (aviso_guardado +
                   f"Estos son los {len(candidatos)} objetos más parecidos. Si ves el tuyo, "
                   "comunícate con el punto de objetos perdidos indicando su código. Te pedirán "
                   "detalles que solo el dueño conoce.")
    else:
        mensaje = (aviso_guardado + "No encontramos ninguna coincidencia clara por ahora. "
                   "Estos son los objetos más cercanos. "
                   + ("" if aviso_guardado else "Si ninguno es el tuyo, guarda tu reporte para que el revisor pueda contactarte si aparece."))
    
    return mensaje, galeria


# ------------------------------------------------- 2. registrar objeto encontrado

def registrar_encontrado(nombre, categoria, ubicacion, descripcion, archivos):
    try:
        rutas = _a_rutas(archivos)
        modelo = motor.obtener_modelo()
        objeto, errores = almacen.registrar_objeto(modelo, nombre, categoria, ubicacion, rutas, descripcion)
    except ERRORES as error:
        return f"⚠️ {error}"
    mensaje = (f"✅ **Objeto registrado**\n\nCódigo: `{objeto['id']}`  \n"
               f"Nombre: {objeto['nombre']}  \nCategoría: {objeto['categoria']}  \n"
               f"Fotos guardadas: {len(objeto['fotos'])}")
    if errores:
        saltadas = "; ".join(f"{n} ({m})" for n, m in errores)
        mensaje += f"\n\n⚠️ Se omitieron estas fotos: {saltadas}"
    return mensaje


# ------------------------------------------------- 3. revisión (con clave)

def _calcular():
    """Recalcula sugerencias y candidatos con los registros activos y los rechazos vigentes."""
    reportes = almacen.cargar_reportes()
    objetos = almacen.cargar_objetos()
    S = motor.matriz_similitud(reportes, objetos)
    rechazados = almacen.pares_rechazados()
    pares, _ = motor.emparejar(reportes, objetos, UMBRAL, rechazados, S=S)
    sugerido = {r["id"]: (o["id"], s) for r, o, s in pares}
    candidatos = {}
    for i, reporte in enumerate(reportes):
        orden = np.argsort(S[i])[::-1]
        lista = [(objetos[j], float(S[i, j])) for j in orden
                 if (reporte["id"], objetos[j]["id"]) not in rechazados]
        candidatos[reporte["id"]] = lista[:TOP_K]
    return {
        "reportes": {r["id"]: r for r in reportes},
        "objetos": {o["id"]: o for o in objetos},
        "orden": [r["id"] for r in reportes],
        "sugerido": sugerido,
        "candidatos": candidatos,
    }


def _vacia_revision(estado, opciones_reporte=None):
    """Salida de 8 componentes con todo limpio: estado, reporte, info, fotos, candidatos, objeto, info, fotos."""
    return (estado, _dropdown(opciones_reporte or [], None), "", [], [], _dropdown([], None), "", [])


def _solo_estado(mensaje):
    """Cambia solo el mensaje de estado y deja el resto de la pestaña como está."""
    return (mensaje,) + tuple(_sin_cambio() for _ in range(7))


def _refrescar_datos(mensaje=""):
    datos = _calcular()
    opciones = [(f"{rid} · {datos['reportes'][rid]['nombre']}", rid) for rid in datos["orden"]]
    resumen = (f"{len(datos['reportes'])} reportes pendientes · {len(datos['objetos'])} objetos "
               f"disponibles · {len(datos['sugerido'])} con sugerencia")
    return _vacia_revision(mensaje + resumen, opciones)


def refrescar(clave):
    try:
        _verificar_clave(clave)
    except ErrorAcceso as error:
        return _vacia_revision(f"⚠️ {error}")
    return _refrescar_datos()


def _detalle_objeto(codigo):
    objeto = almacen.obtener_objeto(codigo)
    if objeto is None:
        return "", []
    info = (f"**{objeto['id']} — {objeto['nombre']}**\n\nCategoría: {objeto['categoria']}  \n"
            f"Ubicación: {objeto['ubicacion']}  \nDescripción: {objeto['descripcion'] or '—'}  \n"
            f"Registrado: {_fecha(objeto['creado'])}")
    return info, [(f, objeto["id"]) for f in objeto["fotos"]]


def elegir_reporte(clave, cod_reporte):
    try:
        _verificar_clave(clave)
    except ErrorAcceso as error:
        return _vacia_revision(f"⚠️ {error}")
    vacio = (_sin_cambio(), _sin_cambio(), "", [], [], _dropdown([], None), "", [])
    if not cod_reporte:
        return vacio

    datos = _calcular()
    reporte = datos["reportes"].get(cod_reporte)
    if reporte is None:
        return _refrescar_datos("Ese reporte ya no está pendiente. ")

    sugerido = datos["sugerido"].get(cod_reporte)
    id_sugerido = sugerido[0] if sugerido else None
    candidatos = datos["candidatos"][cod_reporte]
    opciones = [
        (f"{o['id']} · {o['nombre']} · similitud {s:.2f}" + (" ← sugerido" if o["id"] == id_sugerido else ""), o["id"])
        for o, s in candidatos
    ]
    info_reporte = (f"**{reporte['id']} — {reporte['nombre']}**\n\nCategoría: {reporte['categoria'] or '—'}  \n"
                    f"Contacto: {reporte['contacto'] or '—'}  \nDescripción: {reporte['descripcion'] or '—'}  \n"
                    f"Registrado: {_fecha(reporte['creado'])}")
    if not candidatos:
        info_reporte += "\n\nNo hay objetos disponibles para comparar."
    galeria_reporte = [(f, reporte["id"]) for f in reporte["fotos"]]
    galeria_candidatos = [
        (o["fotos"][0], f"{o['id']} · {s:.2f}" + (" ★" if o["id"] == id_sugerido else ""))
        for o, s in candidatos
    ]
    info_objeto, galeria_objeto = _detalle_objeto(id_sugerido) if id_sugerido else ("", [])
    return (_sin_cambio(), _sin_cambio(), info_reporte, galeria_reporte, galeria_candidatos,
            _dropdown(opciones, id_sugerido), info_objeto, galeria_objeto)


def elegir_objeto(clave, cod_objeto):
    try:
        _verificar_clave(clave)
    except ErrorAcceso:
        return "", []
    if not cod_objeto:
        return "", []
    return _detalle_objeto(cod_objeto)


def confirmar(clave, cod_reporte, cod_objeto):
    try:
        _verificar_clave(clave)
    except ErrorAcceso as error:
        return _vacia_revision(f"⚠️ {error}")
    if not cod_reporte or not cod_objeto:
        return _solo_estado("⚠️ Elige un reporte y un objeto antes de confirmar.")
    try:
        reporte = almacen.obtener_reporte(cod_reporte)
        objeto = almacen.obtener_objeto(cod_objeto)
        if reporte is None or objeto is None:
            raise ErrorDatos("El reporte o el objeto ya no existe.")
        similitud = None
        if reporte["modelo"] == objeto["modelo"]:
            similitud = float(np.dot(reporte["embedding"], objeto["embedding"]))
        almacen.confirmar_entrega(cod_reporte, cod_objeto, similitud)
    except ERRORES as error:
        return _solo_estado(f"⚠️ {error}")
    contacto = reporte["contacto"] or "sin contacto registrado"
    return _refrescar_datos(f"✅ Entrega confirmada: {cod_objeto} → {cod_reporte}. Contacto: {contacto}.\n\n")


def rechazar_par(clave, cod_reporte, cod_objeto):
    try:
        _verificar_clave(clave)
    except ErrorAcceso as error:
        return _vacia_revision(f"⚠️ {error}")
    if not cod_reporte or not cod_objeto:
        return _solo_estado("⚠️ Elige un reporte y un objeto antes de rechazar.")
    try:
        almacen.rechazar(cod_reporte, cod_objeto)
    except ERRORES as error:
        return _solo_estado(f"⚠️ {error}")
    salida = elegir_reporte(clave, cod_reporte)
    return (f"Par rechazado: {cod_reporte} ↔ {cod_objeto}. No volverá a proponerse.",) + tuple(salida[1:])


# ------------------------------------------------- 4. inventario

def ver_inventario(categoria):
    filtro = None if categoria in (None, "", "Todas") else categoria
    objetos = almacen.cargar_objetos(categoria=filtro)
    galeria = []
    for objeto in objetos:
        visible = almacen.vista_publica(objeto)
        if visible["fotos"]:
            galeria.append((visible["fotos"][0], f"{visible['id']} · {visible['nombre']} · {visible['categoria']}"))
    return f"{len(objetos)} objetos disponibles", galeria


# ------------------------------------------------- interfaz

ENCABEZADO = """
<div class="header-uninorte">
  <div class="uninorte-badge">__LOGO__</div>
  <div class="header-titles">
    <h1>Sistema de Reconocimiento y Búsqueda de Objetos Perdidos</h1>
  </div>
</div>
"""
RUTA_LOGO = Path(__file__).parent / "logo-uninorte.png"  


def _encabezado():
    """Pone el logo en el recuadro blanco; si no encuentra el archivo, usa el texto de antes."""
    if RUTA_LOGO.is_file():
        sufijo = RUTA_LOGO.suffix.lower()
        tipo = {".svg": "svg+xml", ".jpg": "jpeg"}.get(sufijo, sufijo.lstrip("."))
        datos = base64.b64encode(RUTA_LOGO.read_bytes()).decode("ascii")
        logo = f'<img class="logo-img" src="data:image/{tipo};base64,{datos}" alt="Universidad del Norte">'
    else:
        logo = ('<span class="un-black-txt">UN</span><div class="divider"></div>'
                '<div class="del-norte"><span class="txt-univ">UNIVERSIDAD</span>'
                '<span class="txt-norte">DEL NORTE</span></div>')
    return ENCABEZADO.replace("__LOGO__", logo)

CSS = """
.header-uninorte { background:#1a1a1a; padding:24px 40px 18px; display:flex; flex-direction:column;
  align-items:center; text-align:center; border-radius:8px; margin-bottom:12px; }
.uninorte-badge { background:#ffffff; padding:8px 18px; border-radius:8px; display:inline-flex;
  align-items:center; gap:12px; box-shadow:0 4px 10px rgba(0,0,0,0.5); margin-bottom:6px; }
.uninorte-badge .un-black-txt { font-size:26px; font-weight:900; color:#000000; letter-spacing:-1px; line-height:1; }
.uninorte-badge .divider { width:2px; height:24px; background:#E31818; }
.uninorte-badge .del-norte { display:flex; flex-direction:column; text-align:left; line-height:1.1; }
.uninorte-badge .txt-univ { font-size:11px; font-weight:bold; color:#000000; letter-spacing:1px; }
.uninorte-badge .txt-norte { font-size:13px; font-weight:900; color:#E31818; letter-spacing:0.5px; }
.logo-img { height:52px; width:auto; display:block; }
.panel-acceso, .panel-pendiente, .panel-reporte, .panel-objeto, .panel-candidatos { border-radius:10px !important; padding:14px !important; background:rgba(120,113,108,0.10) !important; border:1px solid rgba(120,113,108,0.25) !important; }
.header-titles h1 { font-size:22px; font-weight:bold; color:#ffffff !important; margin:6px 0 0 0; }
"""


def construir_interfaz():
    """Arma la página. Devuelve (demo, opciones_de_estilo_para_launch)."""
    import gradio as gr

    tema = gr.themes.Soft(primary_hue="red")
    # Según la versión de Gradio, tema y CSS van en Blocks() o en launch()
    estilo_en_launch = "css" in inspect.signature(gr.Blocks.launch).parameters
    opciones_blocks = {"title": "Objetos Perdidos - Uninorte"}
    if not estilo_en_launch:
        opciones_blocks.update(theme=tema, css=CSS)

    with gr.Blocks(**opciones_blocks) as demo:
        gr.HTML(_encabezado())
        with gr.Tabs():

            with gr.TabItem("🔍 Reportar pérdida"):
                gr.Markdown("Sube una foto del objeto que perdiste y verás los objetos registrados que más se le parecen.")
                r_nombre = gr.Textbox(label="¿Qué perdiste?", placeholder="Ej. Termo negro")
                r_cat = gr.Dropdown(label="Categoría (opcional)",
                                    choices=CATEGORIAS, value=None)
                r_desc = gr.Textbox(label="Características (opcional)", placeholder="Ej. Calcomanía azul en la tapa")
                r_foto = gr.Image(type="filepath", label="Foto del objeto", sources=["upload", "webcam"])
                r_guardar = gr.Checkbox(label="Guardar mi reporte para que el revisor pueda contactarme si el objeto aparece después",
                                        value=True)
                r_contacto = gr.Textbox(label="Contacto (correo o teléfono)", placeholder="Solo lo ve el revisor")
                r_boton = gr.Button("Buscar coincidencias", variant="primary")
                r_mensaje = gr.Markdown()
                r_galeria = gr.Gallery(label="Objetos más parecidos", columns=5, height="auto", object_fit="contain")
                r_boton.click(reportar, [r_nombre, r_cat, r_desc, r_contacto, r_foto, r_guardar],
                              [r_mensaje, r_galeria])

            with gr.TabItem("➕ Registrar objeto encontrado"):
                e_nombre = gr.Textbox(label="Nombre del objeto", placeholder="Ej. Calculadora Casio")
                e_cat = gr.Dropdown(label="Categoría", choices=CATEGORIAS, value=None)
                e_ubic = gr.Textbox(label="Dónde se encontró", placeholder="Ej. Bloque K / Biblioteca")
                e_desc = gr.Textbox(label="Características (opcional)", placeholder="Solo lo ve el revisor")
                e_fotos = gr.File(label="Fotos del objeto",
                                  file_count="multiple", file_types=["image"], type="filepath")
                e_boton = gr.Button("Guardar en inventario", variant="primary")
                e_mensaje = gr.Markdown()
                e_boton.click(registrar_encontrado, [e_nombre, e_cat, e_ubic, e_desc, e_fotos], e_mensaje)

            with gr.TabItem("🛡️ Revisión"):
                gr.Markdown("Solo para el personal autorizado.")
                with gr.Row():
                    with gr.Column(scale=1, elem_classes=["panel-acceso"]):
                        v_clave = gr.Textbox(label="Clave del revisor", type="password")
                        v_entrar = gr.Button("Entrar / actualizar", variant="primary")
                v_estado = gr.Markdown()
                with gr.Column(elem_classes=["panel-pendiente"]):
                    v_reporte = gr.Dropdown(label="1. Elige un reporte pendiente", choices=[], value=None,
                                        interactive=True)
                with gr.Row(equal_height=True):
                    with gr.Column(elem_classes=["panel-reporte"]):
                        gr.Markdown("### Reporte")
                        v_info_rep = gr.Markdown()
                        v_gal_rep = gr.Gallery(label="Foto del reporte", columns=2, height=320,
                                               object_fit="contain")
                    with gr.Column(elem_classes=["panel-objeto"]):
                        gr.Markdown("### Objeto a revisar")
                        v_objeto = gr.Dropdown(label="2. Compáralo y cambia de objeto si hace falta",
                                               choices=[], value=None, interactive=True)
                        v_info_obj = gr.Markdown()
                        v_gal_obj = gr.Gallery(label="Fotos del objeto", columns=2, height=320,
                                               object_fit="contain")
                with gr.Row():
                    v_confirmar = gr.Button("✅ Confirmar entrega", variant="primary")
                    v_rechazar = gr.Button("❌ Rechazar sugerencia")
                with gr.Column(elem_classes=["panel-candidatos"]):
                    v_gal_cand = gr.Gallery(label="Candidatos", columns=5,
                                        height="auto", object_fit="contain")
                salidas = [v_estado, v_reporte, v_info_rep, v_gal_rep, v_gal_cand, v_objeto, v_info_obj, v_gal_obj]
                v_entrar.click(refrescar, [v_clave], salidas)
                v_reporte.input(elegir_reporte, [v_clave, v_reporte], salidas)
                v_objeto.input(elegir_objeto, [v_clave, v_objeto], [v_info_obj, v_gal_obj])
                v_confirmar.click(confirmar, [v_clave, v_reporte, v_objeto], salidas)
                v_rechazar.click(rechazar_par, [v_clave, v_reporte, v_objeto], salidas)

            with gr.TabItem("📋 Inventario"):
                i_cat = gr.Dropdown(label="Filtrar por categoría", choices=["Todas"] + CATEGORIAS, value="Todas")
                i_boton = gr.Button("Cargar inventario", variant="primary")
                i_conteo = gr.Markdown()
                i_galeria = gr.Gallery(label="Objetos disponibles", columns=5, height="auto", object_fit="contain")
                i_boton.click(ver_inventario, [i_cat], [i_conteo, i_galeria])

    return demo, ({"theme": tema, "css": CSS} if estilo_en_launch else {})


if __name__ == "__main__":
    almacen.iniciar(os.environ.get("DATOS_DIR"))
    if CLAVE_REVISOR == CLAVE_PREDETERMINADA:
        print("AVISO: usa la clave predeterminada. Cámbiala con la variable de entorno CLAVE_REVISOR.")
    print("Cargando el modelo...")
    motor.obtener_modelo()
    demo, estilo = construir_interfaz()
    demo.launch(
    server_name="127.0.0.1",
    allowed_paths=[str(almacen.carpeta_datos() / "fotos")],
    share=False,
    **estilo
)
