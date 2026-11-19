# GXCheck — Check build/generation Genexus

Aplicación de escritorio que compara lo que **dice la KB de GeneXus** contra el
**código realmente generado en disco** y explica **por qué falló el último
build**. Sirve para cualquier KB y cualquier modelo (web, Android o ambos).

```
KB (SQL Server)  ──┐
Carpeta generada ──┼──►  GXCheck  ──►  estado por objeto
OutputLogs       ──┘                 ├─►  diagnóstico del build
                                     └─►  contexto para el chat
```

---

## Para qué

GeneXus decide si regenerar un objeto mirando si su **salida registrada** es más
nueva que el objeto. Cuando esa lógica falla (o el build se cae a mitad de
camino), el síntoma típico es:

- el panel se ve viejo en el navegador aunque "el build dio OK";
- un F5 dice que no hay nada para compilar, pero el `.js` no tiene los cambios;
- el build falla con un mensaje que no dice **qué objeto** lo rompió.

Verificar eso a mano es abrir la KB, mirar fechas una por una, buscar el `.cs`,
buscar el `.js` y leer un `Build.log` de miles de líneas. GXCheck hace las tres
cosas de una pasada, por objeto, y deja el resultado en una tabla que se puede
filtrar, imprimir y consultarle a un agente.

El caso que originó la herramienta: el WebPanel `AltaPedidosWW` estaba bien en
la KB, pero el `.cs` regenerado tapaba un `.js` viejo, así que un script que
miraba la fecha **máxima** decía `OK` mientras el navegador seguía mostrando
textos eliminados (`Descuento Reales` en vez de los títulos nuevos).

---

## Qué hace

| Función | Detalle |
|---|---|
| **Compara fechas** | Versión vigente en la KB contra el **mínimo** de los artefactos generados con ese nombre. |
| **Compara contenido** | Textos literales del layout en la KB (`titleExp`, `caption`, `text`, …) buscados dentro del `.cs`/`.js`. |
| **Detecta desincronización** | Si la KB registró una salida **más nueva** que el archivo: GeneXus cree que está al día y el F5 lo saltea. |
| **Diagnostica el build** | Lee `OutputLogs\Build.log`: comando, resultado, fase donde cortó, errores y advertencias atribuidas por objeto. |
| **Explica cada fila** | Popup con qué se comparó, por qué quedó en ese estado y qué mensajes del build lo mencionan. |
| **Exporta** | HTML listo para imprimir + CSV por categoría, a `%TEMP%\gxcheck`. |
| **Regenera pendientes** | Por MSBuild, solo los `PENDIENTE`, con vista previa de la fuente y del generado. |
| **Avisa si falta reorganizar** | Compara el esquema de la KB con el de la base de la aplicación. |
| **Se lo cuenta a un agente** | Chat con opencode embebido, con el estado del análisis como contexto. |
| **Corre en CI** | `gxcheck.py` funciona sin interfaz, imprime la tabla y sale con código 1 si algo no está OK. |

### Estados

| Estado | Significado |
|---|---|
| `OK` | El generado es igual o más nuevo que la fuente y el contenido coincide. |
| `PENDIENTE` | Hay artefacto generado, pero es **más viejo** que la fuente en la KB. |
| `DESINCRONIZADO` | El archivo es más nuevo que la fuente, pero la KB registra una salida posterior: GeneXus lo da por generado y no lo vuelve a generar. |
| `DIFIERE` | Las fechas dan bien, pero al generado le faltan (o le sobran) textos del layout. |
| `SIN BUILD` | El tipo de objeto no genera código propio (Image, Attribute, Domain, Folder, Module, ThemeClass…). |
| `?` | No se pudo determinar la fecha de la fuente. |

---

## Cómo funciona

### De dónde saca los datos

| Fuente | Qué se lee |
|---|---|
| **SQL Server** | `sqlcmd -S <instancia> -E -d <base>`. Tablas `Entity`, `EntityVersion`, `EntityVersionComposition`, `ModelEntityOutput`, `EntityType`, `MODEL`. Sin drivers: no hace falta pyodbc ni ODBC de Python. |
| **Carpeta generada** | La elegida en la UI (`…\<KB>\<Modelo>\web`): `.cs`, `.js`, `.svc`, `gxmetadata\*`, `themes\*`. |
| **Logs** | `…\<KB>\OutputLogs\Build.log`. |

### Reglas de comparación

1. **Fuente** = timestamp de la última versión del objeto en la KB
   (`Entity.EntityLastVersionId` + `EntityVersionTimestamp`).
2. **Partes** = si el objeto tiene partes (WebForm, Events, Rules), se toma la
   más nueva de todas. Los objetos de diseño indicados a mano
   (DesignSystem / MasterPage) cuentan como fuente para los objetos visuales.
3. **Generado** = **mínimo** mtime entre todos los artefactos encontrados con ese
   nombre. Es a propósito: un `.cs` recién regenerado no debe tapar un `.js`
   viejo.
4. **Estado de build** = `ModelEntityOutput` del modelo de la carpeta
   (`MODEL.model_path` → `model_id`), no del modelo más reciente de la base.

### Artefactos que busca

- `web\<objeto>.cs`, `.js`, `.svc`
- `type_<SDT>.cs`, `<objeto>_bc.cs`
- Objetos de módulo: `modulo.objeto.cs` (se indexa también el último segmento
  del nombre)
- DesignSystem y Theme: `web\themes\[Modulo.]<objeto>.json`
- `gxmetadata\<objeto>.*` para el resto de los tipos, salteando
  `.properties.json`, `.panel.android.*`, `.panel.ios.*`, `.externalobjects` y
  `.usercontrols`
- Se saltean las carpetas de framework (`bootstrap`, `DVelop`,
  `Unanimo_chameleon`, `RAMP`, `UserControls`, `node_modules`, etc.)

### Objetos móviles (SDPanel)

Un SDPanel no genera código en la carpeta `web`: su salida es metadata de Android
(`gxmetadata\<objeto>.apis`, `.proc`, `.ref`, `.panel.android.json`), que la
generación web **nunca reescribe**. Compararla contra la fecha de la fuente lo
dejaba `DESINCRONIZADO` para siempre, y regenerarlo no lo arreglaba: el motor solo
vuelve a escribir esa metadata en un build de Android.

Regla aplicada:

| Situación | Estado |
|---|---|
| El SDPanel produce su código web (el `.cs` del *level detail*) | Se compara normalmente |
| No produce código web en este modelo | `SIN BUILD` |

Caso medido en el ambiente `Data009`:

| Objeto | Antes | Después | Motivo |
|---|---|---|---|
| `MGJoyasSD` | `DESINCRONIZADO` | `OK` | tiene `mgjoyassd_level_detail.cs` reciente |
| `CheckoutCarrito` | `DESINCRONIZADO` | `SIN BUILD` | el modelo no genera salida web para él |
| `PanelMPCheckout` | `DESINCRONIZADO` | `SIN BUILD` | ídem |

Para regenerar de verdad un SDPanel hace falta que el ambiente tenga el generador
de **Smart Devices** (Android/iOS) con JDK y SDK configurados; eso queda fuera del
alcance de GXCheck.

### Comparación de contenido

Solo para **WebPanel** y **Transaction** (los únicos con WebForm legible; el
SDPanel no guarda layout en texto). Se extraen los literales del layout y se
exige que aparezcan en el `.cs` **o** en el `.js`. Si el objeto está viejo y el
generado todavía conserva textos que ya no están en la KB, también se marca.

### Diagnóstico del build

`parsear_build()` lee el log (ANSI/cp1252) y arma *runs* por comando
(`Build All`, `Rebuild All`, `Build With These Only`, `Build With This Only`),
con inicio, fin, resultado y mensajes `ERROR`/`WARN`. Los objetos se atribuyen
por el nombre entre comillas del mensaje; las advertencias `spc####` se asocian a
objeto y línea. Los `error: Warning: …` (avisos de Android mal prefijados) se
descartan como bloqueantes.

### Límites conocidos

- Si el proceso de GeneXus **se cae** (`ProcessExitedException`), el log deja el
  *qué* (se abortó el generador y en qué fase) pero **no la causa raíz**: no hay
  stack trace. Ahí hay que mirar el Output del IDE en ese momento.
- La comparación de contenido no cubre SDPanel ni objetos que no sean visuales.
- Los SDPanel solo se regeneran si el modelo tiene generador de Smart Devices
  (Android/iOS); si no, quedan `SIN BUILD` y no hay nada que generar.
- `sqlcmd` es obligatorio; sin cliente de SQL Server instalado no hay lectura de
  la KB.
- Las instancias del desplegable salen del registro de Windows: la app está
  pensada para Windows.

---

## Requisitos

| Requisito | Obligatorio | Nota |
|---|---|---|
| Windows | sí | Registro para listar instancias, `CREATE_NO_WINDOW`, icono `.ico`. |
| Python 3.11+ | sí | Probado con **3.14**. |
| `flet` | sí | Probado con **1.1.2**. |
| `sqlcmd` | sí | Viene con SQL Server / Client Tools. |
| SQL Server con la KB | sí | Autenticación de Windows (`-E`). |
| `opencode` en el `PATH` | no | Solo para el chat. Sin él, todo lo demás anda igual. |
| Agente `genexus` y skill `nexa` en opencode | no | Recomendado para el chat: mejor criterio y conocimiento de la KB. |
| `Pillow` | no | Solo para rearmar `Icono.ico` si cambiás `Icono.png`. |
| GeneXus instalado | no | Solo para **Regenerar pendientes**: usa sus tareas MSBuild. |

### Recursos que toma de la instalación de GeneXus

Solo los usa el botón **Regenerar pendientes**. GXCheck no abre el IDE: le pide
el trabajo al motor de GeneXus por MSBuild. Verificado contra GeneXus 18
(`C:\Program Files (x86)\GeneXus\GeneXus18`).

| Recurso | Qué es | Para qué lo usa |
|---|---|---|
| Carpeta de instalación (`…\GeneXus\GeneXus18`) | Raíz del producto | Ubicar los archivos de abajo. Se toma de `GXCHECK_GX`, `GX_PROGRAM_DIR`, `GX_INSTALL` o `GX_HOME`; si no están, busca `GeneXus*` en `Program Files` y elige la versión más alta que tenga las tareas. |
| `Genexus.Tasks.targets` | Archivo MSBuild con **112** declaraciones `<UsingTask>` y sin imports | Se importa en el `.msbuild` generado: es lo que registra las tareas. |
| `Genexus.MsBuild.Tasks.dll` | DLL de .NET Framework 4.0 (230 KB) | Implementa las tareas que se usan: `OpenKnowledgeBase`, `SetActiveEnvironment`, `SpecifyOneOnly`, `GenerateOnly` (regeneración) y `WriteKnowledgeBaseSchema`, `WriteDatabaseSchema` (chequeo de esquema). |
| `Artech.*.dll` (`Artech.MsBuild.Common`, `Artech.Genexus.Common`, `Artech.Architecture.*`, `Artech.Udm.*`, …) | Motor, modelos y utilidades de GeneXus | Dependencias de la DLL anterior; las resuelve el propio motor al ejecutar. |
| `gxio.dll`, `gxioCTree.dll`, `udmgxio.dll` | Capa de acceso a KB/IO de GeneXus | Es la razón de que MSBuild tenga que ser de 32 bits: con el de 64 el import falla con «Could not load file or assembly 'gxio'». |
| `GeneXus.exe` | El IDE | **No se ejecuta nunca.** Solo se consulta si está corriendo, para avisar que puede tener la KB tomada. |
| `Templates`, `ObjectDefinitions`, `Packages`, `gxnet`, `gxjava`, `gxandroid`… | Plantillas, definiciones de objetos, patrones y generadores | Los usa el motor al especificar y generar. GXCheck no los lee ni los toca. |

Lo que **no** se usa: la tarea `CompareSchemas`. Requiere el assembly
`Mvp.Xml.Common`, que no viene con GeneXus 18 (solo está `Mvp.Xml.dll`, que es
otro). Por eso la comparación de esquemas se hace en Python.

Fuera de la instalación:

| Recurso | Para qué |
|---|---|
| `MSBuild.exe` de .NET Framework 4.0, **32 bits** (`C:\Windows\Microsoft.NET\Framework\v4.0.30319`) | Ejecuta el script. El de `Framework64` no sirve por `gxio`. |
| `sqlcmd` | Leer la fuente del objeto (columna izquierda) y todo el resto de la comparación. |
| La KB (carpeta + base en SQL Server) | Es lo que se regenera. |
| La base de la aplicación (Data Store del ambiente) | Se **lee** para el chequeo de esquema (ingeniería inversa); no se modifica. |
| `…\OutputLogs\Build.log` | Diagnóstico del último build. |

---

## Instalación

```
pip install flet
```

Opcional, si querés cambiar el icono:

```
pip install pillow
```

La primera ejecución de Flet prepara el cliente de escritorio (una sola vez,
tarda un poco). No hay instalador: son dos `.py` que se ejecutan desde la
carpeta.

---

## Uso — interfaz

```
python check_build.py
```

Ventana de 1280×900 (mínimo 1000×640). Guarda los últimos valores usados en
`%APPDATA%\gxcheck.json` y abre con el mismo color, modelo y agente de la vez
anterior.

### 1. Configurar

| Campo | Qué poner |
|---|---|
| **Instancia SQL Server** | Se elige de la lista (sale del registro) o se escribe, ej. `SERVIDOR\SQLEXPRESS`. |
| **Base de datos de la KB** | El botón ⟳ lista las bases de la instancia. Típicamente `GX_KB_<Proyecto>`. |
| **Explore Target Environment Directory** | La carpeta de salida del entorno: `…\<KB>\<Modelo>\web`. De ahí salen el modelo y la raíz de la KB (el botón ? explica la estructura). |
| **Objetos de diseño** (opcional) | DesignSystem o MasterPage separados por coma, ej. `MGJoyasSDDSAndroid`. Su última versión cuenta como fuente de los objetos visuales. |
| **Filtro por nombre** (opcional) | Para comparar solo un subconjunto, ej. `altapedidos`. |
| **Comparar textos del layout** | Activado: además de fechas, compara contenido. Apagado: solo fechas (más rápido). |
| **Ocultar objetos sin build** | Activado por defecto, para que el ruido de Image/Domain/Attribute no tape lo importante. |

Después: **Iniciar comparación**. El progreso va indicando la etapa (objetos,
salidas registradas, `Build.log`, layouts de la KB, comparación).

### 2. Leer el resultado

Arriba de las tablas aparece el **último build**: comando, resultado, horario y,
si falló, los errores reales del log.

Dos paneles con las mismas columnas: **lupa**, `OBJETO`, `TIPO`, `FUENTE`,
`GENERADO`, `ESTADO`.

- **izquierda — OK**
- **derecha — Pendientes** (todo lo que no está OK)

La **lupa** va primera, así se ve aunque el panel quede angosto, y abre el
popup de detalle: qué se comparó, por qué quedó así y qué mensajes del build la
mencionan. Las fechas van como `dd/mm HH:MM`.

Cada panel tiene su **propio filtro** (por objeto, tipo o estado) y scroll
independiente. Las filas se pueden seleccionar (resalte).

Los mensajes de estado, arriba de todo, van en **verde**; solo los errores en
**rojo**.

### 3. Regenerar pendientes

El botón **Regenerar pendientes**, al lado de *Iniciar comparación*, está
deshabilitado hasta que haya un diagnóstico con objetos regenerables. Se habilita
si quedó algún objeto en estado `PENDIENTE`, `DESINCRONIZADO` o `DIFIERE`, y
muestra la cantidad.

Dentro de la ventana hay **tres casillas** para elegir qué estados regenerar
(vienen las tres marcadas):

| Casilla | Estado |
|---|---|
| Pendientes | `PENDIENTE` |
| Desincronizados | `DESINCRONIZADO` |
| Con diferencias | `DIFIERE` |

Debajo se muestra «Se regenerarán N objeto(s)» y se recalcula al marcar o
desmarcar. Si destildás todo, el botón Regenerar se deshabilita.

Antes de abrir la ventana verifica **el esquema de la base** (la primera vez de
cada comparación): genera el esquema que espera la KB y el real de la base, y
los compara. Si faltan tablas o atributos, el popup lo avisa —es señal de que
hace falta reorganizar—. Las fórmulas no cuentan (no existen como columna).
Contra una base cloud el chequeo puede tardar ~1 minuto; contra una local es
rápido.

**Solo avisa: no reorganiza.** La reorganización modifica la base de la
aplicación y se ejecuta desde el IDE, con su respaldo y sus pasos. Esta función
nunca la corre.

Se abre una ventana con dos columnas, la salida del build y una barra de avance:

- **izquierda** — la fuente del objeto tal como está en la KB (SQL Server).
- **derecha** — el archivo generado en disco.
- **centro, arriba** — la barra y `3 de 12 · <objeto>` según va pasando cada
  pendiente.
- **abajo** — «Salida de GeneXus»: las últimas líneas de MSBuild en vivo
  (specification, generating to, warnings).

Botones: **Cerrar** y **Regenerar**. Mientras corre, Regenerar queda
deshabilitado. **Al terminar bien la ventana se cierra sola y vuelve a
comparar**, así ves de inmediato si quedó algo pendiente; si falla, queda
abierta con **Abrir log** y **Cerrar**.

Al lado del botón principal hay un ícono **📄** que abre el último log de
regeneración (`%TEMP%\gxcheck\generar_*.log`).

Qué tarea se usa por objeto:

| Detalle | Valor |
|---|---|
| Tarea | `SpecifyOneOnly`, una por objeto (no acepta listas) |
| `ForceRebuild` | `true` siempre: sin forzar, GeneXus saltea el objeto si lo cree al día |
| Modelo | `Model="<id>"` (no por nombre: hay ambientes homónimos) |

Y un único `GenerateOnly` al final, con el mismo `Model`. **No compila**: deja el
`.cs`/`.js` nuevo en la carpeta del ambiente.

#### Restricción opcional de KBs

La generación **escribe** en la KB (registra la salida) y en la carpeta del
ambiente; no modifica la base de la aplicación ni el esquema (solo lo lee para el
chequeo). El diálogo muestra la KB y el ambiente antes de ejecutar, y el botón
**Regenerar** es la confirmación.

Si querés limitarla a una KB de prueba, definí esta variable **antes de abrir la
app**:

```
setx GXCHECK_KB_PERMITIDAS "C:\KBs\KB_PRUEBAS"
```

Con la variable cargada, toda KB fuera de esa lista queda bloqueada (y las
carpetas con nombre parecido también: `KB_PRUEBAS_2` no entra por
`KB_PRUEBAS`). Sin la variable, se ejecuta sobre la KB que esté en el formulario.

Requiere GeneXus instalado (usa `Genexus.Tasks.targets`) y MSBuild 4.0 de 32
bits. La KB no debe estar abierta en el IDE.

### 4. Imprimir / exportar

El menú **Imprimir / exportar** genera por categoría un **HTML** listo para
imprimir y un **CSV** al lado, en `%TEMP%\gxcheck`, y abre el HTML en el
navegador:

- OK
- Pendientes
- Desincronizados
- Contenido distinto
- Sin build
- Todos los estados

El CSV sale con `;` como separador y BOM UTF-8, para que Excel lo abra bien.

### 5. Colores

Botón de paleta: 12 combinaciones. Las oscuras (`Negro`, `Ónix`, `Carbón`,
`Plomo`, `Grafito`) pintan el lienzo oscuro y aclaran los textos de alrededor;
las tablas y el chat quedan siempre blancos con texto oscuro para que se lean.

### 6. Chat con opencode

La columna derecha es un **chat** contra `opencode run`, no un terminal. Se
habilita cuando hay una comparación con resultados.

> **Recomendado:** tener instalados en opencode el **agente `genexus`** y la
> **skill `nexa`**. El agente aporta el criterio de GeneXus (objetos, ambientes,
> build) y la skill el conocimiento específico de la KB y de la sintaxis. Sin
> ellos el chat funciona igual, pero las respuestas son más genéricas.

- **Primer mensaje**: se antepone automáticamente el estado del análisis
  (resumen + ruta a `estado.md`), para que el agente sepa de qué se habla sin
  explorar la KB.
- **Siguientes**: `--continue`, así mantiene el hilo.
- Cada comparación nueva **reinicia el hilo** (el contexto viejo ya no sirve).
- Se puede elegir **modelo** (`opencode models`) y **agente** (built-ins +
  `opencode.jsonc` + `~/.config/opencode/agents/*.md`).
- La respuesta se lee del `stdout` del CLI en bloques: se ve ir escribiendo, con
  botón **Detener**.
- **Abrir consola aparte (TUI)**: abre `opencode` en una consola real, en la
  carpeta del entorno, para lo que necesita interactividad.

El detalle del análisis queda en dos archivos junto al programa:

| Archivo | Contenido |
|---|---|
| `estado.md` | Resumen corto: modelo, KB, totales, pendientes/desincronizados/diferentes con motivo. Es lo que recibe el agente. |
| `estado_completo.md` | Detalle completo por objeto, incluidos OK y SIN BUILD. |

Los dos se **reescriben enteros en cada comparación**: se truncan al empezar y se
vuelven a escribir al terminar, así nunca queda mezclado el estado de una corrida
con el de la anterior. Si la comparación falla, quedan vacíos (no viejos).

> El contexto inline es corto a propósito: `cmd.exe` corta la línea de comandos
> en **8191 caracteres**.

---

## Línea de comandos (CI)

El motor funciona solo, sin Flet:

```
python gxcheck.py --web C:\PROYECTOS\KBs\MGJoyas\Data009\web --filter altapedidos
python gxcheck.py --web ... --no-content --all
```

| Opción | Default | Qué hace |
|---|---|---|
| `--web` | — (obligatorio) | Carpeta generada, ej. `C:\KB\Data009\web`. |
| `--server` | `DEVELOPER\SQLEXPRESS` | Instancia SQL Server. |
| `--database` | `GX_KB_MGJoyas` | Base de la KB. |
| `--design` | — | Objeto de diseño; se puede repetir. |
| `--filter` | vacío | Subcadena por nombre de objeto. |
| `--all` | no | Incluye los `SIN BUILD` (por defecto se ocultan). |
| `--no-content` | no | Salta la comparación de textos (solo fechas). |

Salida: tabla por consola con el resumen del último build, el conteo por estado y
el APK generado si es un modelo Android. **Código de salida 1** si algún objeto
no está OK, 0 si todo está OK — así un pipeline puede cortar.

---

## Qué archivos hay

| Archivo | Rol |
|---|---|
| `check_build.py` | Interfaz Flet: formulario, paneles, lupa, exportación, chat. |
| `gxcheck.py` | Motor de comparación + diagnóstico de build. También CLI. |
| `generar.py` | Regeneración de pendientes por MSBuild + chequeo de esquema. |
| `README.md` | Este documento. |
| `estado.md` | Estado de la última comparación (lo lee el agente). |
| `estado_completo.md` | Detalle completo por objeto. |
| `Icono.png` / `Icono.ico` | Icono de la ventana y logo del encabezado (el `.ico` se rearma solo desde el PNG). |

---

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| «La carpeta no parece generada por GeneXus (sin .cs)» | Apuntaste a la carpeta equivocada: tiene que ser `…\<Modelo>\web`. |
| «La base no devolvió objetos GeneXus» | Instancia o base equivocadas, o sin permisos de lectura. |
| Todo `SIN BUILD` | El filtro/tipo de objetos no corresponde, o el modelo de la carpeta no es el que generó esos archivos. |
| Un objeto queda `PENDIENTE` para siempre | Revisá la lupa: puede ser un objeto de GAM que no se regenera en ese modelo (caso `OfflineEventReplicator`). |
| El chat dice «No encuentro 'opencode' en el PATH» | Instalá opencode o abrilo desde una consola donde esté disponible. |
| El popup avisa que la base está desactualizada | Hay tablas o atributos de la KB que no están en la base: reorganizá desde el IDE (GXCheck solo avisa). |
| El objeto sigue `PENDIENTE` después de regenerar | Revisá la lupa: puede ser un objeto con `GenerateObject = false` (GAM) o un nombre ambiguo entre dos tipos. |
| La ventana no entra en pantalla | Es de 1280×900; en 1366×768 el formulario superior debería compactarse (pendiente conocido). |

---

## Estado del proyecto

Funcional y completo para el objetivo. Verificación de referencia: KB
`GX_KB_MGJoyas`, **3143 objetos → OK 478, Pendientes 1, Sin build 2664** en
~29 s con comparación de textos activada.

Pendientes y decisiones están en `.memory/TASKS.md` y
`.memory/DECISIONS.md` (esa carpeta es local y no se versiona).
