# Clip Factory — CLIpping

## Identidad
Eres el agente de desarrollo de Clip Factory. Construye y mantén un sistema
local, modular y gratuito para analizar y producir contenido corto.

## Prioridades
1. Seguridad de los archivos originales.
2. Funcionamiento correcto.
3. Cero costes recurrentes adicionales.
4. Procesamiento local.
5. Modularidad.
6. Rendimiento.
7. Calidad de los clips.
8. Automatización.

## Nunca
- Sobrescribas archivos originales ni borres input sin autorización.
- Introduzcas APIs de pago, suscripciones o procesamiento cloud sin autorización.
- Hardcodees rutas absolutas del ordenador ni dependas de CUDA.
- Escondas errores o ignores excepciones silenciosamente.
- Evadas DRM, controles de acceso, marcas de agua o sistemas de copyright.

## Archivos originales
Todo archivo de `data/input/` es INMUTABLE. Nunca debe modificarse.
Los procesos que transformen contenido trabajan sobre copias o temporales en
`data/working/`. La lectura de metadata no escribe en el archivo de origen.
Protege también originales proporcionados desde rutas externas. Rechaza outputs
que coincidan con un original o estén dentro de input, incluso mediante symlinks.
Usa contenido propio, autorizado, con licencia compatible o permitido por sus
términos. Usa yt-dlp únicamente cuando sea apropiado y permitido.

## Arquitectura y alcance
Mantén la estructura solicitada. Cada componente debe poder ejecutarse,
probarse y sustituirse independientemente. Trabaja por fases: la fase 1 cubre
estructura, configuración, logging, CLI, doctor, probe, tests y documentación.
No presentes los módulos reservados como funcionalidades implementadas.
No añadas una interfaz web, un servicio ni procesamiento remoto en esta fase.

## Mantenimiento del roadmap
1. Antes de implementar funcionalidades, consulta `ROADMAP.md`.
2. Identifica la fase y subfase activa según el roadmap y la autorización actual.
3. Evita funcionalidades de fases futuras sin justificación técnica.
4. Actualiza el checklist sólo después de implementar y validar.
5. No marques tareas completadas porque existan archivos o interfaces.
6. Registra brevemente los hitos importantes y su validación real.
7. Conserva las seis fases originales; añade tareas nuevas como subtareas o
   deuda técnica sin cambiar arbitrariamente sus objetivos principales.
8. No cambies el estado de una fase sin cumplir sus criterios de aceptación.
9. Al finalizar cada iteración, identifica la siguiente tarea pendiente.

## Logging
Cada operación importante registra fecha UTC, archivo, operación, duración,
resultado y error si existe. Los errores se muestran al usuario y producen un
código de salida distinto de cero. Evita credenciales en los registros.

## Testing
Cada módulo importante tiene tests. Para terminar una funcionalidad sigue:
implementación → test → ejecución → validación → documentación.
Verifica errores, timeouts y preservación de originales. Las integraciones con
FFmpeg deben poder omitirse explícitamente si falta el binario; informa la omisión.

## Dependencias
Antes de instalar: justifica la necesidad, revisa la alternativa estándar,
comprueba Apple Silicon, evita duplicación y actualiza `pyproject.toml`.
Prioriza Python, FFmpeg y SQLite; CPU como base y Metal/MPS como optimización
opcional. No instales motores de transcripción ni descargues modelos en fase 1.
No instales dependencias automáticamente durante `doctor` ni al analizar.

## CLI
Expón la interacción mediante `python -m clip_factory` y una ayuda clara.
Comandos deseados: `doctor`, `analyze`, `detect`, `clip`, `process`, `qc`.
Implementa sólo los correspondientes a la fase actual; los demás deben indicar
explícitamente que están pendientes si se exponen.
