#!/usr/bin/env python3
r"""
Regenera objetos pendientes de GeneXus sin abrir el IDE.

Se apoya en las tareas MSBuild que trae GeneXus 18 (OpenKnowledgeBase,
SetActiveEnvironment, SpecifyOneOnly, GenerateOnly) y trabaja sobre la misma KB
de SQL Server que analiza gxcheck: genera exactamente lo que quedo guardado.

Se ejecuta en dos pasos, porque no todos los estados necesitan lo mismo:

    PENDIENTE / DIFIERE   -> SpecifyOneOnly ForceRebuild="false"
    DESINCRONIZADO        -> SpecifyOneOnly ForceRebuild="true"
                             (GeneXus cree que esta al dia y lo saltearia)

Despues, un unico GenerateOnly genera lo especificado. No compila: deja el
.cs/.js nuevo en la carpeta del ambiente.

Uso desde consola:

    python generar.py --web C:\KBs\MGJoyas\Data009\web --objetos AltaPedidosWW,Clientes
    python generar.py --web ... --objetos ... --forzar
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime

# Estados que se pueden regenerar desde la ventana: el usuario elige con
# casillas cuáles. SIN BUILD no genera codigo y "?" no tiene fuente.
GENERABLES = {"PENDIENTE", "DESINCRONIZADO", "DIFIERE"}

# Estados donde GeneXus ya registro una salida posterior al archivo: hay que
# forzar la especificacion o el build los saltea.
FORZAR = {"DESINCRONIZADO"}

RUTAS_GX = (
    r"C:\Program Files (x86)\GeneXus",
    r"C:\Program Files\GeneXus",
)

MSBUILD_X86 = r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\MSBuild.exe"

VARIABLES_GX = ("GXCHECK_GX", "GX_PROGRAM_DIR", "GX_INSTALL", "GX_HOME")

# Solo se ejecuta la generacion sobre KBs declaradas aca (rutas separadas por
# ";"). Si la variable no esta, no se ejecuta nada: solo se escribe el script.
# Asi es imposible tocar una KB real por accidente.
VARIABLE_PERMITIDAS = "GXCHECK_KB_PERMITIDAS"


class ErrorGeneracion(Exception):
    """Falta GeneXus, MSBuild, la carpeta o los objetos."""


@dataclass
class Plan:
    """Que se va a generar y sobre que KB/ambiente."""

    kb: str
    ambiente: str
    normales: list[str] = field(default_factory=list)
    forzados: list[str] = field(default_factory=list)
    modelo: int = 0

    @property
    def objetos(self) -> list[str]:
        return self.normales + self.forzados


@dataclass
class Resultado:
    ok: bool
    codigo: int
    objetos: list[str]
    log: str = ""
    script: str = ""
    salida: str = ""
    error: str = ""
    segundos: float = 0.0
    simulado: bool = False


def kb_permitida(kb: str) -> bool:
    """Restricción OPCIONAL de KBs donde se permite generar.

    Sin GXCHECK_KB_PERMITIDAS devuelve True: el botón regenera sobre la KB que
    esté cargada en el formulario (el diálogo muestra cuál). Si la variable está
    definida, solo se permite generar sobre esas rutas:

        setx GXCHECK_KB_PERMITIDAS "C:\\KBs\\KB_PRUEBAS"
    """
    lista = os.environ.get(VARIABLE_PERMITIDAS, "")
    permitidas = [os.path.normcase(os.path.abspath(p.strip()))
                  for p in lista.split(";") if p.strip()]
    if not permitidas:
        return True
    actual = os.path.normcase(os.path.abspath(kb))
    for ruta in permitidas:
        if actual == ruta or actual.startswith(ruta + os.sep):
            return True
    return False


def _xml(texto: str) -> str:
    """Escapa para atributo XML."""
    return (texto.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _decodificar(datos: bytes) -> str:
    """MSBuild escribe en la consola con la codificacion OEM de Windows."""
    for codificacion in ("utf-8", "cp1252", "cp850"):
        try:
            return datos.decode(codificacion)
        except UnicodeDecodeError:
            continue
    return datos.decode("latin-1", "replace")


def buscar_genexus() -> str:
    """Carpeta de GeneXus instalada que tenga las tareas MSBuild."""
    for variable in VARIABLES_GX:
        ruta = os.environ.get(variable)
        if ruta and os.path.isfile(
                os.path.join(ruta, "Genexus.Tasks.targets")):
            return ruta

    candidatas: list[str] = []
    for base in RUTAS_GX:
        candidatas.extend(glob.glob(os.path.join(base, "GeneXus*")))

    def version(ruta: str) -> tuple[int, int]:
        nombre = os.path.basename(ruta)
        numeros = re.findall(r"\d+", nombre)
        if len(numeros) >= 2:
            return (int(numeros[0]), int(numeros[1]))
        return (int(numeros[0]) if numeros else 0, 0)

    for ruta in sorted(candidatas, key=version, reverse=True):
        if os.path.isfile(os.path.join(ruta, "Genexus.Tasks.targets")):
            return ruta
    raise ErrorGeneracion(
        "No encuentro la instalacion de GeneXus (Genexus.Tasks.targets). "
        "Definí la variable GXCHECK_GX con la carpeta de GeneXus.")


def buscar_msbuild() -> str:
    """MSBuild de .NET Framework 4.0 de 32 bits (el de x64 no carga gxio)."""
    if os.path.isfile(MSBUILD_X86):
        return MSBUILD_X86
    ruta = shutil.which("msbuild")
    if ruta:
        return ruta
    raise ErrorGeneracion(
        rf"No encuentro MSBuild.exe en {MSBUILD_X86}.")


def ide_abierto() -> bool:
    """True si hay un GeneXus.exe corriendo (la KB puede estar tomada)."""
    try:
        salida = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq GeneXus.exe", "/NH"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return b"GeneXus.exe" in salida


def contar_generables(items) -> int:
    """Cuántos objetos de la lista se pueden regenerar."""
    return sum(1 for i in items if getattr(i, "estado", "") in GENERABLES)


def planificar(web_dir: str, items) -> Plan:
    """Arma el plan a partir de la carpeta web y los objetos de gxcheck."""
    web = (web_dir or "").rstrip("\\/")
    if not web:
        raise ErrorGeneracion("Falta la carpeta generada (…\\<Modelo>\\web).")
    modelo_dir = os.path.dirname(web)
    plan = Plan(kb=os.path.dirname(modelo_dir),
                ambiente=os.path.basename(modelo_dir))
    vistos: set[str] = set()
    for item in items:
        estado = getattr(item, "estado", "")
        nombre = (getattr(item, "objeto", "") or "").strip()
        if estado not in GENERABLES or not nombre:
            continue
        clave = nombre.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        (plan.forzados if estado in FORZAR else plan.normales).append(nombre)
    return plan


def armar_script(plan: Plan, tareas: str) -> str:
    """Arma el .msbuild con las tareas de GeneXus.

    Se identifica el modelo por id (TargetModelId / Model), no por nombre: en
    una KB puede haber varios ambientes que se llaman igual y GeneXus elegía
    otro. Y SpecifyOneOnly recibe UN objeto por tarea: no acepta listas.
    """
    destino = (f' Model="{plan.modelo}"' if plan.modelo
               else f' EnvironmentName="{_xml(plan.ambiente)}"')
    lineas = [
        '<Project DefaultTargets="Generar" '
        'xmlns="http://schemas.microsoft.com/developer/msbuild/2003">',
        f'  <Import Project="{_xml(tareas)}" />',
        "",
        '  <Target Name="Generar">',
        f'    <OpenKnowledgeBase Directory="{_xml(plan.kb)}"'
        + (f' TargetModelId="{plan.modelo}"' if plan.modelo else "")
        + " />",
    ]
    if not plan.modelo:
        lineas.append(
            f'    <SetActiveEnvironment '
            f'EnvironmentName="{_xml(plan.ambiente)}" />')
    for nombre in plan.normales:
        lineas.append(
            f'    <SpecifyOneOnly ObjectNames="{_xml(nombre)}"{destino}'
            ' ForceRebuild="true" />')
    for nombre in plan.forzados:
        lineas.append(
            f'    <SpecifyOneOnly ObjectNames="{_xml(nombre)}"{destino}'
            ' ForceRebuild="true" />')
    lineas += [
        f'    <GenerateOnly{destino} />',
        "  </Target>",
        "</Project>",
        "",
    ]
    return "\n".join(lineas)


def validar_kb(kb: str) -> str:
    """Revisa que la carpeta sea una KB que GeneXus pueda abrir.

    Devuelve "" si parece válida, o el motivo del rechazo. Una copia de la
    carpeta no sirve: `knowledgebase.connection` sigue apuntando al original y
    GeneXus responde InvalidDirectory.
    """
    conexion = os.path.join(kb, "knowledgebase.connection")
    if not os.path.isfile(conexion):
        return f"No encuentro {conexion}."
    try:
        with open(conexion, encoding="utf-8", errors="replace") as fh:
            texto = fh.read()
    except OSError as exc:
        return f"No se pudo leer {conexion}: {exc}"
    coincidencia = re.search(r"<Directory>(.*?)</Directory>", texto, re.I)
    if coincidencia:
        declarada = coincidencia.group(1).strip().rstrip("\\/")
        actual = os.path.normcase(os.path.abspath(kb))
        if os.path.normcase(os.path.abspath(declarada)) != actual:
            return (
                "La carpeta es una copia de la KB: su knowledgebase.connection "
                f"declara\n  {declarada}\n"
                f"pero está en\n  {kb}\n"
                "GeneXus la rechaza (InvalidDirectory). Abrila desde su carpeta "
                "original o creá una KB de prueba propia.")
    return ""


def generar(web_dir: str, items, avisar=None, timeout: int = 3600,
            simular: bool = False, ambiente: str = "",
            modelo: int = 0) -> Resultado:
    """Regenera los objetos pendientes.

    web_dir: la misma carpeta que se eligio en la UI (…\\<KB>\\<Modelo>\\web).
    items: objetos con .objeto y .estado (los de gxcheck).
    avisar: callback opcional (texto) con la ultima linea del build.
    simular: escribe el .msbuild y no ejecuta nada (no toca la KB).
    ambiente: nombre del ambiente en GeneXus (respaldo si no hay id).
    modelo: id del modelo (MODEL.model_id); es lo que evita ambiguedad.
    """
    plan = planificar(web_dir, items)
    if ambiente:
        plan.ambiente = ambiente
    plan.modelo = int(modelo or 0)
    if not plan.objetos:
        return Resultado(ok=False, codigo=-1, objetos=[],
                         error="No hay objetos para regenerar.")

    if not simular and not kb_permitida(plan.kb):
        raise ErrorGeneracion(
            f"La KB {plan.kb} no esta habilitada para generar.\n"
            f"Definí {VARIABLE_PERMITIDAS} con la carpeta de la KB de prueba, "
            f"por ejemplo:\n"
            f'  setx {VARIABLE_PERMITIDAS} "{plan.kb}_PRUEBAS"')

    if not simular:
        problema = validar_kb(plan.kb)
        if problema:
            raise ErrorGeneracion(problema)

    gx = buscar_genexus()

    carpeta = os.path.join(tempfile.gettempdir(), "gxcheck")
    os.makedirs(carpeta, exist_ok=True)
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_script = os.path.join(carpeta, f"generar_{sello}.msbuild")
    ruta_log = os.path.join(carpeta, f"generar_{sello}.log")

    with open(ruta_script, "w", encoding="utf-8") as fh:
        fh.write(armar_script(plan, os.path.join(gx, "Genexus.Tasks.targets")))

    if simular:
        return Resultado(ok=True, codigo=0, objetos=plan.objetos,
                         script=ruta_script,
                         salida="(simulación: no se ejecutó nada)",
                         simulado=True)

    msbuild = buscar_msbuild()

    comando = [msbuild, ruta_script, "/nologo", "/v:normal",
               "/nodeReuse:false"]
    inicio = time.time()
    proceso = subprocess.Popen(
        comando, cwd=carpeta, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, bufsize=0,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    crudo = b""
    ultimo_aviso = 0.0
    restante = b""
    try:
        while True:
            trozo = proceso.stdout.read(512)
            if not trozo:
                break
            crudo += trozo
            if time.time() - inicio > timeout:
                proceso.kill()
                break
            restante += trozo
            lineas = re.split(rb"[\r\n]+", restante)
            restante = lineas.pop()
            viva = [l for l in lineas if l.strip()]
            if viva and avisar and time.time() - ultimo_aviso >= 0.25:
                ultimo_aviso = time.time()
                avisar(_decodificar(viva[-1]).strip()[:160])
    finally:
        try:
            proceso.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proceso.kill()
            proceso.wait(timeout=5)

    salida = _decodificar(crudo)
    with open(ruta_log, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(salida)

    return Resultado(
        ok=(proceso.returncode == 0),
        codigo=proceso.returncode if proceso.returncode is not None else -1,
        objetos=plan.objetos,
        log=ruta_log,
        script=ruta_script,
        salida=salida,
        segundos=time.time() - inicio,
    )


def ultimo_log() -> str:
    """Ruta del ultimo log de regeneracion ('' si no hay ninguno)."""
    carpeta = os.path.join(tempfile.gettempdir(), "gxcheck")
    logs = glob.glob(os.path.join(carpeta, "generar_*.log"))
    if not logs:
        return ""
    return max(logs, key=os.path.getmtime)


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Regenera objetos pendientes de GeneXus por MSBuild.")
    ap.add_argument("--web", required=True,
                    help=r"carpeta del ambiente, ej. C:\KB\Data009\web")
    ap.add_argument("--objetos", required=True,
                    help="nombres separados por coma")
    ap.add_argument("--forzar", action="store_true",
                    help="ForceRebuild (para los DESINCRONIZADO)")
    ap.add_argument("--simular", action="store_true",
                    help="solo escribe el .msbuild, no ejecuta nada")
    a = ap.parse_args()

    class _Item:
        def __init__(self, nombre: str, estado: str) -> None:
            self.objeto = nombre
            self.estado = estado

    estado = "DESINCRONIZADO" if a.forzar else "PENDIENTE"
    items = [_Item(n.strip(), estado)
             for n in a.objetos.split(",") if n.strip()]
    try:
        res = generar(a.web, items, avisar=lambda t: print(t, flush=True),
                      simular=a.simular)
    except ErrorGeneracion as exc:
        sys.exit(f"ERROR: {exc}")

    print()
    if res.simulado:
        print("SIMULACIÓN: no se ejecutó nada.")
        print(f"Script: {res.script}")
        sys.exit(0)
    print(f"Objetos: {len(res.objetos)}   Codigo: {res.codigo}   "
          f"{res.segundos:.1f} s")
    print(f"Log:    {res.log}")
    print(f"Script: {res.script}")
    sys.exit(0 if res.ok else 1)


if __name__ == "__main__":
    _main()
