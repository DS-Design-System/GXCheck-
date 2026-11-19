#!/usr/bin/env python3
r"""
gxcheck.py  -  Motor de comparacion "KB vs generado" + diagnostico de build.

Sin interfaz: lo usa check_build.py (Flet) y tambien se puede correr por CLI.

    python gxcheck.py --web C:\PROYECTOS\KBs\MGJoyas\Data009\web

Que compara, por objeto:

  1. FECHAS    version vigente en la KB (Entity / EntityVersion /
               EntityVersionComposition / ModelEntityOutput, en UTC) contra la
               fecha de los artefactos generados. Para WebPanel/Transaction se
               exige .cs y .js: manda el mas viejo.
  2. CONTENIDO textos literales del layout en la KB (partes WebForm / Layout)
               contra el .cs / .js generado.
  3. ESTADO    ModelEntityOutput: salida registrada mas nueva que el archivo
               => GeneXus cree que esta al dia y F5 lo saltea.

Y ademas lee OutputLogs\Build.log / General.log para explicar POR QUE fallo el
ultimo build y que bloqueo a cada objeto (errores, advertencias de
especificacion con objeto/linea, errores de compilacion, excepciones).

Incluye todos los tipos de objeto de la KB que son objetos de usuario
(cualquier EntityType con namespace, mas las instancias de patron).
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import tempfile
import time
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Tipos con WebForm legible: unico caso donde se compara el layout
VISUAL = (43, 47)

# Tipos que generan codigo/artefactos propios en la carpeta web. El resto
# (Image, Attribute, Domain, Folder, Module, ThemeClass, File...) solo se copia
# o no genera nada, asi que se informan como SIN BUILD en vez de compararse.
TIPOS_CON_ARTEFACTO = {19, 26, 29, 30, 34, 38, 40, 42, 43, 47, 49, 57}

# textos del layout que terminan en el codigo generado
TEXT_ATTRS = (
    "titleExp", "caption", "text", "value", "description", "label",
    "placeholder", "tooltip", "title", "headerText", "altText",
)

GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEX = set("0123456789abcdefABCDEF")
CHUNK = 3500  # bytes por trozo; el hex (7800) entra en el ancho de sqlcmd
MAX_VERSIONS = 4

# comandos de build que GeneXus registra como "========== X started =========="
BUILD_CMDS = (
    "Build All", "Rebuild All", "Build With These Only", "Build With This Only",
    "Build", "Rebuild",
)

SQL_TYPES = """
SET NOCOUNT ON;
SELECT EntityTypeId, EntityTypeName FROM EntityType
WHERE (ISNULL(EntityTypeNamespace, '') <> '' AND EntityTypeName NOT LIKE 'Udm.Types.%') OR EntityTypeId IN (78);
"""

SQL_OBJECTS = """
SET NOCOUNT ON;
SELECT t.EntityTypeId,
       t.EntityId,
       t.ver,
       n.nm,
       n.tn,
       CONVERT(varchar(23), t.ts, 121)
FROM (
    SELECT EntityTypeId, EntityId, MAX(vid) AS ver, MAX(ts) AS ts
    FROM (
        SELECT e.EntityTypeId, e.EntityId, e.EntityLastVersionId AS vid,
               ev.EntityVersionTimestamp AS ts
        FROM Entity e
        JOIN EntityVersion ev ON ev.EntityTypeId = e.EntityTypeId
                             AND ev.EntityId = e.EntityId
                             AND ev.EntityVersionId = e.EntityLastVersionId
        WHERE e.EntityTypeId IN (SELECT EntityTypeId FROM EntityType
                                 WHERE (ISNULL(EntityTypeNamespace, '') <> '' AND EntityTypeName NOT LIKE 'Udm.Types.%')
                                    OR EntityTypeId = 78)
        UNION ALL
        SELECT c.CompoundEntityTypeId, c.CompoundEntityId,
               c.CompoundEntityVersionId, ev.EntityVersionTimestamp
        FROM EntityVersionComposition c
        JOIN EntityVersion ev ON ev.EntityTypeId = c.ComponentEntityTypeId
                             AND ev.EntityId = c.ComponentEntityId
                             AND ev.EntityVersionId = c.ComponentEntityVersionId
        WHERE c.CompoundEntityTypeId IN (
            SELECT EntityTypeId FROM EntityType
            WHERE (ISNULL(EntityTypeNamespace, '') <> '' AND EntityTypeName NOT LIKE 'Udm.Types.%') OR EntityTypeId = 78)
    ) a
    GROUP BY EntityTypeId, EntityId
) t
JOIN (
    SELECT e.EntityTypeId, e.EntityId, ev.EntityVersionName AS nm,
           ISNULL(et.EntityTypeName, CAST(e.EntityTypeId AS varchar)) AS tn
    FROM Entity e
    JOIN EntityVersion ev ON ev.EntityTypeId = e.EntityTypeId
                         AND ev.EntityId = e.EntityId
                         AND ev.EntityVersionId = e.EntityLastVersionId
    LEFT JOIN EntityType et ON et.EntityTypeId = e.EntityTypeId
    WHERE e.EntityTypeId IN (SELECT EntityTypeId FROM EntityType
                             WHERE (ISNULL(EntityTypeNamespace, '') <> '' AND EntityTypeName NOT LIKE 'Udm.Types.%')
                                OR EntityTypeId = 78)
) n ON n.EntityTypeId = t.EntityTypeId AND n.EntityId = t.EntityId
ORDER BY t.ts DESC;
"""

# Ultima salida registrada por GeneXus para el modelo de la carpeta elegida
SQL_OUTPUT = """
SET NOCOUNT ON;
SELECT o.EntityTypeId, o.EntityId,
       CONVERT(varchar(23), MAX(o.OutputTimestamp), 121)
FROM ModelEntityOutput o
JOIN Entity e ON e.EntityTypeId = o.EntityTypeId AND e.EntityId = o.EntityId
WHERE {filtro_modelo}
  AND o.OutputEntityVersionId = e.EntityLastVersionId
GROUP BY o.EntityTypeId, o.EntityId;
"""

SQL_MODEL = """
SET NOCOUNT ON;
SELECT model_id FROM MODEL WHERE model_path = '{ruta}';
"""

SQL_MODEL_NAME = """
SET NOCOUNT ON;
SELECT model_id, model_name FROM MODEL WHERE model_path = '{ruta}';
"""

SQL_PARTS = f"""
SET NOCOUNT ON;
SELECT c.CompoundEntityTypeId, c.CompoundEntityId, c.ComponentEntityVersionId,
       c.ComponentEntityTypeId, c.ComponentEntityId
FROM EntityVersionComposition c
JOIN Entity e ON e.EntityTypeId = c.CompoundEntityTypeId
             AND e.EntityId = c.CompoundEntityId
JOIN EntityVersion v ON v.EntityTypeId = c.ComponentEntityTypeId
                     AND v.EntityId = c.ComponentEntityId
                     AND v.EntityVersionId = c.ComponentEntityVersionId
WHERE c.CompoundEntityTypeId IN ({",".join(str(t) for t in VISUAL)})
  AND (v.EntityVersionName LIKE '%WebForm%' OR v.EntityVersionName LIKE '%Layout%')
  AND c.CompoundEntityVersionId = e.EntityLastVersionId;
"""

SQL_VERSIONS = """
SET NOCOUNT ON;
SELECT EntityVersionId,
       DATALENGTH(EntityVersionData)
FROM EntityVersion
WHERE EntityTypeId = {t} AND EntityId = {e}
ORDER BY EntityVersionId;
"""

# Version vigente de UN objeto (la que se muestra en la ventana de regeneracion)
SQL_SOURCE = """
SET NOCOUNT ON;
SELECT v.EntityVersionId, DATALENGTH(v.EntityVersionData)
FROM Entity e
JOIN EntityVersion v ON v.EntityTypeId = e.EntityTypeId
                    AND v.EntityId = e.EntityId
                    AND v.EntityVersionId = e.EntityLastVersionId
WHERE e.EntityTypeId = {t} AND e.EntityId = {e};
"""

# Partes de la version vigente (WebForm, Events, Rules, Source...). Es donde
# GeneXus guarda la definicion de casi todos los objetos.
SQL_SOURCE_PARTS = """
SET NOCOUNT ON;
SELECT p.t, p.e, p.v, ev.EntityVersionName, DATALENGTH(ev.EntityVersionData)
FROM (
    SELECT c.ComponentEntityTypeId AS t, c.ComponentEntityId AS e,
           MAX(c.ComponentEntityVersionId) AS v
    FROM EntityVersionComposition c
    JOIN Entity e ON e.EntityTypeId = c.CompoundEntityTypeId
                 AND e.EntityId = c.CompoundEntityId
    WHERE c.CompoundEntityTypeId = {t} AND c.CompoundEntityId = {e}
      AND c.CompoundEntityVersionId = e.EntityLastVersionId
    GROUP BY c.ComponentEntityTypeId, c.ComponentEntityId
) p
JOIN EntityVersion ev ON ev.EntityTypeId = p.t AND ev.EntityId = p.e
                     AND ev.EntityVersionId = p.v;
"""

SQL_NAMED = """
SET NOCOUNT ON;
SELECT e.EntityTypeId, e.EntityId, v.EntityVersionName,
       CONVERT(varchar(23), MAX(v.EntityVersionTimestamp), 121)
FROM Entity e
JOIN EntityVersion v ON v.EntityTypeId = e.EntityTypeId
                    AND v.EntityId = e.EntityId
                    AND v.EntityVersionId = e.EntityLastVersionId
GROUP BY e.EntityTypeId, e.EntityId, v.EntityVersionName;
"""

SQL_DATABASES = """
SET NOCOUNT ON;
SELECT name FROM sys.databases
WHERE name NOT IN ('master','tempdb','model','msdb') AND state = 0
ORDER BY name;
"""

LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+:\s+"
    r"(?P<lvl>INFO|WARN|ERROR)\s+(?:[^\w\s]\s+)?(?P<msg>.*)$")
RUN_START = re.compile(r"^=+\s+(?P<cmd>.*?)\s+started\s+=+$")
RUN_END = re.compile(r"^(?P<res>Success|Failed):\s+(?P<cmd>.*)$")
QUOTED = re.compile(r"'([^']{2,80})'")


class ErrorKB(Exception):
    """Falla de configuracion o de acceso a la KB."""


@dataclass
class Settings:
    server: str
    database: str
    web_dir: str
    design: list[str] = field(default_factory=list)
    content: bool = True
    filter: str = ""
    only_built: bool = True

    @property
    def model(self) -> str:
        return os.path.basename(os.path.dirname(self.web_dir.rstrip("\\/")))

    @property
    def kb_dir(self) -> str:
        return os.path.dirname(os.path.dirname(self.web_dir.rstrip("\\/")))


@dataclass
class Mensaje:
    severidad: str      # ERROR / WARN
    texto: str
    cuando: str = ""
    fase: str = ""

    def linea(self) -> str:
        marca = "✖" if self.severidad == "ERROR" else "!"
        cuando = f"{self.cuando} " if self.cuando else ""
        fase = f"[{self.fase}] " if self.fase else ""
        return f"{marca} {cuando}{fase}{self.texto}"


@dataclass
class Item:
    objeto: str
    tipo: str
    fuente: str
    generado: str
    estado: str
    motivo: str = ""
    mensajes: list[Mensaje] = field(default_factory=list)
    ultima_salida: str = ""
    detalle: str = ""
    type_id: int = 0
    entity_id: int = 0

    @property
    def ok(self) -> bool:
        return self.estado == "OK"


@dataclass
class Run:
    comando: str = ""
    inicio: str = ""
    fin: str = ""
    resultado: str = ""          # Success / Failed / (vacio = en curso)
    fase: str = ""
    errores: list[Mensaje] = field(default_factory=list)
    advertencias: list[Mensaje] = field(default_factory=list)
    por_objeto: dict[str, list[Mensaje]] = field(default_factory=dict)


@dataclass
class Result:
    items: list[Item] = field(default_factory=list)
    total: int = 0
    ok: int = 0
    pendientes: int = 0
    desincronizados: int = 0
    difiere: int = 0
    sin_build: int = 0
    segundos: float = 0.0
    modelo: str = ""
    kb: str = ""
    apk: str = ""
    error: str = ""
    runs: list[Run] = field(default_factory=list)
    ultimo_run: Run | None = None
    log_ruta: str = ""
    log_error: str = ""

    def contar(self) -> None:
        self.total = len(self.items)
        self.ok = sum(1 for i in self.items if i.estado == "OK")
        self.pendientes = sum(1 for i in self.items if i.estado == "PENDIENTE")
        self.desincronizados = sum(
            1 for i in self.items if i.estado == "DESINCRONIZADO")
        self.difiere = sum(1 for i in self.items if i.estado == "DIFIERE")
        self.sin_build = sum(1 for i in self.items if i.estado == "SIN BUILD")


class KB:
    """Acceso a la KB por sqlcmd (no necesita drivers ni pyodbc)."""

    def __init__(self, server: str, database: str):
        self.server = server
        self.database = database

    def run(self, sql: str, wide: bool = False) -> list[str]:
        args = ["sqlcmd", "-S", self.server, "-E", "-d", self.database]
        args += ["-y", "8000"] if wide else ["-h", "-1", "-W", "-s", "|"]
        args += ["-Q", sql]
        try:
            p = subprocess.run(args, capture_output=True, text=True,
                               encoding="cp1252", errors="replace")
        except FileNotFoundError as exc:
            raise ErrorKB("No se encontro sqlcmd en el PATH.") from exc
        if p.returncode != 0:
            detalle = (p.stdout or p.stderr or "").strip().splitlines()
            raise ErrorKB(detalle[-1] if detalle else "sqlcmd fallo sin detalle.")
        return p.stdout.splitlines()

    def rows(self, sql: str) -> list[list[str]]:
        out = []
        for line in self.run(sql):
            line = line.rstrip()
            if not line or "|" not in line:
                continue
            partes = [c.strip() for c in line.split("|")]
            if partes[0].isdigit():
                out.append(partes)
        return out


def ambiente_modelo(server: str, database: str, carpeta: str) -> str:
    """Nombre del ambiente de GeneXus que corresponde a esa carpeta de modelo.

    Para Data009 el ambiente se llama NETSQServer: el nombre esta en
    MODEL.model_name, no en el nombre de la carpeta.
    """
    try:
        kb = KB(server, database)
        filas = kb.rows(SQL_MODEL_NAME.format(
            ruta=carpeta.replace("'", "''")))
        for fila in filas:
            if len(fila) >= 2 and fila[1].strip():
                return fila[1].strip()
    except ErrorKB:
        pass
    return ""


def modelo_id(server: str, database: str, carpeta: str) -> int:
    """Id del modelo de GeneXus para esa carpeta (0 si no aparece).

    Se piden dos columnas a proposito: KB.rows descarta las lineas sin '|' y
    una consulta de una sola columna vuelve como texto suelto.
    """
    try:
        kb = KB(server, database)
        for fila in kb.rows(SQL_MODEL_NAME.format(
                ruta=carpeta.replace("'", "''"))):
            if fila and fila[0].isdigit():
                return int(fila[0])
    except ErrorKB:
        pass
    return 0


ESQUEMA_SCRIPT = """<Project DefaultTargets="Esquema" \
xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <Import Project="{tareas}" />
  <Target Name="Esquema">
    <OpenKnowledgeBase Directory="{kb}"{modelo} />
    <WriteKnowledgeBaseSchema File="{kb_xml}" />
    <WriteDatabaseSchema File="{db_xml}" />
  </Target>
</Project>
"""


def _leer_esquema(ruta: str) -> dict[str, dict[str, dict]]:
    """tabla -> atributo -> {formula, inferred}. Nombres en minusculas."""
    raiz = ET.parse(ruta).getroot()
    tablas: dict[str, dict[str, dict]] = {}
    for tabla in raiz.iter("Table"):
        nombre = (tabla.findtext("Name") or "").strip().lower()
        if not nombre:
            continue
        attrs: dict[str, dict] = {}
        for a in tabla.iter("Attribute"):
            n = (a.findtext("Name") or "").strip().lower()
            if not n:
                continue
            attrs[n] = {
                "formula": (a.findtext("Formula") or "").lower() == "true",
                "inferred": (a.findtext("Inferred") or "").lower() == "true",
            }
        tablas[nombre] = attrs
    return tablas


def comparar_esquemas(ruta_kb: str, ruta_db: str) -> dict:
    """Diferencias entre lo que espera la KB y lo que tiene la base.

    Solo mira presencia de tablas y atributos. Las formulas y los inferidos no
    existen como columna, asi que no cuentan. Los nombres de la base vienen
    truncados a 30 caracteres por SQL Server.
    """
    kb = _leer_esquema(ruta_kb)
    db = _leer_esquema(ruta_db)
    faltan_tablas: list[str] = []
    faltan_attrs: list[tuple[str, str]] = []
    omitidos = 0
    for tabla, attrs in sorted(kb.items()):
        presentes = db.get(tabla) or db.get(tabla[:30])
        if presentes is None:
            faltan_tablas.append(tabla)
            continue
        for atributo, meta in sorted(attrs.items()):
            buscado = atributo[:30]
            esta = any(p == atributo or p == buscado
                       or p.startswith(buscado[:28]) for p in presentes)
            if esta:
                continue
            if meta["formula"] or meta["inferred"]:
                omitidos += 1
            else:
                faltan_attrs.append((tabla, atributo))
    return {"tablas_kb": len(kb), "tablas_db": len(db),
            "faltan_tablas": faltan_tablas,
            "faltan_atributos": faltan_attrs,
            "omitidos": omitidos}


def verificar_esquema(kb_dir: str, modelo_id: int = 0,
                      timeout: int = 900) -> dict:
    """Avisa si la base de la aplicacion esta atrasada respecto de la KB.

    Genera el esquema que espera la KB y el real de la base con las tareas
    WriteKnowledgeBaseSchema / WriteDatabaseSchema, y los compara. Si faltan
    tablas o atributos, hay una reorganizacion pendiente.

    Devuelve {"ok", "error", "faltan_tablas", "faltan_atributos", "omitidos",
    "tablas_kb", "tablas_db", "salida"}.
    """
    import generar as gen  # misma deteccion de GeneXus/MSBuild

    salida = {"ok": False, "error": "", "faltan_tablas": [],
              "faltan_atributos": [], "omitidos": 0, "tablas_kb": 0,
              "tablas_db": 0, "salida": ""}
    if not kb_dir or not os.path.isdir(kb_dir):
        salida["error"] = f"No existe la carpeta de la KB: {kb_dir}"
        return salida
    try:
        gx = gen.buscar_genexus()
        msbuild = gen.buscar_msbuild()
    except gen.ErrorGeneracion as exc:
        salida["error"] = str(exc)
        return salida

    carpeta = os.path.join(tempfile.gettempdir(), "gxcheck")
    os.makedirs(carpeta, exist_ok=True)
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    kb_xml = os.path.join(carpeta, f"esquema_kb_{sello}.xml")
    db_xml = os.path.join(carpeta, f"esquema_db_{sello}.xml")
    script = os.path.join(carpeta, f"esquema_{sello}.msbuild")

    texto = ESQUEMA_SCRIPT.format(
        tareas=gen._xml(os.path.join(gx, "Genexus.Tasks.targets")),
        kb=gen._xml(kb_dir),
        modelo=f' TargetModelId="{int(modelo_id)}"' if modelo_id else "",
        kb_xml=gen._xml(kb_xml),
        db_xml=gen._xml(db_xml),
    )
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(texto)

    try:
        proceso = subprocess.run(
            [msbuild, script, "/nologo", "/v:minimal", "/nodeReuse:false"],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        salida["error"] = f"No se pudo ejecutar MSBuild: {exc}"
        return salida

    crudo = (proceso.stdout or b"") + (proceso.stderr or b"")
    salida["salida"] = gen._decodificar(crudo)[-1500:]
    if proceso.returncode != 0 or not os.path.isfile(kb_xml) \
            or not os.path.isfile(db_xml):
        salida["error"] = "No se pudieron generar los esquemas."
        return salida

    try:
        diferencias = comparar_esquemas(kb_xml, db_xml)
    except (ET.ParseError, OSError) as exc:
        salida["error"] = f"No se pudieron leer los esquemas: {exc}"
        return salida

    salida.update(diferencias)
    salida["ok"] = True
    return salida


def list_databases(server: str) -> list[str]:
    names = []
    for line in KB(server, "master").run(SQL_DATABASES):
        name = line.strip()
        if name and not name.startswith("("):
            names.append(name)
    return names


def list_instances() -> list[str]:
    """Instancias SQL Server instaladas en esta maquina (registro).

    Devuelve nombres listos para sqlcmd: HOST\\INSTANCIA, o HOST para la
    instancia por defecto. Sirve para el desplegable; igual se puede escribir
    un servidor remoto a mano.
    """
    host = os.environ.get("COMPUTERNAME") or "localhost"
    nombres: set[str] = set()
    try:
        import winreg
    except ImportError:  # no es Windows
        return [host, "localhost"]

    clave = r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL"
    for vista in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clave, 0,
                                winreg.KEY_READ | vista) as k:
                i = 0
                while True:
                    try:
                        nombre, _val, _tipo = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if nombre.upper() == "MSSQLSERVER":
                        nombres.add(host)
                    else:
                        nombres.add(f"{host}\\{nombre}")
        except OSError:
            continue
    nombres.add("localhost")
    return sorted(nombres, key=lambda n: (n != host, n.upper()))


def list_types(server: str, database: str) -> dict[int, str]:
    tipos = {}
    for line in KB(server, database).rows(SQL_TYPES):
        tipos[int(line[0])] = line[1]
    return tipos


# --------------------------------------------------------------------------- util

def parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def mtime(path: str) -> datetime | None:
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path))


def fmt(dt: datetime | None) -> str:
    return f"{dt:%d/%m %H:%M}" if dt else "-"


def read_text(path: str) -> str:
    """Los .cs/.js generados no siempre estan en UTF-8."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def gunzip_blob(raw: bytes) -> str | None:
    if len(raw) < 11:
        return None
    try:
        data = zlib.decompress(raw[11:], 16 + zlib.MAX_WBITS)
    except zlib.error:
        return None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def gunzip_parcial(raw: bytes, tope: int = 40000) -> str | None:
    """Descomprime un gzip aunque venga cortado (devuelve hasta `tope` bytes).

    Sirve para mostrar la vista previa de un objeto sin bajar el blob entero.
    """
    if len(raw) < 11:
        return None
    try:
        descompresor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        datos = descompresor.decompress(raw[11:], tope)
    except zlib.error:
        return None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return datos.decode(enc)
        except UnicodeDecodeError:
            continue
    return datos.decode("utf-8", "replace")


COLUMNAS_POR_CONSULTA = 4  # trozos de blob por cada llamada a sqlcmd


def fetch_blob_prefijo(kb: KB, key: tuple[int, int, int],
                       largo: int, tope: int) -> bytes:
    """Primeros `tope` bytes del blob, con varias columnas por consulta.

    Mismo formato que fetch_blob_single, pero baja 4 trozos a la vez: un objeto
    grande pasa de decenas de procesos sqlcmd a unos pocos.
    """
    comp_type, comp_id, ver_id = key
    limite = min(largo, tope)
    piezas: dict[int, str] = {}
    paso = CHUNK * COLUMNAS_POR_CONSULTA
    for off in range(1, limite + 1, paso):
        columnas = []
        for i in range(COLUMNAS_POR_CONSULTA):
            inicio = off + i * CHUNK
            if inicio > limite:
                break
            columnas.append(
                f"CONVERT(varchar(max), "
                f"SUBSTRING(EntityVersionData, {inicio}, {CHUNK}), 2)")
        sql = f"""
SET NOCOUNT ON;
SELECT {", ".join(columnas)}
FROM EntityVersion
WHERE EntityTypeId = {comp_type} AND EntityId = {comp_id}
  AND EntityVersionId = {ver_id};
"""
        for linea in kb.run(sql, wide=True):
            campos = linea.split()
            for i, campo in enumerate(campos):
                if campo.upper() == "NULL":
                    continue
                piezas[off + i * CHUNK] = "".join(c for c in campo if c in HEX)
    if not piezas:
        return b""
    try:
        return bytes.fromhex("".join(piezas[o] for o in sorted(piezas)))
    except ValueError:
        return b""


def fuente_objeto(server: str, database: str, type_id: int, entity_id: int,
                  max_bytes: int = 4_000_000,
                  max_chars: int = 25000) -> str:
    """Vista previa de la version vigente de un objeto, leida de la KB.

    GeneXus guarda la definicion en las PARTES del objeto (WebForm, Events,
    Source...), no en su propia version. Se leen las partes de la version
    vigente y, si no hay, la version propia. Solo se baja el principio de cada
    blob comprimido y se corta al alcanzar `max_chars` (que es lo que se ve).
    """
    if not type_id or not entity_id:
        return ""
    try:
        kb = KB(server, database)
        partes = []
        for fila in kb.rows(SQL_SOURCE_PARTS.format(t=int(type_id),
                                                    e=int(entity_id))):
            if len(fila) < 5 or not all(x.isdigit() for x in
                                        (fila[0], fila[1], fila[2], fila[4])):
                continue
            if int(fila[4]) <= 0:
                continue
            partes.append((int(fila[0]), int(fila[1]), int(fila[2]),
                           fila[3], int(fila[4])))

        def prioridad(parte) -> int:
            nombre = (parte[3] or "").lower()
            for i, clave in enumerate(("webform", "layout", "source",
                                       "events", "rules")):
                if clave in nombre:
                    return i
            return 9

        bloques: list[str] = []
        usados = 0
        for part_t, part_e, ver, nombre, largo in sorted(partes, key=prioridad):
            if usados >= max_chars:
                break
            raw = fetch_blob_prefijo(kb, (part_t, part_e, ver), largo,
                                     min(largo, 12_000))
            texto = gunzip_parcial(raw) if raw else None
            if texto is None:
                texto = raw.decode("utf-8", "replace") if raw else ""
            texto = texto.strip()
            bloques.append(f"== {nombre} ==")
            bloques.append(texto)
            usados += len(texto) + len(nombre) + 8
        if bloques:
            texto = "\n".join(bloques)
            if len(texto) > max_chars:
                texto = texto[:max_chars] + "\n\n… (vista previa recortada)"
            return texto

        # Sin partes: la definicion esta en la version propia del objeto
        filas = kb.rows(SQL_SOURCE.format(t=int(type_id), e=int(entity_id)))
        if not filas:
            return ""
        ver_id = int(filas[0][0])
        largo = int(filas[0][1])
        if largo <= 0:
            return ""
        if largo > max_bytes:
            return (f"(La versión en la KB pesa {largo:,} bytes; "
                    f"no se muestra.)".replace(",", "."))
        tope = min(largo, 24_000)
        raw = fetch_blob_prefijo(kb, (int(type_id), int(entity_id), ver_id),
                                 largo, tope)
        if not raw:
            return ""
        texto = gunzip_parcial(raw)
        if texto is None:
            texto = raw.decode("utf-8", "replace")
        if len(texto) > max_chars:
            texto = texto[:max_chars] + "\n\n… (vista previa recortada)"
        elif largo > tope:
            texto += (f"\n\n… (vista previa: se leyeron {tope} de {largo} "
                      f"bytes comprimidos)")
        return texto
    except (ErrorKB, ValueError, IndexError):
        return ""


def is_literal_text(v: str) -> bool:
    if not (3 <= len(v) <= 60):
        return False
    if GUID_RE.match(v) or v.startswith(("att:", "&", "<", "{", "%", "=",
                                          "+", "!", "(", ")", "[", "#")):
        return False
    if v.upper().startswith("WWP_") or v.upper().startswith("GXM_"):
        return False
    if any(ch in v for ch in '";()=<>\\'):
        return False
    if IDENT_RE.match(v):
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*\.[A-Za-z_][A-Za-z0-9_]*$", v):
        return False
    if re.fullmatch(r"[\d\s.,$#%:/-]+", v):
        return False
    return (" " in v) or bool(re.search(r"[^A-Za-z0-9_]", v))


def layout_texts(xml_text: str) -> set[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return set()
    found: set[str] = set()
    for el in root.iter():
        for attr in TEXT_ATTRS:
            v = el.get(attr)
            if v:
                found.add(v.strip())
        if el.text and el.text.strip():
            found.add(el.text.strip())
    return {v for v in found if is_literal_text(v)}


# ------------------------------------------------------------------ blobs (layout)

def fetch_blobs(kb: KB, needed: dict[tuple[int, int, int], int]
                ) -> dict[tuple[int, int, int], bytes]:
    buf: dict[tuple[int, int, int], dict[int, str]] = {k: {} for k in needed}
    maxlen = max(needed.values(), default=0)
    for off in range(1, maxlen + 1, CHUNK):
        keys = [k for k, ln in needed.items() if ln >= off]
        for i in range(0, len(keys), 150):
            group = keys[i:i + 150]
            values = ",".join(f"({t},{e},{v})" for t, e, v in group)
            sql = f"""
SET NOCOUNT ON;
SELECT ev.EntityTypeId, ev.EntityId, ev.EntityVersionId, {off},
       CONVERT(varchar(max), SUBSTRING(ev.EntityVersionData, {off}, {CHUNK}), 2)
FROM EntityVersion ev
JOIN (VALUES {values}) p(t, e, v)
  ON ev.EntityTypeId = p.t AND ev.EntityId = p.e AND ev.EntityVersionId = p.v;
"""
            for line in kb.run(sql, wide=True):
                fields = line.split(None, 4)
                if len(fields) != 5 or not all(x.isdigit() for x in fields[:4]):
                    continue
                key = (int(fields[0]), int(fields[1]), int(fields[2]))
                if key in buf:
                    buf[key][int(fields[3])] = "".join(
                        c for c in fields[4] if c in HEX)

    out: dict[tuple[int, int, int], bytes] = {}
    for key, pieces in buf.items():
        try:
            data = bytes.fromhex("".join(pieces[o] for o in sorted(pieces)))
        except ValueError:
            data = b""
        if len(data) != needed[key]:
            data = fetch_blob_single(kb, key, needed[key])
        out[key] = data
    return out


def fetch_blob_single(kb: KB, key: tuple[int, int, int], length: int) -> bytes:
    comp_type, comp_id, ver_id = key
    pieces: dict[int, str] = {}
    for off in range(1, length + 1, CHUNK):
        sql = f"""
SET NOCOUNT ON;
SELECT {off},
       CONVERT(varchar(max), SUBSTRING(EntityVersionData, {off}, {CHUNK}), 2)
FROM EntityVersion
WHERE EntityTypeId = {comp_type} AND EntityId = {comp_id}
  AND EntityVersionId = {ver_id};
"""
        for line in kb.run(sql, wide=True):
            fields = line.split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                continue
            pieces[int(fields[0])] = "".join(c for c in fields[1] if c in HEX)
    try:
        return bytes.fromhex("".join(pieces[o] for o in sorted(pieces)))
    except ValueError:
        return b""


def load_layout_texts(kb: KB, kb_rows: list[list[str]],
                      wanted: set[tuple[int, int]]
                      ) -> dict[tuple[int, int], tuple[set[str], set[str]]]:
    pieces: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for comp_t, comp_id, comp_v, part_t, part_id in kb_rows:
        key = (int(comp_t), int(comp_id))
        if key in wanted:
            pieces.setdefault(key, []).append(
                (int(part_t), int(part_id), int(comp_v)))

    part_versions: dict[tuple[int, int], list[tuple[int, int]]] = {}
    needed: dict[tuple[int, int, int], int] = {}
    for parts in pieces.values():
        for part_t, part_id, _comp_v in parts:
            pk = (part_t, part_id)
            if pk in part_versions:
                continue
            vers = []
            for line in kb.rows(SQL_VERSIONS.format(t=part_t, e=part_id)):
                if len(line) >= 2 and line[0].isdigit() and line[1].isdigit():
                    vers.append((int(line[0]), int(line[1])))
            vers = vers[-MAX_VERSIONS:]
            part_versions[pk] = vers
            for ver_id, length in vers:
                needed[(part_t, part_id, ver_id)] = length

    blobs = fetch_blobs(kb, needed)
    textos = {k: layout_texts(gunzip_blob(v) or "") for k, v in blobs.items()}

    result: dict[tuple[int, int], tuple[set[str], set[str]]] = {}
    for key, parts in pieces.items():
        current: set[str] = set()
        hist: set[str] = set()
        for part_t, part_id, comp_v in parts:
            for ver_id, _length in part_versions[(part_t, part_id)]:
                texts = textos.get((part_t, part_id, ver_id), set())
                hist |= texts
                if ver_id == comp_v:
                    current |= texts
        result[key] = (current, hist)
    return result


# --------------------------------------------------------------- artefactos (web)

class Artefactos:
    """Indice de la carpeta generada: raiz del nombre (minusculas) -> rutas.

    Los objetos dentro de modulos generan archivos con el nombre calificado
    (general.services.gxonpendingeventfailed.cs), por eso se indexa tambien el
    ultimo segmento del nombre.
    """

    IGNORAR_DIR = {"bin", "obj", ".git", "__pycache__", "themes", "bootstrap",
                   "dvelop", "unanimo_chameleon", "ramp", "shared",
                   "gxusercontrols", "usercontrols", "resources", "gam",
                   ".gam", "gamresources", "proto", "private", "metadata",
                   "gxmetadata",
                   "openapi3", "queryviewer", "tab", "timer", "window",
                   "rating", "dashboardviewer",
                   "design_system.programs.common", "node_modules"}
    IGNORAR_EXT = {".dll", ".pdb", ".exe", ".zip", ".apk", ".jpg", ".jpeg",
                   ".png", ".gif", ".ico", ".svg", ".map", ".resources",
                   ".cache", ".nupkg", ".so", ".a", ".class", ".jar"}

    def __init__(self, web: str):
        self.web = web
        self.meta = os.path.join(web, "gxmetadata")
        self.por_raiz: dict[str, list[str]] = {}
        self.por_ultimo: dict[str, list[str]] = {}
        self.por_prefijo: dict[str, list[str]] = {}
        self.meta_por_raiz: dict[str, list[str]] = {}
        self.por_tema: dict[str, list[str]] = {}
        self._indexar_web()
        self._indexar_meta()
        self._indexar_temas()

    def _indexar_web(self) -> None:
        """Raiz de web + carpetas de modulos.

        Los objetos de un modulo generan con nombre calificado
        (general.services.gxonpendingeventfailed.cs) dentro de la carpeta del
        modulo, por eso se indexa tambien el ultimo segmento del nombre, pero
        solo para codigo (.cs/.js/.svc) y salteando las carpetas de framework.
        """
        if not os.path.isdir(self.web):
            return
        for actual, dirs, archivos in os.walk(self.web):
            dirs[:] = [d for d in dirs if d.lower() not in self.IGNORAR_DIR]
            en_raiz = os.path.normcase(actual) == os.path.normcase(self.web)
            for nombre in archivos:
                low = nombre.lower()
                stem, ext = os.path.splitext(low)
                if ext in self.IGNORAR_EXT:
                    continue
                ruta = os.path.join(actual, nombre)
                self.por_raiz.setdefault(stem, []).append(ruta)
                if ext in (".cs", ".js", ".svc"):
                    ultimo = stem.rsplit(".", 1)[-1]
                    if ultimo != stem:
                        self.por_ultimo.setdefault(ultimo, []).append(ruta)
                if en_raiz and "_" in stem and len(stem.split("_")[0]) >= 5:
                    self.por_prefijo.setdefault(stem.split("_")[0],
                                                []).append(ruta)

    def _indexar_meta(self) -> None:
        if not os.path.isdir(self.meta):
            return
        for nombre in os.listdir(self.meta):
            ruta = os.path.join(self.meta, nombre)
            if not os.path.isfile(ruta):
                continue
            bajo = nombre.lower()
            # No son codigo generado: propiedades del objeto y metadatos de
            # otras plataformas (Android/iOS). Si se cuentan, arrastran el
            # minimo y el objeto queda PENDIENTE para siempre.
            if (".properties.json" in bajo or ".panel.android." in bajo
                    or ".panel.apple." in bajo or ".panel.ios." in bajo
                    or ".panel.uwp." in bajo
                    or bajo.endswith((".externalobjects", ".usercontrols"))):
                continue
            clave = bajo.split(".")[0]
            self.meta_por_raiz.setdefault(clave, []).append(ruta)

    def _indexar_temas(self) -> None:
        """Design System / Theme: web\\themes\\[Modulo.]<Nombre>.json"""
        carpeta = os.path.join(self.web, "themes")
        if not os.path.isdir(carpeta):
            return
        for nombre in os.listdir(carpeta):
            ruta = os.path.join(carpeta, nombre)
            if not os.path.isfile(ruta):
                continue
            stem = os.path.splitext(nombre.lower())[0]
            self.por_tema.setdefault(stem.rsplit(".", 1)[-1], []).append(ruta)

    def de_objeto(self, nombre: str, tipo: int = 0) -> list[str]:
        low = nombre.lower()
        rutas: set[str] = set(self.por_raiz.get(low, []))
        rutas.update(self.por_raiz.get(f"type_{low}", []))
        rutas.update(self.por_ultimo.get(low, []))
        rutas.update(self.por_prefijo.get(low, []))
        # SDPanel (19): su codigo en este modelo es el .cs del level detail.
        # La metadata de gxmetadata es de Android y no se reescribe al generar
        # web, asi que usarla lo dejaria pendiente para siempre.
        if tipo != 19:
            rutas.update(self.meta_por_raiz.get(low, []))
        if tipo in (42, 57):
            rutas.update(self.por_tema.get(low, []))
        return sorted(rutas)


def compare_content(web: str, tipo: int, name: str,
                    current: set[str], hist: set[str], stale: bool
                    ) -> tuple[bool, str]:
    if tipo not in VISUAL:
        return True, ""
    if not current:
        return True, ""
    low = name.lower()
    bodies = [b for b in (read_text(os.path.join(web, f"{low}{ext}"))
                          for ext in (".cs", ".js")) if b]
    if not bodies:
        return True, ""

    def aparece(texto: str) -> bool:
        return any(texto in b for b in bodies)

    faltan = sorted(t for t in current if not aparece(t))
    viejos = sorted(t for t in (hist - current) if aparece(t))
    if not viejos and len(faltan) == len(current):
        return True, ""
    if faltan:
        return False, "el generado no tiene: " + ", ".join(faltan[:4])
    if stale and viejos:
        return False, "el generado conserva textos eliminados: " + ", ".join(
            viejos[:3])
    return True, ""


def find_apk(kb_dir: str, model: str) -> str:
    patron = os.path.join(kb_dir, model, "mobile", "Android", "*", "build",
                          "outputs", "apk", "*", "*.apk")
    apks = [(mtime(p), p) for p in glob.glob(patron)]
    apks = [(t, p) for t, p in apks if t]
    if not apks:
        return ""
    t, p = max(apks)
    return f"{os.path.basename(p)}: {fmt(t)}"


# ------------------------------------------------------------------ build log

def leer_log(ruta: str) -> list[tuple[str, str, str]]:
    """(timestamp, nivel, mensaje) de cada linea del log."""
    salida = []
    if not os.path.exists(ruta):
        return salida
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            with open(ruta, encoding=enc) as fh:
                contenido = fh.read()
            break
        except UnicodeDecodeError:
            continue
        except OSError:
            return salida
    else:
        return salida
    for linea in contenido.splitlines():
        m = LOG_LINE.match(linea)
        if m:
            salida.append((m.group("ts"), m.group("lvl"),
                           m.group("msg").strip()))
    return salida


def objetos_del_mensaje(texto: str) -> list[str]:
    """Nombres de objeto citados en el mensaje (no tipos ni excepciones)."""
    nombres = []
    for q in QUOTED.findall(texto):
        q = q.strip()
        if not q or q.isdigit() or len(q) > 60:
            continue
        if "." in q or q.lower().endswith(("exception", "error")):
            continue
        nombres.append(q.lower())
    return nombres


def es_bloqueante(msg: str) -> bool:
    """ERROR del log que realmente es un error (no un warning mal prefijado)."""
    m = msg.strip()
    if m.lower().startswith("error: warning"):
        return False
    if m.lower().startswith("warning"):
        return False
    return True


def parsear_build(kb_dir: str) -> tuple[list[Run], str, str]:
    """Devuelve (runs de build, ruta del log, error de lectura)."""
    ruta = os.path.join(kb_dir, "OutputLogs", "Build.log")
    lineas = leer_log(ruta)
    if not lineas:
        return [], ruta, ("No hay OutputLogs\\Build.log (o esta vacio): GeneXus "
                          "no dejo registro del build.")

    runs: list[Run] = []
    actual: Run | None = None
    fase = ""
    esperando_fin = False

    for ts, nivel, msg in lineas:
        m_start = RUN_START.match(msg)
        if m_start:
            comando = m_start.group("cmd").strip()
            fase = comando
            if comando in BUILD_CMDS:
                actual = Run(comando=comando, inicio=ts)
                runs.append(actual)
                esperando_fin = True
            continue

        m_end = RUN_END.match(msg)
        if m_end and actual and esperando_fin:
            if m_end.group("cmd").strip() == actual.comando:
                actual.resultado = m_end.group("res")
                actual.fin = ts
                actual.fase = fase
                esperando_fin = False
                actual = None
            continue

        if not actual:
            continue

        if nivel in ("ERROR", "WARN"):
            mensaje = Mensaje(
                severidad="ERROR" if nivel == "ERROR" else "WARN",
                texto=msg, cuando=ts, fase=fase)
            if nivel == "ERROR":
                actual.errores.append(mensaje)
            else:
                actual.advertencias.append(mensaje)
            for nombre in objetos_del_mensaje(msg):
                actual.por_objeto.setdefault(nombre, []).append(mensaje)

    return runs, ruta, ""


def resumen_run(run: Run | None) -> str:
    if not run:
        return "No hay builds registrados en Build.log."
    if run.resultado:
        estado = run.resultado
    else:
        estado = "sin cierre (abortado o interrumpido)"
    txt = f"{run.comando}: {estado}"
    if run.inicio:
        txt += f"  ({run.inicio[11:]}"
        if run.fin:
            txt += f" -> {run.fin[11:]}"
        txt += ")"
    reales = [e for e in run.errores if es_bloqueante(e.texto)]
    if reales:
        txt += f"  ·  {len(reales)} error(es)"
    if run.advertencias:
        txt += f"  ·  {len(run.advertencias)} advertencia(s)"
    return txt


# ----------------------------------------------------------------------- compare

def compare(s: Settings, progress=None) -> Result:
    def say(texto: str, fraccion: float = 0.0) -> None:
        if progress:
            progress(texto, fraccion)

    if not os.path.isdir(s.web_dir):
        raise ErrorKB(f"No existe la carpeta: {s.web_dir}")
    if not any(f.lower().endswith(".cs") for f in os.listdir(s.web_dir)):
        raise ErrorKB(f"La carpeta no parece generada por GeneXus "
                      f"(sin .cs): {s.web_dir}")

    t0 = time.time()
    kb = KB(s.server, s.database)
    res = Result(modelo=s.model, kb=s.kb_dir)

    say("Leyendo objetos de la KB...", 0.05)
    raw = kb.rows(SQL_OBJECTS)
    if not raw:
        raise ErrorKB(f"La base {s.database} no devolvio objetos GeneXus. "
                      f"Revisa instancia y base.")

    # varias entidades con el mismo nombre: nos quedamos con la mas nueva
    newest: dict[tuple[str, str], tuple] = {}
    for type_id, entity_id, ver, name, tipo, ts in raw:
        t = parse_utc(ts)
        key = (tipo, name)
        if key not in newest or (t and t > newest[key][0]):
            newest[key] = (t, int(type_id), int(entity_id), int(ver))

    say("Leyendo salidas registradas...", 0.12)
    model_id = modelo_id(s.server, s.database, s.model) or None
    filtro_modelo = (f"o.ModelId = {model_id}" if model_id is not None else
                     "(o.ModelId = (SELECT TOP 1 ModelId FROM ModelEntityOutput "
                     "GROUP BY ModelId ORDER BY MAX(OutputTimestamp) DESC))")
    outputs: dict[tuple[int, int], datetime] = {}
    for type_id, entity_id, ts in kb.rows(SQL_OUTPUT.format(filtro_modelo=filtro_modelo)):
        t = parse_utc(ts)
        if t:
            outputs[(int(type_id), int(entity_id))] = t

    design_ts: list[datetime] = []
    if s.design:
        wanted = {n.lower() for n in s.design}
        for _t, _e, name, ts in kb.rows(SQL_NAMED):
            if name.lower() in wanted:
                t = parse_utc(ts)
                if t:
                    design_ts.append(t)

    say("Leyendo OutputLogs\\Build.log...", 0.18)
    runs, log_ruta, log_error = parsear_build(s.kb_dir)
    res.runs = runs
    res.ultimo_run = runs[-1] if runs else None
    res.log_ruta = log_ruta
    res.log_error = log_error

    seleccion = {k for k in newest
                 if not s.filter or s.filter.lower() in k[1].lower()}

    textos: dict[tuple[int, int], tuple[set[str], set[str]]] = {}
    if s.content:
        say("Leyendo layouts de la KB...", 0.3)
        wanted = {(newest[k][1], newest[k][2]) for k in seleccion
                  if newest[k][1] in VISUAL}
        if wanted:
            textos = load_layout_texts(kb, kb.rows(SQL_PARTS), wanted)

    say("Comparando...", 0.88)
    art = Artefactos(s.web_dir)
    items: list[Item] = []

    for key in seleccion:
        tipo, name = key
        src_ts, type_id, entity_id, _ver = newest[key]
        low = name.lower()

        rutas = (art.de_objeto(name, type_id)
                 if type_id in TIPOS_CON_ARTEFACTO else [])
        tiempos = [(p, mtime(p)) for p in rutas]
        tiempos = [(p, t) for p, t in tiempos if t is not None]
        if tiempos:
            fname, gen = min(tiempos, key=lambda x: x[1])
            motivo = os.path.basename(fname)
        else:
            fname, gen = "", None
            motivo = ("sin artefacto generado"
                      if type_id in TIPOS_CON_ARTEFACTO
                      else "el tipo de objeto no genera codigo propio")

        fuente = src_ts
        if design_ts and type_id in VISUAL + (19,):
            newest_design = max(design_ts)
            if fuente is None or newest_design > fuente:
                fuente = newest_design

        contenido_ok = True
        if (type_id, entity_id) in textos:
            current, hist = textos[(type_id, entity_id)]
            stale = bool(gen and fuente and gen < fuente)
            ok, why = compare_content(s.web_dir, type_id, name,
                                      current, hist, stale)
            contenido_ok = ok
            if why:
                motivo = why

        salida = outputs.get((type_id, entity_id))
        if fuente is None:
            estado = "?"
        elif gen is None:
            estado = "SIN BUILD"
        elif gen < fuente:
            estado = "PENDIENTE"
        elif salida and salida > gen:
            estado = "DESINCRONIZADO"
            motivo = f"la KB registro salida {fmt(salida)}"
        elif not contenido_ok:
            estado = "DIFIERE"
        else:
            estado = "OK"

        mensajes: list[Mensaje] = []
        for run in runs:
            mensajes.extend(run.por_objeto.get(low, []))

        item = Item(
            objeto=name, tipo=tipo, fuente=fmt(fuente), generado=fmt(gen),
            estado=estado, motivo=motivo, mensajes=mensajes,
            ultima_salida=fmt(salida), type_id=type_id, entity_id=entity_id,
        )
        item.detalle = explicar(item, res.ultimo_run)
        items.append(item)

    orden = {"PENDIENTE": 0, "DESINCRONIZADO": 1, "DIFIERE": 2,
             "OK": 3, "SIN BUILD": 4, "?": 5}
    items.sort(key=lambda i: (orden.get(i.estado, 9), i.objeto.lower()))

    res.items = items
    res.contar()
    res.segundos = time.time() - t0
    res.apk = find_apk(s.kb_dir, s.model)
    say("Listo.", 1.0)
    return res


def explicar(item: Item, ultimo: Run | None) -> str:
    """Texto del popup: por que esta como esta y que lo bloqueo."""
    lineas: list[str] = []
    lineas.append(f"{item.objeto}  ({item.tipo})")
    lineas.append(f"Estado: {item.estado}")
    lineas.append(f"Fuente en la KB: {item.fuente}   ·   "
                  f"Generado: {item.generado}")
    lineas.append(f"Ultima salida registrada por GeneXus: {item.ultima_salida or '-'}")
    lineas.append(f"Detalle: {item.motivo}")
    lineas.append("")

    errores = [m for m in item.mensajes if m.severidad == "ERROR"]
    avisos = [m for m in item.mensajes if m.severidad == "WARN"]

    if errores:
        lineas.append("Errores de build que lo mencionan:")
        lineas += [m.linea() for m in errores[:20]]
        lineas.append("")
    if avisos:
        lineas.append("Advertencias de especificacion:")
        lineas += [m.linea() for m in avisos[:20]]
        lineas.append("")

    if not errores and not avisos:
        tipos = {
            "PENDIENTE": ("No hay errores registrados para este objeto. El "
                          "codigo generado es mas viejo que el fuente: el build "
                          "no lo incluyo o no lo regenero."),
            "DESINCRONIZADO": ("GeneXus registro que lo genero, pero el archivo "
                               "en disco no cambio. Hay que forzarlo con "
                               "'Build with these only'."),
            "DIFIERE": ("El codigo generado no contiene los textos del layout "
                        "actual: la ultima generacion no corresponde al fuente."),
            "SIN BUILD": ("No existe ningun artefacto con su nombre en la carpeta "
                          "generada: nunca se genero en este modelo."),
            "OK": "Coincide con el fuente y no tiene errores en el log.",
        }
        lineas.append(tipos.get(item.estado, ""))

    if ultimo:
        lineas.append("")
        lineas.append(f"Ultimo build: {resumen_run(ultimo)}")
        if ultimo.resultado == "Failed":
            reales = [e for e in ultimo.errores if es_bloqueante(e.texto)]
            if reales:
                lineas.append("Motivo del fallo:")
                lineas += [m.linea() for m in reales[:10]]
            else:
                lineas.append("El log no deja un error de codigo: el build cayo "
                              "sin error de especificacion (proceso abortado).")
    return "\n".join(lineas)


# --------------------------------------------------------------------------- cli

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Compara la KB de GeneXus contra el codigo generado.")
    ap.add_argument("--web", required=True,
                    help=r"carpeta web generada, ej. C:\KB\Data009\web")
    ap.add_argument("--server", default=r"DEVELOPER\SQLEXPRESS")
    ap.add_argument("--database", default="GX_KB_MGJoyas")
    ap.add_argument("--design", action="append", default=[])
    ap.add_argument("--filter", default="")
    ap.add_argument("--all", action="store_true",
                    help="incluye objetos sin artefacto generado")
    ap.add_argument("--no-content", action="store_true")
    a = ap.parse_args()

    s = Settings(server=a.server, database=a.database, web_dir=a.web,
                 design=a.design, content=not a.no_content, filter=a.filter,
                 only_built=not a.all)
    try:
        res = compare(s)
    except ErrorKB as exc:
        sys.exit(f"ERROR: {exc}")

    if not a.all:
        res.items = [i for i in res.items if i.estado != "SIN BUILD"]
        res.contar()

    print()
    print(resumen_run(res.ultimo_run))
    if res.log_error:
        print(res.log_error)
    print()
    w = max([len(i.objeto) for i in res.items] + [20])
    print(f"{'OBJETO':<{w}}  {'TIPO':<16} {'FUENTE':<16} {'GENERADO':<16} "
          f"{'ESTADO':<15} MOTIVO")
    print("-" * (w + 2 + 16 + 1 + 16 + 1 + 16 + 1 + 15 + 1 + 50))
    for i in res.items:
        print(f"{i.objeto:<{w}}  {i.tipo:<16} {i.fuente:<16} {i.generado:<16} "
              f"{i.estado:<15} {i.motivo}")
    print()
    print(f"Total: {res.total}   OK: {res.ok}   Pendientes: {res.pendientes}   "
          f"Desincronizados: {res.desincronizados}   "
          f"Contenido distinto: {res.difiere}   Sin build: {res.sin_build}   "
          f"({res.segundos:.1f} s)")
    if res.apk:
        print(f"APK: {res.apk}")
    if res.ok != res.total:
        sys.exit(1)


if __name__ == "__main__":
    main()
