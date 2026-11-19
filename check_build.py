#!/usr/bin/env python3
r"""
Check build/generation Genexus
==============================

Interfaz (Flet) para comparar lo que esta en la KB de GeneXus contra el codigo
generado en disco, y para explicar por que fallo el ultimo build.

Sirve para cualquier proyecto: se elige la instancia de SQL Server y la carpeta
generada (…\<modelo>\web).

    pip install flet
    python check_build.py
"""

from __future__ import annotations

import html
import io
import json
import os
import time
import re
import shutil
import subprocess
import sys
from datetime import datetime

import flet as ft

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gxcheck  # noqa: E402
import generar  # noqa: E402

TITULO = "Check build/generation Genexus"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(BASE_DIR, "estado.md")
SNAPSHOT_COMPLETO = os.path.join(BASE_DIR, "estado_completo.md")

ICONO_PNG = os.path.join(BASE_DIR, "Icono.png")
ICONO_ICO = os.path.join(BASE_DIR, "Icono.ico")


def icono_ventana() -> str | None:
    """Icono de la ventana (Windows solo acepta .ico).

    Se rearma el .ico si Icono.png es mas nuevo, asi reemplazar el PNG alcanza.
    Sin Pillow instalado se usa el .ico que ya este versionado al lado.
    """
    hay_png = os.path.exists(ICONO_PNG)
    if os.path.exists(ICONO_ICO) and (
        not hay_png or os.path.getmtime(ICONO_ICO) >= os.path.getmtime(ICONO_PNG)
    ):
        return ICONO_ICO
    try:
        from PIL import Image  # solo para convertir el PNG a .ico
    except ImportError:
        return ICONO_ICO if os.path.exists(ICONO_ICO) else None
    try:
        Image.open(ICONO_PNG).convert("RGBA").save(
            ICONO_ICO,
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                   (64, 64), (128, 128), (256, 256)],
        )
        return ICONO_ICO
    except Exception:
        return ICONO_ICO if os.path.exists(ICONO_ICO) else None

VERDE = ft.Colors.GREEN_700
ROJO = ft.Colors.RED_700
AMARILLO = ft.Colors.AMBER_800
GRIS = ft.Colors.BLUE_GREY_400
COLOR_ESTADO = {
    "OK": VERDE,
    "PENDIENTE": ROJO,
    "DESINCRONIZADO": AMARILLO,
    "DIFIERE": ROJO,
    "SIN BUILD": GRIS,
}

CATEGORIAS = [
    ("OK", ("OK",)),
    ("Pendientes", ("PENDIENTE",)),
    ("Desincronizados", ("DESINCRONIZADO",)),
    ("Contenido distinto", ("DIFIERE",)),
    ("Sin build", ("SIN BUILD",)),
]

# paleta basica: (nombre, color semilla, fondo del lienzo). Los de la lista
# OSCUROS pintan el lienzo oscuro y aclaran los textos de arriba.
OSCUROS = {"Negro", "Ónix", "Carbón", "Plomo", "Grafito"}
PALETA = [
    ("Negro", ft.Colors.BLACK, ft.Colors.GREY_900),
    ("Ónix", ft.Colors.GREY_900, ft.Colors.GREY_800),
    ("Carbón", ft.Colors.BLUE_GREY_900, ft.Colors.BLUE_GREY_900),
    ("Plomo", ft.Colors.GREY_600, ft.Colors.GREY_700),
    ("Grafito", ft.Colors.GREY_800, ft.Colors.GREY_800),
    ("Acero", ft.Colors.BLUE_GREY_700, ft.Colors.BLUE_GREY_50),
    ("Azul noche", ft.Colors.INDIGO_900, ft.Colors.BLUE_GREY_50),
    ("Pizarra", ft.Colors.BLUE_900, ft.Colors.BLUE_50),
    ("Petróleo", ft.Colors.TEAL_900, ft.Colors.TEAL_50),
    ("Verde militar", ft.Colors.GREEN_900, ft.Colors.GREEN_50),
    ("Vino", ft.Colors.RED_900, ft.Colors.RED_50),
    ("Bronce", ft.Colors.BROWN_700, ft.Colors.ORANGE_50),
]


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB]")


def decodificar(raw: bytes) -> str:
    """opencode escribe UTF-8; cmd escribe en OEM. Se prueba en ese orden."""
    for enc in ("utf-8", "cp1252", "oem"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def limpiar_salida(texto: str) -> str:
    """Saca colores ANSI y las lineas de estado que agrega opencode."""
    texto = ANSI_RE.sub("", texto).replace("\r", "")
    lineas = [l for l in texto.splitlines()
              if not re.match(r"^>\s.*·", l.strip())]
    while lineas and not lineas[0].strip():
        lineas.pop(0)
    while lineas and not lineas[-1].strip():
        lineas.pop()
    return "\n".join(lineas)


def _agentes_de_config(ruta: str) -> set[str]:
    """Agentes declarados en un opencode.json / .jsonc."""
    try:
        with open(ruta, encoding="utf-8") as fh:
            texto = fh.read()
    except OSError:
        return set()
    sin_comentarios = re.sub(r"^\s*//.*$", "", texto, flags=re.M)
    try:
        datos = json.loads(sin_comentarios)
        declarados = datos.get("agent")
        if isinstance(declarados, dict):
            return set(declarados)
    except ValueError:
        pass
    bloque = re.search(r'"agent"\s*:\s*\{(.*?)\n\s{0,6}\}', sin_comentarios,
                       re.S)
    if not bloque:
        return set()
    return set(re.findall(r'^\s{2,8}"([\w.\-]+)"\s*:\s*\{', bloque.group(1),
                          re.M))


def listar_agentes(directorio: str = "") -> list[str]:
    """Agentes de opencode: built-ins, config JSON y archivos .md."""
    nombres = {"build", "plan"}
    hogar = os.path.expanduser("~")
    bases = [directorio, os.path.join(hogar, ".config", "opencode"), hogar]
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        for cfg in ("opencode.json", "opencode.jsonc",
                    os.path.join(".opencode", "opencode.json"),
                    os.path.join(".opencode", "opencode.jsonc")):
            nombres |= _agentes_de_config(os.path.join(base, cfg))
        for sub in ("agents", "agent", os.path.join(".opencode", "agent"),
                    os.path.join(".opencode", "agents")):
            carpeta = os.path.join(base, sub)
            if os.path.isdir(carpeta):
                nombres |= {os.path.splitext(f)[0]
                            for f in os.listdir(carpeta)
                            if f.lower().endswith(".md")}
    return sorted(nombres, key=lambda n: n.lower())


def _tabla_md(items) -> str:
    lineas = ["| OBJETO | TIPO | FUENTE | GENERADO | ESTADO | MOTIVO |",
              "|---|---|---|---|---|---|"]
    for i in items:
        motivo = (i.motivo or "").replace("|", "/").replace("\n", " ")
        lineas.append(f"| {i.objeto} | {i.tipo} | {i.fuente} | {i.generado} "
                      f"| {i.estado} | {motivo} |")
    return "\n".join(lineas)


def limpiar_snapshots() -> None:
    """Deja los dos archivos de estado en cero al arrancar una comparacion.

    Se truncan (no se borran) para que, si la comparacion falla, el agente
    encuentre archivos vacios en vez del estado de la corrida anterior.
    """
    for ruta in (SNAPSHOT, SNAPSHOT_COMPLETO):
        try:
            with open(ruta, "w", encoding="utf-8"):
                pass
        except OSError:
            pass


def guardar_snapshot(res) -> tuple[str, str]:
    """Reescribe estado.md y estado_completo.md, y arma el contexto del LLM.

    Los dos archivos se abren en modo "w": se pisa todo lo anterior, nunca se
    agrega. Devuelve (ruta del archivo, texto de contexto para el prompt).
    """
    grupos = [
        ("Pendientes", [i for i in res.items if i.estado == "PENDIENTE"]),
        ("Desincronizados", [i for i in res.items
                             if i.estado == "DESINCRONIZADO"]),
        ("Contenido distinto", [i for i in res.items if i.estado == "DIFIERE"]),
        ("Sin build", [i for i in res.items if i.estado == "SIN BUILD"]),
        ("OK", [i for i in res.items if i.estado == "OK"]),
    ]
    pendientes, desinc, difiere, sin_build, ok = (g[1] for g in grupos)

    def tabla_corta(items) -> str:
        filas = ["| OBJETO | TIPO | FUENTE | GENERADO | MOTIVO |",
                 "|---|---|---|---|---|"]
        for i in items:
            motivo = (i.motivo or "").replace("|", "/").replace("\n", " ")
            filas.append(f"| {i.objeto} | {i.tipo} | {i.fuente} | {i.generado} "
                         f"| {motivo} |")
        return "\n".join(filas)

    por_tipo: dict[str, int] = {}
    for i in sin_build:
        por_tipo[i.tipo] = por_tipo.get(i.tipo, 0) + 1
    tipos_txt = ", ".join(f"{t} {n}" for t, n in
                          sorted(por_tipo.items(), key=lambda x: -x[1])[:15])

    # estado.md = detalle que el agente puede leer del disco
    partes = [
        f"# Estado de la comparación — {datetime.now():%d/%m/%Y %H:%M}",
        "",
        f"- Modelo: {res.modelo}",
        f"- KB: {res.kb}",
        f"- Objetos: {res.total}",
        f"- OK: {res.ok} · Pendientes: {res.pendientes} · "
        f"Desincronizados: {res.desincronizados} · "
        f"Contenido distinto: {res.difiere} · Sin build: {res.sin_build}",
        f"- Último build: {gxcheck.resumen_run(res.ultimo_run)}",
        "",
        f"## Pendientes ({len(pendientes)})",
        tabla_corta(pendientes) if pendientes else "(ninguno)",
        "",
        f"## Desincronizados ({len(desinc)})",
        tabla_corta(desinc) if desinc else "(ninguno)",
        "",
        f"## Contenido distinto ({len(difiere)})",
        tabla_corta(difiere) if difiere else "(ninguno)",
        "",
        f"## Sin build ({len(sin_build)}) — solo tipos",
        tipos_txt or "(ninguno)",
        "",
    ]
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(partes))

    # estado_completo.md = todo, incluidos OK y SIN BUILD, con el detalle
    completo = [
        f"# Estado completo — {datetime.now():%d/%m/%Y %H:%M}",
        "",
        f"- Modelo: {res.modelo}",
        f"- KB: {res.kb}",
        f"- Objetos: {res.total}",
        f"- Último build: {gxcheck.resumen_run(res.ultimo_run)}",
        "",
        "| OBJETO | TIPO | FUENTE | GENERADO | ESTADO | ULTIMA SALIDA | MOTIVO |",
        "|---|---|---|---|---|---|---|",
    ]
    for i in res.items:
        celdas = [i.objeto, i.tipo, i.fuente, i.generado, i.estado,
                  i.ultima_salida or "-", i.motivo or "-"]
        completo.append("| " + " | ".join(
            str(c).replace("|", "/").replace("\n", " ") for c in celdas) + " |")
    with open(SNAPSHOT_COMPLETO, "w", encoding="utf-8") as fh:
        fh.write("\n".join(completo) + "\n")

    # contexto que viaja en el prompt: corto (cmd.exe limita a 8191 caracteres)
    nombres_pend = ", ".join(f"{i.objeto} ({i.tipo})"
                             for i in pendientes[:10]) or "ninguno"
    contexto = "\n".join([
        f'[INSTRUCCIÓN: leé PRIMERO el archivo "{SNAPSHOT}" y respondé con eso. '
        "Es el estado de la comparación ya calculado; no explores la KB, ni "
        "el proyecto, ni el código salvo que te lo pida explícitamente.",
        f"Resumen: modelo {res.modelo}, {res.total} objetos, OK {res.ok}, "
        f"pendientes {res.pendientes}, desincronizados {res.desincronizados}, "
        f"contenido distinto {res.difiere}, sin build {res.sin_build}.",
        f"Último build: {gxcheck.resumen_run(res.ultimo_run)}",
        f"Pendientes: {nombres_pend}.]",
    ])

    return SNAPSHOT, contexto


def exportar(categoria: str, items: list, resumen: str) -> str:
    """Escribe HTML (para imprimir) y CSV de una categoria y abre el HTML."""
    carpeta = os.path.join(os.environ.get("TEMP", "."), "gxcheck")
    os.makedirs(carpeta, exist_ok=True)
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(carpeta, f"{categoria.replace(' ', '_')}_{sello}")

    filas = [[i.objeto, i.tipo, i.fuente, i.generado, i.estado, i.motivo]
             for i in items]

    with open(base + ".csv", "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("OBJETO;TIPO;FUENTE;GENERADO;ESTADO;MOTIVO\n")
        for f in filas:
            fh.write(";".join(c.replace(";", ",") for c in f) + "\n")

    cuerpo = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in f) + "</tr>"
        for f in filas)
    pagina = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{html.escape(TITULO)} - {html.escape(categoria)}</title>
<style>
 body {{ font-family: Segoe UI, Arial, sans-serif; background:#fff; color:#1a1a1a;
        margin:24px; }}
 h1 {{ font-size:18px; margin:0 0 4px; }}
 p  {{ font-size:12px; color:#555; margin:0 0 16px; }}
 table {{ border-collapse:collapse; width:100%; font-size:12px; }}
 th {{ background:#f1f3f5; text-align:left; padding:6px 8px;
       border-bottom:2px solid #cfd4da; }}
 td {{ padding:5px 8px; border-bottom:1px solid #e6e8eb;
       vertical-align:top; }}
 tr:nth-child(even) td {{ background:#fafbfc; }}
</style></head>
<body>
<h1>{html.escape(TITULO)} · {html.escape(categoria)} ({len(filas)})</h1>
<p>{html.escape(resumen)}</p>
<table>
<tr><th>OBJETO</th><th>TIPO</th><th>FUENTE</th><th>GENERADO</th>
<th>ESTADO</th><th>MOTIVO</th></tr>
{cuerpo}
</table>
</body></html>"""
    ruta_html = base + ".html"
    with open(ruta_html, "w", encoding="utf-8") as fh:
        fh.write(pagina)
    try:
        os.startfile(ruta_html)  # noqa: S606 - abre el navegador para imprimir
    except OSError:
        pass
    return ruta_html


def cargar_ajustes() -> dict:
    ruta = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "gxcheck.json")
    try:
        with open(ruta, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def guardar_ajustes(datos: dict) -> None:
    ruta = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "gxcheck.json")
    try:
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(datos, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def tabla(items: list, color: str, con_lupa: bool, al_detalle,
          texto=None, seleccionado=None, al_seleccionar=None) -> ft.DataTable:
    """Tabla de resultados. La lupa (solo en Pendientes) abre el detalle.

    Va como primera columna para que se vea siempre, aunque el panel sea
    angosto y las ultimas columnas queden apretadas.
    """
    filas = []
    for i in items:
        color_estado = COLOR_ESTADO.get(i.estado, GRIS)

        def celda(control):
            return ft.DataCell(ft.Container(
                content=control, padding=ft.Padding.symmetric(vertical=2),
                on_click=((lambda _e, n=i.objeto: al_seleccionar(n))
                          if al_seleccionar else None)))

        celdas = []
        if con_lupa:
            celdas.append(ft.DataCell(ft.Container(
                content=ft.Icon(ft.Icons.SEARCH, size=14, color=color_estado),
                padding=ft.Padding.symmetric(vertical=4, horizontal=2),
                tooltip="¿Por qué?",
                data=i,
                on_click=lambda e: al_detalle(e.control.data),
            )))
        celdas += [
            celda(ft.Text(i.objeto, size=12, color=texto, width=170,
                          overflow=ft.TextOverflow.ELLIPSIS,
                          tooltip=i.objeto)),
            celda(ft.Text(i.tipo, size=12, color=texto, width=90,
                          overflow=ft.TextOverflow.ELLIPSIS,
                          tooltip=i.tipo)),
            celda(ft.Text(i.fuente, size=12, color=texto)),
            celda(ft.Text(i.generado, size=12, color=texto)),
            celda(ft.Text(i.estado, size=12, weight=ft.FontWeight.BOLD,
                          color=color_estado)),
        ]

        filas.append(ft.DataRow(
            tooltip=i.motivo,
            selected=(seleccionado is not None and i.objeto == seleccionado),
            cells=celdas,
        ))

    columnas = []
    if con_lupa:
        columnas.append(ft.DataColumn(label=ft.Text("", size=11)))
    columnas += [
        ft.DataColumn(label=ft.Text("OBJETO", weight=ft.FontWeight.BOLD,
                                    color=texto, size=11)),
        ft.DataColumn(label=ft.Text("TIPO", weight=ft.FontWeight.BOLD,
                                    color=texto, size=11)),
        ft.DataColumn(label=ft.Text("FUENTE", weight=ft.FontWeight.BOLD,
                                    color=texto, size=11)),
        ft.DataColumn(label=ft.Text("GENERADO", weight=ft.FontWeight.BOLD,
                                    color=texto, size=11)),
        ft.DataColumn(label=ft.Text("ESTADO", weight=ft.FontWeight.BOLD,
                                    color=texto, size=11)),
    ]
    return ft.DataTable(
        columns=columnas,
        rows=filas,
        column_spacing=6,
        horizontal_margin=4,
        bgcolor=ft.Colors.WHITE,
        data_row_color={ft.ControlState.SELECTED:
                        ft.Colors.PRIMARY_CONTAINER},
        data_row_min_height=28,
        heading_row_height=30,
    )


def imagen_titulo(max_px: int = 96) -> bytes | None:
    """Bytes PNG del logo para mostrarlo al lado del titulo.

    Se achica con Pillow para no mandar 2 MB por el socket en cada update; sin
    Pillow se devuelve el PNG original (funciona igual, pesa mas).
    """
    try:
        with open(ICONO_PNG, "rb") as fh:
            datos = fh.read()
    except OSError:
        return None
    try:
        from PIL import Image
    except ImportError:
        return datos
    try:
        with Image.open(io.BytesIO(datos)) as im:
            im = im.convert("RGBA")
            im.thumbnail((max_px, max_px), Image.LANCZOS)
            salida = io.BytesIO()
            im.save(salida, format="PNG")
            return salida.getvalue()
    except Exception:
        return datos


def recortar_texto(texto: str, vacio: str, maximo: int = 20000) -> str:
    """Texto listo para el panel de código, acotado para no colgar la UI."""
    if not texto:
        return vacio
    if len(texto) <= maximo:
        return texto
    return texto[:maximo] + f"\n\n… (recortado: {len(texto)} caracteres)"


def abrir_archivo(ruta: str) -> None:
    """Abre un archivo con la aplicación asociada, sin romper si falla."""
    if not ruta:
        return
    try:
        os.startfile(ruta)
    except OSError:
        pass


def vista_codigo(titulo: str, ancho: int = 360,
                 alto: int = 120) -> tuple[ft.Column, ft.Text]:
    """Columna con título y un panel de código con scroll (tamaño fijo)."""
    texto = ft.Text("", size=11, selectable=True, font_family="Consolas",
                    color=ft.Colors.GREY_900)
    columna = ft.Column(
        width=ancho,
        spacing=4,
        controls=[
            ft.Text(titulo, size=11, weight=ft.FontWeight.BOLD),
            ft.Container(
                width=ancho,
                height=alto,
                bgcolor=ft.Colors.WHITE,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=6,
                padding=8,
                content=ft.Column(scroll=ft.ScrollMode.AUTO,
                                  controls=[texto]),
            ),
        ],
    )
    return columna, texto


def dialogo_generacion(intro: list[str], permitida: bool,
                       al_regenerar, al_cerrar, filtros=None) -> dict:
    """Ventana de regeneración: progreso + dos columnas (KB / generado).

    filtros: lista de (clave, etiqueta, cantidad) para elegir qué estados se
    regeneran. Devuelve los controles para que el que genera los actualice.
    """
    barra = ft.ProgressBar(value=0, width=700)
    avance = ft.Text("0 de 0", size=11)
    actual = ft.Text("", size=12, weight=ft.FontWeight.BOLD, selectable=True)
    col_izq, texto_izq = vista_codigo("Fuente en la KB (SQL Server)")
    col_der, texto_der = vista_codigo("Generado en disco")
    consola_texto = ft.Text("", size=10, selectable=True,
                            font_family="Consolas", color=ft.Colors.GREY_900)
    consola = ft.Container(
        width=732, height=70, bgcolor=ft.Colors.WHITE,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=6, padding=6,
        content=ft.Column(scroll=ft.ScrollMode.AUTO,
                          auto_scroll=True, controls=[consola_texto]))
    casillas: dict[str, ft.Checkbox] = {}
    fila_filtros: list[ft.Control] = []
    for clave, etiqueta, cantidad in (filtros or []):
        casilla = ft.Checkbox(label=f"{etiqueta} ({cantidad})", value=True)
        casillas[clave] = casilla
        fila_filtros.append(casilla)
    conteo = ft.Text("", size=10, color=ft.Colors.ON_SURFACE_VARIANT)
    boton = ft.FilledButton(
        content=ft.Text("Regenerar" if permitida else "Generar script"),
        icon=ft.Icons.BUILD if permitida else ft.Icons.DESCRIPTION,
        on_click=lambda _e: al_regenerar())

    dialogo = ft.AlertDialog(
        modal=True,
        title=ft.Text("Regenerar pendientes"),
        content=ft.Column(
            width=760,
            spacing=6,
            controls=[
                ft.Column(tight=True, spacing=2,
                          controls=[ft.Text(t, size=11, selectable=True)
                                    for t in intro]),
                *([ft.Row(controls=fila_filtros, spacing=18, wrap=False)]
                  if fila_filtros else []),
                conteo,
                ft.Row(controls=[barra, avance], spacing=10),
                actual,
                ft.Row(controls=[col_izq, col_der], spacing=12),
                ft.Text("Salida de GeneXus:", size=10,
                        weight=ft.FontWeight.BOLD),
                consola,
                ft.Row(
                    alignment=ft.MainAxisAlignment.END,
                    spacing=8,
                    controls=[
                        ft.TextButton("Cerrar",
                                      on_click=lambda _e: al_cerrar()),
                        boton]),
            ]),
    )
    return {"dialogo": dialogo, "barra": barra, "avance": avance,
            "actual": actual, "izq": texto_izq, "der": texto_der,
            "boton": boton, "consola": consola_texto,
            "filtros": casillas, "conteo": conteo,
            "acciones": dialogo.content.controls[-1]}


def main(page: ft.Page) -> None:
    page.title = TITULO
    page.padding = 16
    page.window.icon = icono_ventana()
    page.window.min_width = 1000
    page.window.min_height = 640
    page.window.width = 1280
    page.window.height = 900
    page.window.resizable = True
    page.theme_mode = ft.ThemeMode.LIGHT
    previo = cargar_ajustes()
    elegido = previo.get("color", "Acero")
    semilla = next((s for n, s, _f in PALETA if n == elegido), PALETA[0][1])
    fondo = next((f for n, _s, f in PALETA if n == elegido), PALETA[0][2])
    page.theme = ft.Theme(color_scheme_seed=semilla)
    page.bgcolor = fondo

    def aplicar_color(nombre: str) -> None:
        previo["color"] = nombre
        guardar_ajustes(previo)
        _pintar_tema(nombre)
        page.pop_dialog()
        page.update()

    def abrir_paleta(_e=None):
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Colores de la interface"),
            content=ft.Row(
                wrap=True, spacing=14, run_spacing=14,
                controls=[
                    ft.Column(
                        tight=True, spacing=4,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(
                                width=40, height=40, bgcolor=semilla,
                                border_radius=20,
                                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                                tooltip=n,
                                on_click=lambda _e, nn=n: aplicar_color(nn),
                            ),
                            ft.Text(n, size=10),
                        ],
                    )
                    for n, semilla, _fondo in PALETA
                ],
            ),
            actions=[ft.TextButton("Cerrar",
                                   on_click=lambda _e: page.pop_dialog())],
        ))

    boton_paleta = ft.IconButton(icon=ft.Icons.PALETTE,
                                 tooltip="Cambiar colores",
                                 on_click=abrir_paleta)

    # ------------------------------------------------------------------ entradas
    instancia = ft.Dropdown(
        label="Instancia SQL Server",
        hint_text="Seleccionar o escribir",
        editable=True,
        enable_filter=True,
        expand=3,
        value=previo.get("server") or None,
        options=[],
    )
    base = ft.Dropdown(
        label="Base de datos de la KB",
        hint_text="Seleccionar",
        editable=True,
        enable_filter=True,
        expand=3,
        options=[],
    )
    carpeta = ft.TextField(
        label="Explore Target Environment Directory",
        expand=6,
        on_change=lambda _e: _refresh_dir(),
    )
    diseno = ft.TextField(
        label="Objetos de diseño (opcional, separados por coma)",
        hint_text="ej. MGJoyasSDDSAndroid",
        value=previo.get("design", ""),
        expand=4,
    )
    filtro = ft.TextField(label="Filtro por nombre (opcional)", expand=3)
    con_textos = ft.Checkbox(label="Comparar textos del layout", value=True)
    solo_generados = ft.Checkbox(label="Ocultar objetos sin build", value=True)

    boton = ft.FilledButton(
        content=ft.Text("Iniciar comparación"),
        icon=ft.Icons.PLAY_ARROW,
    )
    boton_generar = ft.OutlinedButton(
        content=ft.Text("Regenerar pendientes"),
        icon=ft.Icons.BUILD,
        disabled=True,
        tooltip="Genera por MSBuild los objetos pendientes, leyendo la KB",
    )
    boton_log = ft.IconButton(
        icon=ft.Icons.DESCRIPTION,
        tooltip="Abrir el log de la última regeneración",
        on_click=lambda _e: abrir_ultimo_log(),
    )
    progreso = ft.ProgressBar(value=0, visible=False, expand=True)
    estado = ft.Text("", size=15, weight=ft.FontWeight.BOLD)
    banner = ft.Text("", size=12, selectable=True)

    # ------------------------------------------------------------------- estado
    datos = {"ok": [], "otros": [], "todos": [], "resumen": "",
             "contexto": "", "chat_primero": True, "esquema": None}

    def color_ok() -> str:
        """Verde para los mensajes normales (mas claro sobre fondo oscuro)."""
        return ft.Colors.GREEN_400 if datos.get("oscuro") else VERDE

    def actualizar_generar() -> None:
        """Habilita el botón solo si hay objetos que se puedan regenerar."""
        cantidad = generar.contar_generables(datos.get("todos", []))
        boton_generar.disabled = cantidad == 0
        boton_generar.content = ft.Text(
            f"Regenerar {cantidad} pendiente{'s' if cantidad != 1 else ''}"
            if cantidad else "Regenerar pendientes")
        boton_generar.tooltip = (
            f"Regenerar {cantidad} objeto(s) por MSBuild (lee la KB de SQL "
            f"Server)" if cantidad else
            "No hay objetos pendientes de regenerar")

    def imprimir(categoria: str) -> None:
        if not datos["todos"]:
            estado.value = "Primero ejecutá la comparación."
            estado.color = ROJO
            page.update()
            return
        if categoria == "Todos":
            items = list(datos["todos"])
        else:
            estados = dict(CATEGORIAS)[categoria]
            items = [i for i in datos["todos"] if i.estado in estados]
        if not items:
            estado.value = f"No hay objetos en {categoria}."
            estado.color = color_ok()
            page.update()
            return
        ruta = exportar(categoria, items,
                        datos["resumen"] or f"{len(items)} objetos")
        estado.value = f"{categoria}: {len(items)} objeto(s) → {ruta}"
        estado.color = VERDE
        page.update()

    opciones = [
        ft.MenuItemButton(content=ft.Text(nombre),
                          on_click=lambda _e, n=nombre: imprimir(n))
        for nombre, _est in CATEGORIAS
    ]
    opciones.append(ft.MenuItemButton(content=ft.Text("Todos los estados"),
                                      on_click=lambda _e: imprimir("Todos")))
    menu = ft.MenuBar(
        style=ft.MenuStyle(bgcolor=ft.Colors.TRANSPARENT,
                           elevation=0,
                           padding=ft.Padding.all(0)),
        controls=[
            ft.SubmenuButton(
                content=ft.Text("Imprimir / exportar"),
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.PRIMARY,
                    color=ft.Colors.ON_PRIMARY,
                    padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                    shape=ft.RoundedRectangleBorder(radius=8),
                ),
                controls=opciones,
            )])

    def ver_detalle(item: gxcheck.Item) -> None:
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text(f"{item.objeto} · {item.estado}"),
            content=ft.Container(
                width=780,
                height=420,
                content=ft.Column(
                    scroll=ft.ScrollMode.AUTO,
                    controls=[ft.Text(item.detalle, size=12, selectable=True)],
                ),
            ),
            actions=[ft.TextButton("Cerrar",
                                   on_click=lambda _e: page.pop_dialog())],
        ))

    def panel(titulo: str, color: str, clave: str) -> dict:
        encabezado = ft.Text(f"{titulo}  (0)", size=15,
                             weight=ft.FontWeight.BOLD, color=color)
        buscar = ft.TextField(
            hint_text="filtrar por objeto, tipo o estado",
            text_size=12, expand=True, height=54,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            on_change=lambda _e: refrescar(clave),
        )
        cuerpo = ft.Column(expand=True, scroll=ft.ScrollMode.ALWAYS,
                           spacing=0,
                           alignment=ft.MainAxisAlignment.START,
                           horizontal_alignment=ft.CrossAxisAlignment.START,
                           controls=[encabezado, buscar])
        marco = ft.Container(
            content=cuerpo,
            expand=1,
            padding=8,
            bgcolor=ft.Colors.WHITE,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
        )
        return {"marco": marco, "titulo": encabezado, "buscar": buscar,
                "cuerpo": cuerpo, "color": color, "clave": clave,
                "base": [encabezado, buscar]}

    izq = panel("OK", VERDE, "ok")
    der = panel("Pendientes", ROJO, "otros")

    def seleccionar(clave: str, objeto: str) -> None:
        actual = datos.get(f"sel_{clave}")
        datos[f"sel_{clave}"] = None if actual == objeto else objeto
        refrescar(clave)

    def refrescar(clave: str) -> None:
        p = izq if clave == "ok" else der
        texto = (p["buscar"].value or "").strip().lower()
        items = datos[clave]
        if texto:
            items = [i for i in items
                     if texto in i.objeto.lower() or texto in i.tipo.lower()
                     or texto in i.estado.lower()]
        controles = list(p["base"])
        if items:
            controles.append(tabla(items, p["color"], con_lupa=(clave != "ok"),
                                   al_detalle=ver_detalle, texto=None,
                                   seleccionado=datos.get(f"sel_{clave}"),
                                   al_seleccionar=lambda n, c=clave:
                                   seleccionar(c, n)))
        p["cuerpo"].controls = controles
        base_txt = "OK" if clave == "ok" else "Pendientes"
        p["titulo"].value = f"{base_txt}  ({len(items)})"
        page.update()

    resumen = ft.Text("", size=13, weight=ft.FontWeight.BOLD)

    # ------------------------------------------------------------- elegir carpeta
    selector = ft.FilePicker()

    async def elegir_carpeta(_e):
        ruta = await selector.get_directory_path(
            dialog_title="Carpeta generada (…\\<modelo>\\web)")
        if ruta:
            carpeta.value = ruta
            _refresh_dir()
            page.update()

    boton_carpeta = ft.IconButton(icon=ft.Icons.FOLDER_OPEN,
                                  tooltip="Elegir carpeta",
                                  on_click=elegir_carpeta)

    def info_target(_e=None):
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Target Environment Directory"),
            content=ft.Container(
                width=560,
                content=ft.Column(
                    tight=True,
                    spacing=10,
                    controls=[
                        ft.Text("Es la carpeta de salida del entorno "
                                "(environment) con el que estás trabajando: la "
                                "que GeneXus usa como destino de la generación.",
                                size=12),
                        ft.Text("Estructura:  …\\<KB>\\<Modelo>\\web", size=12,
                                weight=ft.FontWeight.BOLD),
                        ft.Text("Ahí quedan los .cs, .js y gxmetadata que "
                                "después se publican. De esa ruta el programa "
                                "deduce el modelo (para leer "
                                "ModelEntityOutput) y la raíz de la KB (para "
                                "leer OutputLogs\\Build.log).", size=12),
                        ft.Text("Si cambiás de entorno, de modelo o de KB, "
                                "volvé a elegir la carpeta; si no, compara "
                                "contra archivos de otro lado.", size=12),
                        ft.Text("Este directorio no se inventa: es el que "
                                "muestra GeneXus en Tools → Explore Target "
                                "Environment Directory (menú del IDE). Copiá "
                                "esa ruta tal cual.", size=12),
                        ft.Text(r"Ejemplo: C:\KBs\PROYECTO_NAME\DATA\web",
                                size=11, italic=True,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ],
                ),
            ),
            actions=[ft.TextButton("Cerrar",
                                   on_click=lambda _e: page.pop_dialog())],
        ))

    boton_info = ft.IconButton(icon=ft.Icons.INFO_OUTLINE,
                               icon_size=16,
                               tooltip="¿Qué es el Target Environment?",
                               on_click=info_target)

    # -------------------------------------------------------- cargar instancias
    def cargar_instancias() -> None:
        try:
            nombres = gxcheck.list_instances()
        except Exception:  # noqa: BLE001
            nombres = []
        actual = (instancia.value or "").strip()
        if actual and actual not in nombres:
            nombres.insert(0, actual)
        instancia.options = [ft.DropdownOption(key=n, text=n) for n in nombres]
        if not actual:
            instancia.value = nombres[0] if nombres else None

    # -------------------------------------------------------- cargar bases de datos
    def refrescar_todo(_e=None):
        cargar_instancias()
        cargar_bases()
        page.update()

    def cargar_bases(_e=None):
        estado.value = f"Consultando instancia {instancia.value}…"
        estado.color = color_ok()
        page.update()
        try:
            nombres = gxcheck.list_databases((instancia.value or "").strip())
        except gxcheck.ErrorKB as exc:
            estado.value = f"No se pudo listar bases: {exc}"
            estado.color = ROJO
            page.update()
            return
        base.options = [ft.DropdownOption(key=n, text=n) for n in nombres]
        base.value = None
        estado.value = f"{len(nombres)} bases disponibles."
        estado.color = color_ok()
        page.update()

    # ---------------------------------------------------------------- comparacion
    def ejecutar(_e=None):
        if not carpeta.value:
            estado.value = "Falta la carpeta generada."
            estado.color = (ft.Colors.RED_400 if datos.get("oscuro") else ROJO)
            page.update()
            return
        if not base.value:
            estado.value = "Falta la base de datos de la KB."
            estado.color = (ft.Colors.RED_400 if datos.get("oscuro") else ROJO)
            page.update()
            return

        limpiar_snapshots()
        boton.disabled = True
        progreso.visible = True
        progreso.value = 0
        estado.color = color_ok()
        estado.value = "Iniciando…"
        banner.value = ""
        datos["ok"], datos["otros"] = [], []
        datos["todos"], datos["resumen"] = [], ""
        datos["chat_primero"] = True
        datos["esquema"] = None
        boton_generar.disabled = True
        _actualizar_chat()
        izq["cuerpo"].controls.clear()
        der["cuerpo"].controls.clear()
        izq["titulo"].value = "OK  (0)"
        der["titulo"].value = "Pendientes  (0)"
        resumen.value = ""
        page.update()

        ajustes = gxcheck.Settings(
            server=(instancia.value or "").strip(),
            database=(base.value or "").strip(),
            web_dir=carpeta.value.strip(),
            design=[d.strip() for d in (diseno.value or "").split(",") if d.strip()],
            content=bool(con_textos.value),
            filter=(filtro.value or "").strip(),
            only_built=bool(solo_generados.value),
        )
        previo.update({
            "server": ajustes.server,
            "database": ajustes.database,
            "web_dir": ajustes.web_dir,
            "design": diseno.value or "",
        })
        guardar_ajustes(previo)

        def avisar(texto: str, fraccion: float) -> None:
            progreso.value = fraccion
            estado.value = texto
            estado.color = color_ok()
            page.update()

        def trabajo():
            try:
                res = gxcheck.compare(ajustes, avisar)
            except gxcheck.ErrorKB as exc:
                estado.value = f"ERROR: {exc}"
                estado.color = ROJO
                progreso.visible = False
                boton.disabled = False
                page.update()
                return
            except Exception as exc:  # noqa: BLE001
                estado.value = f"ERROR inesperado: {exc}"
                estado.color = ROJO
                progreso.visible = False
                boton.disabled = False
                page.update()
                return

            datos["ok"] = [i for i in res.items if i.estado == "OK"]
            otros = [i for i in res.items if i.estado != "OK"]
            if ajustes.only_built:
                otros = [i for i in otros if i.estado != "SIN BUILD"]
            datos["otros"] = otros
            datos["todos"] = list(res.items)

            # build: estado del ultimo run + errores reales
            ultimo = res.ultimo_run
            cabecera = gxcheck.resumen_run(ultimo)
            detalle = []
            if res.log_error:
                detalle.append(res.log_error)
            elif ultimo:
                reales = [e for e in ultimo.errores if gxcheck.es_bloqueante(e.texto)]
                if ultimo.resultado == "Failed" and not reales:
                    detalle.append("El build cayó sin error de especificación: "
                                   "el proceso de GeneXus abortó (ver Output del IDE).")
                for e in reales[:3]:
                    detalle.append(f"✖ {e.texto[:200]}")
            banner.value = cabecera + ("\n" + "\n".join(detalle) if detalle else "")
            banner.color = (ROJO if ultimo and ultimo.resultado == "Failed"
                            else None)

            izq["buscar"].value = ""
            der["buscar"].value = ""
            refrescar("ok")
            refrescar("otros")

            resumen.value = (
                f"Modelo {res.modelo}   ·   {res.total} objetos   ·   "
                f"OK {res.ok}   ·   Pendientes {res.pendientes}   ·   "
                f"Desincronizados {res.desincronizados}   ·   "
                f"Contenido distinto {res.difiere}   ·   "
                f"Sin build {res.sin_build}   ·   {res.segundos:.1f} s"
            )
            datos["resumen"] = resumen.value
            datos["contexto"] = guardar_snapshot(res)[1]
            actualizar_generar()
            _refresh_dir()
            estado.value = ("Todo coincide con lo escrito en la KB."
                            if not datos["otros"] else
                            f"{len(datos['otros'])} objeto(s) requieren build. "
                            f"Usá la lupa para ver por qué.")
            if datos.get("oscuro"):
                estado.color = (ft.Colors.GREEN_400 if not datos["otros"]
                                else ft.Colors.RED_400)
            else:
                estado.color = VERDE if not datos["otros"] else ROJO
            progreso.value = 1
            progreso.visible = False
            boton.disabled = False
            _actualizar_chat()
            page.update()

        page.run_thread(trabajo)

    boton.on_click = ejecutar
    page.services.append(selector)

    # ------------------------------------------------- regenerar (MSBuild)
    def regenerar(_e=None) -> None:
        """Ventana de regeneración con selección por estado."""
        estados = ("PENDIENTE", "DESINCRONIZADO", "DIFIERE")
        etiquetas = {"PENDIENTE": "Pendientes",
                     "DESINCRONIZADO": "Desincronizados",
                     "DIFIERE": "Con diferencias"}
        items = [i for i in datos.get("todos", []) if i.estado in estados]
        if not items:
            estado.value = ("No hay objetos pendientes, desincronizados ni con "
                            "diferencias.")
            estado.color = color_ok()
            page.update()
            return
        try:
            plan = generar.planificar(carpeta.value.strip(), items)
        except generar.ErrorGeneracion as exc:
            estado.value = f"ERROR: {exc}"
            estado.color = ROJO
            page.update()
            return

        permitida = generar.kb_permitida(plan.kb)
        ambiente = gxcheck.ambiente_modelo(
            (instancia.value or "").strip(), (base.value or "").strip(),
            plan.ambiente)
        modelo = gxcheck.modelo_id(
            (instancia.value or "").strip(), (base.value or "").strip(),
            plan.ambiente)
        if ambiente:
            plan.ambiente = ambiente
        plan.modelo = modelo

        intro = [
            f"KB: {plan.kb}   ·   Ambiente: {plan.ambiente}",
        ]
        if permitida:
            intro.append(
                "Se fuerza la regeneración de los objetos elegidos · escribe en "
                "la KB y en el ambiente · no compila")
            if generar.ide_abierto():
                intro.append("ATENCIÓN: GeneXus está abierto; conviene cerrarlo.")
        else:
            intro.append(
                f"MODO SEGURO: KB no habilitada, solo se escribe el script "
                f"({generar.VARIABLE_PERMITIDAS}).")

        cantidades = {e: sum(1 for i in items if i.estado == e) for e in estados}
        run = {"nombres": [], "por": {}}
        controles: dict = {}
        cache: dict[tuple[int, int], str] = {}
        artefactos = {"a": None}

        def mostrar(item, pos: int) -> None:
            """Actualiza las dos columnas y el avance para un objeto."""
            nombres = run["nombres"]
            controles["actual"].value = f"{item.objeto}   ({item.tipo})"
            controles["avance"].value = f"{pos} de {len(nombres)}"
            controles["barra"].value = pos / len(nombres) if nombres else 0
            clave = (item.type_id, item.entity_id)
            if clave not in cache:
                controles["izq"].value = "Leyendo la fuente de la KB…"
                page.update()
                cache[clave] = gxcheck.fuente_objeto(
                    (instancia.value or "").strip(),
                    (base.value or "").strip(),
                    item.type_id, item.entity_id)
            controles["izq"].value = recortar_texto(
                cache[clave], "(La KB no devolvió la fuente de este objeto.)")
            controles["der"].value = "Leyendo el generado…"
            page.update()
            if artefactos["a"] is None:
                artefactos["a"] = gxcheck.Artefactos(carpeta.value.strip())
            rutas = artefactos["a"].de_objeto(item.objeto, item.type_id)
            elegido = (next((r for r in rutas if r.lower().endswith(".cs")), "")
                       or next((r for r in rutas
                                if r.lower().endswith(".js")), "")
                       or (rutas[0] if rutas else ""))
            generado = gxcheck.read_text(elegido) if elegido else ""
            controles["der"].value = recortar_texto(
                generado, "(Todavía no hay archivo generado para este objeto.)")
            page.update()

        def trabajo() -> None:
            seleccion = [i for i in items
                         if controles["filtros"][i.estado].value]
            if not seleccion:
                controles["actual"].value = "No hay estados seleccionados."
                page.update()
                return

            plan_run = generar.planificar(carpeta.value.strip(), seleccion)
            run["nombres"] = plan_run.objetos
            run["por"] = {i.objeto.lower(): i for i in seleccion}

            controles["boton"].disabled = True
            boton_generar.disabled = True
            estado.value = "Regenerando…"
            estado.color = color_ok()
            page.update()
            visto = {"i": -1}
            salida: list[str] = []

            def avisar(texto: str) -> None:
                salida.append(texto)
                del salida[:-14]
                controles["consola"].value = "\n".join(salida)
                plano = texto.lower()
                for pos, nombre in enumerate(run["nombres"]):
                    if nombre.lower() in plano:
                        if pos != visto["i"]:
                            visto["i"] = pos
                            try:
                                mostrar(run["por"][nombre.lower()], pos + 1)
                            except Exception as exc:  # noqa: BLE001
                                controles["actual"].value = (
                                    f"No se pudo actualizar la vista: {exc}")
                                page.update()
                        break
                estado.value = f"Regenerando… {texto}"

            if run["nombres"]:
                try:
                    mostrar(run["por"][run["nombres"][0].lower()], 1)
                except Exception as exc:  # noqa: BLE001
                    controles["actual"].value = (
                        f"No se pudo leer la fuente: {exc}")
                    page.update()

            try:
                res = generar.generar(carpeta.value.strip(), seleccion, avisar,
                                      simular=not permitida, ambiente=ambiente,
                                      modelo=modelo)
            except generar.ErrorGeneracion as exc:
                controles["actual"].value = f"ERROR: {exc}"
                controles["boton"].disabled = False
                boton_generar.disabled = False
                estado.value = f"ERROR: {exc}"
                estado.color = ROJO
                page.update()
                return
            except Exception as exc:  # noqa: BLE001
                controles["actual"].value = f"ERROR inesperado: {exc}"
                controles["boton"].disabled = False
                boton_generar.disabled = False
                estado.value = f"ERROR inesperado: {exc}"
                estado.color = ROJO
                page.update()
                return

            boton_generar.disabled = False
            actualizar_generar()
            controles["barra"].value = 1
            controles["avance"].value = (f"{len(run['nombres'])} de "
                                         f"{len(run['nombres'])}")

            if res.simulado:
                controles["actual"].value = "Script generado (no se ejecutó nada)"
                estado.value = "Script generado; la KB no se tocó."
                estado.color = color_ok()
                controles["acciones"].controls = [
                    ft.TextButton("Abrir script",
                                  on_click=lambda _e: abrir_archivo(res.script)),
                    ft.TextButton("Cerrar",
                                  on_click=lambda _e: page.pop_dialog())]
                page.update()
                return

            if res.ok:
                controles["actual"].value = "Listo: comparando de nuevo…"
                estado.value = (f"{len(res.objetos)} objeto(s) en "
                                f"{res.segundos:.0f} s. Comparando de nuevo…")
                estado.color = VERDE
                page.update()
                page.pop_dialog()
                ejecutar(None)
                return

            controles["actual"].value = "Terminó con errores"
            estado.value = "Falló la regeneración. Mirá el log."
            estado.color = ROJO
            acciones = []
            if res.log:
                acciones.append(ft.TextButton(
                    "Abrir log",
                    on_click=lambda _e: abrir_archivo(res.log)))
            acciones.append(ft.TextButton(
                "Cerrar", on_click=lambda _e: page.pop_dialog()))
            controles["acciones"].controls = acciones
            page.update()

        def lanzar() -> None:
            """Envuelve el trabajo para que un error se vea, no se pierda."""
            try:
                trabajo()
            except Exception as exc:  # noqa: BLE001
                estado.value = f"ERROR al regenerar: {exc}"
                estado.color = ROJO
                if controles.get("actual"):
                    controles["actual"].value = f"ERROR: {exc}"
                if controles.get("boton"):
                    controles["boton"].disabled = False
                boton_generar.disabled = False
                page.update()

        def abrir_dialogo() -> None:
            lineas = list(intro)
            esquema = datos.get("esquema") or {}
            if esquema.get("error"):
                lineas.append(f"No se pudo verificar la base: {esquema['error']}")
            elif not esquema:
                lineas.append("Base: no verificada.")
            elif esquema.get("faltan_tablas") or esquema.get("faltan_atributos"):
                cuantas_t = len(esquema.get("faltan_tablas") or [])
                cuantos_a = len(esquema.get("faltan_atributos") or [])
                lineas.append(
                    f"ATENCIÓN: la base parece desactualizada respecto de la "
                    f"KB ({cuantas_t} tabla(s) y {cuantos_a} atributo(s) "
                    f"faltantes). Probablemente haga falta reorganizar.")
                for tabla, atributo in (esquema.get("faltan_atributos") or [])[:3]:
                    lineas.append(f"   · {tabla}.{atributo}")
                for tabla in (esquema.get("faltan_tablas") or [])[:3]:
                    lineas.append(f"   · falta la tabla {tabla}")
            else:
                lineas.append(
                    f"Base sincronizada con la KB "
                    f"({esquema.get('tablas_kb', 0)} tablas comparadas).")

            controles.update(dialogo_generacion(
                lineas, permitida,
                al_regenerar=lambda: page.run_thread(lanzar),
                al_cerrar=lambda: page.pop_dialog(),
                filtros=[(e, etiquetas[e], cantidades[e]) for e in estados]))

            def recalcular(_ev=None) -> None:
                elegidos = [i for i in items
                            if controles["filtros"][i.estado].value]
                controles["conteo"].value = (
                    f"Se regenerarán {len(elegidos)} objeto(s).")
                controles["boton"].disabled = not elegidos
                page.update()

            for casilla in controles["filtros"].values():
                casilla.on_change = recalcular
            recalcular()
            page.show_dialog(controles["dialogo"])

        if datos.get("esquema") is None:
            estado.value = ("Verificando el esquema de la base… "
                            "(puede tardar)")
            estado.color = color_ok()
            page.update()

            def verificar() -> None:
                try:
                    datos["esquema"] = gxcheck.verificar_esquema(plan.kb, modelo)
                except Exception as exc:  # noqa: BLE001
                    datos["esquema"] = {"error": str(exc)}
                estado.value = ""
                page.update()
                abrir_dialogo()

            page.run_thread(verificar)
            return

        abrir_dialogo()

    boton_generar.on_click = regenerar

    def abrir_ultimo_log(_e=None) -> None:
        ruta = generar.ultimo_log()
        if not ruta:
            estado.value = "Todavía no hay ningún log de regeneración."
            estado.color = ROJO
            page.update()
            return
        abrir_archivo(ruta)
        estado.value = f"Log abierto: {ruta}"
        estado.color = color_ok()
        page.update()


    boton_bases = ft.IconButton(icon=ft.Icons.REFRESH,
                                tooltip="Refrescar instancias y bases",
                                on_click=refrescar_todo)

    # ------------------------------------------------------- consola / opencode
    conversacion = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO,
                             auto_scroll=True, spacing=6, controls=[])
    dir_actual = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
    mapa_modelos: dict[str, str] = {}
    proceso_actual: dict = {}
    burbuja = {"texto": None}

    def _refresh_dir() -> None:
        d = (carpeta.value or "").strip()
        dir_actual.value = f"Directorio: {d}" if d else \
            "Directorio: (elegí el Target Environment Directory)"

    def _recortar() -> None:
        while len(conversacion.controls) > 60:
            conversacion.controls.pop(0)

    def _agregar(texto: str, color=None) -> None:
        conversacion.controls.append(ft.Row(controls=[
            ft.Icon(ft.Icons.INFO_OUTLINE, size=14, color=ft.Colors.BLUE_GREY_400),
            ft.Text(texto, size=11, selectable=True, expand=True,
                    color=color or ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.START,
            expand=True))
        _recortar()
        page.update()

    def _iniciar_intercambio(pregunta: str) -> None:
        conversacion.controls.append(ft.Row(controls=[
            ft.Icon(ft.Icons.PERSON, size=14, color=ft.Colors.BLUE_GREY_600),
            ft.Text(pregunta, size=11, weight=ft.FontWeight.BOLD,
                    selectable=True, expand=True),
        ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.START,
            expand=True))
        respuesta = ft.Text("", size=11, selectable=True, expand=True,
                            color=ft.Colors.ON_SURFACE)
        conversacion.controls.append(ft.Row(controls=[
            ft.Icon(ft.Icons.COMPUTER, size=14, color=ft.Colors.GREEN_700),
            respuesta,
        ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.START,
            expand=True))
        burbuja["texto"] = respuesta
        _recortar()
        page.update()

    def _pintar(datos: bytes, final: bool = False) -> None:
        cuerpo = limpiar_salida(decodificar(datos))
        destino = burbuja.get("texto")
        if destino is None:
            return
        if final and not cuerpo:
            destino.value = "(sin respuesta)"
            destino.color = ROJO
        else:
            destino.value = cuerpo + ("" if final else "▌")
            destino.color = ft.Colors.ON_SURFACE
        page.update()

    def _ejecutar_stream(comando, pregunta: str) -> None:
        cwd = (carpeta.value or "").strip()
        if not os.path.isdir(cwd):
            _agregar("Elegí primero el Target Environment Directory.", ROJO)
            return
        _iniciar_intercambio(pregunta)
        try:
            proceso = subprocess.Popen(
                comando, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            _agregar(f"No se pudo ejecutar: {exc}", ROJO)
            return

        proceso_actual["p"] = proceso
        datos = b""
        ultimo = 0.0
        try:
            while True:
                trozo = proceso.stdout.read(256)
                if not trozo:
                    break
                datos += trozo
                ahora = time.time()
                if ahora - ultimo >= 0.07:
                    ultimo = ahora
                    _pintar(datos)
        finally:
            try:
                proceso.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proceso.kill()
            proceso_actual["p"] = None

        _pintar(datos, final=True)

    def _en_hilo(comando, pregunta: str, primero: bool = False) -> None:
        boton_opencode.disabled = True
        boton_detener.visible = True
        girando.visible = True
        page.update()
        try:
            _ejecutar_stream(comando, pregunta)
            if primero:
                datos["chat_primero"] = False
        finally:
            girando.visible = False
            boton_detener.visible = False
            _actualizar_chat()
            page.update()

    def detener(_e=None) -> None:
        proceso = proceso_actual.get("p")
        if proceso and proceso.poll() is None:
            try:
                proceso.terminate()
            except OSError:
                pass

    def cargar_modelos(_e=None) -> None:
        def tarea() -> None:
            exe = shutil.which("opencode")
            if not exe:
                _agregar("No encuentro 'opencode' en el PATH.", ROJO)
                return
            cwd = (carpeta.value or "").strip()
            cwd = cwd if os.path.isdir(cwd) else os.path.dirname(
                os.path.abspath(__file__))
            try:
                p = subprocess.run(["cmd", "/c", exe, "models"], cwd=cwd,
                                   capture_output=True, timeout=180)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _agregar(f"No se pudieron listar los modelos: {exc}", ROJO)
                return
            texto = decodificar((p.stdout or b"") + (p.stderr or b""))
            ids = [l.strip() for l in texto.splitlines()
                   if "/" in l and " " not in l.strip()]

            def etiqueta(m: str) -> str:
                proveedor, _, nombre = m.partition("/")
                return f"{nombre}  ({proveedor})"

            actual = mapa_modelos.get((modelo.value or "").strip(),
                                      (modelo.value or "").strip())
            mapa_modelos.clear()
            modelo.options = []
            for m in ids:
                mapa_modelos[m] = m
                mapa_modelos[etiqueta(m)] = m
                modelo.options.append(ft.DropdownOption(key=m, text=etiqueta(m)))
            if actual and actual not in ids:
                modelo.options.insert(0, ft.DropdownOption(
                    key=actual, text=etiqueta(actual)))
                mapa_modelos[etiqueta(actual)] = actual
            if actual:
                modelo.value = actual
            page.update()

        page.run_thread(tarea)

    def cargar_agentes(_e=None) -> None:
        nombres = listar_agentes((carpeta.value or "").strip())
        actual = (agente.value or "").strip()
        agente.options = [ft.DropdownOption(key=n, text=n) for n in nombres]
        if actual and actual not in nombres:
            agente.options.insert(0, ft.DropdownOption(key=actual, text=actual))
        agente.value = actual or None
        page.update()

    def correr_opencode(_e=None) -> None:
        if not datos.get("todos"):
            _agregar("Primero ejecutá la comparación.", ROJO)
            return
        consulta_txt = (consulta.value or "").strip()
        if not consulta_txt:
            _agregar("Escribí una consulta.", ROJO)
            return
        exe = shutil.which("opencode")
        if not exe:
            _agregar("No encuentro 'opencode' en el PATH.", ROJO)
            return
        elegido = mapa_modelos.get((modelo.value or "").strip(),
                                   (modelo.value or "").strip())
        agente_elegido = (agente.value or "").strip()
        previo["modelo"] = elegido
        previo["agente"] = agente_elegido
        guardar_ajustes(previo)

        primero = bool(datos.get("chat_primero", True))
        contexto = (datos.get("contexto") or "") if primero else ""

        argumentos = [exe, "run"]
        if elegido:
            argumentos += ["--model", elegido]
        if agente_elegido:
            argumentos += ["--agent", agente_elegido]
        if not primero:
            argumentos.append("--continue")
        texto = (f"{contexto}\n\nPregunta: {consulta_txt}"
                 if contexto else consulta_txt)
        argumentos.append(texto)
        consulta.value = ""
        page.update()
        page.run_thread(lambda: _en_hilo(["cmd", "/c", *argumentos],
                                         consulta_txt, primero))

    def abrir_ventana_aparte(_e=None) -> None:
        cwd = (carpeta.value or "").strip()
        if not os.path.isdir(cwd):
            _agregar("Elegí primero el Target Environment Directory.", ROJO)
            return
        try:
            subprocess.Popen(["cmd", "/k", "opencode"], cwd=cwd,
                             creationflags=getattr(subprocess,
                                                    "CREATE_NEW_CONSOLE", 0))
            _agregar(f"Consola aparte abierta en {cwd}")
        except OSError as exc:
            _agregar(f"No se pudo abrir la consola: {exc}", ROJO)

    modelo = ft.Dropdown(label="Modelo LLM",
                         hint_text="(el que use opencode por defecto)",
                         editable=True, enable_filter=True, enable_search=True,
                         dense=True, expand=True,
                         value=previo.get("modelo") or None,
                         options=[])
    agente = ft.Dropdown(label="Agente", hint_text="(agente por defecto)",
                         editable=True, enable_filter=True, enable_search=True,
                         dense=True, expand=True,
                         value=previo.get("agente") or "genexus",
                         options=[])
    consulta = ft.TextField(label="Chat",
                            hint_text="ej. por qué falla el build de Clientes",
                            expand=True, dense=True, on_submit=correr_opencode)
    girando = ft.ProgressRing(width=16, height=16, stroke_width=2,
                              visible=False)
    boton_detener = ft.IconButton(icon=ft.Icons.STOP_CIRCLE, icon_size=18,
                                  tooltip="Detener", visible=False,
                                  on_click=lambda _e: detener())
    boton_opencode = ft.FilledButton(content=ft.Text("Enviar"),
                                     icon=ft.Icons.SEND,
                                     on_click=correr_opencode)
    boton_modelos = ft.IconButton(icon=ft.Icons.REFRESH, icon_size=16,
                                  tooltip="Listar modelos de opencode",
                                  on_click=lambda _e: cargar_modelos())
    boton_agentes = ft.IconButton(icon=ft.Icons.REFRESH, icon_size=16,
                                  tooltip="Listar agentes de opencode",
                                  on_click=lambda _e: cargar_agentes())

    def _actualizar_chat() -> None:
        listo = bool(datos.get("todos"))
        consulta.disabled = not listo
        boton_opencode.disabled = not listo
        consulta.hint_text = ("ej. por qué falla el build de Clientes" if listo
                              else "Iniciá la comparación para chatear")

    panel_open = ft.Container(
        width=390,
        bgcolor=ft.Colors.WHITE,
        padding=12,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        content=ft.Column(
            spacing=8,
            expand=True,
            controls=[
                ft.Row(controls=[
                    ft.Icon(ft.Icons.TERMINAL, size=18),
                    ft.Text("OpenCode", size=15, weight=ft.FontWeight.BOLD),
                ], spacing=6),
                dir_actual,
                ft.Row(controls=[modelo, boton_modelos], spacing=6),
                ft.Row(controls=[agente, boton_agentes], spacing=6),
                ft.Divider(height=1),
                conversacion,
                ft.Divider(height=1),
                ft.Row(controls=[girando, consulta, boton_opencode,
                                 boton_detener], spacing=6),
                ft.Row(controls=[
                    ft.TextButton(
                        content=ft.Text("Abrir consola aparte (TUI)", size=11),
                        icon=ft.Icons.OPEN_IN_NEW,
                        on_click=abrir_ventana_aparte,
                    )], alignment=ft.MainAxisAlignment.CENTER),
            ],
        ),
    )

    marco_banner = ft.Container(
        content=banner,
        padding=8,
        bgcolor=ft.Colors.WHITE,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=6,
    )
    titulo_app = ft.Text(TITULO, size=24, weight=ft.FontWeight.BOLD)
    _logo = imagen_titulo()
    marca = (ft.Image(src=_logo, width=40, height=40, fit=ft.BoxFit.CONTAIN,
                      border_radius=8) if _logo else None)
    ayuda = ft.Text("Elegí la instancia de SQL Server y la carpeta generada; "
                    "se compara la KB contra el código en disco y se explica "
                    "el último build.", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT)

    def _pintar_tema(nombre: str) -> None:
        for n, semilla, fondo in PALETA:
            if n != nombre:
                continue
            oscuro = n in OSCUROS
            datos["oscuro"] = oscuro
            page.theme_mode = ft.ThemeMode.LIGHT
            page.theme = ft.Theme(color_scheme_seed=semilla)
            page.bgcolor = fondo
            marco_banner.bgcolor = (fondo if oscuro
                                    else ft.Colors.WHITE)
            marco_banner.border = ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)
            fondo_panel = ft.Colors.WHITE
            for marco in (izq["marco"], der["marco"], panel_open):
                marco.bgcolor = fondo_panel
            izq["titulo"].color = VERDE
            der["titulo"].color = ROJO
            izq["color"] = izq["titulo"].color
            der["color"] = der["titulo"].color
            for p in (izq, der):
                p["buscar"].color = None
                p["buscar"].hint_style = ft.TextStyle(color=ft.Colors.GREY_600)
            dir_actual.color = ft.Colors.ON_SURFACE_VARIANT
            color_txt = ft.Colors.WHITE if oscuro else None
            for control in (instancia, base, carpeta, diseno, filtro):
                control.color = color_txt
                control.label_style = ft.TextStyle(color=color_txt)
                control.hint_style = ft.TextStyle(
                    color=ft.Colors.GREY_400 if oscuro else ft.Colors.GREY_600)
            for chk in (con_textos, solo_generados):
                chk.label_style = ft.TextStyle(color=color_txt)
            try:
                for sub in menu.controls:
                    sub.content.color = color_txt
            except Exception:
                pass
            banner.color = ft.Colors.WHITE if oscuro else None
            for control in (modelo, agente, consulta):
                control.color = None
                control.label_style = None
            refrescar("ok")
            refrescar("otros")
            titulo_app.color = ft.Colors.WHITE if oscuro else None
            ayuda.color = (ft.Colors.GREY_400 if oscuro
                           else ft.Colors.ON_SURFACE_VARIANT)
            resumen.color = ft.Colors.WHITE if oscuro else None
            estado.color = (ft.Colors.GREEN_400 if oscuro else VERDE)
            return

    page.add(
        ft.Row(
            controls=[
                ft.Column(
                    expand=True,
                    spacing=10,
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Row(
                                    controls=([marca] if marca else []) +
                                             [titulo_app],
                                    spacing=10,
                                    vertical_alignment=(
                                        ft.CrossAxisAlignment.CENTER),
                                ),
                                ft.Row(controls=[menu, boton_paleta], spacing=4),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Divider(height=1),
                        ft.Container(height=14),
                        ayuda,
                        ft.Row(controls=[instancia, boton_bases, base],
                               spacing=8),
                        ft.Row(controls=[carpeta, boton_carpeta, boton_info],
                               spacing=8),
                        ft.Row(controls=[diseno, filtro], spacing=8),
                        ft.Row(controls=[con_textos, solo_generados, boton,
                                         boton_generar, boton_log, progreso],
                               spacing=12,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        estado,
                        marco_banner,
                        resumen,
                        ft.Row(controls=[izq["marco"], der["marco"]],
                               expand=True, spacing=12,
                               vertical_alignment=ft.CrossAxisAlignment.STRETCH),
                    ],
                ),
                panel_open,
            ],
            expand=True,
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )

    _pintar_tema(elegido)
    cargar_instancias()
    cargar_bases()
    cargar_modelos()
    cargar_agentes()
    _actualizar_chat()


if __name__ == "__main__":
    ft.run(main)
