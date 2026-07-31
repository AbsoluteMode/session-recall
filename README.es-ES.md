

# session-recall

**Memoria compartida para Claude Code y Codex.** Retoma el trabajo de hace un mes sin tener que reexplicarlo: y Claude puede leer lo que Codex resolvió ayer, porque ambos motores alimentan un mismo índice. No es un archivo de resumen que alguien mantiene a mano: son los turnos reales, incluidas las llamadas a herramientas y el razonamiento, buscables por significado.

```console
$ session-recall index
indexed 2175 chunks from changed transcripts

your history: 1052 sessions spanning 168 days, 40,035 searchable fragments
  Claude Code 372 · Codex 680
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

`recall_search`, `grep` y `recent_sessions` también aceptan un opcional `scope_cwd`: pasa tu directorio de trabajo actual para limitar los resultados al repo actual (los worktrees colapsan a la raíz del repo); omítelo para recordatorios entre proyectos. Los resultados clasificados incluyen una marca de tiempo legible por humanos `when_human` junto con la época raw. Cada herramienta MCP acepta un `source` opcional (`claude` o `codex`); omítelo para usar el historial unificado. Los resultados incluyen procedencia como `source=claude` o `source=codex`. Las tres herramientas de descubrimiento también aceptan `on_date` para un solo día o `start_date` / `end_date` inclusivos (`YYYY-MM-DD`) más un `timezone` IANA opcional, para que un agente pueda restringir la recuperación a un día calendario local real en lugar de esperar que una fecha escrita en la consulta semántica afecte la clasificación. Si se omite `timezone`, Session Recall usa la zona horaria de la computadora que ejecuta el servidor MCP.

**Estado:** v1, construido y validado con historial real. La clave del razonamiento de diseño está en [docs/decisions/](docs/decisions/).

## Cómo funciona

Las transcripciones de Claude Code y las sesiones de Codex desde `~/.codex/sessions` y `~/.codex/archived_sessions` comparten el mismo índice.
Solo se incrusta la "superficie" de la conversación: los prompts del usuario y las respuestas de texto del asistente.
Las llamadas a herramientas, resultados, razonamiento y otros datos de traza no se incrustan, pero permanecen accesibles bajo demanda mediante `expand_around` (y `step`) o `grep`. Los archivos de transcripción raw de Codex permanecen locales; solo la superficie de conversación extraída se envía al proveedor de incrustaciones configurado.

Incrustaciones: Voyage `voyage-4-large` (dim 1024) → SQLite (`sqlite-vec` KNN + FTS5, clasificación bm25) → Voyage `rerank-2.5` → top-k. La indexación es incremental (por metadatos de archivo, incluidos inode+tamaño de Codex) y económica en transcripciones en vivo: son solo de agregación, por lo que los fragmentos sin cambios coinciden por hash de contenido y se reutilizan sus vectores: solo los nuevos turnos consultan la API de incrustaciones. Mover una versión de Codex al archivo también reutiliza sus vectores existentes. Cada archivo se indexa en su propia transacción; un archivo fallido se registra y reintenta en la siguiente ejecución, sin abortar el resto. Los subprocesos laterales de Claude (`<session>/subagents/`) y las sesiones de subagentes generados por Codex se omiten intencionalmente: son herramientas internas, no la conversación principal usuario/agente.

Las incrustaciones son intercambiables (Voyage es el predeterminado); el reranker es opcional, y el sistema se degrada elegantemente a KNN + FTS sin él. Se detecta el cambio de proveedor/modelo de incrustación (una huella de incrustación forma parte de la firma de índice de cada archivo) y desencadena una reincrustación limpia en lugar de mezclar espacios vectoriales en silencio.

## Instalación

Dos componentes: un CLI de Python (que también incluye el servidor MCP) y un plugin que lo conecta a tu agente. Presupuesta unos dos minutos más la primera ejecución del índice.

### 1. CLI

```bash
pipx install git+https://github.com/AbsoluteMode/session-recall
session-recall index   # first run walks your whole history; later runs are incremental
```

Eso es todo si tienes un servidor de incrustaciones local en ejecución: consulta [Embedding providers](#embedding-providers) para la configuración local gratuita. Para incrustaciones alojadas de Voyage, exporta una clave primero:

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

### 3. Comprueba que funciona

```bash
session-recall search "something you actually discussed last week"
```

Los resultados con un `score` significan que la búsqueda semántica está activa. En el agente, `claude mcp list` debería mostrar `session-recall ✔ Connected`, y preguntarle sobre trabajo pasado debería activar `recall_search`.

No hay nada más que configurar: el hook `SessionStart` incluido vuelve a indexar en segundo plano a partir de entonces, por lo que el índice se mantiene actualizado con ambos hosts automáticamente.

### Solución de problemas

Empieza aquí: verifica toda la cadena y sale con código distinto de cero cuando algo está realmente roto, por lo que también funciona desde un temporizador:

```console
$ session-recall health
[ok  ] Freshness  2 minutes behind
[warn] Embedder   responded in 5828 ms
                  → slow provider will make indexing crawl
[ok  ] Corpus     1053 sessions (claude 373, codex 680)
[ok  ] Sources    claude, codex present

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
session-recall index --source claude|codex|all   # defaults to all
session-recall search "query" --source codex
session-recall recent --date 2026-07-14          # this computer's timezone
session-recall search "deployment work" --start-date 2026-07-14 \
  --end-date 2026-07-14 --timezone Asia/Yekaterinburg
session-recall grep "exact" --limit 100          # raw scan, no API key needed
session-recall prune                             # drop rows for deleted transcripts
```

`search`, `recent`, `grep` y `prune` aceptan un opcional `--source claude|codex`; omítelo para buscar en ambos. Los filtros de fecha son inclusivos y se puede omitir cualquiera de los límites; la zona horaria predeterminada es la de esta computadora y acepta cualquier nombre IANA. `grep` se limita a 100 coincidencias por defecto.

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

Sin ningún preset configurado, session-recall elige Voyage cuando `VOYAGE_API_KEY` está presente, y de lo contrario busca un servidor local que ya esté escuchando: mejor que predeterminar a un proveedor que está garantizado para rechazar la solicitud. Con una clave configurada, no se ejecuta la búsqueda.

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

Si instalaste el plugin, esto ya está manejado: pasa a la siguiente sección. El hook `SessionStart` incluido funciona en ambos hosts y ejecuta `session-recall index` en segundo plano, y el `--source all` predeterminado actualiza ambos historiales. La indexación es incremental (omite archivos ya indexados por firma), por lo que mantenerse al día es económico.

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
- Las transcripciones de Claude Code junto con las transcripciones activas y archivadas de Codex se leen localmente. Solo el texto superficial de usuario/asistente se incrusta; los datos de traza de herramientas/razonamiento se mantienen fuera de las incrustaciones y se exponen solo mediante expansión raw explícita o grep.
- Los textos de los fragmentos SE ENVÍAN a tu proveedor de incrustación/rerank configurado (Voyage por defecto) — elige un proveedor en el que confíes con tus transcripciones, o apunta el proveedor compatible con OpenAI a un punto de conexión local.
