---
name: trend-researcher
description: Organiza temas y oportunidades de contenido a partir de datos aportados por el usuario; se usa al evaluar tendencias o planificar ideas para Clip Factory.
---

# Trend Researcher

Identifica temas con potencial a partir de información proporcionada por el
usuario. La fase 1 incluye esta guía; no hay recolector de tendencias ni API
conectada. Consulta `AGENTS.md` y preserva el procesamiento local y gratuito.

## Evidencia

Distingue explícitamente:

- **Dato observado**: información verificable, con fuente, fecha y periodo.
- **Inferencia**: interpretación derivada de esos datos; explica el vínculo.
- **Hipótesis**: posibilidad pendiente de validación.

No inventes tendencias, cifras, crecimiento ni actualidad. Un tema popular en
una muestra no demuestra crecimiento. Si faltan comparaciones temporales, marca
crecimiento como desconocido. Registra el idioma, país y plataforma cuando la
fuente los identifique. No conviertas una hipótesis en dato por repetición.

## Puntuación

```text
Trend Score =
actualidad
+ crecimiento
+ interés
+ facilidad
- competencia
```

Usa componentes de 0 a 5: actualidad según fechas verificadas; crecimiento
según cambio observado entre periodos comparables; interés según señales
aportadas; facilidad según recursos locales disponibles; competencia según
oferta comparable observada. El resultado bruto va de -5 a 20; declara la
escala. Explica cada valor y evidencia. Si falta un componente, devuélvelo
como `null` y deja el score completo como `null`; no inventes un cero ni
compares resultados parciales con scores completos.

## Informe por tema

```text
Tema
Potencial
Actualidad
Competencia
Facilidad de producción
Riesgo
Oportunidad
Razones
```

Añade los componentes y el Trend Score, fuentes/fechas y etiquetas de dato,
inferencia o hipótesis en las razones. El potencial es una estimación, sin
promesas de viralidad. Incluye riesgos concretos de producción, permisos o
incertidumbre de datos cuando correspondan.

## Extensión futura

Separa adquisición, normalización y puntuación. Un futuro proveedor de fuentes
públicas deberá entregar fuente, fecha de captura, periodo, región, plataforma,
tema y métricas originales. La lógica de puntuación no debe depender de un
proveedor concreto. Documenta límites de cobertura y términos de uso. No
implementes APIs de pago ni envíes datos del usuario a servicios externos como
parte de esta guía.
