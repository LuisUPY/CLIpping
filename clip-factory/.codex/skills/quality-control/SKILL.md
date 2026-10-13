---
name: quality-control
description: Verifica exports de video y subtítulos de Clip Factory antes de marcarlos terminados; se usa para controles de calidad locales con resultados comprobables y errores explícitos.
---

# Quality Control

Valida outputs sin modificar originales. En fase 1 no hay un motor completo de
QC ni un comando `qc` operativo. FFprobe sólo aporta metadata; no demuestra que
todos los frames y el audio se decodifiquen correctamente.

## Comprobaciones requeridas

```text
archivo existe
↓
video reproducible
↓
codec correcto
↓
resolución correcta
↓
FPS válido
↓
audio presente
↓
duración válida
↓
sin frames negros inesperados
↓
subtítulos válidos
↓
nombre válido
```

Compara contra el perfil de export efectivo: por defecto 1080×1920, H.264 y
AAC. Comprueba stream de video y decodificación con FFmpeg, FPS positivo y
finito dentro del perfil, duración positiva dentro de tolerancia del intervalo
solicitado y presencia/decodificación del audio requerido. Un contenedor MP4
por sí solo no prueba codecs ni reproducibilidad.

Detecta intervalos negros con umbrales y duración mínima configurables. No
confundas una transición o escena oscura intencionada con un fallo: registra
intervalos y justifica cuáles son inesperados. No declares ausencia de negro
si no ejecutaste este análisis.

Para subtítulos exigidos, comprueba formato, codificación, secuencia temporal,
`0 <= inicio < fin <= duración`, sincronización tras el recorte y referencias
a archivos existentes. Si son opcionales y no existen, registra el control
como no aplicable con su razón. Validar SRT no demuestra legibilidad de texto
quemado: ese modo requiere revisar la imagen renderizada.

Valida el nombre frente a una convención explícita del perfil y que no colisione
con un original. No inventes una convención y después afirmes conformidad.

## Resultado

Estos ejemplos describen el contrato futuro, no un control ya ejecutado:

```json
{
  "status": "PASS",
  "checks": {
    "file_exists": true,
    "video_valid": true,
    "audio_valid": true,
    "resolution": true,
    "duration": true
  }
}
```

```json
{
  "status": "FAIL",
  "errors": []
}
```

En un resultado real incluye **todos** los controles requeridos, la evidencia,
herramienta/versión, tolerancias y duración de cada operación. Cada `false`
requiere un error concreto; `errors` no puede quedar vacío cuando hay un fallo.
Una comprobación no ejecutada usa `null` con motivo, nunca `true`. Usa estado
`INCOMPLETE` cuando faltan controles requeridos y no se ha demostrado un fallo;
no conviertas omisiones, timeouts o falta de binarios en `PASS`.
`PASS` exige verificar todos los controles requeridos y justificar los no
aplicables. Un error comprobado produce `FAIL`, aunque también falten controles.
Conserva resultados y errores sin ocultarlos. Consulta `AGENTS.md`.
