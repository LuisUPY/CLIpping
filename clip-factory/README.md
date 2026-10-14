# CLIpping · Clip Factory

Base local, modular y gratuita para producir contenido corto. El objetivo es
analizar videos, encontrar y puntuar momentos, recortar, convertir a vertical,
subtitular y validar los resultados sin modificar los originales.

**Estado: Fase 2 aceptada; análisis local y candidatos por reglas verificados.**
Obtiene metadata con FFprobe y analiza cambios visuales y señales acústicas con
FFmpeg mediante `analyze --scenes` y `analyze --audio`, individualmente o juntos.
Transcripción opcional **2B.1 validada con inferencia real** mediante
`analyze --transcribe`, whisper.cpp y GGML base multilingüe, sobre muestras TTS
controladas ES/EN. Esto no es una evaluación de precisión sobre voz natural.
Moment Detection (2B.2) está implementado y validado mediante reglas
deterministas: `analyze --moments` combina señales ya calculadas para proponer
candidatos explicables que requieren revisión editorial. Scoring y edición
siguen pendientes. Las skills distinguen capacidades disponibles y etapas futuras.

Consulta [ROADMAP.md](ROADMAP.md) para el estado validado, las seis fases y sus
criterios de aceptación. Fase 2 está aceptada dentro del alcance local descrito;
Fases 3–6 permanecen pendientes.

## Inicio en macOS / Apple Silicon

Usa Python 3.10 o posterior; esta funcionalidad se valida con Python 3.12. Comprueba la
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
  inspeccionar videos; FFmpeg ejecuta escenas/audio y prepara la futura edición.
  El diagnóstico completo exige ambos y devuelve `FAIL` si falta alguno,
  aunque `probe` puede funcionar sólo con FFprobe.
- `init`: crea los directorios de trabajo y el esquema SQLite si faltan.
- `config`: muestra la configuración efectiva en JSON.
- `recover`: completa publicaciones SQLite/JSON interrumpidas y confirma sus
  registros de recuperación; nunca sustituye un output existente en conflicto.
- `probe VIDEO`: inspecciona duración, streams, resolución, FPS, codecs y audio.
- `analyze VIDEO`: metadata básica, escenas con `--scenes`, audio con `--audio`
  y transcripción opcional con `--transcribe`, combinables. `--moments` activa
  escenas/audio y combina sus resultados; voz sólo se ejecuta con `--transcribe`.
- `detect VIDEO`: alias de `analyze VIDEO --moments`, con las mismas opciones.

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
con nombre `probe-<identificador>.probe.json`, independiente de la longitud o
Unicode del nombre original. `--output -` envía el JSON a la
salida estándar. Las inspecciones quedan registradas en SQLite y en el log.
Un archivo JSON existente no se sobrescribe. Los destinos explícitos deben
terminar en `.json`, permanecer dentro de la raíz y fuera de input. Un destino
relativo se interpreta respecto a la raíz; `VIDEO` relativo se interpreta
respecto al directorio desde el que ejecutas el comando.

Sin `--scenes`, `--audio`, `--transcribe` ni `--moments`, el JSON incluye `scope: metadata_only` y marca los análisis avanzados como
`not_run`. Una validación básica fallida conserva la metadata para diagnóstico,
registra el fallo y devuelve código de salida 1; no se presenta como análisis
correcto ni como QC completo.

FFprobe conserva `diagnostics` incluso con código de proceso 0. El estado
`PASS` indica metadata válida sin diagnósticos; `WARN` conserva advertencias,
las muestra y termina con código 0; `FAIL` conserva errores y termina con código
1. FFprobe usa `-v error`, por lo que su stderr no vacío se clasifica como error.
El adaptador genérico no clasifica como error el stderr normal de FFmpeg.

### Scene Detection (Fase 2A)

```zsh
python -m clip_factory analyze data/input/video.mp4 --scenes --output -
python -m clip_factory --profile mac_m1 analyze data/input/video.mp4 --scenes \
  --scene-threshold 10 --scene-timeout 300 --min-scene-duration 0.5 --video-stream 0
```

`--scene-threshold` usa el porcentaje 0–100 de
[FFmpeg scdet](https://ffmpeg.org/ffmpeg-filters.html#scdet): un umbral menor
es más sensible. El valor base es 10. `--video-stream` es el **índice absoluto**
de FFprobe, incluyendo las posiciones ocupadas por audio u otros streams;
no es el ordinal entre pistas de video. Sin selección explícita se prefiere
la pista de video marcada default, o la primera reproducible, excluyendo portadas.

El detector devuelve modelos inmutables (`SceneDetectionResult`, `SceneChange`)
desde `analysis/scenes.py`; no escribe JSON, SQLite, logs ni videos. La CLI
serializa y reutiliza la publicación recuperable de Fase 1. Sin `--output`,
un análisis de escenas crea `analyze-<identificador>.analyze.json`. Con escenas,
el JSON de sólo escenas usa `schema_version: 2`, `scope: metadata_and_scenes`,
`analysis.scenes: completed` y un objeto `scene_detection` que contiene:

- `status`, motor/versión, configuración efectiva y `elapsed_seconds`.
- `stream_index`, `time_base` racional, `start_pts` y `frames_analyzed`.
- `changes`: `pts`, `timestamp_seconds`, `source_timestamp_seconds` y `score`
  (0–100 o `null` cuando el motor no lo proporciona).

`timestamp_seconds` se calcula como `(pts - start_pts) * time_base`, respecto
al **primer frame decodificado de la pista elegida**. `source_timestamp_seconds`
es `pts * time_base`, con el offset positivo o negativo de origen. Para pistas
que comienzan tarde, ese origen puede diferir del inicio del contenedor o audio.
PTS entero y base racional conservan la referencia exacta; los segundos JSON
son su representación flotante. No se usa FPS promedio, índices de frames ni
el timestamp redondeado de `lavfi.scd.time`. La precisión de localización es la
del frame observado; no se infieren cortes entre frames.

`--min-scene-duration` filtra candidatos demasiado próximos al inicio o al
último corte aceptado; no garantiza la duración del último segmento. No se
añaden cortes artificiales al inicio/fin. `changes: []` con `status: PASS`
significa análisis ejecutado sin cortes. Falta de video/pista, PTS inválidos
o decrecientes, corrupción, filtro ausente y timeout producen error visible y
código 1; no publican un análisis parcial como correcto. Los errores de
decodificación se rechazan también cuando FFmpeg devuelve código 0 con stderr.

El timeout de escenas cubre preparación y decodificación del detector (300 s
por defecto); el probe inicial de la CLI mantiene su timeout independiente.
La lectura es incremental: FFmpeg conserva sus buffers de decodificación y
Python recibe sólo metadata, con líneas y cola stderr de hasta 64 KiB. Se
guardan únicamente los cortes aceptados, por lo que esa lista crece con su
número. Se limita el decoder a `runtime.threads` y el filtro a un hilo. No se
muestrea, escala, convierte a CFR ni necesita GPU, modelos o paquetes nuevos.
Ante timeout o interrupción se termina y recoge el proceso hijo.

Requiere FFmpeg con `scdet`, `metadata`, `settb` y `-fps_mode`; probado con
FFmpeg 9.0.2. `doctor` verifica ejecutables/dependencias; la compatibilidad del
filtro se verifica al analizar. No hay fallback silencioso. Un flash puede
parecer un corte; cambios sutiles o transiciones graduales pueden no detectarse.
La puntuación mide cambio visual, no interés narrativo ni potencial de un clip.

### Audio Analysis (Fase 2A.2)

```zsh
python -m clip_factory analyze data/input/video.mp4 --output -
python -m clip_factory analyze data/input/video.mp4 --scenes --output -
python -m clip_factory analyze data/input/video.mp4 --audio --output -
python -m clip_factory analyze data/input/video.mp4 --scenes --audio --output -
python -m clip_factory --profile mac_m1 analyze data/input/video.mp4 --audio \
  --audio-window 0.25 --audio-peak-threshold -6 --audio-change-threshold 6 \
  --audio-silence-threshold -50 --audio-min-silence 0.5 --audio-timeout 300
```

El motor `analysis/audio.py:analyze_audio` devuelve modelos inmutables y no
escribe JSON, SQLite, logs ni muestras de audio. Reutiliza probe y el adaptador
de procesos incremental. FFmpeg [astats](https://ffmpeg.org/ffmpeg-filters.html#astats)
calcula RMS, pico de muestra y conteo; `asetnsamples` forma ventanas sin rellenar
la última con ceros. No se reduce a mono ni se cambia la frecuencia: RMS combina
la energía media de todos los canales y el pico toma el máximo entre ellos.
Canales en antifase conservan su energía. No mide LUFS, true peak ni presión SPL.

Se publican métricas por ventana, RMS global ponderado por el número de muestras
y cuatro tipos de eventos independientes:

- `peak`: pico absoluto de muestra >= `peak_dbfs` (base -6 dBFS).
- `energy_rise` / `energy_drop`: cambio RMS entre ventanas contiguas >=
  `change_db` en magnitud (base 6 dB), con signo y dirección conservados.
- `silence`: ventanas con RMS <= `silence_dbfs` (base -50 dBFS), agrupadas
  durante al menos `min_silence_duration` (base 0,5 s).

Se agrupan ventanas consecutivas del mismo tipo, sin unir huecos temporales ni
direcciones opuestas. Los eventos pueden solaparse: un pico absoluto y un
aumento relativo son señales diferentes. No significan grito, emoción, reacción,
habla ni interés. Un tono fuerte constante puede formar un único evento `peak`
sin cambios relativos. El silencio se define por energía RMS de la ventana;
impulsos breves pueden coexistir con una media baja. No se localiza la muestra
exacta del pico ni los límites de silencio dentro de una ventana.

`window_seconds` admite 0,01–10 s (base 0,5 s), redondeados al sample más próximo;
`window_samples` declara el tamaño efectivo. El bloque final conserva su tamaño
real. Para silencio digital, `rms: 0` y dBFS `null` representan -infinito en un
análisis `PASS`; no se emite JSON con NaN/Infinity. El piso configurable
`energy_floor_dbfs` (base -120, rango -300..-120) se usa **sólo** al comparar
energía relativa, sin reemplazar las mediciones originales. Audio flotante que
supera 0 dBFS se conserva y genera una advertencia.

El resultado incluye `stream_index`, `sample_rate`, `channels`, `source_time_base`,
`time_base: 1/sample_rate`, `start_pts`, configuración efectiva, hilos, versión,
advertencias y duración del procesamiento. Cada ventana/evento expone PTS
enteros y segundos relativos al primer frame decodificado de la pista de audio,
además de `source_start_seconds` / `source_end_seconds` con el offset original.
Los intervalos son `[inicio, fin)`. La precisión temporal queda limitada por
el sample y el tick del contenedor; `timestamp_tolerance_samples` declara la
tolerancia a su cuantización. No deriva tiempos de FPS ni usa el inicio del
video como si fuera el inicio del audio. El priming/padding de codecs como AAC
se refleja mediante las muestras y PTS realmente decodificados.

Antes de formar ventanas se auditan los PTS de cada frame frente al origen y
las muestras acumuladas. Discontinuidades mayores que la precisión del
contenedor se rechazan; no se inventan silencios ni se ocultan offsets. PTS o
métricas ausentes/inválidos, corrupción, filtros ausentes y timeout fallan
visiblemente. Sólo la última ventana puede ser parcial.

`--audio-stream` utiliza el índice **absoluto** de FFprobe. Sin él se prefiere
la pista de audio default, o la primera. No se mezclan pistas. Sin audio y sin
índice explícito, el resultado es `SKIPPED`, `analysis.audio: skipped_no_audio`,
métricas `null` y una advertencia; escenas puede terminar y el comando devuelve
0 si el resto es válido. Una selección explícita inexistente/no acústica falla.
El motor también acepta archivos sólo de audio; la CLI conserva su validación
básica de video de Fase 1.

Audio sin voz/momentos utiliza `schema_version: 3`, `scope: metadata_and_audio` (o
`metadata_and_scenes_and_audio` para ambos), y un objeto `audio_analysis`.
Metadata y sólo escenas conservan schemas 1/2. Los análisis se ejecutan por
etapas con un único decoder activo para controlar RAM. La CLI reutiliza logs,
SQLite y recuperación; un fallo no publica un resultado parcial como correcto.
Las opciones acústicas se validan antes de ejecutar los decodificadores.

YAML `analysis.audio` contiene las diez opciones; la CLI prevalece y requiere
`--audio`, `--moments` o `detect`. Además de los umbrales anteriores: `--audio-energy-floor`,
`--audio-max-windows` (base 100.000; máximo 1.000.000) y `--audio-max-events`
(base 20.000; máximo 100.000) limitan resultados retenidos. Superar un límite
falla explícitamente: aumenta el límite, ajusta umbrales o usa ventanas mayores.
Python no recibe muestras; sólo conserva ventanas/eventos hasta esos límites.
Las líneas y la cola stderr del proceso se limitan a 64 KiB. El timeout acústico
(300 s base) es independiente del probe y escenas, y recoge el proceso al vencer.

Se admiten 1–64 canales y frecuencias de hasta 768 kHz. La ventana flotante se
limita a 32 MiB; FFmpeg puede mantener varias copias y buffers auxiliares, por
lo que éste no es un límite global de RAM. Ambos `astats` usan `length=0` para
no reservar ventanas deslizantes que estas métricas no necesitan. El decoder
usa `runtime.threads` y los filtros un hilo. Requiere `aformat`, `asettb`,
`asetnsamples`, `astats` y `ametadata`; probado en FFmpeg 9.0.2. No instala
paquetes, modelos ni servicios. Quedan sin validar cambios de frecuencia/layout
durante una pista, VODs largos y una matriz de versiones/codecs.

### Transcripción opcional (2B.1 validada con muestras controladas)

```zsh
python -m clip_factory analyze data/input/video.mp4 --transcribe --output -
python -m clip_factory --profile mac_m1 analyze data/input/video.mp4 \
  --scenes --audio --transcribe --transcript-model models/ggml-base.bin \
  --transcript-language auto --transcript-timeout 600 --transcript-audio-stream 1
```

`analysis/transcript.py:transcribe_media` usa **whisper.cpp**, mediante un
ejecutable `whisper-cli` y un archivo GGML locales. No añade dependencias Python,
no llama APIs ni accede a un registro de modelos. FFmpeg prepara PCM16 mono a
16 kHz por bloques; el motor, la CLI y SQLite conservan responsabilidades
separadas. Los originales sólo se leen. La ejecución base usa CPU (`--no-gpu`),
con los hilos configurados, y no requiere CUDA ni Metal.

`--transcribe` es obligatorio para ejecutar voz. Sus nueve opciones corresponden
a `analysis.transcription` en YAML; CLI prevalece:

- `--transcript-engine` / `engine`: `whisper.cpp`, único adaptador implementado.
- `--transcript-executable` / `executable`: `whisper-cli`; nombre en PATH o ruta
  local. Las rutas relativas con `/` se resuelven contra la raíz del proyecto.
- `--transcript-model` / `model`: ruta local GGML. El YAML del proyecto selecciona
  `models/ggml-base.bin`; los defaults incorporados sin YAML conservan `null`. Las rutas
  relativas se resuelven contra la raíz. No se aceptan nombres de repositorios
  como mecanismo de descarga.
- `--transcript-language` / `language`: `auto`, `es` o `en`; base `auto`.
- `--transcript-timeout` / `timeout`: deadline total de voz, base 300 s;
  probe inicial, escenas y audio mantienen sus propios timeouts.
- `--transcript-audio-stream` / `audio_stream`: índice absoluto FFprobe;
  base default/primera pista de audio. Es independiente de `--audio-stream`.
- `--transcript-chunk-seconds` / `chunk_seconds`: 1..30 s; base 30 s.
- `--transcript-max-segments` / `max_segments`: 1..1.000.000; base 100.000. Superarlo falla;
  no se publica una transcripción truncada como completa.
- `--transcript-silence-threshold` / `silence_dbfs`: -120..-20; base -50 dBFS; omite
  bloques cuyo pico de muestras es muy bajo antes de inferir. Puede omitir voz muy débil.

Con voz solicitada sin momentos, JSON usa `schema_version: 4`, `analysis.transcription` y
`transcription`; los formatos 1/2/3 siguen iguales al no solicitarla. Incluye
motor, versión cuando sea observable, modelo local utilizado, configuración,
idioma detectado, segmentos con texto, advertencias y tiempos separados de
preparación acústica e inferencia. `status: SKIPPED` y razones `no_audio`,
`engine_unavailable` o `model_unavailable` distinguen omisiones; escenas/audio
pueden terminar normalmente. Un índice explícito inválido, JSON malformado del
motor, fallo o timeout devuelve error visible sin publicar resultados parciales.
`doctor` comprueba disponibilidad opcional sin cargar modelos ni certificar ASR.

`start_seconds`/`end_seconds` son relativos al primer audio decodificado de la
pista elegida. `source_start_seconds`/`source_end_seconds` conservan su offset
original, incluso negativo; no los confundas con el inicio relativo de escenas.
Se auditan PTS y continuidad, sin rellenar huecos silenciosamente. Los offsets
del JSON de Whisper son milisegundos derivados de su rejilla de 10 ms: representan
estimaciones de voz, no alineación exacta de cada palabra. La conversión a mono
puede cancelar canales en antifase; esto no modifica las mediciones de Audio Analysis.

El silencio omitido devuelve cero segmentos e idioma `null`, nunca un texto
fabricado. Los filtros del motor y el filtrado de salida reducen alucinaciones,
pero energía y confianza no certifican presencia de voz. Ruido, música, frases poco
inteligibles y silencios dentro de un bloque activo siguen siendo limitaciones;
revisa la transcripción. No se promete reconocimiento perfecto ni diarización.
Los bloques independientes pueden cortar palabras o perder contexto en sus
bordes. La autodetección puede variar entre bloques bilingües; las advertencias
conservan las ambigüedades. No se ejecuta traducción.

Cada bloque contiene como máximo 960.000 bytes de PCM (~0,916 MiB); Python
puede mantener copias y arrays adicionales. El WAV temporal y JSON (máximo
8 MiB por bloque) se guardan en una carpeta privada en working y se retiran
al terminar/fallar; los pipes conservan colas de 64 KiB. El modelo añade RAM
independiente. El texto UTF-8 acumulado se limita a 32 MiB, además del número de
segmentos; superar cualquiera de los límites falla visiblemente sin truncar.
El modelo se carga de nuevo por bloque. Una muerte abrupta puede
dejar `.transcript-<UUID>/`; `clean` conserva esas carpetas y no las borra
automáticamente. Sólo retíralas tras comprobar que no hay análisis activo.

#### Por qué whisper.cpp

[whisper.cpp](https://github.com/ggml-org/whisper.cpp) ofrece CPU, ARM NEON,
Accelerate y Metal con un ejecutable independiente. Permite mantener PyYAML
como única dependencia Python de producción y sustituir el adaptador sin
cambiar la CLI ni SQLite. La CLI carga entero el audio recibido: por eso se
alimenta con bloques acotados. El contrato se contrastó con el
[código oficial v1.9.5](https://github.com/ggml-org/whisper.cpp/blob/v1.9.5/examples/cli/cli.cpp);
una versión distinta debe superar las pruebas locales antes de aceptarse.

[mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
aprovecha MLX/Metal, pero sus
[dependencias oficiales](https://github.com/ml-explore/mlx-examples/blob/main/whisper/mlx_whisper/requirements.txt)
incluyen PyTorch, NumPy, SciPy, Numba y Hugging Face Hub. El
[cargador](https://github.com/ml-explore/mlx-examples/blob/main/whisper/mlx_whisper/load_models.py)
puede descargar cuando no encuentra una ruta local; requeriría aislamiento y
chunking propios. Es una alternativa futura, sin adaptador instalado.

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) utiliza CTranslate2,
PyAV, ONNX Runtime y tokenizers. CTranslate2 admite
[macOS ARM64 en CPU](https://opennmt.net/CTranslate2/installation.html), sin
backend Metal documentado; GPU requiere CUDA. Offline exige pesos y tokenizer
locales completos. Los benchmarks publicados usan otro hardware y no permiten
afirmar que sea más rápido en este M1. No se instalaron estos motores ni se
midió una comparación de velocidad local.

#### Modelos e instalación manual

Cifras orientativas de la
[documentación upstream](https://github.com/ggml-org/whisper.cpp#memory-usage),
sin incluir el sistema, FFmpeg y otros procesos; **no son RAM medida en este Mac**:

- Multilingüe `tiny`: 75 MiB en disco; RAM aproximada 273 MB.
- Multilingüe `base`: 142 MiB; RAM aproximada 388 MB. Primera elección para M1/8 GB.
- Multilingüe `small`: 466 MiB; RAM aproximada 852 MB; calidad/rendimiento pendientes.
- `medium`: 1,5 GiB; RAM aproximada 2,1 GB. No es el modelo inicial recomendado.
- `large`: 2,9 GiB; RAM aproximada 3,9 GB. Evitar como base en un equipo de 8 GB.

Las variantes `.en` sólo sirven para inglés; español y `auto` requieren un
modelo multilingüe. Los GGML cuantizados compatibles pueden reducir tamaño/RAM,
pero su calidad y velocidad siguen pendientes de validación. Formatos MLX,
CTranslate2 y safetensors no sirven directamente para este adaptador.

En esta validación se autorizó e instaló **whisper.cpp 1.9.5 ARM64 mediante
Homebrew** y únicamente el modelo oficial GGML `base` multilingüe. Homebrew
instaló ggml 0.26.0 y llama.cpp 0.6.0_1, y actualizó libomp a 23.1.3 como
dependencias; reutilizó las demás. No hay dependencias Python nuevas ni servicios.

Modelo local `models/ggml-base.bin`: **147.951.465 bytes (141,10 MiB)**,
SHA-256 verificado contra la
[metadata oficial](https://huggingface.co/ggerganov/whisper.cpp/blob/main/ggml-base.bin):

```text
60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe
```

El [descargador upstream](https://github.com/ggml-org/whisper.cpp/blob/v1.9.5/models/download-ggml-model.sh)
apunta al mismo repositorio oficial de Hugging Face. La descarga se verificó
antes de publicar el nombre final del archivo; `.gitignore` excluye pesos y
parciales. `config/default.yaml` utiliza `executable: whisper-cli` y la ruta
relativa del modelo, sin rutas personales hardcodeadas. No se descargaron otros
modelos ni voces. La inferencia y las pruebas se ejecutaron localmente.

**Reutiliza esta instalación. Para nuevas instalaciones/descargas fuera de lo
autorizado, pide autorización indicando tamaño, destino y modelo.**
En otra máquina, el procedimiento manual tras autorización es:

1. Instalar el CLI opcional mediante `brew install whisper.cpp`, o compilar una
   versión fijada siguiendo el [proyecto oficial](https://github.com/ggml-org/whisper.cpp#quick-start).
   Comprobar que `whisper-cli --help` ofrece JSON completo y las opciones requeridas;
   si el distribuidor usa otro nombre, indicar `--transcript-executable`.
   La [fórmula Homebrew](https://formulae.brew.sh/formula/whisper.cpp) incluye
   dependencias adicionales; compilar sólo el target `whisper-cli`, sin SDL2,
   server, tests ni CURL, ofrece una instalación mínima. El archivo upstream
   `for-tests-ggml-tiny.bin` no contiene pesos válidos para certificar ASR.
2. En un checkout separado de whisper.cpp, ejecutar
   `sh ./models/download-ggml-model.sh base` **sólo tras autorizar los 142 MiB**.
   Copiar `ggml-base.bin` a `models/` o conservarlo fuera del proyecto y configurar
   su ruta absoluta. Verificar tamaño y hash frente al origen del archivo.
3. Probar un archivo propio en español y otro en inglés, con sus referencias;
   ejecutar los tests ASR opt-in y el benchmark de inferencia descritos abajo.
   Después de instalar, inferir no necesita conexión.

### Moment Detection (2B.2 validada)

```zsh
python -m clip_factory analyze data/input/video.mp4 --moments --output -
python -m clip_factory detect data/input/video.mp4 --output -
python -m clip_factory analyze data/input/video.mp4 --moments --transcribe \
  --moment-min-duration 5 --moment-max-duration 45 --moment-min-sources 2 \
  --moment-coincidence-window 1 --output data/working/candidatos.json
```

`analysis/moments.py:detect_moments` recibe modelos inmutables de escenas,
audio y voz opcional. No lee archivos ni ejecuta FFmpeg o Whisper; tampoco
accede a SQLite. La CLI calcula cada etapa solicitada una sola vez, por orden
escenas → audio → transcripción opcional → reglas, y reutiliza sus resultados
en memoria. El alias `detect` conserva el nombre invocado en el log. No hay
modelos ni dependencias nuevos.

Se requieren al menos **dos familias diferentes** entre `scenes`, `audio` y
`transcription`; dos métricas acústicas siguen siendo una sola familia. Las
coincidencias de tres familias deben ser próximas entre sí, sin convertir una
cadena de proximidades en una coincidencia global. Las razones implementadas
son `scene_change`, `audio_peak`, `audio_energy_rise`, `audio_energy_drop` y
`speech_activity`. El audio aporta el inicio de un transitorio; los eventos
sostenidos de más de cuatro segundos se excluyen por defecto. El silencio no
genera candidatos. Voz exige duración y número mínimo de palabras; no se
deducen emociones, importancia narrativa ni potencial viral.

Las reglas añaden contexto, respetan las duraciones mínima/máxima y fusionan
intervalos próximos si la unión cabe en el máximo. Si una fusión no cabe,
suprimen solapamientos conservando el primer candidato cronológico; el cooldown
se mide entre **inicios**. `max_candidates` también conserva los primeros, sin
ranking. Cada candidato retiene hasta 64 evidencias; las supresiones se informan.
La voz prolongada puede aportar un intervalo de soporte localizado alrededor
de la coincidencia; su texto es contexto del segmento original, sin alineación
por palabra. Una tercera fuente opcional no elimina un par válido porque su
soporte sea demasiado largo.

El origen común es el primer frame decodificado de la pista de video elegida:
`scenes.start_pts * scenes.time_base`. Audio usa PTS enteros/base de muestras;
voz conserva su origen resampleado y los offsets originales. Se comprueban
coherencia, orden y cobertura antes de combinar; no se usa FPS promedio ni se
rellenan discontinuidades. Evidencias incluyen pista, PTS cuando existen,
base racional, timestamps originales/relativos e intervalo de soporte.

El límite del video procede de la **pista seleccionada**, mediante
`duration_ts * time_base` o `stream.duration` desde su inicio declarado. Para
Matroska/WebM, el tag `DURATION` escrito por FFmpeg se interpreta como fin
absoluto, conservando offsets positivos y negativos. No se resta el origen de
forma universal a `format.duration`, ni se usa el final del audio o el último
corte como final del video. Metadata contradictoria falla; si falta un límite
utilizable, `duration` es `null` y se advierte que el final no está verificado.
Estos límites proceden de metadata; no certifican el último frame decodificado.

Con momentos solicitados, JSON usa **schema 5**, `analysis.moments: completed`,
una lista superior `moments` y un objeto `moment_detection`. Cada candidato
incluye `start`, `end`, `duration`, `reasons` y `evidence`; el objeto de detección
incluye configuración, origen, duración/procedencia, fuentes disponibles y
ausentes, advertencias, tiempo de reglas y contadores. `moments: []` representa
ejecución válida sin coincidencias suficientes. Sin audio, o con ASR omitido,
se mantienen escenas y advertencias; no se inventan señales. Sin `--moments`
se conservan schemas 1–4 y `analysis.moments: not_run`.

Las catorce opciones `--moment-*` corresponden a `analysis.moments` en YAML;
CLI prevalece y requieren `--moments` o `detect`:

- `min_duration` / `max_duration`: 5 / 45 s, positivos y máximo >= mínimo.
- `context_before` / `context_after`: 2 / 3 s, no negativos.
- `coincidence_window` / `merge_gap` / `cooldown`: 1 / 1 / 10 s, no negativos.
- `min_sources`: 2 por defecto; admite 2 o 3 familias.
- `min_speech_duration` / `min_speech_words`: 2 s / 6 palabras; duración positiva,
  palabras entre 2 y 10.000.
- `max_audio_event_duration`: 4 s, positivo.
- `max_candidates`: 20, entre 1 y 10.000; limita salida con advertencia.
- `max_events`: 100.000, entre 1 y 1.000.000; incluye señales filtradas y excederlo falla.
- `timeout`: 10 s, positivo; limita sólo reglas, separado de los analizadores.

Las claves usan guiones bajos en YAML y guiones en CLI: por ejemplo,
`min_speech_duration` corresponde a `--moment-min-speech-duration`.
Configuración de reglas inválida se rechaza antes del probe; con `--moments`, metadata
básica fallida también se rechaza antes de decodificar. Errores de etapas,
bounds o reglas no publican candidatos parciales; se reutilizan logging,
SQLite y recuperación existentes, sin migración de base de datos.

La búsqueda usa índices temporales y vecinos acotados, con ordenación
O(N log N). Memoria y resultados crecen con las señales hasta los límites
configurados; no es memoria constante. `max_events`/timeout fallan visiblemente;
los límites de candidatos/evidencias suprimen repeticiones y lo declaran.
Las coincidencias sintéticas verifican sincronización y reglas, no precisión
editorial. Flashes, ruido transitorio, música y errores ASR pueden producir
candidatos irrelevantes; voz natural y VODs reales largos siguen pendientes.

### Publicación y recuperación

SQLite y el filesystem no comparten una transacción atómica. Para outputs JSON,
la CLI valida primero el repositorio, escribe y sincroniza un journal en
`data/working/.publications/`, confirma SQLite y publica el JSON exclusivamente.
Si falla la publicación, intenta compensar sólo su propio registro. Si hay un
corte abrupto o falla la compensación, conserva el journal para recuperación:

```zsh
python -m clip_factory recover
```

El comando es idempotente: compara contenido mediante un hash JSON canónico,
completa lo pendiente y retira únicamente journals propios confirmados. Reconoce
los pares temporal/final que una interrupción dejó enlazados al mismo inode,
sin permitir escrituras genéricas sobre hardlinks. Si existe un archivo diferente
en el destino, se preservan el archivo y el journal y se devuelve un error para
resolver el conflicto. No borres ni edites journals para forzar la recuperación.
`.publications/` está reservado y no puede elegirse como destino de un informe.

`clip`, `process` y `qc` pertenecen a fases posteriores y no son
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
`logging.level`. `analysis.scenes` define `threshold`, `min_scene_duration`,
`timeout` y `video_stream`; las opciones de escenas de la CLI prevalecen sobre
YAML y requieren `--scenes`, `--moments` o `detect`. `analysis.audio` configura
ventanas, umbrales, silencios, pista, timeout y límites de audio; sus opciones
requieren `--audio`, `--moments` o `detect`.
`analysis.transcription` configura el motor opcional, modelo, idioma, bloques,
pista, timeout y límites; sus opciones requieren `--transcribe`.
`analysis.moments` configura coincidencia, contexto, fusión, supresión y límites;
sus opciones requieren `--moments` o `detect` y no cambian los umbrales de los
analizadores.
`runtime.threads` limita el decoder de cada motor. `runtime.device` permanece
reservado; los analizadores usan CPU y las reglas se ejecutan en Python.

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

La inspección y los motores visual/acústico leen el original mediante FFprobe/FFmpeg. Las escrituras se destinan a
metadata, base de datos y logs. Se rechazan destinos dentro de input o que
coincidan con el original, incluso mediante enlaces simbólicos. Estos comandos
no borran ni transforman videos. Conserva tus respaldos habituales.

Los scripts auxiliares no instalan software:

```zsh
python scripts/doctor.py
python scripts/setup.py
python scripts/clean.py
```

`setup.py` inicializa directorios y SQLite; no es el instalador del paquete.
`clean.py` muestra una vista previa y sólo considera temporales propios
`.metadata-<UUID>.tmp` inactivos en working; conserva otros `*.tmp`, informes
`.json` y journals. El borrado exige `--apply` explícito. Los locks coordinan
limpieza, creación, publicación y recuperación, también al usar el escritor
por separado. Si otra operación está activa, la limpieza devuelve un error
de recurso ocupado. Rechaza enlaces simbólicos; ante un temporal enlazado a un
JSON tras una interrupción, ejecuta primero `recover`.
Consulta `python scripts/clean.py --help` antes de utilizarlo.

## Arquitectura

Estabilización de Fase 1: se mantienen los módulos actuales. Las escrituras
sensibles usan descriptores de directorio y aperturas sin seguir enlaces;
la limpieza y la publicación se coordinan mediante bloqueos locales.
SQLite y JSON son recursos distintos: la publicación usa un pequeño registro
de recuperación, persiste primero en SQLite y publica después el JSON de
forma exclusiva, con compensación ante errores. No se introduce un servicio,
una nueva dependencia ni un VFS personalizado de SQLite. La protección se
orienta al uso local y a procesos cooperativos; no pretende aislar procesos
maliciosos que ya tengan los mismos permisos del usuario.

Los locks `.clip_factory.lock` de la raíz y working son archivos locales
persistentes; no deben borrarse durante operaciones activas. SQLite utiliza
su propio `<base>.lock`: bloquear el DB directamente entra en conflicto con
SQLite en macOS. Las consultas usan `mode=ro` y no crean bases, directorios ni
locks. Si alguien convierte manualmente el DB a WAL, SQLite puede necesitar
sus propios sidecars; no se usa `immutable=1`, que ignoraría cambios activos.
Se comprueban identidades y enlaces de DB y sidecars antes y después de abrir.
Eliminar completamente la ventana interna de SQLite frente a un proceso hostil
del mismo usuario requeriría un VFS específico; queda fuera del modelo local.

La coordinación utiliza `fcntl` y descriptores POSIX de la biblioteca estándar:
macOS y Linux; esta iteración no promete soporte Windows. Los directorios nuevos
y las publicaciones se sincronizan con `fsync`. Las pruebas de interrupción
validan muerte de procesos, no simulan físicamente una pérdida de energía.

El paquete `src/clip_factory/` separa responsabilidades:

- `cli/`: interfaz de terminal con `argparse`.
- `config/`: lectura segura, combinación y validación de YAML.
- `database/`: modelos y repositorio SQLite.
- `media/`: ejecución de binarios, probe y contratos de validación.
- `utils/`: rutas, escrituras seguras y logging.
- `analysis/scenes.py`: detector de cambios visuales independiente de la CLI.
- `analysis/audio.py`: RMS, picos, cambios de energía y silencios independientes
  de CLI/JSON/SQLite, sin almacenar muestras.
- `analysis/transcript.py`: proveedor local de transcripción, temporales de audio
  y modelos de resultado independientes de CLI/SQLite; ASR real validado con
  base multilingüe sobre TTS controlado ES/EN.
- `analysis/moments.py`: reglas puras sobre resultados existentes, candidatos
  con evidencia y límites temporales; sin lectura de multimedia ni persistencia.
- Resto de `analysis/`, `scoring/`, `editing/`, `pipeline/`: contratos y módulos
  reservados para etapas posteriores.

Los comandos pendientes responden con un mensaje y código de salida 2;
los módulos reservados contienen documentación y aún no exponen motores.
Los motores de transcripción y la futura
edición se conectan por interfaces independientes; cambiar Whisper no debe
obligar a reescribir el resto del sistema. La CPU es la base. Metal/MPS será una
optimización opcional cuando el motor elegido lo permita; CUDA no es requisito.

Las cinco skills del proyecto están en `.codex/skills/`: `trend-researcher`,
`video-analyzer`, `clip-scorer`, `shorts-editor` y `quality-control`.
`AGENTS.md` define las reglas del desarrollo.

`ROADMAP.md` es la fuente de progreso: antes de implementar se consulta la fase
activa; un archivo reservado o interfaz por sí solos no completan una tarea.

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
- **FFprobe / FFmpeg**, programas locales externos, proporcionan inspección,
  Scene Detection, Audio Analysis y la futura edición. FFprobe ofrece salida JSON sin incorporar
  otro SDK de video: [documentación oficial](https://ffmpeg.org/ffprobe.html).
- **SQLite, JSON, argparse, logging, pathlib, dataclasses y fcntl** proceden de la
  biblioteca estándar; no se añaden paquetes para estas funciones.
- **whisper.cpp 1.9.5**, ejecutable externo opcional para transcripción, y GGML
  base multilingüe local. Instalados con autorización mediante Homebrew/origen
  oficial; no forman parte de las dependencias Python ni se descargan al analizar.

No se instalan Whisper, faster-whisper, OpenCV, librosa, PySceneDetect ni yt-dlp
para metadata, escenas ni Audio Analysis. Cada futura dependencia deberá aportar una necesidad concreta,
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

Estabilización posterior: **178 pruebas aprobadas, sin omisiones**, doctor PASS
y binarios Python/FFmpeg/FFprobe ARM64 en Apple M1. Las regresiones añaden
diagnósticos con retorno 0, compensación/recuperación, conflictos preservados,
interrupciones reales de proceso, limpieza concurrente, rutas sustituidas,
ciclo de vida de logging, nombres largos/Unicode y consultas sin creación.
Se mantiene pendiente una matriz de Python admitidos y benchmarks de VOD largo.

Scene Detection validado el **2026-10-13: 276 pruebas PASS, sin omisiones**
(178 existentes + 98 nuevas), `doctor PASS`, Python 3.12.14 y FFmpeg/FFprobe
9.0.2 ARM64 en MacBook Air Apple M1 con 8 GB. Incluye 15 integraciones nuevas:
cortes conocidos, ausencia de cortes, sensibilidad/separación mínima, PTS
fraccionarios, VFR con offset cero/positivo/negativo, múltiples pistas, ausencia
de video, corrupción en contenedor/decoder, timeout real y CLI JSON/SQLite/logs.
Las pruebas unitarias comprueban streaming incremental, buffers, cierre del
proceso ante error/interrupción y recuperación de una publicación de escenas.
Hashes y mtime de originales sintéticos se conservan.

Mediciones locales del detector con videos sintéticos MPEG-4 1920×1080, 30 FPS,
cortes blanco/negro cada 15 s, `threads=4`, umbral 10 y tres ejecuciones seriales:

- 60 s / 1.800 frames / 3 cortes: **1,518 s mediana**, rango 1,514–1,655 s.
- 180 s / 5.400 frames / 11 cortes: **4,402 s mediana**, rango 4,382–4,405 s.
- Pico RSS observado: Python **23,3 MiB**; mayor proceso hijo (FFmpeg/FFprobe)
  **54,5 MiB**, medidos por separado con `resource.getrusage` en macOS.

Los tiempos incluyen probe interno, consulta de versión, decodificación y
parseo; excluyen generación del video y publicación JSON/SQLite. La memoria
no creció proporcionalmente al número de frames entre estas muestras. No son
VODs reales ni una garantía de rendimiento: quedan pendientes videos largos,
4K, otros codecs/contenido y la matriz de versiones Python/FFmpeg. Scene
Detection no sustituye QC ni los otros análisis del video.

Audio Analysis 2A.2 validado el **2026-10-13: 424 PASS, 0 fallidas, 0 omitidas**
(276 existentes + 148 nuevas), `doctor PASS`, Python 3.12.14 y FFmpeg/FFprobe
9.0.2 ARM64 en el mismo MacBook Air M1 de 8 GB. Hay 39 integraciones nuevas de
audio/CLI y 109 pruebas unitarias nuevas. Cubren señales silenciosas y conocidas,
RMS mono/estéreo en antifase, picos/cambios relativos, agrupación, bloques finales,
múltiples pistas, offsets negativos/positivos, cuantización a 44,1 kHz, priming AAC,
ausencia de audio, corrupción incluso con exit 0, timeouts y límites de recursos.
Las cuatro modalidades, JSON, SQLite, logs y recuperación están verificadas.
Los hashes/mtime se conservan y Scene Detection pasa todas sus regresiones.

Mediciones acústicas con videos sintéticos MPEG-4 1080p/30 y PCM float estéreo
48 kHz, energía baja/alta/cero por tramos, ventanas de 0,5 s, `threads=4`, tres
repeticiones seriales con hash leído incrementalmente:

- 60 s / 2.880.000 muestras por canal / 120 ventanas: **0,268 s mediana**,
  rango 0,268–0,323 s; 9 eventos agrupados.
- 180 s / 8.640.000 muestras por canal / 360 ventanas: **0,648 s mediana**,
  rango 0,648–0,658 s; 29 eventos agrupados.
- Pico RSS Python **27,3 MiB**; mayor hijo FFmpeg/FFprobe **18,4 MiB**, medidos
  por separado con `resource.getrusage`, en un proceso nuevo por duración.

Incluye probe interno, versión, decodificación acústica, auditoría y análisis;
excluye generación, hashes, análisis visual y publicación JSON/SQLite. El motor
no decodifica la imagen. Los resultados retenidos crecen hasta los límites
configurados; estas muestras no sustituyen benchmarks de VOD largo, otros
codecs/versiones ni cambios de formato durante una pista.

Implementación inicial de transcripción opcional del **2026-10-13**, antes de
instalar motor/modelo: **631 PASS, 0 fallidas y 2 SKIP**;
`doctor PASS`. Incluye las 424 regresiones previas, 147 unitarias del motor,
35 pruebas CLI/configuración y 25 integraciones FFmpeg/proceso. Las dos
omisiones son ASR real ES/EN por falta de motor/modelo/grabaciones. Los dobles
de proceso nunca cuentan como reconocimiento real. Cobertura: idiomas,
silencio, sin audio, offsets ±, AAC/44,1 kHz, pistas, bloques, timeouts,
corrupción, errores/JSON inválido, límites de memoria/texto, originales,
publicación SQLite/JSON y recuperación. Sin dependencias Python nuevas.

Archivos de esta iteración:

- `src/clip_factory/analysis/transcript.py`.
- `src/clip_factory/cli/main.py`, `src/clip_factory/config/loader.py`, `config/default.yaml`.
- `tests/unit/test_transcript.py`, `tests/unit/test_cli_transcript.py`,
  `tests/integration/test_transcript_integration.py`.
- `scripts/benchmark_transcription.py`.
- `README.md`, `ROADMAP.md`, `.codex/skills/video-analyzer/SKILL.md`.

Scene Detection, Audio Analysis y el esquema SQLite permanecen sin cambios.
La implementación inicial no instaló/descargó software ni modelos. La
instalación autorizada y validación real posterior se describen arriba y abajo.
No se hicieron commits/push.

Benchmark de preprocesamiento de transcripción del **2026-10-13**, MacBook Air
M1 de 8 GB, Python 3.12.14/FFmpeg 9.0.2: videos sintéticos negros 1080p/1fps,
audio FLAC estéreo 48 kHz, bloques PCM mono/16k de 30 s, tres procesos nuevos
por duración. **No ejecuta Whisper ni reconoce voz**:

- 60 s: mediana **0,145 s**, rango 0,145–0,147 s.
- 180 s: mediana **0,364 s**, rango 0,362–0,365 s.
- 1.800 s (30 min): mediana **3,371 s**, rango 3,358–3,389 s.
- Pico máximo RSS Python **33,44 MiB**; mayor hijo FFmpeg/FFprobe **19,00 MiB**,
  separados con `resource.getrusage`. No son RAM total ni memoria del modelo.

Incluye streaming, resampling/downmix, auditoría PTS, gate y escritura/retirada
WAV; excluye del tiempo probe, generación, hashes, inferencia y publicación.
Los hijos RSS incluyen FFprobe. Hash/mtime de las tres fuentes permanecen
intactos. El crecimiento observado es pequeño entre 1 y 30 min; no certifica
VODs reales, otros codecs ni límites duros del RSS. Evidencia local:
`data/working/transcription-benchmarks/benchmark-{60,180,1800}s.json`.
La inferencia y RAM del modelo se midieron posteriormente por separado.

Reproducir preprocesamiento o medir ASR cuando el modelo esté instalado:

```zsh
python scripts/benchmark_transcription.py data/input/video.mp4 --audio-only --repeats 3
python scripts/benchmark_transcription.py data/input/video.mp4 \
  --model models/ggml-base.bin --language es --repeats 3
```

El segundo modo informa tiempos separados de preparación e inferencia. El RSS
de hijos es el mayor proceso individual, sin atribución de RAM por etapa ni suma
de procesos. Si el motor
o modelo falta, informa SKIPPED, sin convertirlo en una medición ASR válida.

Para ejecutar ASR real opt-in sin descargar nada, configura
`CLIP_FACTORY_WHISPER_MODEL`, opcionalmente `CLIP_FACTORY_WHISPER_BIN`,
`CLIP_FACTORY_SPEECH_ES/EN` (rutas a grabaciones propias) y
`CLIP_FACTORY_SPEECH_ES_TEXT/EN_TEXT` (texto de referencia), y ejecuta:

```zsh
python -m pytest tests/integration/test_transcript_integration.py -k real_whisper_asr -v
```

Este test inicial compara palabras de referencia; es una comprobación mínima,
sin evidencia de calidad en conversaciones largas. La suite adicional de
validación real compara WER normalizado y se describe a continuación.

### Validación real de 2B.1 — 2026-10-13

**642 pruebas aprobadas, 0 fallidas y 0 omitidas; doctor PASS**. Se activaron
las dos pruebas ASR antes omitidas y nueve pruebas reales adicionales. No se
modificaron los motores de Scene Detection, Audio Analysis ni Transcription
para obtener estos resultados. JSON estándar, SQLite, CLI, logs y temporales
se comprobaron junto al reconocimiento.

Se usó texto original escrito para esta validación, sintetizado localmente con
voces macOS ya disponibles: **Paulina (es_MX)** y **Samantha (en_US)**, 160 palabras
por minuto. Las muestras permanecen en working, con referencias, procedencia,
hashes y mtime en `fixtures-local/manifest.json`. No se afirma licencia comercial
ni se redistribuyen voces. **TTS controlado valida funcionamiento ASR real;
no estima precisión sobre grabaciones humanas naturales.**

- Español: 20,0356 s, 47 palabras; auto detecta `es`, cuatro segmentos.
  WER normalizado **8,51% (4 ediciones)**. Errores observados: «pan y flores»
  se reconoció «paniflores», y «calma» como «calmas».
- Inglés: 15,8628 s, 51 palabras; auto detecta `en`, cuatro segmentos en CPU.
  WER normalizado **0%** en esta muestra. Metal produjo cinco segmentos y el
  mismo texto normalizado; una segmentación distinta no implica desincronización.
- Idioma explícito ES/EN también pasó. Offsets originales **+2,5 s y −1,25 s**
  se preservan en `source_*`; timestamps relativos monótonos dentro del audio.
  La rejilla de 10 ms y los offsets correctos no certifican límites fonéticos.
- Silencio digital de 10 s: cero segmentos, idioma `null`, inferencia omitida
  por el gate. Tono audible 440 Hz de 10 s: **inferencia real ejecutada** y cero
  segmentos. Video sin audio: `SKIPPED/no_audio`, sin impedir escenas.
- Audio inglés consecutivo de 47,5884 s: dos bloques (30 s + 17,5884 s), texto
  reconocido en ambos, timestamps continuos y WER dentro del umbral de prueba
  (<=35%). Se recarga el modelo por bloque y se advierte sobre bordes/contexto.
- CLI combinada `--scenes --audio --transcribe`: corte visual conocido a 10 s,
  ventanas acústicas y voz española reconocida; JSON schema 4/SQLite coherentes.
- Hashes/mtime de las 13 fuentes de fixture permanecen intactos. No se tocó input.

Las once pruebas reales pasaron en **45,92 s**; la suite completa en **68,09 s**.
El umbral WER de 35% en tests detecta una integración rota, no define calidad
aceptable para todos los usos. Música, ruido complejo, acentos, voces débiles,
conversaciones naturales, diarización y VODs reales largos siguen sin evaluar.

Para reproducir, sin descargas automáticas:

```zsh
python scripts/prepare_transcription_validation.py --help
# Usar un destino nuevo en working; las voces deben estar ya disponibles.
python scripts/prepare_transcription_validation.py \
  --directory data/working/transcription-validation/fixtures-nuevas
export CLIP_FACTORY_WHISPER_MODEL="$PWD/models/ggml-base.bin"
export CLIP_FACTORY_WHISPER_BIN=whisper-cli
export CLIP_FACTORY_VALIDATION_FIXTURES="$PWD/data/working/transcription-validation/fixtures-nuevas/manifest.json"
export CLIP_FACTORY_SPEECH_ES="$PWD/data/working/transcription-validation/fixtures-nuevas/speech_es.wav"
export CLIP_FACTORY_SPEECH_EN="$PWD/data/working/transcription-validation/fixtures-nuevas/speech_en.wav"
export CLIP_FACTORY_SPEECH_ES_TEXT="$(cat data/working/transcription-validation/fixtures-nuevas/speech_es.txt)"
export CLIP_FACTORY_SPEECH_EN_TEXT="$(cat data/working/transcription-validation/fixtures-nuevas/speech_en.txt)"
python -m pytest -q
python -m clip_factory doctor
```

`say` y Metal necesitaron acceso nativo al equipo: dentro del entorno
restringido, `say` produjo un archivo vacío y Metal no pudo reservar buffers.
No se aceptaron esas ejecuciones como pruebas válidas. Los tests CPU y la
inferencia de Clip Factory funcionaron en el entorno restringido, sin red.
`doctor` sigue siendo una inspección de disponibilidad: su campo
`inference_validated: false` indica que doctor no realiza ASR, aunque las
pruebas de esta sección sí lo han validado. `engine_version` del adaptador puede
ser `null` porque `--help` no imprime versión; `whisper-cli --version` confirmó 1.9.5.

#### Benchmark de inferencia CPU y Metal

MacBook Air Apple M1, 8 GB; cuatro hilos; mismo modelo GGML base y WAV mono PCM16
16 kHz. Tres procesos nuevos **seriales** por idioma/backend, después de la
primera compilación Metal. Cada tiempo nativo incluye carga, inferencia y JSON,
sin FFmpeg ni el adaptador de bloques. RTF = tiempo / duración del audio;
un valor menor que 1 indica procesamiento más rápido que tiempo real.

- ES 20,0356 s: CPU **4,78 s** mediana (4,78–4,99), RTF **0,239**;
  Metal **1,16 s** (1,13–1,33), RTF **0,058**; aceleración **4,12×**.
- EN 15,8628 s: CPU **4,35 s** (4,30–4,59), RTF **0,274**;
  Metal **1,05 s** (1,02–1,06), RTF **0,066**; aceleración **4,14×**.
- Pico RSS máximo del proceso nativo: CPU **371,80 MiB**, Metal **382,06 MiB**.
  Peak memory footprint máximo: CPU **323,92 MiB**, Metal **334,74 MiB**.
  Medidos con `/usr/bin/time -l`; RSS/footprint son métricas distintas y no se
  suman. Tampoco representan RAM total del sistema ni GPU aislada (memoria unificada).
- Primera ejecución Metal, EN: **8,80 s** real, con **6,936 s** de compilación
  de shaders y total interno Whisper de **1,652 s**. No se incluyó en las
  medianas posteriores; el arranque frío puede ser más lento que CPU.

Los logs confirman `use gpu = 1`, backend `MTL0` y dispositivo `Apple M1`.
**El adaptador de Clip Factory conserva CPU `--no-gpu`**: Metal se midió mediante
whisper-cli nativo, sin implementar una nueva opción de producto. No atribuyas
la aceleración Metal a `analyze --transcribe`.

```zsh
python scripts/benchmark_whisper_backends.py \
  --manifest data/working/transcription-validation/fixtures-local/manifest.json \
  --model models/ggml-base.bin --backend cpu --repeats 3 \
  --output-dir data/working/transcription-validation/cpu-nuevo
# Repetir con --backend metal y otro output-dir nuevo.
```

#### Benchmark del adaptador de Clip Factory

Tres workers nuevos seriales por muestra, CPU/4 hilos en el entorno restringido.
Tiempos mediana de audio / inferencia / total del adaptador (excluye probe inicial):

- ES 20,0356 s: **0,070 / 4,775 / 4,854 s**, RTF de inferencia **0,238**.
- EN 15,8628 s: **0,063 / 4,265 / 4,335 s**, RTF **0,269**.
- EN consecutivo 47,5884 s, dos bloques: **0,120 / 12,430 / 12,557 s**,
  RTF **0,261**, 11 segmentos en cada repetición. Inferencia 12,376–12,973 s;
  no se perdió el segundo bloque y el temporal se retiró al finalizar.
- Pico RSS observado en estas nueve ejecuciones: Python **29,75 MiB**;
  mayor hijo **314,72 MiB**, separados con `resource.getrusage`. No sumarlos
  como un pico simultáneo. Los resultados retenidos y el modelo siguen teniendo
  límites independientes.

El tiempo de inferencia incluye carga del modelo por bloque, proceso y escritura
JSON; el tiempo acústico excluye esa espera y comprende lectura/auditoría/WAV y
parseo. FFmpeg puede avanzar unos buffers durante inferencia; no son tiempos CPU
exclusivos. La inicialización no atribuida a las etapas explica la pequeña
diferencia con el total. Los benchmarks nativos CPU/Metal usan acceso nativo al
equipo y otra instrumentación; pueden inicializar Metal incluso con GPU desactivada.
Por eso sus RSS no son intercambiables con los de estos workers restringidos.
No se extrapola el rendimiento a VODs de varias horas ni conversaciones naturales.

También se ejecutó `analyze --scenes --audio --transcribe` con el YAML real del
proyecto, **sin opciones de ejecutable/modelo**: encontró ambos mediante
configuración, publicó JSON y SQLite coherentes y preservó el medio original.
Su resultado está en `analyze-default-config.json` dentro de la evidencia.

Evidencia local: `data/working/transcription-validation/`, con verificación del
modelo, manifest, referencias, transcripciones, tests TXT/JUnit, JSON completo,
logs de backend y métricas nativas por repetición. `benchmark-cpu/benchmark.json`
y `benchmark-metal/benchmark.json` contienen cifras sin redondear;
`pipeline-benchmark-*.json` conserva las mediciones por bloques.

Archivos añadidos para validar: `scripts/prepare_transcription_validation.py`,
`scripts/benchmark_whisper_backends.py` y
`tests/integration/test_transcript_real_validation.py`. Configuración portátil
en `config/default.yaml`; pesos excluidos en `.gitignore`. README, ROADMAP y
skill video-analyzer se actualizan con evidencia. Sin commits/push.

Las pruebas verifican configuración, metadata, persistencia, CLI, errores y
protección de originales. Las pruebas de integración con medios sintéticos
requieren FFmpeg y FFprobe y se omiten explícitamente si faltan. Un probe
correcto no certifica que todo el video decodifique ni sustituye el futuro
control de calidad de exports.

Si falla un comando, revisa su mensaje y `logs/`. Los fallos producen un código
de salida distinto de cero; no se ocultan excepciones. Si `doctor` informa que
falta FFprobe, instala FFmpeg manualmente o configura el binario antes de
inspeccionar un video.

### Validación de Moment Detection 2B.2 — 2026-10-14

**854 pruebas aprobadas, 0 fallidas y 0 omitidas; doctor PASS**. La base previa
de 642 pasó en 70,51 s y la suite integrada en 82,46 s. Se añaden **212 pruebas**:
141 unitarias del motor, 34 de CLI/configuración y 37 integraciones, incluida
compatibilidad ASR real con el modelo/muestras locales ya disponibles. Los
contratos explícitos no se presentan como reconocimiento real ni las señales
sintéticas como precisión editorial. **2B.2 y Fase 2 aceptadas** dentro del
alcance de reglas locales verificadas, sin scoring ni edición.

La cobertura nueva comprueba coincidencias entre dos/tres familias, eventos
aislados, silencio, señales sostenidas, offsets distintos de pistas, PTS
fraccionarios/VFR, contexto y límites del video, fusión/supresión, entrada
malformada, timeouts y límites. También CLI/schema 5/SQLite/logs, ausencia
opcional de voz/audio, fallos sin publicaciones parciales y recuperación de
payload guardado sin ejecutar otra vez los analizadores. Un soporte opcional
demasiado largo no elimina pares utilizables. Hashes y mtime de los 17 archivos
previos auditados permanecen intactos, incluidos los tres motores de escenas,
audio y transcripción, que no se modificaron.

El helper de límites se comprobó con metadata real de MKV/MP4 y offsets
positivos/negativos, incluyendo selección de otra pista de video. Evidencia:
`data/working/moment-timeline-review/probe-evidence.json`. Snapshot de los
archivos y auditoría de recuperación:
`data/working/moment-recovery-20261014/`. Resultados finales y diagnóstico:
`data/working/moments-validation-20261014/full-tests-final.txt`,
`full-tests-final.xml`, `doctor.json` y `preservation.json`. Durante esa iteración
se informó que Git no estaba disponible; la revisión posterior encontró el
repositorio en el directorio padre del proyecto. No se creó otro repositorio
ni se reescribieron commits. Consulta el cierre de reproducibilidad en ROADMAP.

`scripts/benchmark_moments.py` separa los tiempos observados de los analizadores
y las reglas ejecutadas sobre sus modelos en memoria. Modo `--stress-events`
genera exclusivamente metadata explícita, sin multimedia ni ASR; no constituye
un benchmark de decodificación o reconocimiento. Ejemplos:

```zsh
python scripts/benchmark_moments.py --video data/input/video.mp4 --repeats 3
python scripts/benchmark_moments.py --stress-events 10000 --repeats 3
```

Voz requiere `--transcribe` explícito en el modo con video. El script utiliza
los modelos ya instalados y nunca descarga. RSS y tiempo de reglas deben
interpretarse por separado del decoder y de la inferencia; los resultados no
demuestran calidad narrativa ni rendimiento de VODs reales largos.

Mediciones reales en MacBook Air **Apple M1, 8 GB**, Python 3.12.14 y
FFmpeg/FFprobe 9.0.2, con resultados deterministas entre repeticiones:

- Video sintético de 20 s, cambios/pulsos conocidos: dos candidatos
  **3–9,5 s y 13–19,5 s**. Escenas **0,0750 s** y audio **0,0624 s**, ejecutados
  una sola vez. Reglas sobre ocho eventos, 100 repeticiones: mediana
  **0,000258 s (0,258 ms)**, rango 0,253–0,389 ms. RSS máximo Python
  **28,31 MiB**; mayor hijo **17,25 MiB**. Original preservado.
- Video español TTS controlado de 20,5 s, tres fuentes: escenas **0,0567 s**,
  audio **0,0691 s**, transcripción **5,1803 s** en total, con preparación
  **0,0654 s** e inferencia CPU **5,1021 s**. Reglas sobre 20 eventos,
  100 repeticiones: mediana **0,000571 s (0,571 ms)**, rango 0,555–1,106 ms,
  un candidato. RSS Python **29,78 MiB**, mayor hijo **296,94 MiB**. ASR real
  mediante la instalación existente; no es precisión sobre voz natural.
- Stress de metadata generada, tres repeticiones por tamaño y sin multimedia
  ni ASR: **1.000 eventos → 0,0232 s / 28,50 MiB Python**;
  **10.000 → 0,2358 s / 38,92 MiB**;
  **100.000 → 2,5476 s / 140,23 MiB**. Salidas 1/4/20 candidatos; el último
  alcanza el límite configurado y declara las supresiones. Memoria incluye
  inputs y resultados retenidos; no es el RSS aislado de una función.

Los RSS son máximos individuales, con etapas previas incluidas; no se suman
ni representan memoria simultánea total. Los tiempos de reglas excluyen
probe, decodificación e inferencia. Los analizadores se midieron una sola vez
por muestra y no proporcionan medianas de esas etapas. Evidencia reproducible:
`benchmark-synthetic.json`, `benchmark-three-sources.json` y
`benchmark-events-{1000,10000,100000}.json` bajo
`data/working/moments-validation-20261014/`. Los límites de salida y el stress
no evalúan narrativa, ruido/música complejos ni VOD real largo.

También se ejecutaron y publicaron estos ejemplos CLI completos, con igualdad
entre JSON y SQLite y hashes/mtime originales preservados. Sus destinos ya
existen: para repetirlos, elige nombres nuevos; la CLI no sobrescribe informes.

```zsh
python -m clip_factory analyze \
  data/working/transcription-validation/fixtures-local/speech_es_two_scenes.mkv \
  --moments --transcribe --moment-min-sources 3 \
  --output data/working/moments-validation-20261014/example-three-sources.json
python -m clip_factory detect \
  "data/working/moments-validation-20261014/synthetic-project/data/input/original momentos local.mkv" \
  --output data/working/moments-validation-20261014/example-synthetic-detect.json
```

El primer video tiene 20,5 s y voz TTS española controlada. Ejecutó inferencia
ASR real, detectó idioma `es` y produjo un candidato **3,92–15,6 s**, duración
**11,68 s**, con razones `audio_peak`, `scene_change` y `speech_activity` y
evidencia de las tres familias. Verifica integración y publicación sobre TTS;
no estima calidad narrativa ni precisión sobre voz humana natural. El ejemplo
`detect` sintético produjo los dos candidatos **3–9,5 s y 13–19,5 s**, sin ASR.
Resultados: `example-three-sources.json`, `example-synthetic-detect.json` y
`publication-validation.json` bajo `data/working/moments-validation-20261014/`.

### Reproducibilidad de referencias y continuidad Git

Los SHA-256 del manifiesto identifican el contenido de las muestras. Su
`mtime_ns` histórico documenta la generación, pero no es una fecha que deba
reproducirse tras copiar archivos o recuperar un checkout. Las pruebas de ASR
validan primero el hash esperado y capturan `(SHA-256, mtime_ns)` localmente;
al terminar cada operación, incluso ante un error, exigen igualdad exacta con
esa referencia inicial. No se aplican tolerancias a cambios durante el proceso.
El benchmark nativo también valida hash y tamaño frente al manifiesto y
conserva la comprobación estricta de metadata antes/después.

No regeneres un manifiesto sólo para aceptar archivos que han cambiado. Si el
hash difiere, investiga la procedencia del archivo. Los fixtures y su manifiesto
original no se modificaron para resolver la incidencia de fechas; se añadieron
regresiones que rechazan contenido alterado, cambios de 1 ns durante la
operación y modificaciones aunque el motor falle.

El repositorio Git se encuentra en el directorio padre de `clip-factory/`.
Los dos commits previos se conservan. El cierre de Fase 2 prepara únicamente
código, configuración, documentación y pruebas para revisión; los modelos,
medios, bases, logs y cachés permanecen locales. El respaldo externo completo
incluye también `.git` y archivos ignorados, que un commit por sí solo no guarda.
No se crea el commit ni se publica sin la revisión solicitada.

## Próximas fases

El detalle y los criterios están en [ROADMAP.md](ROADMAP.md):

1. Fundamentos (incluye estabilización).
2. Video Analyzer: 2A Scene Detection y Audio Analysis; 2B Transcription opcional
   y Moment Detection.
3. Clip Scorer: puntuación configurable 0–100.
4. Shorts Editor: clips verticales mediante FFmpeg.
5. Quality Control: validación de exports.
6. Pipeline Integration: flujo automatizado entre componentes.

Tendencias, subtítulos, hooks, thumbnails, publicación y analítica se conservan
en un backlog separado. **2B.1 está completada dentro del alcance de validación
controlada descrito.** Moment Detection 2B.2 y Fase 2 están aceptadas con reglas
locales verificadas. El siguiente paso es Fase 3, Clip Scorer, en una iteración
autorizada. Precisión en voz natural y VODs largos quedan en el
backlog; no se implementan scoring ni recortes aquí.

Usa contenido propio, autorizado, con licencia compatible o permitido por sus
términos. El proyecto no incluye evasión de DRM, controles de acceso, descargas
privadas, eliminación de marcas de agua ni evasión de copyright. Una futura
integración de yt-dlp sólo se añadirá para usos apropiados y permitidos.

## Licencia

Código bajo [MIT](LICENSE). Las licencias y términos de FFmpeg, dependencias,
modelos y contenido se revisan por separado; esta licencia no concede derechos
sobre videos de terceros.
