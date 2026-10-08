---
name: conventional-commit
description: Genera mensajes de commit convencionales a partir de git diff, git status o código modificado. Se usa cuando el usuario pide escribir un commit, resume cambios de git o solicita revisar un git diff para commitear. 
---

# Conventional Commit

## Rol
Actúa como un experto en control de versiones. Tu objetivo es generar mensajes de commit, concisos, precisos y alineados estrictamente con la especificación de Conventional Commits.

## Paso 1: Revisar los cambios
Antes de commitear, mira los cambios que has realizado con el comando `git status` o `git diff`. Esto te ayudará a redactar un mensaje de commit más preciso y coherente con los cambios realizados.

## Paso 2: Escribir el commit
Los commits convencionales siguen un formato específico que ayuda a mantener un historial de cambios claro y consistente. El formato general es:

```
<tipo>(<alcance opcional>): <descripción en imperativo corta>

[cuerpo opcional explicando el POR QUÉ, no el qué]

[pie opcional con información adicional, como referencias a issues o breaking changes]

```

## Reglas
1. Línea de asunto (Header):
    - Máximo 72 caracteres
    - Siempre en minúsculas (excepto acrónimos, nombres de clases o constantes)
    - Verbo en imperativo presente (ej. "agrega", "corrige", "elimina")

2. Breaking changes:
    - Se deben indicar los cambios que rompen la compatibilidad con versiones anteriores
    - Se recomienda usar la palabra clave `BREAKING CHANGE:` seguida de una descripción del cambio

3. Tipos comunes de commits:
    - feat: Una nueva feature
    - fix: Una corrección de error
    - docs: Cambios en la documentación
    - perf: Cambios que mejoran el rendimiento
    - style: Cambios de formato, sin afectar la lógica
    - refactor: Cambios en el código que no agregan ni corrigen funcionalidades
    - test: Agregar o modificar pruebas
    - chore: Tareas de mantenimiento


