---
name: clip-scorer
description: Evalúa candidatos a clips con evidencia y pesos configurables de Clip Factory; se usa al ordenar momentos por potencial estimado, sin prometer viralidad.
---

# Clip Scorer

Puntúa momentos candidatos de manera explicable. En fase 1 existe esta guía y
el módulo reservado, pero no un motor automático de scoring operativo.
No presentes evaluaciones manuales como resultados de ese motor.

## Dimensiones

Considera, cuando existan datos suficientes:

```text
Hook
Emoción
Sorpresa
Actividad
Audio
Contexto
Duración
Completitud
```

Asigna cada dimensión disponible a una escala 0–100 y explica la señal:
apertura comprensible para hook, expresividad observada para emoción, contraste
identificable para sorpresa, cambios relevantes para actividad, inteligibilidad
para audio, autonomía narrativa para contexto, adecuación a la duración
configurada y cierre de la idea para completitud. No infieras emoción sólo a
partir del volumen ni sorpresa sólo a partir de un cambio de escena.

## Fórmula y configuración

```text
Clip Score = sum(peso_i * valor_i) / sum(peso_i)
```

Incluye únicamente dimensiones con datos y peso positivo. Los pesos deben
leerse de `scoring.weights` en configuración; no los fijes dentro del motor futuro. Valida pesos
finitos y no negativos y una suma activa mayor que cero. El score final está
entre 0 y 100. Conserva el valor decimal para ordenar; para asignar categoría
redondea al entero más cercano, con mitades hacia arriba, y declara esa regla.

Declara dimensiones ausentes y cobertura: suma de pesos evaluados dividida por
suma de pesos configurados. Si no hay datos suficientes para una evaluación
útil, devuelve score `null` y explica qué falta. Renormalizar datos disponibles
no aumenta la confianza; señala una cobertura baja y evita ordenar sin reservas
candidatos con coberturas muy distintas.

## Categorías

```text
90-100 = excelente
80-89  = fuerte
70-79  = posible
60-69  = débil
0-59   = descartar
```

Usa la expresión **"potencial estimado"**. Nunca afirmes que un clip será viral.
Devuelve inicio/fin en segundos, score, categoría, cobertura, pesos efectivos,
valores, razones y limitaciones. Las razones deben distinguir señales medidas
de juicios editoriales. Preserva la metadata original y escribe la evaluación
como resultado separado. Consulta `AGENTS.md`.
