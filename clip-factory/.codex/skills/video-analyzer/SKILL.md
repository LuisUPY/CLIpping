---
name: video-analyzer
description: Analiza videos locales con metadata, escenas, audio, voz opcional y candidatos explicables por reglas; no puntúa ni recorta videos.
---

# Video Analyzer

Analiza videos localmente y produce metadata estructurada. `probe` y `analyze`
leen metadata de FFprobe; `analyze --scenes` ejecuta Scene Detection (2A.1) y
`analyze --audio` ejecuta Audio Analysis (2A.2). Pueden solicitarse juntos.
`analyze --transcribe` añade transcripción opcional con whisper.cpp y un modelo
local; 2B.1 está aceptada tras probar inferencia real española/inglesa sobre TTS
controlado. No representa precisión sobre voz natural.
`analyze --moments` y `detect` combinan esas señales en candidatos explicables
mediante reglas locales. No presentes etapas omitidas como
ejecutadas ni tests con dobles como reconocimiento real. No crea clips.
Consulta `ROADMAP.md` y `AGENTS.md`.

## Uso actual

Desde la raíz del proyecto, con su entorno Python activo:

```zsh
python -m clip_factory doctor
python -m clip_factory probe data/input/video.mp4
python -m clip_factory analyze data/input/video.mp4 --output -
python -m clip_factory analyze data/input/video.mp4 --scenes --output -
python -m clip_factory analyze data/input/video.mp4 --audio --output -
python -m clip_factory analyze data/input/video.mp4 --scenes --audio --output -
python -m clip_factory analyze data/input/video.mp4 --transcribe --output -
python -m clip_factory analyze data/input/video.mp4 --scenes --audio --transcribe \
  --transcript-model models/ggml-base.bin --transcript-language auto --output -
python -m clip_factory analyze data/input/video.mp4 --moments --output -
python -m clip_factory detect data/input/video.mp4 --output -
python -m clip_factory analyze data/input/video.mp4 --moments --transcribe --output -
```

Obtén duración, resolución, FPS, codecs y presencia de audio mediante el módulo
`media/probe.py`. No supongas FPS constante, duración conocida o una sola pista.
Conserva `null` y explica metadata ausente; no rellenes valores arbitrarios.
El probe lee el original y no certifica una decodificación completa del video.

## Scene Detection disponible

Usa `analysis/scenes.py:detect_scenes` como motor independiente. La CLI conserva
el JSON de metadata (schema 1) sin opciones de análisis; sólo escenas publica schema 2,
`scope: metadata_and_scenes`, `analysis.scenes: completed` y `scene_detection`.
El detector devuelve dataclasses; la CLI gestiona JSON, SQLite y logs.

- Sensibilidad: `--scene-threshold` 0–100 (base 10); menor = más sensible.
- `--scene-timeout` limita escenas (base 300 s), separado de `media.probe_timeout`.
- `--video-stream` usa el índice absoluto de FFprobe. Sin él, prefiere video
  default o el primero reproducible; excluye portadas y rechaza pistas de audio.
- `--min-scene-duration` limita la separación desde inicio/corte aceptado;
  no garantiza el tramo final. YAML: `analysis.scenes`, con las mismas claves
  `threshold`, `timeout`, `video_stream`, `min_scene_duration`.

Conserva `pts`, `start_pts` y `time_base`. `timestamp_seconds` es relativo al
primer frame decodificado de la pista elegida; `source_timestamp_seconds`
mantiene el offset del archivo, incluso negativo. No mezcles pistas/audio sin
convertir sus referencias. No derives segundos del FPS ni de `lavfi.scd.time`,
que se redondea. `score` es cambio visual 0–100, o `null` si falta; no mide interés.

`changes: []` sólo indica ejecución válida sin cortes. No conviertas errores,
timeout, ausencia de video o metadata vacía en éxito sin cortes. FFmpeg procesa
frames en streaming a resolución nativa; Python recibe sólo metadata. El
detector no escribe videos ni instala dependencias. Requiere `scdet` en FFmpeg;
filtros ausentes fallan visiblemente. Los flashes pueden dar falsos positivos
y las transiciones sutiles pueden omitirse; documenta sensibilidad y resultados.

## Audio Analysis disponible

Usa `analysis/audio.py:analyze_audio` con modelos inmutables. La CLI serializa
`audio_analysis` en schema 3 cuando no se añade voz/momentos, `scope: metadata_and_audio` o
`metadata_and_scenes_and_audio`; metadata y sólo escenas conservan schemas 1/2.
Los motores son independientes y se ejecutan por etapas, con un decoder activo.
Reutiliza publicación SQLite/JSON y `recover`; no publiques parciales como éxito.

RMS combina energía de canales sin downmix; el pico es el máximo sample peak,
no true peak ni LUFS. Eventos: `peak` por umbral absoluto, `energy_rise` y
`energy_drop` por diferencia RMS relativa, `silence` por RMS bajo con duración
mínima. Agrupa sólo ventanas consecutivas del mismo tipo. No deduzcas emociones,
gritos, habla o interés. Un pico y un cambio relativo pueden solaparse; el
silencio RMS puede contener impulsos breves. La resolución del evento es la ventana.

CLI: `--audio-window` (0.01..10 s, base 0.5), `--audio-peak-threshold` (base -6
dBFS), `--audio-silence-threshold` (base -50), `--audio-change-threshold` (base 6
dB), `--audio-min-silence` (base 0.5 s), `--audio-timeout` (base 300 s) y
`--audio-stream` (índice absoluto FFprobe; default/primera pista si se omite).
`--audio-energy-floor` (base -120 dBFS) limita sólo comparaciones relativas.
YAML: `analysis.audio`; consulta README para las diez claves y rangos. Opciones
acústicas requieren `--audio`, `--moments` o `detect`; no activan audio por sí solas.

Conserva `pts`, `end_pts`, `start_pts`, `time_base: 1/sample_rate` y
`source_time_base`. `start_seconds`/`end_seconds` son relativos al primer frame
de audio, y `source_start_seconds`/`source_end_seconds` conservan el offset
original, incluyendo priming y valores negativos. No mezcles estas referencias
con segundos relativos de escenas sin convertirlas. El motor audita continuidad
antes de formar ventanas; rechaza huecos mayores a `timestamp_tolerance_samples`.
No rellena huecos con silencio. La última ventana conserva las muestras reales.

En un resultado PASS, `rms: 0` y dBFS `null` representan silencio digital
(-infinito). Sin audio, auto-selección devuelve `SKIPPED`, métricas `null` y
`analysis.audio: skipped_no_audio`, con advertencia y escenas operativa.
No lo presentes como audio silencioso ni como ejecución sin eventos. Un índice
explícito inválido, corrupción, metadata incompleta o timeout es un error visible.

Procesa sólo metadata incremental; no guardes muestras. Respeta los límites
`--audio-max-windows`/`--audio-max-events` y el buffer de ventana de 32 MiB
(no es la RAM global del proceso). Frecuencias <=768 kHz, 1–64 canales;
`astats length=0` evita buffers deslizantes innecesarios. No instales dependencias.

## Transcripción opcional 2B.1 validada con inferencia real controlada

Usa `analysis/transcript.py:transcribe_media` con `TranscriptConfig`; devuelve
modelos inmutables, sin CLI/logging/SQLite. Sólo el adaptador whisper.cpp está
implementado. Requiere `whisper-cli` local con JSON completo y un GGML existente.
No añade dependencias Python. No instales ni descargues automáticamente al
analizar o diagnosticar. Antes de cualquier instalación/descarga significativa,
explica modelo/tamaño/destino y solicita autorización.

Multilingües: tiny 75 MiB, base 142 MiB, small 466 MiB; RAM orientativa upstream
273/388/852 MB, no mediciones M1. Base es el punto inicial para M1/8 GB. `.en`
sólo inglés; auto/es requieren multilingüe. Medium/large (1,5/2,9 GiB) no son la
base recomendada. Consulta evaluación, fuentes e instalación manual en README.

CLI requiere `--transcribe`. Opciones/YAML `analysis.transcription`:
`--transcript-engine`/`engine`, `--transcript-executable`/`executable`,
`--transcript-model`/`model`, `--transcript-language`/`language` (auto/es/en),
`--transcript-timeout`/`timeout`, `--transcript-audio-stream`/`audio_stream`,
`--transcript-chunk-seconds`/`chunk_seconds`,
`--transcript-max-segments`/`max_segments`,
`--transcript-silence-threshold`/`silence_dbfs`.
La pista es un índice absoluto FFprobe e independiente de `--audio-stream`.
Las rutas relativas del modelo se resuelven contra root. El YAML del proyecto
selecciona `models/ggml-base.bin`; sin YAML los defaults incorporados usan null.
Se instaló con autorización whisper.cpp 1.9.5 Homebrew y únicamente base
multilingüe (147.951.465 bytes, SHA-256 verificado, detalle README). Reutiliza
la instalación; no vuelvas a pedir permiso para acciones ya autorizadas.

Con transcripción solicitada sin momentos se publica schema 4; sin ella se
conservan 1/2/3. Momentos solicitados publican schema 5.
`transcription` incluye motor/versión observable/modelo/configuración, idioma,
segments, warnings y tiempos separados de audio e inferencia.
`analysis.transcription: skipped_no_audio/skipped_engine_unavailable/
skipped_model_unavailable` conserva las otras etapas operativas y muestra WARN.
Una inferencia fallida, corrupción, JSON inválido, pista errónea o timeout falla
visiblemente y no publica un parcial como éxito. Reutiliza logs y recuperación.

FFmpeg decodifica una pista en streaming; retiene bloques PCM16 mono/16k <=30 s,
con backpressure y buffers de pipes acotados. WAV/JSON temporales exclusivos en
working se retiran al terminar o fallar; una muerte abrupta puede dejar su carpeta
privada (clean no la borra automáticamente). Se recarga modelo por bloque,
acotando audio pero perdiendo contexto y rendimiento entre bordes. La memoria
del modelo es adicional al bloque; no prometas que el proceso completo usa 1 MiB.
Además de max_segments, se limita a 32 MiB el texto UTF-8 retenido; exceder
cualquier límite produce error visible, sin truncar una salida como completa.

Conserva PTS/orígenes del stream. `start_seconds/end_seconds` son relativos al
primer audio decodificado; `source_start_seconds/source_end_seconds` conservan
offsets originales, también negativos. No mezcles segundos de escenas sin
convertir referencias. El motor rechaza discontinuidades sin rellenarlas.
La rejilla Whisper es 10 ms, con límites estimados; no es alineación exacta.

El gate de silencio usa pico de muestras, no detector de voz. Omite bloques
muy débiles y rechaza segmentos ubicados sobre silencio. Tokens especiales y
baja confianza se filtran conservadoramente. No inventes texto para reemplazar
lo omitido: cero segmentos e idioma null cuando no se obtiene voz reconocible.
Ruido/música/voz ininteligible pueden producir errores aun con filtros;
antifase puede cancelar voz al pasar a mono. No declares garantía de ausencia de
alucinaciones ni emociones. No hay VAD/diarización/traducción implementados.

Pruebas ASR reales opt-in: `CLIP_FACTORY_WHISPER_MODEL`,
`CLIP_FACTORY_WHISPER_BIN`, grabaciones `CLIP_FACTORY_SPEECH_ES/EN` y referencias
`CLIP_FACTORY_SPEECH_ES_TEXT/EN_TEXT`. Si faltan, informa SKIP/PENDIENTE; no
simules aceptación. `scripts/benchmark_transcription.py --audio-only` mide
preparación real sin ASR; con `--model` mide inferencia local. No confundas ambos.
Marca 2B.1 completada sólo después de probar transcripción real satisfactoria.
Validación de 2B.1: **642 PASS, 0 SKIP; doctor PASS**. ES Paulina 20,0356 s,
47 palabras, WER 8,51%; EN Samantha 15,8628 s, 51 palabras, WER 0%, sobre
texto original y TTS local sin voces descargadas. Idiomas, offsets +2,5/−1,25,
silencio, tono audible sin voz con inferencia real, ausencia de audio, CLI
combinada y bloques consecutivos 47,5884 s validados. No atribuyas estas cifras
a voz natural, música, conversaciones largas u otros modelos.
Suite adicional: `tests/integration/test_transcript_real_validation.py`, env
`CLIP_FACTORY_VALIDATION_FIXTURES` con manifest, además del motor/modelo.
Evidencia: `data/working/transcription-validation/`; README contiene benchmarks.
Metal Apple M1 medido nativamente (4,12–4,14× en muestras), pero el adaptador
mantiene CPU --no-gpu. No lo presentes como feature de analyze. Contexto de
bloques y precisión natural siguen pendientes; Moment Detection se describe abajo.

## Moment Detection 2B.2

Usa `analysis/moments.py:detect_moments` con modelos de resultados ya obtenidos.
Es un motor puro: no lee archivos ni ejecuta decodificación, ASR o persistencia. La CLI
`analyze --moments` o el alias `detect` calcula escenas/audio una sola vez;
transcripción sólo con `--transcribe`. Sin motor/modelo ASR, las otras etapas
siguen operativas y la fuente omitida se declara. No añadas modelos ni
dependencias para ejecutar reglas. No hay scoring, ranking ni recortes.

Requiere dos familias distintas entre escenas, audio y voz; varios eventos
acústicos cuentan como una familia. Las triples son próximas entre sí, sin
usar coincidencia transitiva. Razones: `scene_change`, `audio_peak`,
`audio_energy_rise`, `audio_energy_drop`, `speech_activity`. Audio aporta el
inicio de transitorios; silencio y eventos sostenidos se excluyen. Voz exige
duración y palabras suficientes; no interpreta emociones ni interés editorial.

El origen común es `scenes.start_pts * scenes.time_base`, primer frame de la
pista de video seleccionada. Convierte audio/voz mediante PTS/base/origen
originales y valida orden, cobertura y coherencia. No uses FPS promedio,
inicio global del contenedor o último corte como referencias sustitutas. El
fin usa duración de la pista seleccionada; en MKV/WebM, tag DURATION según
convención FFmpeg de fin absoluto. Metadata contradictoria falla. Duración
desconocida sigue null con advertencia: no certifiques el límite final.

Contexto, fusión y duraciones están acotados. Si la unión excede el máximo,
los solapamientos se suprimen conservando el primer candidato; cooldown se
mide entre inicios. `max_candidates` conserva orden cronológico, sin ranking;
64 evidencias por candidato como máximo, con advertencia de supresiones.
La voz prolongada puede localizar soporte; el extracto es contexto del
segmento original, sin alineación por palabra. No reconstruyas texto omitido.

YAML `analysis.moments`, CLI `--moment-*` con guiones en lugar de `_`:
`min_duration=5`, `max_duration=45`, `context_before=2`, `context_after=3`,
`coincidence_window=1`, `merge_gap=1`, `cooldown=10`, `min_sources=2` (2 o 3),
`min_speech_duration=2`, `min_speech_words=6`, `max_audio_event_duration=4`,
`max_candidates=20`, `max_events=100000`, `timeout=10`. Segundos salvo palabras,
fuentes y conteos. CLI prevalece y exige `--moments` o `detect`; consulta README
para rangos. Timeout sólo limita reglas; los analizadores conservan los suyos.

Schema 5 publica `analysis.moments: completed`, lista superior `moments` y
`moment_detection` con configuración, fuentes ausentes/disponibles, duración y
procedencia, advertencias y contadores. Candidatos: `start`, `end`, `duration`,
`reasons`, `evidence`; evidencia conserva pista, PTS/base, intervalos original,
relativo y de soporte. Sin momentos se conservan schemas 1–4/not_run.
`moments: []` sólo significa reglas ejecutadas sin candidatos suficientes.
Metadata fallida con momentos se rechaza antes de decodificar; fallos de
etapas, límites o reglas no publican parciales. Reutiliza logs, SQLite y recover.

Validación final: **854 PASS, 0 FAIL, 0 SKIP; doctor PASS** el 2026-10-14,
incluida base previa de 642 y compatibilidad ASR real con muestras TTS locales.
Consulta README/ROADMAP y `data/working/moments-validation-20261014/` para
cobertura y benchmarks. Las pruebas verifican reglas/sincronización, no
precisión narrativa sobre voz natural. Flashes, ruido, música y errores ASR
pueden producir candidatos irrelevantes: presenta resultados para revisión.
Fase 3 — Clip Scorer es la siguiente fase pendiente.

## Pipeline con momentos

```text
VIDEO
 ↓
MEDIA PROBE
 ↓
SCENE DETECTION
 ↓
AUDIO ANALYSIS
 ↓
TRANSCRIPTION (sólo --transcribe)
 ↓
MOMENT DETECTION
```

Cada etapa debe tener entrada y salida independientes, tests y errores visibles.
Registra nombre/versión del motor y configuración con sus resultados. Un motor
de transcripción debe poder sustituirse sin cambiar la detección de momentos.
Usa CPU como base y optimizaciones Apple Silicon cuando estén disponibles;
CUDA nunca es requisito. No instales motores o modelos automáticamente.

Lee `AGENTS.md`: originales inmutables; intermediarios en working; metadata
fuera de input; contenido propio o autorizado y sin evasión de DRM o accesos.
