# Clip Factory — Roadmap

## Visión
CLIpping transforma contenido propio o autorizado en contenido corto mediante
componentes gratuitos, locales y sustituibles. Prioridades: originales intactos,
funcionamiento correcto, coste recurrente adicional de $0 y CPU/Apple Silicon.

## Estado real
- Última fase aceptada: **FASE 2 — VIDEO ANALYZER**, dentro del alcance de
  reglas locales verificadas; Fase 1 conserva su aceptación.
- Base previa verificada: **424 pruebas aprobadas, 0 fallidas y 0 omitidas; doctor PASS**.
- Base de transcripción aceptada: **642 pruebas aprobadas, 0 fallidas,
  0 omitidas; doctor PASS**.
- Validación actual: **865 pruebas aprobadas, 0 fallidas y 0 omitidas;
  doctor PASS** el 2026-10-14.
- Continuidad Git verificada en el directorio padre de `clip-factory/`:
  commits `4cc1dd1` y `b1b3359` conservados. El commit de cierre de Fase 2
  queda pendiente de revisión; no se realiza push.
- **2A.1 Scene Detection y 2A.2 Audio Analysis implementadas y verificadas**;
  Fase 2 aceptada; Fases 3–6 permanecen pendientes.
- **2B.1 Transcription opcional completada y validada con inferencia real**:
  whisper.cpp 1.9.5 y base multilingüe, muestras TTS controladas ES/EN.
  No es una evaluación de precisión sobre voz natural. Se conservan Scene
  Detection, Audio Analysis y Fase 1.
- **2B.2 Moment Detection completada y validada**: reglas locales
  sobre escenas/audio y transcripción opcional, CLI `--moments`/`detect`, schema 5.
  No se implementan Clip Scorer ni recortes.

## FASE 1 — FUNDAMENTOS
- [x] Estructura del repositorio y `pyproject.toml`.
- [x] Configuración YAML y perfil `mac_m1`.
- [x] Logging, CLI y diagnóstico de dependencias.
- [x] FFmpeg/FFprobe para metadata local.
- [x] SQLite, pruebas básicas e integraciones con videos sintéticos.
- [x] README, AGENTS.md y cinco skills iniciales.
- [x] Conservar stderr y distinguir éxito, advertencia y error de FFprobe.
- [x] Coordinar persistencia SQLite y publicación JSON con recuperación segura.
- [x] Evitar que la limpieza borre temporales activos.
- [x] Reforzar aperturas y rutas sensibles frente a cambios de enlaces.
- [x] Aislar handlers y preservar errores funcionales cuando falle el log.
- [x] Generar nombres seguros ante nombres extensos y Unicode.
- [x] Consultar SQLite sin crear bases accidentalmente.
- [x] Validar regresiones, suite completa, doctor, M1 y originales intactos.
- [x] Verificar coherencia ROADMAP/AGENTS/README.

Criterios de aceptación: todos los puntos anteriores implementados y validados;
fallos visibles, recuperación documentada, sin sobrescribir originales ni
resultados existentes, dependencias mínimas y sin servicios de pago.

Aceptación verificada el 2026-10-13: 178 tests PASS, doctor PASS, Python y
FFmpeg/ffprobe ARM64 en M1. `data/input/` conserva hash y mtime; las regresiones
con videos sintéticos verifican también preservación. La recuperación se probó
con interrupción real de procesos, fallos de inserción/publicación/compensación
y conflictos de contenido. No se simularon cortes físicos de energía.
La protección es proporcional al entorno local: SQLite no utiliza un VFS
especializado contra procesos hostiles con los mismos permisos del usuario.

## FASE 2 — VIDEO ANALYZER
### 2A.1 Scene Detection
- [x] Definir contrato del detector y resultado independiente de CLI/SQLite.
- [x] Configurar motor local, umbrales, duración mínima y límites de ejecución.
- [x] Detectar cambios de escena con timestamps del stream elegido.
- [x] Validar cortes conocidos, ausencia de cortes, VFR y origen temporal.

Aceptación de Scene Detection verificada el 2026-10-13: FFmpeg `scdet`, modelos
inmutables, PTS entero/base racional sin FPS promedio, score visual opcional,
selección por índice absoluto (default excluye portadas), timeout independiente
y streaming de metadata con buffers acotados. `min_scene_duration` filtra la
separación desde inicio/corte aceptado; no garantiza la longitud del tramo final.
`analyze --scenes` publica schema 2 mediante el mecanismo recuperable existente;
sin esa opción conserva schema 1 y `metadata_only`.

Validación: 178 pruebas existentes + 98 nuevas = **276 PASS**, sin omisiones;
`doctor PASS`, Python 3.12.14/FFmpeg 9.0.2 en MacBook Air M1 de 8 GB. Cortes
conocidos, ausencia de cortes, VFR, PTS fraccionarios y offsets positivos y
negativos verificados con medios reales sintéticos. También múltiples pistas,
sin video, corrupción, errores con salida 0, timeouts, interrupción del lector,
publicación/recuperación y hashes/mtime intactos. Sin nuevas dependencias.

Benchmark sintético 1080p/30, MPEG-4, CPU/4 hilos, tres repeticiones seriales:
60 s en 1,518 s mediana; 180 s en 4,402 s mediana. Pico RSS Python 23,3 MiB y
mayor hijo 54,5 MiB, medidos por separado. Más detalle y límites en README.
No valida VODs largos/4K ni garantiza detección de transiciones sutiles; flashes
pueden dar falsos positivos. **Scene Detection por sí sola no completa Fase 2A ni Fase 2.**

### 2A.2 Audio Analysis
- [x] Definir contrato de señales de audio y ventanas temporales.
- [x] Medir intensidad y picos mediante herramientas locales justificadas.
- [x] Manejar videos sin audio y conservar la sincronización temporal.

Aceptación verificada el 2026-10-13: modelos inmutables independientes de
CLI/JSON/SQLite, FFmpeg `astats`/`asetnsamples`, RMS normalizado/dBFS y pico
de muestra por ventanas configurables. Eventos `peak`, `energy_rise`,
`energy_drop` y `silence` separados y agrupados por continuidad; no se atribuyen
emociones, gritos ni interés. RMS global ponderado por muestras, mono/estéreo
sin downmix, selección default o índice absoluto y último bloque sin padding.

PTS enteros/base de samples y origen de pista conservan offsets positivos,
negativos y priming AAC. Auditoría previa al agrupamiento rechaza discontinuidades
mayores que un tick del contenedor; no rellena huecos con silencios. Ventanas y
eventos tienen precisión declarada, y los eventos resolución de ventana.
Silencio digital se serializa como RMS 0/dBFS null, sin NaN/Infinity. Sin audio:
`SKIPPED` con advertencia, sin romper el detector visual ni simular silencio.

`analyze --audio` y `--scenes --audio` usan schema 3; metadata y sólo escenas
conservan schemas 1/2. Validación, logging, publicación y recuperación existentes
reutilizados. Timeouts independientes, salida incremental con buffers de 64 KiB,
límites explícitos de ventanas/eventos y ventana flotante <=32 MiB. `astats
length=0` evita buffers deslizantes innecesarios. Hasta 64 canales/768 kHz;
un decoder activo por etapa. Sin nuevas dependencias ni servicios.

Validación: **276 existentes + 148 nuevas = 424 PASS**, 0 fallidas/omitidas;
`doctor PASS`, Python 3.12.14, FFmpeg/FFprobe 9.0.2, MacBook Air M1 de 8 GB.
Pruebas unitarias y 39 integraciones nuevas de audio/CLI cubren señales conocidas,
silencio, picos/cambios, agrupación, mono/estéreo en antifase, múltiples pistas,
offsets, cuantización a 44,1 kHz, AAC, ausencia de audio, corrupción, timeouts,
límites, cuatro modos CLI, recuperación y hashes/mtime preservados.

Benchmark de audio en videos sintéticos 1080p/30 con PCM float estéreo 48 kHz,
ventanas 0,5 s, CPU/4 hilos, tres ejecuciones seriales: 60 s en **0,268 s mediana**,
180 s en **0,648 s**. Pico RSS Python **27,3 MiB**, mayor hijo **18,4 MiB**,
medidos por separado. No incluye análisis visual ni publicación JSON/SQLite.
Limitaciones: no LUFS/true peak/SPL, resolución de eventos por ventana,
discontinuidades rechazadas; VODs largos, cambios de formato en una pista y
matriz de versiones siguen sin validar. **2A.2 aceptada. En ese hito, Fase 2
seguía pendiente.**

### 2B.1 Transcription opcional — completada con validación controlada real
- [x] Definir proveedor independiente de CLI/SQLite y modelo local opcional.
- [x] Evaluar whisper.cpp, mlx-whisper y faster-whisper con fuentes primarias.
- [x] Adaptador whisper.cpp CPU, streaming PCM acotado, timestamps y offsets.
- [x] Configuración/CLI/JSON schema 4, logs y publicación recuperable existentes.
- [x] Ausencia de motor/modelo/audio con SKIPPED/WARN; errores visibles.
- [x] Evitar descargas automáticas durante análisis o diagnóstico.
- [x] Pruebas del contrato con dobles explícitos y FFmpeg real, regresiones.
- [x] Benchmark M1 de preprocesamiento, separado de inferencia.
- [x] Validar transcripción **real** satisfactoria en español e inglés.
- [x] Medir inferencia y RAM real del modelo en M1/8 GB.
- [x] Aceptar/completar 2B.1 después de las validaciones reales anteriores.

Implementación del 2026-10-13: `analysis/transcript.py`, modelos inmutables,
whisper-cli externo opcional y GGML local, sin dependencias Python nuevas.
PCM16 mono/16k en streaming con bloques <=30 s (~0,916 MiB de PCM), auditoría
PTS antes/después de resample y límites de segmentos/JSON/pipes, más 32 MiB
de texto UTF-8 acumulado. El modelo
añade su propia RAM y se recarga por bloque; contexto entre bloques pendiente.
Silencio por pico de muestras y por segmento, filtrado de tokens/confianza
conservador; son heurísticas, sin garantías contra ruido/música/alucinaciones.
Esa iteración no añadió VAD, diarización, traducción, momentos ni scoring.

La suite distingue integraciones FFmpeg/proceso de ASR real: los tests con
dobles no reconocen voz. La implementación inicial dejó dos tests ASR opt-in
omitidos por falta de motor/modelo; se ejecutaron satisfactoriamente en la
validación autorizada posterior descrita abajo. Originales preservados.

Benchmark real de preparación acústica en MacBook Air M1/8 GB, Python 3.12.14,
FFmpeg 9.0.2: videos sintéticos negros 1080p/1fps, FLAC estéreo 48k, bloques
30 s, tres workers nuevos por duración. Medianas: 60 s → **0,145 s**;
180 s → **0,364 s**; 1.800 s → **3,371 s**. Pico máximo Python **33,44 MiB**,
mayor hijo FFmpeg/FFprobe **19,00 MiB**, medidos individualmente; no es RAM total.
Incluye conversión/auditoría/gate y WAV temporal, excluye probe del tiempo,
inferencia, generación y persistencia. Hash/mtime intactos. No es benchmark ASR
ni un VOD real. Inferencia y memoria del modelo se midieron posteriormente.
Mediciones reproducibles en `scripts/benchmark_transcription.py`; detalle README.

**Aceptación real verificada el 2026-10-13:** Homebrew instaló whisper.cpp
**1.9.5 ARM64** y dependencias necesarias (ggml, llama.cpp y actualización
libomp), sin paquetes Python nuevos. Único modelo descargado: GGML `base`
multilingüe oficial, **147.951.465 bytes (141,10 MiB)**. SHA-256 verificado:
`60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe`.
Configuración portátil `executable: whisper-cli`, `model: models/ggml-base.bin`;
pesos excluidos de Git. Inferencia local sin APIs ni descarga obligatoria.

Texto original y voces macOS existentes Paulina/Samantha, TTS controlado con
referencias: ES **20,0356 s / 47 palabras / WER 8,51% (4 errores)**; EN
**15,8628 s / 51 palabras / WER 0%**. Auto detecta es/en; idioma explícito,
segmentos/timestamps y offsets **+2,5/−1,25 s** pasan. Silencio evita inferencia;
tono sin voz ejecuta modelo y produce cero segmentos; sin audio SKIPPED.
EN consecutivo **47,5884 s**, dos bloques y voz reconocida en ambos.
CLI combinada verifica escenas/audio/ASR/JSON/SQLite; el YAML instalado funciona
sin pasar rutas de modelo/ejecutable en la CLI. Hash/mtime de 13 fixtures intactos.
No se modificaron los tres motores de análisis para superar la validación.

**642 PASS, 0 FAIL, 0 SKIP**, incluidas las dos ASR antes omitidas y nueve
pruebas reales adicionales; suite 68,09 s, subconjunto real 45,92 s. `doctor
PASS` con motor/modelo disponibles (doctor no realiza inferencia). Evidencia
local: `data/working/transcription-validation/`, con manifest, referencias,
transcripciones, XML/TXT de tests, hashes y benchmarks completos; README explica
reproducción y límites. **2B.1 completada. En ese hito, Fase 2 seguía pendiente.**

Benchmark nativo, CPU vs Metal, cuatro hilos, tres procesos nuevos seriales por
muestra/backend, caché de shaders preparada: ES CPU **4,78 s / RTF 0,239**,
Metal **1,16 s / 0,058**; EN CPU **4,35 s / 0,274**, Metal **1,05 s / 0,066**.
Aceleración observada **4,12–4,14×**; RSS máximo CPU **371,80 MiB**, Metal
**382,06 MiB**, individual, sin sumar RAM del sistema/GPU. Arranque Metal frío
EN **8,80 s**, incluyendo compilación de shaders (6,936 s), separado de medianas.
Metal Apple M1 confirmado en logs; **el adaptador sigue CPU**, sin feature nueva.

Benchmark del adaptador CPU, preparación/inferencia separadas: ES **0,070/4,775 s**,
EN **0,063/4,265 s**, consecutivo 47,5884 s **0,120/12,430 s** (RTF 0,261;
dos bloques, 11 segmentos). Pico Python **29,75 MiB**, mayor hijo **314,72 MiB**
en workers restringidos, no intercambiable con RSS nativo. Modelos se recargan
por bloque; no es evaluación de precisión natural ni benchmark de VOD largo.
Ruido complejo/música/acentos/VOD real y contexto entre bloques siguen en backlog.

### 2B.2 Moment Detection — completada con reglas locales verificadas
- [x] Combinar señales disponibles en candidatos explicables.
- [x] Identificar inicio, fin, razones, evidencia y señales ausentes.
- [x] Producir metadata estructurada sin editar videos.
- [x] Reutilizar modelos existentes en memoria, sin decodificación ni inferencia nuevas en reglas.
- [x] Integrar `analyze --moments` y alias `detect` con configuración, logs y recuperación.
- [x] Normalizar PTS/orígenes y límites de la pista de video seleccionada.
- [x] Cerrar suite completa, doctor y benchmarks M1 con evidencia final.
- [x] Aceptar 2B.2 y Fase 2 tras cumplir los criterios de implementación/validación.

Implementación del 2026-10-14: `analysis/moments.py`, reglas deterministas y
modelos inmutables independientes de CLI/SQLite/FFmpeg/Whisper. Se requieren
dos familias entre escenas, audio y voz; varias métricas de audio no cuentan
como familias distintas. Coincidencias de tres familias son próximas entre sí.
Picos, cambios de energía y actividad de voz son señales observadas, sin
atribuir emoción, calidad narrativa ni potencial viral. Silencio, transitorios
acústicos demasiado largos y voz insuficiente se excluyen explícitamente.

Contexto y duraciones configurables, fusión limitada por máximo, supresión de
solapamientos/cooldown entre inicios y conservación cronológica sin ranking.
Voz prolongada puede localizar soporte sin inventar alineación de palabras;
una tercera fuente opcional no invalida un par utilizable. Salida acotada por
max_candidates y 64 evidencias por candidato, con advertencias de supresión;
max_events y timeout fallan visiblemente. Búsqueda O(N log N), memoria ligada
a las señales hasta límites configurados, sin modelos ni dependencias nuevos.

Origen común desde PTS/base racional del primer frame del video seleccionado;
audio y voz conservan offsets originales. Fin mediante duración de esa pista
o tag Matroska DURATION como fin absoluto según FFmpeg. Nunca mediante FPS
promedio, último corte o duración global de otra pista. Metadata contradictoria
falla; límites desconocidos son null y llevan advertencia, sin certificar el
último frame. `--moments` ejecuta escenas/audio una vez y sólo añade voz si se
solicita `--transcribe`; reglas reutilizan los resultados. JSON schema 5, logs
y publicación/recuperación existentes, sin migración SQLite ni outputs parciales
ante fallos. Metadata básica fallida con momentos se rechaza antes de decodificar;
los modos anteriores conservan su comportamiento diagnóstico.

Aceptación verificada el 2026-10-14: **854 PASS, 0 FAIL, 0 SKIP; doctor PASS**.
La base de 642 pasó en 70,51 s; suite completa en 82,46 s. Se añaden 212 pruebas:
141 unitarias del motor, 34 de CLI/config y 37 integraciones, incluida
compatibilidad ASR real con muestras TTS/modelo previamente disponibles.
Cobertura: reglas, offsets, familias, fusión/supresión, límites, CLI y
recuperación, con hashes/mtime de 17 archivos previos auditados intactos.
Los contratos explícitos no cuentan como reconocimiento real; las muestras
sintéticas no evalúan calidad narrativa ni precisión sobre voz natural.
Resultados finales, diagnóstico y benchmarks:
`data/working/moments-validation-20261014/`. Auditoría/snapshot local:
`data/working/moment-recovery-20261014/`. En esa iteración se informó ausencia
de Git; la auditoría posterior localizó el repositorio en el directorio padre.
No se creó otro repositorio ni se modificaron los commits anteriores.
**2B.2 y Fase 2 aceptadas dentro de este alcance; Fase 3 permanece pendiente.**

Benchmark M1 Air/8 GB, Python 3.12.14/FFmpeg 9.0.2: reglas sobre datos ya
calculados, 100 repeticiones por muestra. Video sintético 20 s, ocho eventos,
dos candidatos 3–9,5 y 13–19,5 s: mediana reglas **0,258 ms**; escenas
**0,0750 s**, audio **0,0624 s** medidos una vez. Video TTS español 20,5 s,
20 eventos, un candidato y tres fuentes reales: reglas **0,571 ms**; escenas
**0,0567 s**, audio **0,0691 s**, ASR preparación/inferencia **0,0654/5,1021 s**
(total voz 5,1803 s). RSS Python **28,31/29,78 MiB** y mayor hijo
**17,25/296,94 MiB** respectivamente, máximos individuales con etapas previas,
sin sumar RAM simultánea. Originales preservados y salida determinista.

Stress separado, metadata generada sin multimedia/ASR, tres repeticiones:
1.000/10.000/100.000 eventos → medianas **0,0232/0,2358/2,5476 s**, RSS Python
**28,50/38,92/140,23 MiB**. Memoria incluye inputs/resultados; salidas 1/4/20
candidatos, con supresiones declaradas. No demuestra calidad narrativa ni
rendimiento de VOD real. Reproducción en `scripts/benchmark_moments.py`; detalle
y evidencia en README y `data/working/moments-validation-20261014/`.

Criterios de aceptación: 2A funcional y probada; candidatos 2B reproducibles y
temporales precisos; transcripción opcional sin impedir el resto; originales
intactos. Requiere Fase 1 aceptada. No incluye scoring ni recortes.

## FASE 3 — CLIP SCORER
- [ ] Puntuar candidatos de 0 a 100 con pesos configurables.
- [ ] Explicar dimensiones, cobertura, categorías y potencial estimado.
- [ ] Probar datos incompletos y evitar promesas de viralidad.

Criterios de aceptación: resultados acotados, configurables y explicables sobre
candidatos de Fase 2, con pruebas independientes y documentación.

## FASE 4 — SHORTS EDITOR
- [ ] Recortar candidatos seleccionados mediante FFmpeg.
- [ ] Crear vertical 9:16 por crop/padding sin deformaciones.
- [ ] Preservar audio y originales; outputs y temporales separados.
- [ ] Validar 1080×1920, H.264/AAC y fuentes con diferentes proporciones.

Criterios de aceptación: exportación local reproducible y probada, sin
sobrescrituras, con errores visibles. Requiere intervalos válidos de Fase 2;
Fase 3 aporta selección priorizada, no acopla el editor al scorer.

## FASE 5 — QUALITY CONTROL
- [ ] Validar existencia, reproducción/decodificación, codecs y resolución.
- [ ] Validar FPS, audio, duración y nombres.
- [ ] Detectar frames negros inesperados y emitir evidencia de los controles.
- [ ] Diferenciar PASS, FAIL y controles no ejecutados/no aplicables.

Criterios de aceptación: ningún export se marca terminado sin los controles
requeridos; pruebas de fallos reales y resultados estructurados. Requiere
exports de Fase 4; el QC permanece ejecutable por separado.

## FASE 6 — PIPELINE INTEGRATION
- [ ] Integrar los componentes mediante el orquestador.
- [ ] Controlar estados, recuperación, reintentos y recursos locales.
- [ ] Exponer un flujo CLI completo y mantener ejecución por etapas.
- [ ] Validar el recorrido completo hasta export aprobado por QC.

Criterios de aceptación: Fases 1–5 aceptadas, ejecución completa reproducible,
fallos recuperables, trazabilidad y protección de originales en todo el flujo.

## Dependencias entre etapas
Fundamentos → análisis → candidatos → scoring → edición → QC → integración.
Scene Detection y Audio Analysis comparten metadata y línea temporal; pueden
desarrollarse por separado. La transcripción es opcional. Cada componente
conserva una interfaz independiente del pipeline final.

## Historial de hitos
- 2026-10-13: base de Fase 1 validada con 107 pruebas y doctor PASS.
- 2026-10-13: auditoría identificó siete áreas de estabilización.
- 2026-10-13: roadmap persistente y nueve reglas de mantenimiento incorporados.
- 2026-10-13: siete hallazgos corregidos y validados; Fase 1 aceptada con 178
  pruebas y doctor PASS; ninguna funcionalidad de Fase 2 implementada.
- 2026-10-13: Scene Detection de 2A implementada; 276 pruebas PASS, doctor PASS,
  CLI y preservación verificadas, benchmarks sintéticos medidos en M1/8 GB.
- 2026-10-13: Audio Analysis 2A.2 aceptada con 424 pruebas PASS, doctor PASS,
  cuatro modalidades CLI, offsets/muestras y originales verificados; benchmarks
  acústicos medidos en M1/8 GB. Sin transcripción, momentos ni scoring.
- 2026-10-13: Transcription opcional implementada mediante whisper.cpp; **631
  PASS, 2 SKIP, 0 fallidas; doctor PASS**. Las 424 regresiones se conservan;
  147 unitarias del motor +35 CLI/config +25 integraciones FFmpeg/proceso.
  CLI combinada, JSON schema4/SQLite/logs/recuperación y originales verificados.
  Benchmark acústico real hasta 30min; ASR ES/EN e inferencia pendientes por
  ausencia de motor/modelo/grabaciones. **2B.1 no completada**. Sin descargas,
  dependencias nuevas, commits ni cambios en Scene Detection/Audio Analysis.
- 2026-10-13: validación real autorizada de whisper.cpp 1.9.5 + base multilingüe
  con SHA-256 oficial; **642 PASS, 0 SKIP; doctor PASS**. ES/EN reconocidos sobre
  TTS controlado, offsets/silencio/tono/CLI/bloques/originales verificados;
  benchmarks CPU/Metal y memoria medidos. **2B.1 completada**, sin Moment
  Detection/Clip Scorer, sin cambios de motores ni commits/push.
- 2026-10-14: Moment Detection implementado con reglas sobre modelos existentes,
  CLI `--moments`/`detect` y JSON schema 5. **854 PASS, 0 FAIL, 0 SKIP; doctor
  PASS**, incluidas 642 regresiones y 212 pruebas nuevas. **2B.2 y Fase 2
  aceptadas**, con originales intactos y benchmarks M1. Snapshot/auditoría local
  preservados; sin modelos/dependencias nuevos, scoring, recortes ni commits/push.
- 2026-10-14: cierre de reproducibilidad y continuidad Git. La auditoría
  encontró el repositorio en el padre de `clip-factory/`, con dos commits
  anteriores intactos; no fue necesario inicializar ni recuperar otro historial.
  Las 10 pruebas fallidas comparaban mtime local con fechas históricas del
  manifiesto, aunque los 13 SHA-256 y tamaños coincidían. Esas fechas no son
  una identidad portable que Git preserve. Se mantiene SHA-256 de referencia
  y se comprueba hash/mtime exactos antes/después de cada operación, también
  ante fallos. El benchmark nativo aplica la misma separación entre identidad
  del contenido y preservación durante la ejecución. Sin tolerancias ni
  regeneración del manifiesto; fixtures, pesos y motores no se modificaron.
  **865 PASS, 0 FAIL, 0 SKIP en 83,57 s; doctor PASS**, desde la carpeta
  habitual y las referencias locales existentes, con 11 regresiones nuevas.
  Se verificaron hash, tamaño y mtime de 20 originales/archivos protegidos.
  Evidencia en `data/working/phase2-close-20261014-44d4b18c/`.
  La revisión de Git detectó metadata Finder `.DS_Store` dentro de `refs`,
  que no es una referencia válida ni un commit. El cierre exige copiar y
  verificar primero todo el árbol, conservar ese artefacto en cuarentena
  reversible fuera de `refs` y comprobar `git fsck` antes de preparar cambios.
  **Fase 2 mantiene su aceptación; Fase 3 no iniciada.** El commit sigue
  pendiente de la revisión humana de los archivos preparados; no hay push.

## Próximas tareas recomendadas
1. Siguiente tarea: Fase 3 — Clip Scorer, en una iteración autorizada por
   separado; no se implementa scoring en esta entrega.
2. Conservar trazabilidad, normalización temporal y fuentes ausentes al
   consumir candidatos; no equiparar mediciones con calidad narrativa.
3. Mantener Fases 3–6 pendientes. Precisión en voz
   natural, ruido complejo, VODs reales largos/4K y optimización Metal del
   adaptador quedan en backlog; no confundir TTS controlado con esos controles.

## Archivos de esta estabilización
- Implementación: `media/ffmpeg.py`, `media/probe.py`, `media/validation.py`,
  `utils/files.py`, `utils/logging.py`, `database/repository.py`, `cli/main.py`
  dentro de `src/clip_factory/`, y `scripts/clean.py`.
- Seguimiento y documentación: `ROADMAP.md`, `AGENTS.md`, `README.md`, `.gitignore`.
- Regresiones: `test_media_ffmpeg.py`, `test_media_probe.py`,
  `test_media_integration.py`, `test_clean.py`, `test_logging_stabilization.py`,
  `test_repository_safety.py`, `test_cli_diagnostics.py`, `test_files_review.py`,
  `test_stabilization_review.py`, `test_publication_recovery.py` en `tests/`.
- Sin nuevas dependencias ni migración del esquema SQLite (versión 1).
- La revisión inicial informó ausencia de `.git`; la auditoría posterior lo
  encontró en el directorio padre. El proyecto es un subdirectorio del mismo
  árbol de trabajo, con los commits anteriores conservados.

## Backlog separado
- Investigación de tendencias mediante evidencia y fuentes públicas.
- Subtítulos locales, sincronización y opciones de presentación.
- Generación de hooks y propuestas editoriales.
- Thumbnails.
- Publicación opcional, con permisos y términos de cada plataforma.
- Analítica local y evaluación de resultados.
- Matriz de Python admitidos y prueba de instalación mediante wheel.
- Benchmark con VODs largos en M1, memoria acotada y cancelación.
- Evaluar VFS/aislamiento adicional sólo si cambia el modelo de seguridad local;
  requiere justificar el coste arquitectónico antes de incorporarlo.

Estas ideas se conservan sin alterar las seis fases ni declararse implementadas.
