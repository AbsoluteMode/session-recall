

# session-recall

*Traducción de la comunidad (gracias, @webbrain-one). Puede ir por detrás del
[README en inglés](README.md), que es la referencia; versión en ruso:
[docs/README.ru.md](docs/README.ru.md).*

**Memoria compartida para Claude Code, Codex y Cursor.** Retoma el trabajo de hace un mes sin tener que reexplicarlo: Claude puede leer lo que Codex o Cursor resolvieron ayer, porque los tres alimentan un mismo índice. No es un archivo de resumen que alguien mantiene a mano: son los turnos reales, incluidas las llamadas a herramientas y el razonamiento, buscables por significado.

```console
$ session-recall index
indexed 2175 chunks from changed transcripts

your history: 1053 sessions spanning 168 days, 40,037 searchable fragments
  Claude Code 372 · Codex 680 · Cursor 1
  busiest: sidekey, trend_detection, glitch
```

Entonces, tu agente dejará de preguntarte qué estabas haciendo:

> **tú:** estábamos arreglando el conflicto del token de autenticación entre los dos servicios, ¿en qué quedamos?
>
> **agente:** *(recall_search → expand_around)* Ambos servicios compartían una cuenta OAuth, y el proveedor rota los tokens de refresco por cuenta, por lo que cada refresco invalidaba la copia del otro. Rechazaste el parche de directorio de credenciales compartidas por estar demasiado acoplado, y te decidiste por un servicio gestor propietario de la sesión. Las especificaciones nunca se escribieron: ese era el siguiente paso.

Cinco herramientas a través de MCP:

- `recall_search(query)` — encuentra una discusión pasada **por significado** (no por subcadena). Responde `{"anchors": [...], "degraded": null | str}`; `degraded` se establece cuando el proveedor de incrustaciones es inalcanzable y solo se ejecutó la coincidencia literal, para que el agente pueda indicarlo en lugar de confundir un fallo léxico con un historial vacío.
- `expand_around(session_id, uuid)` — un cursor en el turno raw (llamadas a herramientas, salidas, pensamiento).
- `step(session_id, uuid, direction)` — muévete a un turno adyacente (paso de cursor económico).
- `grep(pattern)` — exploración bajo demanda de subcadenas en **todos** los transcripciones indexadas, incluidos los turnos internos (salida de herramientas, pensamiento) que nunca se convirtieron en fragmentos de búsqueda.
- `recent_sessions()` — las sesiones pasadas más recientes primero (qué está activo, qué tan fresco es el índice).

Bajo demanda (sin autoinyección proactiva en v1). Local y de código abierto.

`recall_search`, `grep` y `recent_sessions` también aceptan un opcional `scope_cwd`: pasa tu directorio de trabajo actual para limitar los resultados al repo actual (los worktrees colapsan a la raíz del repo); omítelo para recordatorios entre proyectos. Los resultados clasificados incluyen una marca de tiempo legible por humanos `when_human` junto con la época raw. Cada herramienta MCP acepta un `source` opcional (`claude`, `codex` o `cursor`); omítelo para usar el historial unificado. Los resultados incluyen la procedencia correspondiente. Las tres herramientas de descubrimiento también aceptan `on_date` para un solo día o `start_date` / `end_date` inclusivos (`YYYY-MM-DD`) más un `timezone` IANA opcional, para que un agente pueda restringir la recuperación a un día calendario local real en lugar de esperar que una fecha escrita en la consulta semántica afecte la clasificación. Si se omite `timezone`, Session Recall usa la zona horaria de la computadora que ejecuta el servidor MCP.

**Estado:** v1, construido y validado con historial real. La clave del razonamiento de diseño está en [docs/decisions/](docs/decisions/).

## Cómo funciona

Las transcripciones de Claude Code, las sesiones de Codex desde `~/.codex/sessions` y `~/.codex/archived_sessions`, y las conversaciones de Cursor comparten el mismo índice. Cursor se lee desde su SQLite local y cada conversación se conserva como una instantánea JSONL normalizada dentro del directorio de datos de session-recall; por eso `expand_around`, `step` y `grep` siguen funcionando aunque Cursor esté cerrado o se desinstale.
Solo se incrusta la "superficie" de la conversación: los prompts del usuario y las respuestas de texto del asistente.
Las llamadas a herramientas, resultados, razonamiento y otros datos de traza no se incrustan, pero permanecen accesibles bajo demanda mediante `expand_around` (y `step`) o `grep`. Las transcripciones originales de Claude/Codex y las instantáneas raw normalizadas de Cursor permanecen locales; solo la superficie de conversación extraída se envía al proveedor de incrustaciones configurado.

La ruta sin configuración usa un modelo ONNX local elegido por idioma → SQLite (`sqlite-vec` KNN + FTS5, clasificación bm25) → top-k. Con una clave de Voyage se usa la ruta alojada de mayor calidad: `voyage-4-large` (dim 1024) → SQLite → `rerank-2.5`. La indexación es incremental y reutiliza los vectores de fragmentos sin cambios. Cada archivo o sesión se indexa en su propia transacción; un fallo se registra y se reintenta sin destruir los datos buenos anteriores.

Las incrustaciones son intercambiables (el modelo local incluido es el predeterminado sin claves); el reranker es opcional, y el sistema se degrada elegantemente a KNN + FTS sin él. Se detecta el cambio de proveedor/modelo de incrustación y la búsqueda semántica se detiene hasta que todos los orígenes pertenezcan al mismo espacio vectorial.

## Instalación

Dos componentes: un CLI de Python (que también incluye el servidor MCP) y un plugin que lo conecta a tu agente. Presupuesta unos dos minutos más la primera ejecución del índice.

### 1. CLI

```bash
pipx install git+https://github.com/AbsoluteMode/session-recall
session-recall index   # first run walks your whole history; later runs are incremental
```

Eso es todo: sin una clave ni un servidor local, se descarga una vez el modelo ONNX incluido y luego se ejecuta en tu máquina. Consulta [Proveedores de incrustaciones](#proveedores-de-incrustaciones) para las demás opciones. Para usar las incrustaciones alojadas de Voyage, exporta una clave primero:

```bash
export VOYAGE_API_KEY=...   # voyageai.com; put the line in your shell profile
```

`pipx` coloca `session-recall` y `session-recall-mcp` en `~/.local/bin`: exactamente donde los manifiestos del plugin los buscan. La primera indexación depende de cuánto historial tengas: minutos para meses de transcripciones, segundos después.

### 2. Plugin

**Claude Code**

```
/plugin marketplace add AbsoluteMode/session-recall
/plugin install session-recall
```

Luego inicia una nueva sesión: los servidores MCP y las habilidades se cargan al iniciar la sesión, no al instalar.

**Codex** — el manifiesto `.codex-plugin/plugin.json` está listo para colocar en un repo local o en tu marketplace personal; consulta la [local plugin installation guide](https://learn.chatgpt.com/docs/build-plugins#install-a-local-plugin-manually). Codex también te pedirá revisar los hooks recién instalados una vez mediante `/hooks`.

**Cursor** — el repositorio incluye un plugin nativo con MCP, skills, comandos, subagente de recall y un hook `sessionStart` en el formato de Cursor:

```bash
cursor-agent plugin marketplace add https://github.com/AbsoluteMode/session-recall.git
```

Después ejecuta `/add-plugin session-recall` dentro de Cursor Agent. Para desarrollo local se puede iniciar con `cursor-agent --plugin-dir /ruta/absoluta/a/session-recall`.
Cursor muestra su aprobación habitual una sola vez para el servidor MCP stdio local; aprueba `session-recall` para iniciar las herramientas.

### 3. Comprueba que funciona

```bash
session-recall search "something you actually discussed last week"
```

Los resultados con un `score` significan que la búsqueda semántica está activa. En el agente, `claude mcp list` debería mostrar `session-recall ✔ Connected`, y preguntarle sobre trabajo pasado debería activar `recall_search`.

No hay nada más que configurar: cada plugin nativo incluye el formato de hook de su host y vuelve a indexar en segundo plano, manteniendo actualizados los tres historiales.

Cursor se detecta automáticamente en su ruta de datos normal de macOS/Linux y no necesita estar abierto. Para un perfil portátil o personalizado, usa `SESSION_RECALL_CURSOR_DB=/ruta/a/User/globalStorage/state.vscdb`. Session Recall abre la base en modo de solo lectura y obtiene una copia coherente mediante la API de backup de SQLite.

### Solución de problemas

Empieza aquí: verifica toda la cadena y sale con código distinto de cero cuando algo está realmente roto, por lo que también funciona desde un temporizador:

```console
$ session-recall health
[ok  ] Freshness  2 minutes behind
[warn] Embedder   responded in 5828 ms
                  → slow provider will make indexing crawl
[ok  ] Vector space  builtin/BAAI/bge-small-en-v1.5/384
[ok  ] Corpus     1054 sessions (claude 373, codex 680, cursor 1)
[ok  ] Sources    claude, codex, cursor present

verdict: AMBER (voyage/voyage-4-large, index at ~/.local/share/session-recall/index.db)
```

La frescura compara la transcripción más nueva en el disco con el turno más nuevo en el índice, por lo que un indexador que se ejecuta en cada sesión y falla cada vez sigue mostrando un retraso: que es exactamente el fallo que de otra forma sería invisible.

| Síntoma | Causa |
|---|---|
| `recall_search` responde con `degraded` establecido | El proveedor de incrustaciones es inalcanzable: solo se ejecutó la coincidencia literal de palabras. Los resultados siguen siendo reales, pero un fallo no prueba nada. |
| El indexador registra `HTTP code 403` con un cuerpo HTML | No es tu clave: un WAF está bloqueando tu IP (común en salidas de VPN y centros de datos). El mismo 403 aparece sin clave alguna. Redirige la salida a otro lado o cambia de proveedor. |
| `Missing dependencies for SOCKS support` | Se ha configurado un proxy SOCKS en el entorno pero `PySocks` no está instalado en ese venv. |
| `recent_sessions` muestra una marca de tiempo antigua | El indexador no ha tenido éxito recientemente. Ejecuta `session-recall index` manualmente y lee la salida. |

### Referencia de CLI

```bash
session-recall index --source claude|codex|cursor|all   # defaults to all
session-recall search "query" --source codex
session-recall recent --date 2026-07-14          # this computer's timezone
session-recall search "deployment work" --start-date 2026-07-14 \
  --end-date 2026-07-14 --timezone Asia/Yekaterinburg
session-recall grep "exact" --limit 100          # raw scan, no API key needed
session-recall prune                             # drop rows for deleted transcripts
```

`search`, `recent`, `grep` y `prune` aceptan un opcional `--source claude|codex|cursor`; omítelo para buscar en el historial unificado. Los filtros de fecha son inclusivos y se puede omitir cualquiera de los límites; la zona horaria predeterminada es la de esta computadora y acepta cualquier nombre IANA. `grep` se limita a 100 coincidencias por defecto.

Para desarrollo, un virtualenv dentro del árbol también funciona:

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/session-recall index
```

Para registrar el servidor MCP manualmente en lugar de usar el plugin:

```bash
claude mcp add session-recall --scope user -- /absolute/path/.venv/bin/session-recall-mcp
```

## Proveedores de incrustaciones

Nada está atado a un solo proveedor. `SESSION_RECALL_EMBED=<preset>` establece punto de conexión, modelo, dimensión y reranker juntos, porque esos cuatro no son elecciones independientes:

| preset | runs | model | dim | reranker |
|---|---|---|---|---|
| `voyage` | hosted, needs a key | `voyage-4-large` | 1024 | `rerank-2.5` |
| `ollama` | **local, free** | `nomic-embed-text` | 768 | — |
| `lmstudio` | **local, free** | `nomic-embed-text-v1.5` | 768 | — |
| `openai` | hosted, needs a key | `text-embedding-3-large` | 1024 | — |
| `builtin-en` | **incluido, gratis** | `bge-small-en-v1.5` | 384 | — |
| `builtin-zh` | **incluido, gratis** | `bge-small-zh-v1.5` | 512 | — |
| `builtin-multi` | **incluido, gratis** | `paraphrase-multilingual-MiniLM-L12-v2` | 384 | — |

Sin ningún preset configurado, session-recall elige Voyage cuando `VOYAGE_API_KEY` está presente, luego busca un servidor local que ya esté escuchando y, si no encuentra ninguno, usa el modelo ONNX incluido. Con una clave configurada, no se ejecuta la búsqueda local.

**Gratuito y local, de principio a fin:**

```bash
ollama pull nomic-embed-text
export SESSION_RECALL_EMBED=ollama
session-recall index
```

**Tu propio punto de conexión** — cualquier servidor que hable `/v1/embeddings` (llama.cpp, vLLM, una puerta de enlace corporativa). Las variables individuales siempre prevalecen sobre el preset, así que mezcla libremente:

```bash
export SESSION_RECALL_EMBED_PROVIDER=openai-compatible
export SESSION_RECALL_EMBED_BASE_URL=https://embeddings.internal/v1
export SESSION_RECALL_EMBED_MODEL=your-model
export SESSION_RECALL_EMBED_DIM=1024
```

Dos cosas que vale la pena saber antes de cambiar:

- **Un embedder diferente necesita su propio índice.** Las tablas vectoriales tienen ancho fijo, por lo que cambiar el modelo o la dimensión significa reconstruir: elimina `~/.local/share/session-recall/index.db` y vuelve a ejecutar `index`. Intentar reutilizar el antiguo ahora falla con un mensaje que dice exactamente eso, en lugar de parecer un embedder muerto.
- **Los presets locales no incluyen reranker**, por lo que la clasificación es solo KNN + FTS: suficiente, pero notablemente más gruesa que la ruta alojada.

Sobre la elección del modelo: `nomic-embed-text` es el predeterminado porque es Apache-2.0 e instala en un solo comando. Existen modelos pequeños más potentes: `jina-embeddings-v5-text-nano` puntúa mucho más alto para su tamaño, pero son **CC BY-NC**, lo que cualquiera que indexe historial de trabajo violaría sin que nunca se lo digan. Si tu uso es genuinamente no comercial, apunta las variables anteriores a uno de ellos. Si trabajas en más idiomas que el inglés, `qwen3-embedding:0.6b` (Apache-2.0) maneja el historial multilingüe mucho mejor que `nomic`.

## Mantener el índice actualizado

Si instalaste el plugin, esto ya está manejado: pasa a la siguiente sección. El hook `SessionStart` incluido ejecuta `session-recall index` en segundo plano, y el `--source all` predeterminado actualiza Claude, Codex y Cursor. La indexación es incremental, por lo que mantenerse al día es económico.

Solo si registraste el servidor MCP manualmente, añade el hook tú mismo en `~/.claude/settings.json`:

```json
"hooks": {
  "SessionStart": [
    { "hooks": [ {
      "type": "command",
      "command": "sr=/abs/path/.venv/bin/session-recall; pgrep -f \"$sr index\" >/dev/null 2>&1 || (VOYAGE_API_KEY=... \"$sr\" index >/tmp/sr-index.log 2>&1 &)"
    } ] }
  ]
}
```

La guardia `pgrep` previene ejecuciones superpuestas; `(\` … \`& )\` se desacopla para que el inicio de sesión no espere. Mantén el hook a nivel de host síncrono: el shell ya envía el indexador a segundo plano, y Codex ignora la extensión `async` de Claude. Un temporizador `launchd`/cron es otra opción. (Local en una máquina es suficiente; un índice del lado del servidor solo tiene sentido en varias máquinas: a costa de la privacidad y la red.)

## meta docs — la memoria del proyecto, escrita

El recordatorio raw responde a "qué se dijo". meta docs responde a las preguntas que los agentes hacen realmente a mitad de tarea: *¿se arregló este error antes? ¿cómo realizo esta acción? ¿por qué se decidió de esta manera?* Un trabajo diario destila el diálogo de cada sesión — solo mensajes del usuario y respuestas finales, nunca el ruido de las herramientas — en documentos vivos dentro de un repositorio git de tu elección:

- `<project>/bugs.md` — errores que se arreglaron realmente: cómo se reconoció, diagnosticó, arregló y demostró que estaba arreglado cada uno;
- `<project>/actions.md` — procedimientos, paso a paso, escritos para que un agente consultado de nuevo pueda seguir la entrada solo;
- `<project>/decisions.md` — decisiones controvertidas: qué se decidió, por qué así, qué se rechazó;
- `USER.md` — un mapa de dónde vive tu información y *cómo encontrarla* (comandos de búsqueda, ubicaciones de almacenamiento — nunca los valores almacenados en sí).

```bash
session-recall metadocs init ~/meta-docs            # every git project; or --projects name…
session-recall metadocs run                         # one pass now
session-recall metadocs enable                      # daily launchd job (default 21:00)
session-recall metadocs status
```

Las ejecuciones son incrementales (marcas de agua por sesión), el destilador es una llamada `claude -p` enjaulada sin todas las herramientas, cada documento se escanea en busca de secretos antes de escribirse (un documento marcado se bloquea, no se enmascara), y cada ejecución termina en un commit git — la revisión es un diff, deshacer es un revert, y compartir la memoria con un equipo es simplemente enviar el repo a algún lugar privado. Los commits permanecen locales a menos que optes por `--push`.

## Privacidad — invariante estricta

Este es un repositorio público. **Solo entra código en él.**

- Datos, índices, transcripciones raw, incrustaciones → `~/.local/share/session-recall/`, **fuera del árbol del repo**. Físicamente no pueden ser commitados.
- Claves API → solo en el entorno (`VOYAGE_API_KEY`); `.gitignore` bloquea `.env`.
- Pruebas → solo fixtures sintéticos, nunca una porción real de una sesión.
- Las transcripciones de Claude Code, las transcripciones activas y archivadas de Codex y el SQLite de Cursor se leen localmente. Las instantáneas normalizadas de Cursor permanecen en el directorio de datos. Solo el texto superficial de usuario/asistente se incrusta; las herramientas y el razonamiento quedan fuera de las incrustaciones.
- Los textos de los fragmentos se envían al proveedor configurado. Sin claves, el proveedor incluido permanece completamente local; si eliges Voyage u otro endpoint alojado, usa uno en el que confíes.
