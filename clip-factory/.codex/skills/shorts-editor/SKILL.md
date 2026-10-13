---
name: shorts-editor
description: Guía la conversión local de clips a vertical 9:16 mediante FFmpeg, con crop o padding y subtítulos; se usa al preparar exports para Shorts, TikTok y Reels en Clip Factory.
---

# Shorts Editor

Transforma una copia de un clip en contenido vertical. En fase 1 los módulos de
edición están reservados: no hay comandos de recorte ni exportación operativos.
Esta guía define el contrato para implementarlos posteriormente.

## Formato objetivo

```text
1080x1920
9:16
H.264
AAC
```

FFmpeg es la herramienta principal. Inspecciona primero con FFprobe la geometría
real, relación de aspecto de píxel, rotación, duración, FPS y pistas de audio.
No asumas que todo video es 16:9 ni interpretes metadata de rotación como si ya
estuviera aplicada a los píxeles.

## Geometría

Detecta `16:9`, `4:3`, `21:9`, `1:1` y `9:16` a partir de dimensiones de
visualización, con tolerancia documentada; conserva otras relaciones como
`custom` y aplica el modo configurado. El probe de fase 1 usa una tolerancia
relativa del 2 %. No fuerces una etiqueta si no coincide.

- **crop** (`editing.mode: crop`): escala manteniendo proporciones hasta cubrir 1080×1920 y recorta
  el excedente según posición configurada. Advierte sobre contenido fuera del
  encuadre. El centrado no equivale a seguimiento inteligente de personas.
- **padding** (`editing.mode: pad`): escala manteniendo proporciones para caber en 1080×1920 y
  rellena el espacio restante según configuración.

Nunca deformes el video. Para entradas verticales compatibles preserva la
composición y aplica sólo los ajustes necesarios para el contrato de salida.
No asumas límites de duración de plataformas: utiliza un perfil explícito y
verificado cuando sea necesario.

## Preservación y audio

Lee `AGENTS.md`. Protege el original, incluso cuando esté fuera de input.
Crea una copia o temporal independiente en working y un archivo de export nuevo;
rechaza destinos que coincidan con input, originales o archivos existentes.
Valida `start < end`, límites de duración y timestamps precisos antes de recortar.
No publiques un output parcial como terminado si falla FFmpeg.

Mantén audio cuando exista y codifica AAC para el export. Si la fuente no tiene
audio, registra la limitación; no inventes una pista ni declares que está
presente. No elimines marcas de agua ni saltes DRM o controles de acceso.

## Subtítulos y entrega

Permite subtítulos locales mediante un adaptador de transcripción sustituible.
Distingue archivo lateral SRT/VTT de subtítulos incrustados y de texto quemado.
Valida orden, tiempos, sincronía respecto al recorte, codificación y legibilidad
en las zonas seguras configuradas. Nunca inventes texto que no fue transcrito.

Registra fuente, intervalo, modo geométrico, codecs, configuración y motor.
Somete cada export al control de calidad. Usa CPU como alternativa obligatoria;
aceleración Apple Silicon es opcional y CUDA no es requisito. No instales ni
descargues motores automáticamente.
