# CLIpping · Clip Factory

Base local, modular y gratuita para producir contenido corto. El objetivo es
analizar videos, encontrar y puntuar momentos, recortar, convertir a vertical,
subtitular y validar los resultados sin modificar los originales.

**Estado: fase 1.** Esta entrega prepara el proyecto y obtiene metadata con
FFprobe. Todavía no detecta momentos, transcribe, recorta ni exporta videos.
Las instrucciones de las cinco skills describen esos contratos futuros; no
equivalen a motores implementados.

## Inicio en macOS / Apple Silicon

Usa Python 3.10 o posterior; se recomienda 3.11 o posterior. Comprueba la
versión: el Python incluido con un Mac puede ser demasiado antiguo.

```zsh
cd clip-factory
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Si `python3 --version` es inferior a 3.10, selecciona primero un intérprete
compatible y úsalo para crear el entorno. No se instala Python automáticamente.

FFmpeg y FFprobe son programas externos. Si utilizas Homebrew, puedes
instalarlos manualmente:

```zsh
brew install ffmpeg
```

La [documentación de Homebrew](https://docs.brew.sh/Installation) explica su
instalación en Apple Silicon. `doctor` sólo inspecciona el entorno: no instala
paquetes, no descarga modelos y no utiliza servicios externos.

```zsh
python -m clip_factory doctor
python -m clip_factory init
python -m clip_factory --profile mac_m1 config
```

La instalación inicial necesita acceso a los repositorios de paquetes. Después,
los comandos de esta fase procesan archivos locales sin llamadas a APIs.

## Comandos disponibles

Todas las opciones globales se colocan **antes** del subcomando. Consulta la
ayuda para ver los argumentos admitidos:

```zsh
python -m clip_factory --help
python -m clip_factory probe --help
```

- `doctor`: informa Python, plataforma y disponibilidad de dependencias;
  `doctor --json` produce un informe estructurado. FFprobe es necesario para
  inspeccionar videos; FFmpeg prepara las siguientes fases de edición.
  El diagnóstico completo exige ambos y devuelve `FAIL` si falta alguno,
  aunque `probe` puede funcionar sólo con FFprobe.
- `init`: crea los directorios de trabajo y el esquema SQLite si faltan.
- `config`: muestra la configuración efectiva en JSON.
- `probe VIDEO`: inspecciona duración, streams, resolución, FPS, codecs y audio.
- `analyze VIDEO`: alias de la inspección de metadata en esta fase. No analiza
  intensidad de audio, escenas, texto ni momentos.

Ejemplos, desde la raíz de `clip-factory` y con el entorno activado:

```zsh
python -m clip_factory probe data/input/video.mp4
python -m clip_factory analyze data/input/video.mp4
python -m clip_factory --profile mac_m1 probe data/input/video.mp4
python -m clip_factory probe data/input/video.mp4 --output data/working/informe.json
python -m clip_factory probe data/input/video.mp4 --output -
python -m clip_factory --config config/default.yaml config
```

Sin `--output`, la metadata se guarda en un archivo nuevo en `data/working/`,
con nombre `<video>-<identificador>.probe.json`. `--output -` envía el JSON a la
salida estándar. Las inspecciones quedan registradas en SQLite y en el log.
Un archivo JSON existente no se sobrescribe. Los destinos explícitos deben
terminar en `.json`, permanecer dentro de la raíz y fuera de input. Un destino
relativo se interpreta respecto a la raíz; `VIDEO` relativo se interpreta
respecto al directorio desde el que ejecutas el comando.

El JSON incluye `scope: metadata_only` y marca los análisis avanzados como
`not_run`. Una validación básica fallida conserva la metadata para diagnóstico,
registra el fallo y devuelve código de salida 1; no se presenta como análisis
correcto ni como QC completo.

`detect`, `clip`, `process` y `qc` pertenecen a fases posteriores y no son
funciones operativas de esta entrega.

## Configuración

`config/default.yaml` define los valores base y
`config/profiles/mac_m1.yaml` aplica ajustes para un MacBook Air M1. Las rutas
de datos se resuelven respecto a la raíz del proyecto; no dependen de una ruta
personal hardcodeada.

La precedencia es: valores incorporados → `config/default.yaml` → archivo
`--config` → perfil `--profile`. Las claves desconocidas, rutas inseguras y
valores incompatibles se rechazan. Las opciones principales son `paths`,
`media.ffprobe_bin`, `media.ffmpeg_bin`, `media.probe_timeout` y
`logging.level`. `runtime.threads` y `runtime.device` reservan ajustes para
los motores de procesamiento posteriores.

Las operaciones importantes se registran siempre, incluso si se configura un
nivel superior a INFO, para conservar el historial obligatorio de resultados.

Usa `--root DIR` para indicar otra raíz. También puedes exportar
`CLIP_FACTORY_ROOT`; la opción explícita `--root` tiene prioridad. La raíz
contiene la configuración, los datos, la base SQLite y los logs.

```zsh
export CLIP_FACTORY_ROOT="$PWD"
python -m clip_factory --profile mac_m1 config
```

`.env.example` es una referencia. La aplicación **no carga `.env`
automáticamente** ni necesita claves de API. Revisa `config` antes de procesar
si has cambiado rutas o perfiles. La configuración reserva parámetros de
edición y puntuación para futuras fases; su presencia no activa esas funciones.

## Archivos y preservación

- `data/input/`: originales inmutables. También se protegen videos proporcionados
  desde fuera de este directorio.
- `data/working/`: temporales y metadata de análisis.
- `data/clips/`, `data/exports/`, `data/thumbnails/`, `data/subtitles/`:
  resultados de las futuras etapas.
- `data/archive/`: archivo local reservado; no se mueve input automáticamente.
- `database/clip_factory.db`: historial local de inspecciones.
- `logs/`: operaciones en JSON Lines con fecha UTC, archivo, operación,
  duración, resultado y error si existe.

La inspección lee el original mediante FFprobe. Las escrituras se destinan a
metadata, base de datos y logs. Se rechazan destinos dentro de input o que
coincidan con el original, incluso mediante enlaces simbólicos. La fase 1 no
borra ni transforma videos. Conserva tus respaldos habituales.

Los scripts auxiliares no instalan software:

```zsh
python scripts/doctor.py
python scripts/setup.py
python scripts/clean.py
```

`setup.py` inicializa directorios y SQLite; no es el instalador del paquete.
`clean.py` muestra una vista previa de archivos `*.tmp` elegibles en working;
no elimina los informes `.json`.
El borrado exige `--apply` explícito, se limita a working y rechaza enlaces
simbólicos. Consulta `python scripts/clean.py --help` antes de utilizarlo.

## Arquitectura

El paquete `src/clip_factory/` separa responsabilidades:

- `cli/`: interfaz de terminal con `argparse`.
- `config/`: lectura segura, combinación y validación de YAML.
- `database/`: modelos y repositorio SQLite.
- `media/`: ejecución de binarios, probe y contratos de validación.
- `utils/`: rutas, escrituras seguras y logging.
- `analysis/`, `scoring/`, `editing/`, `pipeline/`: contratos y módulos
  reservados para etapas posteriores.

Los comandos pendientes responden con un mensaje y código de salida 2;
los módulos reservados contienen documentación y aún no exponen motores.
Los futuros motores de transcripción, escenas y
edición se conectarán por interfaces independientes; cambiar Whisper no debe
obligar a reescribir el resto del sistema. La CPU es la base. Metal/MPS será una
optimización opcional cuando el motor elegido lo permita; CUDA no es requisito.

Las cinco skills del proyecto están en `.codex/skills/`: `trend-researcher`,
`video-analyzer`, `clip-scorer`, `shorts-editor` y `quality-control`.
`AGENTS.md` define las reglas del desarrollo.

## Dependencias y coste

Objetivo: **$0 de coste recurrente adicional**. Esta base no usa APIs de pago,
suscripciones, créditos de IA ni procesamiento cloud. El almacenamiento y el
tiempo de cómputo corresponden al equipo local.

- **PyYAML** es la única dependencia de ejecución de Python. La biblioteca
  estándar no incluye un lector YAML y la estructura solicitada usa YAML.
  Se utiliza carga segura. El proyecto publica distribuciones para macOS ARM64:
  [PyYAML en PyPI](https://pypi.org/project/PyYAML/).
- **pytest**, sólo en el extra `dev`, organiza pruebas unitarias e integración
  y permite omitir integraciones cuando faltan binarios externos. No forma
  parte del procesamiento de video:
  [documentación oficial de pytest](https://docs.pytest.org/en/stable/).
- **setuptools**, dependencia de construcción, permite instalar el paquete
  desde `pyproject.toml` y crear la entrada de terminal `clipping`. No es un
  motor de procesamiento ni requiere una GPU:
  [documentación oficial de setuptools](https://setuptools.pypa.io/en/latest/userguide/quickstart.html).
- **FFprobe / FFmpeg**, programas locales externos, proporcionan inspección
  multimedia y la futura edición. FFprobe ofrece salida JSON sin incorporar
  otro SDK de video: [documentación oficial](https://ffmpeg.org/ffprobe.html).
- **SQLite, JSON, argparse, logging, pathlib y dataclasses** proceden de la
  biblioteca estándar; no se añaden paquetes para estas funciones.

No se instalan Whisper, faster-whisper, OpenCV, librosa, PySceneDetect ni yt-dlp
en fase 1. Cada futura dependencia deberá aportar una necesidad concreta,
compatibilidad con Apple Silicon y una alternativa por CPU. Si no existe una
opción local gratuita adecuada, se documentará la limitación y se conservará
una interfaz sustituible.

## Validación

```zsh
python -m pytest
```

Validación de la entrega inicial (13 de octubre de 2026, macOS ARM64):
**107 pruebas aprobadas, sin omisiones**, incluidas tres integraciones con
FFmpeg/FFprobe reales. `doctor` devolvió `PASS`. La prueba completa ejecutó la
CLI instalada desde fuera del proyecto, comprobó JSON, SQLite y logs, y verificó
el hash y la fecha de modificación del video sintético original.

Las pruebas verifican configuración, metadata, persistencia, CLI, errores y
protección de originales. Las pruebas de integración con medios sintéticos
requieren FFmpeg y FFprobe y se omiten explícitamente si faltan. Un probe
correcto no certifica que todo el video decodifique ni sustituye el futuro
control de calidad de exports.

Si falla un comando, revisa su mensaje y `logs/`. Los fallos producen un código
de salida distinto de cero; no se ocultan excepciones. Si `doctor` informa que
falta FFprobe, instala FFmpeg manualmente o configura el binario antes de
inspeccionar un video.

## Próximas fases

1. Análisis local de audio, escenas y transcripción con metadata temporal.
2. Detección de momentos y puntuación configurable del potencial estimado.
3. Recortes y conversión vertical 1080×1920, H.264/AAC, subtítulos locales.
4. Control de calidad de exports y orquestación del pipeline.
5. Fuentes públicas de tendencias, hooks y analítica mediante interfaces
   independientes, sin habilitar servicios de pago automáticamente.

Usa contenido propio, autorizado, con licencia compatible o permitido por sus
términos. El proyecto no incluye evasión de DRM, controles de acceso, descargas
privadas, eliminación de marcas de agua ni evasión de copyright. Una futura
integración de yt-dlp sólo se añadirá para usos apropiados y permitidos.

## Licencia

Código bajo [MIT](LICENSE). Las licencias y términos de FFmpeg, dependencias,
modelos y contenido se revisan por separado; esta licencia no concede derechos
sobre videos de terceros.
