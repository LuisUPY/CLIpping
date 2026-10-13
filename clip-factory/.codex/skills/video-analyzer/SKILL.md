---
name: video-analyzer
description: Inspecciona metadata de videos locales y guía el análisis modular de audio, escenas, transcripción y momentos de Clip Factory; se usa para analizar un video sin recortarlo.
---

# Video Analyzer

Analiza videos localmente y produce metadata estructurada. En fase 1 sólo
funcionan `probe` y `analyze` para metadata de FFprobe; audio, escenas,
transcripción y momentos están reservados. Nunca presentes esas etapas como
ejecutadas si no hay motor y resultados verificables. No crea clips.

## Uso actual

Desde la raíz del proyecto, con su entorno Python activo:

```zsh
python -m clip_factory doctor
python -m clip_factory probe data/input/video.mp4
python -m clip_factory analyze data/input/video.mp4 --output -
```

Obtén duración, resolución, FPS, codecs y presencia de audio mediante el módulo
`media/probe.py`. No supongas FPS constante, duración conocida o una sola pista.
Conserva `null` y explica metadata ausente; no rellenes valores arbitrarios.
El probe lee el original y no certifica una decodificación completa del video.

## Pipeline previsto

```text
VIDEO
 ↓
MEDIA PROBE
 ↓
AUDIO ANALYSIS
 ↓
SCENE DETECTION
 ↓
TRANSCRIPTION
 ↓
MOMENT DETECTION
```

Cada etapa debe tener entrada y salida independientes, tests y errores visibles.
Registra nombre/versión del motor y configuración con sus resultados. Un motor
de transcripción debe poder sustituirse sin cambiar la detección de momentos.
Usa CPU como base y optimizaciones Apple Silicon cuando estén disponibles;
CUDA nunca es requisito. No instales motores o modelos automáticamente.

## Contrato de momentos futuro

Este ejemplo es un **contrato ilustrativo**, no una salida implementada ni un
resultado de análisis real:

```json
{
  "video": "example.mp4",
  "duration": 1234.5,
  "moments": [
    {
      "start": 531.2,
      "end": 559.8,
      "reason": ["speech_intensity", "audio_peak", "scene_change"]
    }
  ]
}
```

Usa segundos respecto al inicio del video, preserva precisión decimal y declara
la resolución temporal del análisis. Valida `0 <= start < end <= duration`
cuando la duración sea conocida. No uses índices de frames como segundos ni
mezcles timestamps de audio extraído sin compensar su offset. Cada razón exige
una señal observada: un pico de audio por sí solo no demuestra interés.
Representa una etapa no ejecutada con estado pendiente/omitido y su motivo;
`moments: []` sólo significa que se ejecutó la detección sin candidatos.

Lee `AGENTS.md`: originales inmutables; intermediarios en working; metadata
fuera de input; contenido propio o autorizado y sin evasión de DRM o accesos.
