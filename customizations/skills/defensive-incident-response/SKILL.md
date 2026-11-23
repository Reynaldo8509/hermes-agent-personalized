---
name: defensive-incident-response
description: Guía el triaje defensivo de incidentes autorizados.
version: 1.0.0
author: Reynaldo Rodriguez (Rey); Hermes Agent
license: MIT
platforms: [linux]
category: security
tags:
  - incident-response
  - defensive-security
---

# Respuesta defensiva a incidentes Skill

Esta skill guía el triaje y la documentación de incidentes en sistemas autorizados. Ayuda a separar evidencia de hipótesis y a planificar acciones seguras; no ejecuta una respuesta automatizada ni autoriza cambios por sí sola.

## When to Use

- Ante alertas, actividad sospechosa o indicios de compromiso en un sistema dentro del alcance autorizado.
- Para ordenar una revisión inicial y preparar un informe reproducible.
- No la uses para investigar sistemas de terceros sin autorización explícita.

## Prerequisites

- Confirma el propietario, los activos, el alcance y las acciones permitidas.
- Identifica la zona horaria y la fuente horaria de la evidencia.
- Respeta las políticas de manejo, retención y cadena de custodia aplicables.
- No solicites ni copies contraseñas, tokens, claves privadas ni datos personales innecesarios.
- Utiliza únicamente las capacidades autorizadas del entorno, como `terminal`, `read_file` y `search_files`.

## How to Run

1. Delimita el sistema y el objetivo con el operador antes de consultar datos.
2. Empieza con observaciones de solo lectura y registra el origen y la hora de cada hallazgo.
3. Si una acción puede bloquear acceso, aislar un equipo, detener un servicio, borrar datos o alterar evidencia, explica su impacto y espera autorización explícita.
4. Resume los hechos, la incertidumbre y los siguientes pasos en un informe breve.

## Quick Reference

| Fase | Resultado esperado |
|---|---|
| Alcance | Activos, ventana temporal y autoridad definidos |
| Preservación | Evidencia pertinente registrada antes de alterar el sistema |
| Evaluación | Hechos separados de hipótesis, con impacto y confianza |
| Contención | Opciones reversibles descritas y autorizadas por el responsable |
| Cierre | Acciones, límites, validación y pendientes documentados |

## Procedure

1. **Aclarar el incidente.** Registra el síntoma, el primer momento observado, los activos afectados, el origen de la alerta y el propietario que autoriza la investigación. Si el alcance es ambiguo, limita la consulta a la información ya entregada y pide precisión antes de explorar otros sistemas.
2. **Preservar primero.** Prioriza registros, estado de procesos, conexiones y cambios recientes mediante lecturas no destructivas. Anota la hora, la fuente, el comando o herramienta y cualquier limitación. Calcula hashes solo cuando sea pertinente y esté autorizado. No limpies archivos, reinicies servicios ni cambies reglas antes de preservar la evidencia necesaria.
3. **Triage con evidencia.** Busca indicadores relacionados con el síntoma y la ventana temporal; no amplíes el alcance por coincidencias aisladas. Valida cada indicador con una fuente independiente cuando sea posible. Distingue observación, inferencia e hipótesis, y asigna confianza explícita.
4. **Valorar impacto.** Identifica activos, cuentas y servicios potencialmente afectados. Describe alcance confirmado y desconocido. Usa ATT&CK solo cuando la evidencia respalde la técnica; no atribuyas actores ni intenciones sin sustento.
5. **Proponer contención.** Presenta primero opciones proporcionales y reversibles, junto con impacto, dependencias y método de recuperación. Aislar, bloquear, apagar, borrar, revocar credenciales o detener servicios requiere autorización explícita del propietario, salvo que ya esté comprendida en una política de respuesta autorizada.
6. **Remediar y validar.** Tras la autorización, cambia solo los elementos acordados. Verifica el resultado con evidencia independiente, comprueba efectos colaterales y deja disponible el procedimiento de rollback. Si una acción falla, no la repitas automáticamente cuando pueda multiplicar el impacto.
7. **Cerrar y aprender.** Resume cronología, evidencia, acciones autorizadas, resultado, limitaciones, riesgos residuales y medidas preventivas. Conserva referencias a la evidencia sin copiar secretos ni material personal innecesario.

## Pitfalls

- No conviertas una alerta o un hash aislado en una conclusión de compromiso.
- No envíes mensajes, correos, postulaciones ni cargas de prueba durante el triage.
- No ejecutes explotación, persistencia, evasión, borrado de logs ni acciones sobre terceros.
- No reinicies o aísles un activo antes de considerar el impacto sobre continuidad y evidencia.
- No reveles credenciales ni las incluyas en informes, comandos, fixtures o capturas.
- No describas una remediación propuesta como ejecutada o verificada.

## Verification

- El alcance y la autorización aparecen documentados.
- Los hallazgos citan su fuente y hora, y separan hechos de hipótesis.
- Las acciones externas o con impacto se identifican como pendientes hasta tener autorización.
- Los resultados se validan sin exponer secretos ni incluir datos personales innecesarios.
- Los límites y la vía de rollback quedan claros cuando hubo cambios autorizados.
