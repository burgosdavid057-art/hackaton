"""
Interfaz del Copiloto de Produccion HACEB.

    streamlit run app_produccion.py

App separada de app.py a proposito: son dos productos con dos usuarios. Aquel
atiende a quien compra un electrodomestico; este a quien lo fabrica.

Cuatro pantallas, en el orden en que se usan un lunes a las 6 a.m.:

  Cargar    - los reportes de turno entran por aqui.
  Revisar   - lo que el extractor no supo clasificar se corrige aqui. Es la
              pantalla que hace confiable al resto: sin ella, el numero del
              resumen ejecutivo sale de datos que nadie miro.
  Preguntar - chat con el agente, con la traza y el sello del validador a la
              vista. Un agente que no se puede auditar no se puede usar.
  Resumen   - el reporte para el gerente, con rango y linea.

Nada de esta app calcula. Todo numero que se muestra vino de la DB o de una
herramienta; aqui solo se pinta y se recogen las correcciones del supervisor.
"""

from __future__ import annotations

import importlib
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

RAIZ = Path(__file__).resolve().parent
INBOX = RAIZ / "produccion" / "inbox"
EJEMPLOS = RAIZ / "ejemplos"

# Extensiones que ingest.a_texto sabe leer. Filtrar en el uploader evita que el
# supervisor suba un .docx y reciba un error tres pasos despues.
FORMATOS = ["xlsx", "xls", "csv", "pdf", "txt", "md"]

# Cuantas filas pendientes se traen por rerun. El mismo numero alimenta el
# contador de la barra lateral y la cola de la pestaña Revisar: si se usaran dos
# consultas distintas, tarde o temprano dirian cosas distintas y el supervisor
# dejaria de creerle a las dos.
LIMITE_REVISION = 200

# Filas de la cola que se pintan de una. Cada una es un formulario completo;
# mas de esto y la pagina pesa sin que nadie la use.
POR_PAGINA = 15

AVISO_CPU = (
    "En esta máquina el modelo corre en CPU: cada respuesta puede tomar entre "
    "1 y 3 minutos. No se colgó — está pensando localmente, sin salir a internet."
)

# La tabla de la que sale la fila decide que causas canonicas se le pueden
# ofrecer. Ofrecer causas de scrap para una parada es lo que ensucia el
# historico y vuelve invisible la recurrencia.
TIPO_POR_TABLA = {"paradas": "parada", "scrap": "scrap", "calidad": "calidad"}

# Columnas que el supervisor puede corregir a mano, por tabla. Es una lista
# blanca, no una comodidad: el nombre de columna se interpola en el SQL —SQLite
# no admite parametros para identificadores— asi que lo unico que impide una
# inyeccion es que el nombre haya salido de este diccionario.
COLUMNAS_EDITABLES: dict[str, dict[str, str]] = {
    "paradas": {"minutos": "numero", "hora_inicio": "texto"},
    "scrap": {"unidades": "numero", "kg": "numero"},
    "calidad": {"unidades": "numero"},
}

# La magnitud principal de cada tabla: la que suman las herramientas y la que
# hay que mirar primero cuando el extractor no la encontro literal.
COLUMNA_PRINCIPAL = {"paradas": "minutos", "scrap": "unidades", "calidad": "unidades"}

# Umbral por defecto si db.py no lo expone. Debajo de el, una fila entra a la
# cola de revision.
UMBRAL_REVISION = 0.75

MODULOS_REQUERIDOS = ("db", "ingest", "agente", "reporte")

st.set_page_config(
    page_title="HACEB · Copiloto de Producción",
    page_icon="🏭",
    layout="wide",
)


# --- Design system: los tres estados de un dato -------------------------------
#
# Del bundle de Claude Design (_ds/modernist-07ecfa1c). Los colores base y las
# esquinas viven en .streamlit/config.toml; aqui va solo lo que el tema nativo no
# puede expresar.
#
# La regla visual del producto es una sola y no admite excepciones: en pantalla,
# un numero se ve distinto segun de donde salio.
#
#   VERIFICADO      esta escrito en el documento          subrayado 2px
#   DUDOSO          lo produjo el modelo, no el documento rayado + borde acento + ≈
#   SIN CLASIFICAR  causa que no cuadro con el catalogo   borde punteado
#
# Por eso el rojo de acento NO se usa para nada mas. Si se gasta en un boton
# bonito, deja de significar "esto no lo puedo respaldar" y el supervisor pierde
# la unica senal que le dice que mirar. Las tres clases se generan desde
# `dato()`, nunca escribiendo el span a mano.

VARS_TEMA = {
    "light": """
      --dsm-text:#201e1d; --dsm-bg:#f3f2f2; --dsm-surface:#eae9e9;
      --dsm-divider:#9e9d9d; --dsm-neutral-600:#7d7979; --dsm-neutral-700:#605d5d;
      --dsm-accent:#ec3013; --dsm-accent-100:#fff2ef; --dsm-accent-200:#ffe0d9;
      --dsm-accent-fuerte:#ae1800;
    """,
    "dark": """
      --dsm-text:#f4f2f1; --dsm-bg:#171615; --dsm-surface:#242221;
      --dsm-divider:#6b6a69; --dsm-neutral-600:#9b9797; --dsm-neutral-700:#bab6b6;
      --dsm-accent:#ff563c; --dsm-accent-100:#3a1a12; --dsm-accent-200:#4d2018;
      --dsm-accent-fuerte:#ff9783;
    """,
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&display=swap');

:root { %(vars)s }

/* Las tres marcas de procedencia de un dato. */
.dato-ok {
  font-family:'Archivo',system-ui,sans-serif; font-weight:800;
  border-bottom:2px solid var(--dsm-text); white-space:nowrap;
}
.dato-dudoso {
  font-family:'Archivo',system-ui,sans-serif; font-weight:800;
  color:var(--dsm-accent-fuerte); border:2px solid var(--dsm-accent);
  padding:0 6px; white-space:nowrap;
  background:repeating-linear-gradient(135deg,
    var(--dsm-accent-100) 0 6px, var(--dsm-accent-200) 6px 12px);
}
.dato-sinclas {
  font-family:'Archivo',system-ui,sans-serif; font-weight:800;
  color:var(--dsm-neutral-700); border:2px dashed var(--dsm-neutral-600);
  padding:0 6px; white-space:nowrap;
}

/* Estado de un archivo cargado. "Ya estaba" es neutro a proposito: un duplicado
   no es un error del supervisor y pintarlo de rojo lo manda a buscar un problema
   que no existe. */
.est-ok, .est-dup, .est-err {
  font-family:'Archivo',system-ui,sans-serif; font-weight:800;
  font-size:11px; letter-spacing:.06em; padding:3px 9px; white-space:nowrap;
}
.est-ok  { border:2px solid var(--dsm-text); }
.est-dup { border:1px solid var(--dsm-divider); background:var(--dsm-surface);
           color:var(--dsm-neutral-700); }
.est-err { background:var(--dsm-accent); color:var(--dsm-bg); }

.dsm-kicker {
  font-size:11px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--dsm-neutral-600); font-family:'Archivo',system-ui,sans-serif;
  font-weight:800;
}

/* Pestanas: subrayado de 3px en la activa, sin pastilla.
   Se apunta a [role="tab"] y NO a [data-baseweb="tab"]: Streamlit 1.60 dejo de
   emitir ese atributo (verificado en el DOM, 0 elementos) y con el la regla no
   pintaba nada. La nota "Cómo se arma esto en Streamlit" de la maqueta trae el
   selector viejo. */
.stTabs [role="tablist"] { gap:0; border-bottom:2px solid var(--dsm-divider); }
.stTabs [role="tab"] {
  font-family:'Archivo',system-ui,sans-serif; font-weight:800; font-size:14px;
  padding:14px 18px; border-bottom:3px solid transparent;
}
.stTabs [role="tab"][aria-selected="true"] { border-bottom-color:var(--dsm-accent); }

/* Zona de arrastre: 2px dashed, como la maqueta. */
[data-testid="stFileUploaderDropzone"] {
  border:2px dashed var(--dsm-divider); border-radius:0; background:var(--dsm-surface);
}

/* Avance de la cola de revision. */
[data-testid="stProgress"] > div > div > div,
.stProgress > div > div > div { border-radius:0; }

/* Las opciones de causa se leen en columna: el texto va a la izquierda. */
.stButton > button { text-align:left; justify-content:flex-start; }

/* La cita textual de lo que escribio el supervisor. */
.dsm-cita {
  border-left:3px solid var(--dsm-neutral-600); padding:2px 0 2px 12px;
  font-size:17px; line-height:1.45;
}
</style>
"""


def aplicar_estilo() -> None:
    """Inyecta la capa visual, resuelta contra el tema activo.

    Se lee `st.context.theme` en vez de usar prefers-color-scheme: el usuario
    puede forzar claro u oscuro en Streamlit sin tocar el tema del sistema
    operativo, y ahi las dos cosas dejan de coincidir.
    """
    try:
        tema = st.context.theme.type or "light"
    except Exception:  # noqa: BLE001 - version sin st.context: el claro sirve
        tema = "light"
    st.html(CSS % {"vars": VARS_TEMA.get(tema, VARS_TEMA["light"])})


def dato(valor, estado: str = "ok", sufijo: str = "") -> str:
    """HTML de un dato con su procedencia. Devuelve, no pinta.

    `estado` es "ok" | "dudoso" | "sinclas". El prefijo ≈ del dudoso se pone
    aqui y no en el texto que llega: asi ningun sitio de la app puede mostrar un
    numero sin respaldo sin que se le note.
    """
    clases = {"ok": "dato-ok", "dudoso": "dato-dudoso", "sinclas": "dato-sinclas"}
    clase = clases.get(estado, "dato-ok")
    texto = f"≈{valor}" if estado == "dudoso" else f"{valor}"
    if sufijo:
        texto = f"{texto} {sufijo}"
    return f'<span class="{clase}">{texto}</span>'


def leyenda_datos() -> None:
    """La leyenda de los tres estados. Va fija en la barra lateral.

    No es adorno: sin ella, el rayado rojo se lee como "error" y el supervisor
    corrige datos que estaban bien.
    """
    st.markdown('<div class="dsm-kicker">Cómo leer un dato</div>',
                unsafe_allow_html=True)
    # <b> y no **: el bloque se pinta con unsafe_allow_html y ahi el markdown no
    # se procesa — los asteriscos salen literales en pantalla.
    filas = [
        (dato(136), "<b>VERIFICADO</b><br>está escrito en el documento"),
        (dato(30, "dudoso"), "<b>DUDOSO</b><br>lo produjo el modelo, no el documento"),
        (dato("?", "sinclas"), "<b>SIN CLASIFICAR</b><br>no cuadró con el catálogo"),
    ]
    st.markdown(
        "".join(
            f'<div style="display:flex;gap:10px;align-items:flex-start;margin-top:10px">'
            f'<span style="flex:none">{marca}</span>'
            f'<span style="font-size:11px;line-height:1.3">{texto}</span></div>'
            for marca, texto in filas
        ),
        unsafe_allow_html=True,
    )


# --- Utilidades de carga y forma de datos ------------------------------------

def _a_dict(fila) -> dict:
    """Normaliza una fila a dict.

    El contrato promete list[dict], pero sqlite3.Row se comporta casi igual y es
    facil que se cuele desde db.py. Convertir aqui evita que la pantalla reviente
    con un AttributeError por algo que no cambia el sentido del dato.
    """
    if isinstance(fila, dict):
        return fila
    try:
        return dict(fila)
    except Exception:  # noqa: BLE001
        return {}


def _cargar_modulos() -> tuple[dict, dict]:
    """Importa los modulos de produccion/ tolerando que alguno falte.

    Durante el desarrollo los modulos aterrizan en momentos distintos. Si falta
    'reporte' no hay por que impedir cargar archivos: se deshabilita esa pestaña
    y se dice cual falta, en vez de mostrar un stack trace en la cara.
    """
    modulos: dict[str, object] = {}
    errores: dict[str, str] = {}
    for nombre in MODULOS_REQUERIDOS:
        try:
            modulos[nombre] = importlib.import_module(f"produccion.{nombre}")
        except Exception as e:  # noqa: BLE001
            errores[nombre] = f"{type(e).__name__}: {e}"
    return modulos, errores


def _estado_db(db) -> dict:
    """Radiografia de la DB para la barra lateral.

    Va por SQL directo sobre db.conectar() y no por una herramienta: esto es el
    panel de estado de la aplicacion, no una respuesta al supervisor. Contar
    turnos con SQL es exacto por construccion y no gasta un turno del modelo.
    """
    ruta = Path(getattr(db, "RUTA_DB", RAIZ / "data" / "produccion.db"))
    estado = {
        "ruta": ruta,
        "existe": ruta.exists(),
        "inicializada": False,
        "turnos": 0,
        "documentos": 0,
        "causas": 0,
        "lineas_con_datos": 0,
        "desde": None,
        "hasta": None,
        "error": None,
    }
    if not estado["existe"]:
        return estado

    try:
        con = db.conectar()
    except Exception as e:  # noqa: BLE001
        estado["error"] = f"{type(e).__name__}: {e}"
        return estado

    try:
        tablas = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        # El archivo puede existir y estar vacio (SQLite lo crea al conectar).
        # Sin la tabla turnos no hay esquema: cuenta como no inicializada.
        if "turnos" not in tablas:
            return estado
        estado["inicializada"] = True

        fila = con.execute(
            "SELECT COUNT(*), MIN(fecha), MAX(fecha) FROM turnos"
        ).fetchone()
        estado["turnos"] = fila[0] or 0
        estado["desde"], estado["hasta"] = fila[1], fila[2]
        estado["documentos"] = con.execute(
            "SELECT COUNT(*) FROM documentos"
        ).fetchone()[0]
        estado["causas"] = con.execute("SELECT COUNT(*) FROM causas").fetchone()[0]
        estado["lineas_con_datos"] = con.execute(
            "SELECT COUNT(DISTINCT linea_id) FROM turnos WHERE linea_id IS NOT NULL"
        ).fetchone()[0]
    except Exception as e:  # noqa: BLE001
        estado["error"] = f"{type(e).__name__}: {e}"
    finally:
        con.close()
    return estado


def _ping_modelo() -> dict:
    """Comprueba que Ollama responda y que el modelo configurado este descargado.

    Lista modelos en vez de generar texto: en CPU una generacion de prueba
    costaria un minuto cada vez que se recarga la pagina. Ademas distingue los
    dos fallos que se confunden en planta — "el servidor no responde" y "el
    servidor responde pero ese modelo no esta bajado".
    """
    from agent import llm

    cfg = llm.config_openai()
    info = {
        "modelo": cfg.get("modelo", "?"),
        "base_url": cfg.get("base_url", "?"),
        "proveedor": cfg.get("proveedor", "?"),
        "num_ctx": cfg.get("num_ctx"),
        "responde": False,
        "modelo_listo": False,
        "modelos": [],
        "motivo": None,
    }
    try:
        from openai import OpenAI

        cli = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=10.0)
        info["modelos"] = sorted(m.id for m in cli.models.list().data)
        info["responde"] = True
    except Exception as e:  # noqa: BLE001
        info["motivo"] = f"{type(e).__name__}: {e}"
        return info

    # Ollama reporta el tag completo ("qwen2.5:7b"); el .env puede traerlo sin
    # tag. Se acepta la coincidencia exacta y la del nombre base con tag latest.
    nombre = info["modelo"]
    info["modelo_listo"] = any(
        m == nombre or m == f"{nombre}:latest" for m in info["modelos"]
    )
    return info


def _causas_por_tipo(db) -> dict[str, list[dict]]:
    """Catalogo de causas agrupado por tipo, para poblar los selectbox."""
    try:
        todas = [_a_dict(c) for c in db.causas()]
    except Exception:  # noqa: BLE001
        return {}
    agrupadas: dict[str, list[dict]] = {}
    for c in todas:
        agrupadas.setdefault(str(c.get("tipo") or ""), []).append(c)
    return agrupadas


def _normalizar_fila(fila_cruda, umbral: float) -> dict:
    """Traduce una fila de db.pendientes_revision a lo que pinta la pantalla.

    La traduccion que importa es la de la confianza. Nadie persiste
    `campos_no_literales`: esa lista muere en el extractor. Lo que si queda en la
    fila es `confianza = 0.0`, que es exactamente la marca que pone
    verificar_literalidad cuando el numero no aparecia en el texto. Asi que
    confianza cero se lee aqui como "esta cifra no la pude encontrar en el
    reporte" y se pinta en rojo. Una confianza baja pero no cero es otra cosa
    distinta —el modelo dudo de la clasificacion, no de la cifra— y no se pinta
    igual: mezclarlas mandaria a corregir numeros que estaban bien.

    Se aceptan claves sinonimas donde es barato: esta pantalla no tiene por que
    romperse si db.py renombra una columna del SELECT.
    """
    f = _a_dict(fila_cruda)
    tabla = str(f.get("tabla") or f.get("origen") or "").strip().lower()
    try:
        confianza = float(f.get("confianza")) if f.get("confianza") is not None else None
    except (TypeError, ValueError):
        confianza = None

    principal = COLUMNA_PRINCIPAL.get(tabla)
    valores = {}
    for columna in COLUMNAS_EDITABLES.get(tabla, {}):
        # `cantidad` es el nombre generico que uso una version anterior de la
        # cola para la magnitud principal. Se acepta como respaldo para no
        # depender de cual de las dos formas devuelva db.py.
        if columna in f:
            valores[columna] = f[columna]
        elif columna == principal:
            valores[columna] = f.get("cantidad")
        else:
            valores[columna] = None

    documento = f.get("documento") or f.get("archivo") or f.get("ruta")
    return {
        "crudo": f,
        "tabla": tabla,
        "id": f.get("fila_id", f.get("id")),
        "tipo": f.get("tipo") or TIPO_POR_TABLA.get(tabla, "parada"),
        "texto": f.get("texto") or f.get("causa_texto") or "(sin texto)",
        "causa_id": f.get("causa_id"),
        "causa_propuesta": (
            f"{f['causa_codigo']} · {f['causa_nombre']}"
            if f.get("causa_codigo") and f.get("causa_nombre") else None
        ),
        "confianza": confianza,
        "no_literal": confianza is not None and confianza <= 0.0,
        "baja_confianza": confianza is not None and 0.0 < confianza < umbral,
        "motivo": f.get("motivo"),
        "descripcion": f.get("descripcion"),
        "valores": valores,
        "principal": principal,
        "fecha": f.get("fecha"),
        "turno": f.get("turno"),
        "linea": f.get("linea") or f.get("linea_nombre"),
        "estacion": f.get("estacion") or f.get("estacion_nombre"),
        "documento": Path(str(documento)).name if documento else None,
    }


def _corregir_valor(db, tabla: str, columna: str, fila_id: int, valor) -> None:
    """Escribe a mano el dato que el extractor no pudo verificar.

    El contrato no tiene funcion para esto —marcar_revisado solo mueve la causa—
    y sin embargo corregir el numero es la otra mitad del trabajo de esta
    pantalla: de nada sirve clasificar bien una parada de "1250 minutos" que en
    el reporte decia 12. Se hace por db.conectar(), que es el unico acceso
    permitido, con tabla y columna validadas contra COLUMNAS_EDITABLES.

    No toca `confianza`: de eso se encarga marcar_revisado, que corre justo
    despues y la deja en 1.0 junto con revisado = 1. Subirla aqui tambien dejaria
    la fila como confiable y sin revisar si el guardado fallara en el paso
    siguiente.
    """
    if columna not in COLUMNAS_EDITABLES.get(tabla, {}):
        raise ValueError(f"columna no editable: {tabla}.{columna}")
    con = db.conectar()
    try:
        con.execute(
            f"UPDATE {tabla} SET {columna} = ? WHERE id = ?", (valor, fila_id)
        )
        con.commit()
    finally:
        con.close()


# --- Piezas de UI reutilizables ----------------------------------------------

def _sello_validador(dictamen: dict | None) -> None:
    """Pinta el veredicto del auditor con tres estados bien distintos.

    Verde y amarillo NO son lo mismo y mezclarlos es peligroso: "esta
    fundamentada" y "no pude comprobarlo" llevan a decisiones distintas.
    """
    if not dictamen:
        st.info("Sin dictamen del validador para esta respuesta.", icon="ℹ️")
        return

    verificado = dictamen.get("verificado")
    fundamentada = dictamen.get("fundamentada")
    sin_respaldo = dictamen.get("afirmaciones_sin_respaldo") or []
    explicacion = dictamen.get("explicacion") or ""

    if verificado and fundamentada:
        st.success(
            "🟢 **Fundamentada** — cada cifra de la respuesta aparece en la "
            "evidencia de las herramientas."
            + (f"\n\n{explicacion}" if explicacion else ""),
        )
    elif verificado and not fundamentada:
        st.error(
            "🔴 **Hay afirmaciones sin respaldo.** No uses estas cifras sin "
            "verificarlas contra el reporte original."
        )
        if sin_respaldo:
            for a in sin_respaldo:
                st.markdown(f":red[· {a}]")
        elif explicacion:
            st.markdown(f":red[· {explicacion}]")
    else:
        st.warning(
            "🟡 **No se pudo verificar.** El auditor no corrió o no devolvió un "
            "dictamen legible; la respuesta puede estar bien, pero nadie la revisó."
            + (f"\n\n{explicacion}" if explicacion else ""),
        )


def _propiedad(objeto, nombre: str):
    """Lee una propiedad opcional de la Traza sin acoplarse a que exista.

    `coberturas` y `faltantes` enriquecen el panel de auditoria pero no estan en
    el contrato: si un dia no estan, el panel muestra menos, no se cae.
    """
    try:
        return getattr(objeto, nombre, None)
    except Exception:  # noqa: BLE001
        return None


def _pintar_traza(traza, dictamen: dict | None) -> None:
    """Expander de auditoria: que se consulto y que dijo el validador."""
    pasos = list(getattr(traza, "pasos", []) or [])
    with st.expander(f"🔍 De dónde salió esto · {len(pasos)} consulta(s)", expanded=False):
        if not pasos:
            st.caption(
                "El agente no llamó ninguna herramienta. Toda cifra en esa "
                "respuesta saldría de su memoria, no de los datos de planta."
            )
        for i, paso in enumerate(pasos, start=1):
            paso = _a_dict(paso)
            nombre = paso.get("herramienta") or paso.get("nombre") or "?"
            args = paso.get("argumentos") or paso.get("args") or {}
            if isinstance(args, dict):
                firma = ", ".join(f"{k}={v!r}" for k, v in args.items())
            else:
                firma = str(args)
            st.code(f"{i}. {nombre}({firma})", language="python")

        # La cobertura es la diferencia entre "el scrap del mes" y "el scrap de
        # los 12 turnos que alguien cargo". Va junto a la traza y no enterrada en
        # la evidencia, porque cambia como se lee la cifra.
        coberturas = _propiedad(traza, "coberturas") or []
        if coberturas:
            st.caption("Cobertura de los datos usados:")
            for c in coberturas:
                c = _a_dict(c)
                st.caption(
                    f"· {c.get('herramienta')}: {c.get('turnos_encontrados')} de "
                    f"{c.get('turnos_esperados')} turnos"
                    + (f" · {c.get('sin_clasificar')} sin clasificar"
                       if c.get("sin_clasificar") else "")
                    + ("  ⚠️ parcial" if c.get("parcial") else "")
                )

        faltantes = _propiedad(traza, "faltantes") or []
        if faltantes:
            st.caption("Lo que no se pudo responder con los datos cargados:")
            for f in faltantes:
                st.caption(f"· {f}")

        st.divider()
        _sello_validador(dictamen)


def _con_paciencia(titulo: str, fn):
    """Corre fn() con un estado visible y cronometro.

    Devuelve (resultado, error, segundos). Un usuario que no ve movimiento a los
    40 segundos asume que se colgo y recarga la pagina — y en CPU recargar es
    tirar a la basura un minuto de computo. Por eso el aviso va adentro del
    estado, no en una nota al pie.
    """
    inicio = time.perf_counter()
    with st.status(titulo, expanded=True) as estado:
        st.caption(AVISO_CPU)
        try:
            resultado = fn()
        except Exception as e:  # noqa: BLE001
            estado.update(label=f"{titulo} — no se pudo completar", state="error")
            return None, e, time.perf_counter() - inicio
        segundos = time.perf_counter() - inicio
        estado.update(label=f"{titulo} · {segundos:.0f} s", state="complete")
        return resultado, None, segundos


def _evidencia_json(traza) -> str:
    """La traza expone evidencia_json como metodo; se tolera que sea atributo."""
    ev = getattr(traza, "evidencia_json", None)
    if callable(ev):
        try:
            return ev()
        except Exception:  # noqa: BLE001
            return "[]"
    return ev if isinstance(ev, str) else "[]"


# --- Estado de sesion ---------------------------------------------------------

def inicializar_estado() -> None:
    valores = {
        "mensajes_prod": [],     # lo que se pinta en el chat
        "historial_prod": [],    # memoria que ve el modelo
        "resultados_carga": [],  # ultima corrida de ingesta
        "ping": None,            # diagnostico de Ollama, cacheado
        "resumen": None,         # ultimo resumen ejecutivo generado
    }
    for clave, valor in valores.items():
        if clave not in st.session_state:
            st.session_state[clave] = valor


# --- Barra lateral ------------------------------------------------------------

def _boton_inicializar(db, clave: str, forzar: bool = False) -> None:
    etiqueta = "Reconstruir DB (borra los datos)" if forzar else "Inicializar DB"
    if not st.button(etiqueta, key=clave, type="primary", width="stretch"):
        return
    try:
        with st.spinner("Creando esquema y cargando la taxonomía…"):
            db.inicializar(forzar=forzar)
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo inicializar la base: {type(e).__name__}: {e}")
        return
    st.success("Base de datos lista.")
    st.rerun()


def barra_lateral(db, estado: dict, errores: dict, pendientes: list) -> None:
    with st.sidebar:
        st.markdown(
            '<div style="font-family:Archivo,system-ui,sans-serif;font-weight:800;'
            'font-size:18px;line-height:1.05">COPILOTO DE<br>PRODUCCIÓN</div>'
            '<div class="dsm-kicker" style="margin-top:6px">'
            'Haceb · modelo local · sin internet</div>',
            unsafe_allow_html=True,
        )
        st.divider()
        leyenda_datos()
        st.divider()

        st.markdown("### Estado del sistema")

        # --- Modelo ---
        if st.session_state.ping is None:
            st.session_state.ping = _ping_modelo()
        ping = st.session_state.ping

        st.markdown("**Modelo**")
        st.caption(f"`{ping['modelo']}` · {ping['proveedor']} · {ping['base_url']}")
        if not ping["responde"]:
            st.error("No responde", icon="🔴")
            st.caption(str(ping["motivo"])[:200])
        elif not ping["modelo_listo"]:
            st.warning("Servidor arriba, modelo no descargado", icon="🟡")
            st.caption(
                f"`ollama pull {ping['modelo']}`. Disponibles: "
                + (", ".join(ping["modelos"][:6]) or "ninguno")
            )
        else:
            st.success("Responde", icon="🟢")
        if ping.get("num_ctx"):
            st.caption(f"Ventana de contexto: {ping['num_ctx']} tokens")
        if st.button("Volver a probar", width="stretch"):
            st.session_state.ping = _ping_modelo()
            st.rerun()

        st.divider()

        # --- Base de datos ---
        st.markdown("**Base de datos**")
        st.caption(f"`{estado['ruta']}`")
        if estado["error"]:
            st.error(estado["error"][:200], icon="🔴")
        if not estado["inicializada"]:
            st.warning(
                "Sin inicializar" if estado["existe"] else "No existe", icon="🟡"
            )
            _boton_inicializar(db, "init_lateral")
        else:
            col_a, col_b = st.columns(2)
            col_a.metric("Turnos", estado["turnos"])
            col_b.metric("Documentos", estado["documentos"])
            col_c, col_d = st.columns(2)
            col_c.metric("Líneas con datos", estado["lineas_con_datos"])
            # El "+" avisa que el conteo esta topado por el limite y no es el
            # total real. Mostrar 200 a secas seria una cifra falsa.
            tope = "+" if len(pendientes) >= LIMITE_REVISION else ""
            col_d.metric("Por revisar", f"{len(pendientes)}{tope}")

            if estado["desde"] and estado["hasta"]:
                st.caption(f"Rango cargado: **{estado['desde']} → {estado['hasta']}**")
            else:
                st.caption("Rango cargado: sin turnos todavía.")

            if not estado["causas"]:
                st.warning(
                    "La tabla de causas está vacía: la taxonomía no se cargó. "
                    "Sin ella nada se puede clasificar.",
                    icon="⚠️",
                )
            else:
                st.caption(f"Taxonomía: {estado['causas']} causas canónicas.")

            with st.expander("Zona de riesgo"):
                st.caption(
                    "Reconstruir borra turnos, paradas, scrap y el historial de "
                    "documentos. Solo para empezar de cero."
                )
                if st.checkbox("Entiendo que se pierden los datos", key="ok_forzar"):
                    _boton_inicializar(db, "init_forzar", forzar=True)

        # --- Modulos ---
        if errores:
            st.divider()
            st.markdown("**Módulos faltantes**")
            for nombre, motivo in errores.items():
                st.caption(f"· `produccion/{nombre}.py` — {motivo[:120]}")


# --- Pestaña: Cargar ----------------------------------------------------------

ICONO_ESTADO = {"ok": "✅", "duplicado": "🔁", "error": "⚠️"}


def _fila_resultado(res: dict) -> dict:
    """Aplana el retorno de ingest.procesar a una fila de tabla.

    'duplicado' se pinta con su propio icono y no con el de error: que un archivo
    ya estuviera cargado es el sistema funcionando (la idempotencia por sha256),
    no una falla del supervisor que lo volvio a arrastrar.
    """
    estado = str(res.get("estado", "error"))
    return {
        "Archivo": res.get("archivo", "?"),
        "Formato": res.get("formato", "?"),
        "Estado": f"{ICONO_ESTADO.get(estado, '·')} {estado}",
        "Turnos guardados": res.get("turnos_guardados", 0),
        "Campos dudosos": res.get("campos_dudosos", 0),
        "Detalle": res.get("motivo") or "",
    }


def _procesar_rutas(ingest, rutas: list[Path]) -> list[dict]:
    """Corre el pipeline archivo por archivo, mostrando avance real."""
    barra = st.progress(0.0, text="Procesando…")
    resultados = []
    for i, ruta in enumerate(rutas):
        barra.progress(i / len(rutas), text=f"Leyendo {ruta.name}…")
        try:
            res = ingest.procesar(ruta)
        except Exception as e:  # noqa: BLE001
            # Un archivo corrupto no puede tumbar la carga de los otros seis.
            res = {
                "archivo": ruta.name,
                "formato": ruta.suffix.lstrip(".") or "desconocido",
                "estado": "error",
                "turnos_guardados": 0,
                "campos_dudosos": 0,
                "motivo": f"{type(e).__name__}: {e}",
            }
        res.setdefault("archivo", ruta.name)
        resultados.append(res)
    barra.progress(1.0, text=f"{len(rutas)} archivo(s) procesado(s).")
    return resultados


CLASE_ESTADO = {"ok": "est-ok", "duplicado": "est-dup", "error": "est-err"}
ROTULO_ESTADO = {"ok": "GUARDADO", "duplicado": "YA ESTABA", "error": "NO SE PUDO"}


def _mostrar_resultados(resultados: list[dict]) -> None:
    """Una fila por archivo, con el estado como rotulo y no como icono.

    Se pinta a mano y no con st.dataframe porque la columna de dudosos tiene que
    salir con la marca rayada del design system: es la que manda al supervisor a
    la pestana Revisar, y dentro de una tabla se pierde entre las demas.
    """
    if not resultados:
        return
    st.markdown("#### Resultado por archivo")
    for r in resultados:
        estado = str(r.get("estado", "error"))
        dudosos = int(r.get("campos_dudosos", 0) or 0)
        c1, c2, c3, c4, c5 = st.columns([4, 1.5, 1.3, 1, 2],
                                        vertical_alignment="center")
        c1.markdown(
            f'<div style="font-family:Archivo,system-ui,sans-serif;font-weight:800;'
            f'font-size:15px;word-break:break-all">{r.get("archivo", "?")}</div>'
            f'<div style="font-size:12px;color:var(--dsm-neutral-700);margin-top:2px">'
            f'{r.get("motivo") or ""}</div>',
            unsafe_allow_html=True,
        )
        c2.markdown(
            f'<span style="font-size:12px;color:var(--dsm-neutral-700)">'
            f'{r.get("formato", "?")}</span>',
            unsafe_allow_html=True,
        )
        c3.markdown(
            f'<span class="{CLASE_ESTADO.get(estado, "est-err")}">'
            f'{ROTULO_ESTADO.get(estado, estado.upper())}</span>',
            unsafe_allow_html=True,
        )
        c4.markdown(
            f'<span style="font-family:Archivo,system-ui,sans-serif;font-weight:800;'
            f'font-size:14px">{r.get("turnos_guardados", 0)} turnos</span>',
            unsafe_allow_html=True,
        )
        c5.markdown(
            "<span style='font-size:13px;color:var(--dsm-neutral-700)'>"
            "ningún campo dudoso</span>"
            if dudosos == 0 else
            f"{dato(dudosos, 'dudoso')}"
            f"<span style='font-size:13px;margin-left:8px'>campos por revisar</span>",
            unsafe_allow_html=True,
        )
        st.markdown(
            '<hr style="margin:6px 0;border:0;border-bottom:1px solid var(--dsm-divider)">',
            unsafe_allow_html=True,
        )

    ok = sum(1 for r in resultados if r.get("estado") == "ok")
    dup = sum(1 for r in resultados if r.get("estado") == "duplicado")
    err = sum(1 for r in resultados if r.get("estado") == "error")
    turnos = sum(int(r.get("turnos_guardados") or 0) for r in resultados)
    dudosos = sum(int(r.get("campos_dudosos") or 0) for r in resultados)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Procesados", ok)
    c2.metric("Ya estaban", dup)
    c3.metric("Con error", err)
    c4.metric("Turnos guardados", turnos)

    if dudosos:
        st.warning(
            f"{dudosos} campo(s) quedaron marcados como dudosos. Están en la "
            "pestaña **Revisar**: hasta que alguien los mire, cualquier total "
            "que los incluya es provisional.",
            icon="🔎",
        )
    elif turnos:
        st.success("Todos los campos verificados contra el texto original.")


def pestana_cargar(ingest) -> None:
    st.markdown("### Cargar reportes de turno")
    st.caption(
        "Excel, CSV, PDF o el texto pegado del grupo de WhatsApp. Cada archivo se "
        "guarda en `produccion/inbox/` y se procesa completo: extraer, verificar "
        "contra el texto original, normalizar causas y guardar."
    )

    archivos = st.file_uploader(
        "Arrastra los reportes",
        type=FORMATOS,
        accept_multiple_files=True,
    )

    col_a, col_b = st.columns([2, 3])
    with col_a:
        procesar = st.button(
            f"Procesar {len(archivos)} archivo(s)" if archivos else "Procesar",
            type="primary",
            disabled=not archivos,
            width="stretch",
        )
    with col_b:
        ejemplos = sorted(p for p in EJEMPLOS.glob("*") if p.is_file()) if EJEMPLOS.exists() else []
        cargar_ejemplos = st.button(
            f"Cargar los {len(ejemplos)} ejemplos del repo",
            disabled=not ejemplos,
            width="stretch",
            help="Reportes de turno de muestra, para ver el flujo completo sin datos reales.",
        )

    if procesar and archivos:
        INBOX.mkdir(parents=True, exist_ok=True)
        rutas = []
        for archivo in archivos:
            # El nombre de un archivo subido es entrada no confiable: puede traer
            # separadores de ruta. Path(...).name lo deja en un nombre plano.
            destino = INBOX / (Path(archivo.name).name or "reporte.txt")
            destino.write_bytes(archivo.getvalue())
            rutas.append(destino)
        st.session_state.resultados_carga = _procesar_rutas(ingest, rutas)
        st.rerun()

    if cargar_ejemplos and ejemplos:
        INBOX.mkdir(parents=True, exist_ok=True)
        rutas = []
        for origen in ejemplos:
            destino = INBOX / origen.name
            destino.write_bytes(origen.read_bytes())
            rutas.append(destino)
        st.session_state.resultados_carga = _procesar_rutas(ingest, rutas)
        st.rerun()

    _mostrar_resultados(st.session_state.resultados_carga)

    with st.expander("Inbox en disco"):
        en_disco = sorted(p for p in INBOX.glob("*") if p.is_file()) if INBOX.exists() else []
        if not en_disco:
            st.caption("`produccion/inbox/` está vacía.")
        else:
            for p in en_disco:
                st.caption(f"· {p.name} ({p.stat().st_size / 1024:.1f} KB)")
            st.caption(
                "Reprocesar es seguro: los archivos ya cargados se detectan por "
                "sha256 y se reportan como duplicados."
            )
            if st.button("Reprocesar todo el inbox"):
                try:
                    resultados = ingest.procesar_inbox(INBOX)
                except Exception as e:  # noqa: BLE001
                    st.error(f"Falló el reproceso: {type(e).__name__}: {e}")
                else:
                    st.session_state.resultados_carga = list(resultados)
                    st.rerun()


# --- Pestaña: Revisar ---------------------------------------------------------

def _opciones_causa(causas: list[dict]) -> tuple[list[str], dict[str, int | None]]:
    """Arma las etiquetas del selectbox y su mapa a causa_id."""
    etiquetas = ["— dejar pendiente —", "No aplica ninguna (marcar revisada)"]
    mapa: dict[str, int | None] = {e: None for e in etiquetas}
    for c in sorted(causas, key=lambda x: (str(x.get("categoria") or ""), str(x.get("codigo") or ""))):
        etiqueta = f"{c.get('codigo')} · {c.get('nombre')}"
        if c.get("categoria"):
            etiqueta += f"  [{c['categoria']}]"
        etiquetas.append(etiqueta)
        mapa[etiqueta] = c.get("id")
    return etiquetas, mapa


def _cabecera_fila(fila: dict) -> str:
    partes = [p for p in (fila["fecha"], f"T{fila['turno']}" if fila["turno"] else None,
                          fila["linea"], fila["estacion"]) if p]
    contexto = " · ".join(str(p) for p in partes) or "sin contexto de turno"
    cabecera = f"**{fila['tabla'] or 'fila'} #{fila['id']}** — {contexto}"
    # El nombre del reporte no es decoracion: es lo que le permite al supervisor
    # ir a la fuente y decidir, en vez de adivinar cual de los quince archivos
    # de la semana produjo esta fila.
    return cabecera + (f"  ·  reporte: `{fila['documento']}`" if fila["documento"] else "")


def _numero(valor) -> float | None:
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _formulario_fila(db, fila: dict, causas: list[dict]) -> None:
    st.markdown(_cabecera_fila(fila))

    # El texto original, sin tocar ni corregir. Es lo unico contra lo que el
    # supervisor puede contrastar lo que recuerda del turno.
    es_calidad = fila["tabla"] == "calidad"
    st.markdown("Tipo de defecto registrado:" if es_calidad else "Texto original de la causa:")
    st.code(str(fila["texto"]), language=None)

    if fila["descripcion"]:
        st.caption(f"Descripción: {fila['descripcion']}")
    if fila["motivo"]:
        st.caption(f"Está en la cola por: {fila['motivo']}")
    if fila["causa_propuesta"] and not es_calidad:
        st.caption(f"El extractor propuso: {fila['causa_propuesta']} (sin confirmar)")

    principal = fila["principal"]
    if fila["no_literal"]:
        st.markdown(
            f":red[**Cifra sin respaldo en el reporte.** El extractor escribió "
            f"**{principal} = {fila['valores'].get(principal)}**, pero no encontró "
            f"ese número en el texto original. Corrígela contra el reporte o "
            f"confírmala:]"
        )
    elif fila["baja_confianza"]:
        st.caption(
            f"Confianza del extractor: {fila['confianza']:.2f} — dudó de la "
            f"clasificación, no de la cifra."
        )

    editables = COLUMNAS_EDITABLES.get(fila["tabla"], {})

    with st.form(key=f"rev-{fila['tabla']}-{fila['id']}"):
        etiquetas, mapa = _opciones_causa(causas)
        eleccion = etiquetas[0]

        if es_calidad:
            # La tabla calidad no tiene columna causa_id: el defecto se describe
            # con texto libre y marcar_revisado ignora el argumento. Ofrecer un
            # selector aqui seria pedirle un dato que no se guarda en ningun lado.
            st.caption(
                "Los defectos de calidad no se clasifican con el catálogo de "
                "causas: se revisa la cifra y se confirma."
            )
        else:
            # Si el extractor propuso una causa (con confianza baja), se
            # preselecciona: confirmar cuesta un clic y corregir tambien, pero no
            # se tira a la basura lo que ya acerto.
            indice = 0
            if fila["causa_id"] is not None:
                for i, e in enumerate(etiquetas):
                    if mapa.get(e) == fila["causa_id"]:
                        indice = i
                        break
            eleccion = st.selectbox(
                f"Causa canónica ({fila['tipo']})",
                etiquetas,
                index=indice,
                key=f"causa-{fila['tabla']}-{fila['id']}",
            )

        nuevos: dict[str, object] = {}
        columnas = st.columns(len(editables)) if editables else []
        for col, (campo, clase) in zip(columnas, editables.items()):
            actual = fila["valores"].get(campo)
            # El rojo va sobre el campo concreto que el extractor no pudo
            # verificar, no sobre la fila entera: los demas estaban bien.
            marca = "🔴 " if (fila["no_literal"] and campo == principal) else ""
            clave = f"val-{fila['tabla']}-{fila['id']}-{campo}"
            with col:
                if clase == "numero":
                    # value=None a proposito: un campo vacio se queda vacio.
                    # Prellenarlo con 0.0 convertiria "no se sabe" en "fue cero",
                    # que es justo la confusion que este proyecto no se permite.
                    nuevos[campo] = st.number_input(
                        f"{marca}{campo}", value=_numero(actual), step=1.0, key=clave,
                    )
                else:
                    nuevos[campo] = st.text_input(
                        f"{marca}{campo}",
                        value=str(actual) if actual is not None else "",
                        key=clave,
                    ) or None

        guardar = st.form_submit_button(
            "Confirmar cifra" if es_calidad else "Guardar", type="primary"
        )

    if not guardar:
        return

    cambios = []
    try:
        for campo, valor in nuevos.items():
            actual = fila["valores"].get(campo)
            # Solo se escribe lo que de verdad cambio. Vaciar un campo no borra
            # el valor guardado: no hay forma de distinguir "lo vacie a
            # proposito" de "no lo toque", y borrar un dato por accidente es peor
            # que dejar marcado como dudoso uno que ya lo estaba.
            if valor is None:
                continue
            if editables[campo] == "numero":
                anterior = _numero(actual)
                if anterior is not None and abs(float(valor) - anterior) <= 1e-9:
                    continue
                valor = float(valor)
            elif str(valor) == str(actual):
                continue
            _corregir_valor(db, fila["tabla"], campo, fila["id"], valor)
            cambios.append(f"{campo} = {valor}")

        if es_calidad:
            db.marcar_revisado(fila["tabla"], fila["id"], None)
            cambios.append("marcada como revisada")
        elif eleccion != etiquetas[0]:
            db.marcar_revisado(fila["tabla"], fila["id"], mapa[eleccion])
            cambios.append(f"causa: {eleccion}")
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo guardar: {type(e).__name__}: {e}")
        return

    if not cambios:
        st.info(
            "No cambió nada: elige una causa (o «No aplica ninguna») para sacarla "
            "de la cola."
        )
        return
    st.success("Guardado: " + "; ".join(cambios))
    st.rerun()


def pestana_revisar(db, pendientes: list) -> None:
    st.markdown("### Cola de revisión")
    st.caption(
        "Lo que el extractor no supo clasificar y los números que no encontró "
        "literales en el reporte. Nada de esto se descarta ni se adivina: espera "
        "aquí a que alguien que estuvo en el turno lo confirme."
    )

    if not pendientes:
        st.success(
            "**Nada pendiente.** Todas las filas cargadas tienen causa canónica "
            "asignada y sus cifras aparecen literales en el reporte original. Los "
            "totales del resumen ejecutivo se pueden leer completos.",
            icon="✅",
        )
        return

    umbral = float(getattr(db, "UMBRAL_REVISION", UMBRAL_REVISION))
    filas = [_normalizar_fila(f, umbral) for f in pendientes]
    # Sin id o sin tabla no hay forma de escribir la correccion de vuelta.
    filas = [f for f in filas if f["id"] is not None and f["tabla"]]

    if not filas:
        st.warning(
            "Hay filas pendientes pero llegaron sin tabla ni id, así que no se "
            "pueden corregir desde aquí.",
            icon="⚠️",
        )
        return

    causas = _causas_por_tipo(db)
    if not causas:
        st.error(
            "No pude leer el catálogo de causas. Sin taxonomía cargada no hay "
            "nada que asignar: revisa la inicialización de la base."
        )
        return

    sin_clasificar = sum(1 for f in filas if f["causa_id"] is None and f["tabla"] != "calidad")
    no_literales = sum(1 for f in filas if f["no_literal"])
    c1, c2, c3 = st.columns(3)
    c1.metric("En cola", len(filas))
    c2.metric("Sin clasificar", sin_clasificar)
    c3.metric("Cifras no literales", no_literales)

    tipos = sorted({f["tipo"] for f in filas})
    filtro = st.radio(
        "Tipo", ["todos"] + tipos, horizontal=True, key="filtro_revision"
    )
    visibles = [f for f in filas if filtro == "todos" or f["tipo"] == filtro]
    if not visibles:
        st.info(f"Nada pendiente de tipo «{filtro}».")
        return

    # El deslizador solo aparece cuando de verdad hay que paginar: con cuatro
    # filas en cola, un control para elegir cuantas ver es ruido (y ademas
    # Streamlit no admite un slider con min == max).
    if len(visibles) > POR_PAGINA:
        tope = st.slider("Filas a mostrar", 5, len(visibles), POR_PAGINA)
    else:
        tope = len(visibles)
    st.caption(f"{len(visibles)} fila(s) en cola; mostrando {min(tope, len(visibles))}.")

    for fila in visibles[:tope]:
        with st.container(border=True):
            _formulario_fila(db, fila, causas.get(fila["tipo"], []))


# --- Pestaña: Preguntar -------------------------------------------------------

PREGUNTAS_EJEMPLO = [
    "¿Qué se está repitiendo en L2 esta semana?",
    "¿Cuánto scrap hubo en L1 en los últimos 7 días?",
    "Dame el pareto de paradas de L3.",
    "¿Cumplimos el plan de producción ayer?",
]


def _procesar_pregunta(agente, pregunta: str) -> None:
    st.session_state.mensajes_prod.append({"rol": "user", "texto": pregunta})
    with st.chat_message("user"):
        st.markdown(pregunta)

    with st.chat_message("assistant"):
        resultado, error, segundos = _con_paciencia(
            "Consultando los datos de planta…",
            lambda: agente.responder(pregunta, st.session_state.historial_prod),
        )
        if error is not None:
            st.error(f"No pude consultar el modelo: {type(error).__name__}")
            st.caption(str(error)[:400])
            st.caption(
                "Si es un timeout: el modelo en CPU puede pasarse del límite del "
                "cliente. Prueba con un modelo más pequeño (`OLLAMA_MODEL=qwen2.5:3b`)."
            )
            # La pregunta ya quedo pintada; se saca del historial para que un
            # reintento no arrastre un turno sin respuesta.
            st.session_state.mensajes_prod.pop()
            return

        texto, traza, historial = resultado
        st.session_state.historial_prod = historial

        dictamen, error_val, _ = _con_paciencia(
            "Auditando la respuesta…",
            lambda: agente.validar(texto, _evidencia_json(traza)),
        )
        if error_val is not None:
            dictamen = {
                "verificado": False,
                "fundamentada": None,
                "afirmaciones_sin_respaldo": [],
                "explicacion": f"El auditor falló ({type(error_val).__name__}).",
            }

        st.markdown(texto)
        st.caption(f"Respondido en {segundos:.0f} s.")
        _pintar_traza(traza, dictamen)

    st.session_state.mensajes_prod.append({
        "rol": "assistant", "texto": texto, "traza": traza,
        "dictamen": dictamen, "segundos": segundos,
    })


def pestana_preguntar(agente) -> None:
    st.markdown("### Preguntar")
    st.info(AVISO_CPU, icon="⏳")

    columnas = st.columns(len(PREGUNTAS_EJEMPLO))
    sugerida = None
    for col, ejemplo in zip(columnas, PREGUNTAS_EJEMPLO):
        if col.button(ejemplo, width="stretch"):
            sugerida = ejemplo

    if st.session_state.mensajes_prod and st.button("Limpiar conversación"):
        st.session_state.mensajes_prod = []
        st.session_state.historial_prod = []
        st.rerun()

    for m in st.session_state.mensajes_prod:
        with st.chat_message("user" if m["rol"] == "user" else "assistant"):
            st.markdown(m["texto"])
            if m["rol"] == "assistant":
                if m.get("segundos"):
                    st.caption(f"Respondido en {m['segundos']:.0f} s.")
                _pintar_traza(m.get("traza"), m.get("dictamen"))

    pregunta = st.chat_input("Pregunta sobre turnos, paradas, scrap o calidad")
    if sugerida:
        pregunta = sugerida
    if pregunta:
        _procesar_pregunta(agente, pregunta)


# --- Pestaña: Resumen ejecutivo ----------------------------------------------

def _rango_por_defecto(estado: dict) -> tuple[date, date]:
    """Ultima semana con datos, no la ultima semana del calendario.

    Si los turnos cargados son de junio, ofrecer "los ultimos 7 dias" desde hoy
    devuelve un reporte vacio y parece que el sistema no sirve.
    """
    def _fecha(texto, alterna):
        try:
            return datetime.strptime(str(texto)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return alterna

    hoy = date.today()
    hasta = _fecha(estado.get("hasta"), hoy)
    desde = max(_fecha(estado.get("desde"), hasta - timedelta(days=6)), hasta - timedelta(days=6))
    return desde, hasta


def pestana_resumen(db, reporte, estado: dict) -> None:
    st.markdown("### Resumen ejecutivo")
    st.caption(
        "Las cifras se calculan primero en Python y solo entonces el modelo las "
        "redacta. Nunca se le pregunta al modelo cuánto scrap hubo."
    )

    desde_def, hasta_def = _rango_por_defecto(estado)
    try:
        nombres = [str(_a_dict(l).get("nombre")) for l in db.lineas()]
    except Exception:  # noqa: BLE001
        nombres = []

    c1, c2, c3 = st.columns(3)
    desde = c1.date_input("Desde", value=desde_def)
    hasta = c2.date_input("Hasta", value=hasta_def)
    linea = c3.selectbox("Línea", ["Todas"] + nombres)

    if desde > hasta:
        st.error("El rango está al revés: 'desde' es posterior a 'hasta'.")
        return

    if st.button("Generar resumen ejecutivo", type="primary"):
        linea_arg = None if linea == "Todas" else linea
        resultado, error, segundos = _con_paciencia(
            "Calculando y redactando…",
            lambda: reporte.resumen_ejecutivo(desde.isoformat(), hasta.isoformat(), linea_arg),
        )
        if error is not None:
            st.error(f"No se pudo generar: {type(error).__name__}: {error}")
            return
        st.session_state.resumen = {
            "datos": resultado,
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "linea": linea_arg,
            "segundos": segundos,
        }

    guardado = st.session_state.resumen
    if not guardado:
        return

    datos = guardado["datos"] or {}
    markdown = datos.get("markdown") or "*(El reporte volvió vacío.)*"

    st.divider()
    st.caption(
        f"{guardado['desde']} → {guardado['hasta']} · "
        f"{guardado['linea'] or 'todas las líneas'} · "
        f"generado en {guardado['segundos']:.0f} s"
    )
    st.markdown(markdown)

    nombre = f"resumen_{guardado['desde']}_{guardado['hasta']}"
    if guardado["linea"]:
        nombre += f"_{guardado['linea']}"
    st.download_button(
        "Descargar .md",
        data=markdown,
        file_name=f"{nombre}.md",
        mime="text/markdown",
    )

    st.divider()
    _sello_validador(datos.get("validacion"))
    evidencia = datos.get("evidencia")
    if evidencia:
        with st.expander("Evidencia numérica que se le pasó al redactor"):
            st.json(evidencia)


# --- Arranque en frio ---------------------------------------------------------

def panel_arranque_frio(db, estado: dict) -> None:
    st.warning(
        "**La base de datos todavía no existe.** Es el primer arranque: hay que "
        "crear el esquema y cargar la taxonomía (líneas, estaciones y las 45 "
        "causas canónicas) antes de poder cargar reportes.",
        icon="🧊",
    )
    st.caption(f"Se creará en `{estado['ruta']}`.")
    izquierda, _ = st.columns([1, 2])
    with izquierda:
        _boton_inicializar(db, "init_principal")


# --- Main ---------------------------------------------------------------------

def main() -> None:
    inicializar_estado()
    aplicar_estilo()

    st.title("Copiloto de Producción HACEB")
    st.caption(
        "Consolida los reportes de turno, muestra lo que se repite y responde "
        "preguntas — todo local, sin que un solo dato de planta salga de la red."
    )

    modulos, errores = _cargar_modulos()
    db = modulos.get("db")
    if db is None:
        st.error(
            "No se pudo importar `produccion/db.py`, que es de donde sale todo "
            "lo demás. Sin ese módulo la aplicación no tiene qué mostrar."
        )
        st.code(errores.get("db", "módulo ausente"))
        st.stop()

    estado = _estado_db(db)
    pendientes = []
    if estado["inicializada"]:
        try:
            pendientes = list(db.pendientes_revision(limite=LIMITE_REVISION))
        except Exception as e:  # noqa: BLE001
            st.warning(f"No pude leer la cola de revisión: {type(e).__name__}: {e}")

    barra_lateral(db, estado, errores, pendientes)

    if not estado["inicializada"]:
        panel_arranque_frio(db, estado)
        st.stop()

    # El badge de pendientes va en el nombre de la pestaña: es el unico numero
    # que la app quiere que el supervisor vea sin buscarlo.
    # Sin emojis: el design system usa tipografia y el acento rojo para jerarquia,
    # y ese rojo esta reservado para los datos sin respaldo.
    etiqueta_revisar = "Revisar" + (f" ({len(pendientes)})" if pendientes else "")
    cargar, revisar, preguntar, resumen = st.tabs(
        ["Cargar", etiqueta_revisar, "Preguntar", "Resumen ejecutivo"]
    )

    with cargar:
        if modulos.get("ingest") is None:
            st.error(f"`produccion/ingest.py` no está disponible: {errores.get('ingest')}")
        else:
            pestana_cargar(modulos["ingest"])

    with revisar:
        pestana_revisar(db, pendientes)

    with preguntar:
        if modulos.get("agente") is None:
            st.error(f"`produccion/agente.py` no está disponible: {errores.get('agente')}")
        else:
            pestana_preguntar(modulos["agente"])

    with resumen:
        if modulos.get("reporte") is None:
            st.error(f"`produccion/reporte.py` no está disponible: {errores.get('reporte')}")
        else:
            pestana_resumen(db, modulos["reporte"], estado)


if __name__ == "__main__":
    main()
