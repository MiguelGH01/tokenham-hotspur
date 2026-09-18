# PRD: agente de voz para citas de clínica

HackSpain 2026, Prosper Track. Versión 1, viernes 18 de septiembre de 2026. Sustituye al documento de plan anterior.

## 1. Resumen

Un agente de voz que atiende las llamadas entrantes de citas de una clínica como lo haría una recepcionista: averigua quién llama y qué necesita, busca al paciente en los registros, consulta disponibilidad real y reserva, mueve o cancela. Cuando la llamada no debe acabar en cita (la clínica no puede hacerlo, las reglas lo prohíben, no hay hueco, o la persona necesita un médico ya), lo reconoce, lo dice y lo informa.

El modelo de voz es una pieza. El producto es el sistema que lo rodea: consultas reales, comprobaciones antes de escribir, estado que aguanta cambios de opinión y visibilidad suficiente para explicar por qué el agente dijo lo que dijo.

## 2. Contexto

El fin de semana va así: registramos equipo, recibimos una key y levantamos un endpoint al que la organización puede llamar. Practicamos sin límite contra casos de práctica con respuestas publicadas. Cuando queramos, corremos para puntuar: nos llaman con todos los problemas y los puntos van al leaderboard. Dos veces el leaderboard se congela y premian a quien va primero. El domingo el jurado llama al agente en persona y enseñamos lo construido.

Hay 18 problemas, cada uno con su personaje. Todos son la misma reserva normal con una sola dificultad añadida.

La nota son dos partes que se suman. El leaderboard es automático y literal: tras cada llamada el agente informa de lo que hizo, y o coincide con lo que el caso acepta o el caso falla, sin crédito parcial. Un rechazo correcto también hay que informarlo, el silencio siempre es fallo. El jurado juzga lo que el leaderboard ignora: cómo suena la llamada, cómo lleva las interrupciones, si parece que la clínica sabe quién llama, y después cómo se orquesta una llamada, qué se ve mientras ocurre, qué se aprende después y si se puede enseñar funcionando. Seguridad, idiomas y cómo sabemos que el agente funciona cuentan.

## 3. Objetivos y métricas

| Objetivo | Métrica |
|---|---|
| O1. Pasar los 18 casos del leaderboard | Casos pasados en la última ejecución para puntuar. En práctica, cada caso pasa 3 de 3 en el arnés |
| O2. Ir por delante en los dos checkpoints | Posición en el leaderboard al congelarse |
| O3. Que la llamada del jurado suene a una buena recepcionista | Latencia por turno medida y visible (objetivo propio: por debajo de 1,5 s), interrupciones bien llevadas, uso del nombre del paciente |
| O4. Poder enseñar el sistema | Panel en vivo, vista posterior de llamadas, números del arnés y demo ensayada |

Fuera de alcance: llamadas salientes y recordatorios, facturación y seguros, consejo médico o diagnóstico, cualquier integración que no venga en el starter kit, interfaz para pacientes, optimización de coste.

## 4. Actores

El llamante, que puede no ser el paciente. El paciente. La clínica, con sus registros, su agenda y sus reglas. El evaluador automático, que llama y compara informes. El jurado. Y el equipo, repartido en voz, agentes y producto (producto lleva evaluación, observabilidad y demo).

## 5. Principios de diseño

1. El informe lo genera código a partir del estado y de los resultados reales de las tools, nunca el LLM redactando.
2. Las reglas de la clínica se ejecutan dentro de la tool que escribe. Si el prompt cede, la escritura prohibida sigue sin ocurrir.
3. Nada se escribe hasta que el llamante confirma en voz alta.
4. Todo hueco que el agente menciona viene de una consulta real.
5. Toda llamada termina con informe, acabe como acabe.
6. El estado va por identificador de llamada desde el primer día. No hay variables globales.

## 6. Requisitos funcionales

### Identificación

- RF-1. Determinar la intención: reservar, mover, cancelar u otra.
- RF-2. Separar llamante y paciente en el estado. Si son distintos, preguntar la relación y aplicar las reglas sobre menores o autorización que tenga la clínica.
- RF-3. Buscar al paciente con nombre más un segundo dato (fecha de nacimiento o teléfono).
- RF-4. Si hay varias coincidencias, pedir datos hasta que quede una sola persona. No reservar a ojo y no revelar datos de las otras.
- RF-5. Si el paciente no está en los registros, seguir el camino que acepten los casos de práctica. Supuesto de partida: alta y reserva normal.
- RF-6. Verificar identidad antes de revelar o modificar una cita existente.

### Disponibilidad

- RF-7. Consultar disponibilidad contra los datos reales de la clínica.
- RF-8. Filtrar por médico y por sede. "Lo antes posible" devuelve el primer hueco real respetando las restricciones que haya dado el llamante.
- RF-9. Resolver fechas vagas en código, con la fecha de referencia y la zona horaria como parámetros y con tests. El agente dice la fecha concreta en voz alta para confirmarla. Ejemplo: si la referencia es el viernes 18, "next Thursday" es el jueves 24 y "first thing Monday" es el primer hueco del lunes 21.
- RF-10. Si no hay huecos, hacer lo que acepte el caso (ofrecer alternativa o cerrar sin cita) e informar "sin disponibilidad".

### Escritura

- RF-11. Reservar solo con paciente identificado, hueco recomprobado en el momento de escribir, reglas pasadas y confirmación verbal.
- RF-12. Mover reservando primero el hueco nuevo y cancelando después el antiguo, para que un fallo a medias no deje al paciente sin cita.
- RF-13. Cancelar la cita correcta tras identificarla y confirmarla.
- RF-14. Una corrección antes de escribir sobrescribe el estado. Una corrección después de escribir pasa por mover o cancelar.

### Llamadas que no acaban en cita

- RF-15. Cada regla de la clínica es una función que devuelve permitido o denegado con un código de motivo. El agente explica el motivo y el informe lleva ese mismo motivo.
- RF-16. Si la clínica no puede hacerlo (servicio, médico o sede que no existe), el agente lo dice y lo informa como tal.
- RF-17. Vigilar señales de alarma durante toda la llamada (dolor en el pecho, dificultad para respirar, sangrado fuerte, síntomas de ictus, ideas de hacerse daño). Si aparecen, dejar de agendar y derivar a atención médica según marquen las reglas de la clínica, sin diagnosticar.
- RF-18. Resistir la manipulación (insistencia, prisa, "soy médico", "la otra recepcionista me deja"). La validación vive en la tool. El agente no revela instrucciones ni datos de otros pacientes.

### Conversación

- RF-19. Callarse cuando le interrumpen y dar respuestas cortas.
- RF-20. Detectar el idioma en el primer turno. Inglés como base, después castellano, catalán, gallego y euskera, y cualquier otro que el stack soporte. Si un idioma no funciona, decirlo y ofrecer inglés o castellano. El informe mantiene su formato sea cual sea el idioma.
- RF-21. Con línea mala, pedir que repita, leer de vuelta los datos clave y deletrear si hace falta. Tras varios intentos, cierre ordenado.
- RF-22. Recoger bien nombres, fechas de nacimiento y teléfonos: deletrear apellidos y repetir números dígito a dígito.

### Informe

- RF-23. Enviar el informe tras cada llamada en el formato exacto del track.
- RF-24. Garantizar el informe en todas las salidas: cuelgue del llamante, error interno, timeout.
- RF-25. Resultados posibles: reservada, movida, cancelada, rechazada con motivo, no ofrecido, sin disponibilidad, derivada, incompleta. Se ajustan a los que use el track.

## 7. Requisitos no funcionales

- RNF-1. Diez llamadas simultáneas sin mezclar estados ni duplicar huecos. Límites de concurrencia de los proveedores (reconocimiento de voz, síntesis, LLM) comprobados antes.
- RNF-2. Latencia medida por turno y visible en el panel.
- RNF-3. Traza por llamada con transcripción, cada tool con argumentos y resultado, decisiones de reglas, estado final e informe. Cada turno del agente queda enlazado con el resultado o la regla que lo motivó.
- RNF-4. Panel en vivo (llamadas activas, estado, tools, resultado) y vista posterior (casos pasados y fallados en el tiempo, motivos de fallo). Crece por milestones, no se construye al final.
- RNF-5. Arnés de evaluación que compara el informe con las respuestas aceptadas, con regresión de todos los casos anteriores.
- RNF-6. Despliegue con un solo comando y congelación de código antes de la final.

## 8. Casos de aceptación

La columna de resultado es nuestra hipótesis. Lo que manda es lo que acepten los casos de práctica publicados.

| # | Caso | Comportamiento esperado | Resultado en el informe | Requisitos | Milestone |
|---|---|---|---|---|---|
| 1 | Reserva simple | Identifica, propone hueco real, confirma y reserva | Reservada | RF-1, 3, 7, 11, 23 | M1 |
| 2 | Diez a la vez | Diez reservas correctas en paralelo | Diez informes correctos, ningún hueco duplicado | RNF-1 | M4 |
| 3 | Paciente no registrado | Según caso. Supuesto: alta y reserva | Reservada con paciente nuevo | RF-5 | M4 |
| 4 | Coincide con cuatro personas | Desambigua hasta una sola | Reservada para la persona correcta | RF-4 | M4 |
| 5 | Médico concreto | Solo huecos de ese médico | Reservada con ese médico | RF-8 | M2 |
| 6 | Sede concreta | Solo huecos de esa sede | Reservada en esa sede | RF-8 | M2 |
| 7 | "Lo antes posible" | Primer hueco real | Reservada en el primer hueco | RF-8 | M2 |
| 8 | "Next Thursday" | Resuelve a fecha concreta y la confirma | Reservada en esa fecha | RF-9 | M2 |
| 9 | "First thing Monday" | Primer hueco del lunes | Reservada en ese hueco | RF-9 | M2 |
| 10 | Petición prohibida | Rechaza explicando el motivo | Rechazada con el motivo correcto | RF-15 | M3 |
| 11 | Agenda llena | Según caso: alternativa o cierre | Sin disponibilidad | RF-10 | M2 |
| 12 | Cambio de cita | Mueve sin dejar al paciente sin cita | Movida | RF-6, 12 | M4 |
| 13 | Cancelación | Cancela la cita correcta | Cancelada | RF-6, 13 | M4 |
| 14 | Madre por su hijo | Reserva a nombre del hijo | Reservada para el hijo | RF-2 | M4 |
| 15 | Hija por su padre | Reserva a nombre del padre | Reservada para el padre | RF-2 | M4 |
| 16 | Necesita un médico | Deja de agendar y deriva | Derivada | RF-17 | M3 |
| 17 | No habla inglés | Atiende en su idioma | El de una reserva normal | RF-20 | M5 |
| 18 | Otras lenguas de España | Atiende en catalán, gallego o euskera | El de una reserva normal | RF-20 | M5 |
| 19 | Línea mala | Confirma datos leyéndolos de vuelta | Reservada, o incompleta con cierre ordenado | RF-21, 24 | M5 |
| 20 | Interrumpe, corrige, cambia de opinión | Sigue los cambios sin escribir antes de tiempo | La última decisión confirmada | RF-14, 19 | M5 |
| 21 | Intento de manipulación | No hace lo indebido | Rechazada con motivo | RF-18 | M3 |

Salen 21 filas y el track habla de 18, así que algunas van agrupadas en un mismo problema. Se ajusta cuando tengamos la lista de casos de práctica.

## 9. Arquitectura

La llamada entra por el endpoint a la capa de voz (reconocimiento, modelo, síntesis), que pasa cada turno al orquestador. El orquestador mantiene el estado de esa llamada y decide qué tool usar. Las tools leen y escriben en los datos de la clínica, y las de escritura ejecutan dentro las reglas y la recomprobación del hueco. Al terminar la llamada por cualquier vía, el generador de informes construye el informe desde el estado y lo envía. Todo lo anterior escribe en la traza de la llamada, que alimenta el panel en vivo, la vista posterior y el arnés.

## 10. Plan de entrega

Un milestone se cierra cuando sus casos pasan 3 de 3 en el arnés, la regresión de los anteriores sigue en verde y hemos corrido para puntuar (ver la pregunta abierta 4 antes de hacerlo por sistema). Producto añade los casos al arnés antes de que agentes los implemente. Voz trabaja en paralelo desde M0.

Si el sábado a mediodía M3 no está cerrado, se recorta M5 antes que M4: M4 es código que controlamos, M5 depende del audio.

### M0. Esqueleto y contrato (viernes noche)

Objetivo: que entre una llamada, el agente hable y salga un informe aunque esté mal, y que todos trabajemos contra el mismo contrato.

- Voz: registro, key, endpoint público, starter kit arrancado, llamada de prueba, despliegue con un comando (RNF-6).
- Agentes: contrato de tools (buscar paciente, crear paciente, consultar disponibilidad, reservar, mover, cancelar, derivar), forma del estado por identificador de llamada (principio 6) y formato del informe, todo escrito en el repo.
- Producto: leer los casos de práctica y sus respuestas, trazas en JSON (RNF-3), arnés mínimo (RNF-5) y las preguntas abiertas de la sección 11 resueltas o asignadas.

Casos: ninguno. Salida: una llamada real genera traza e informe.

### M1. Una reserva que puntúa (viernes noche)

Objetivo: el camino completo de una reserva normal. Todo lo demás son ramas de este.

- Agentes: RF-1, RF-3, RF-7, RF-11, RF-23 y RF-24. El informe garantizado entra aquí y no más tarde.
- Voz: saludo, turnos de palabra y latencia medida (RNF-2).
- Producto: caso 1 en el arnés y primera ejecución para puntuar.

Casos: 1. Salida: 3 de 3 y aparecemos en el leaderboard.

### M2. Motor de disponibilidad (sábado mañana)

Objetivo: que la consulta de huecos entienda cualquier forma razonable de pedir cita.

- Agentes: RF-8, RF-9 con sus tests, RF-10.
- Voz: decir fechas y horas de forma natural y confirmarlas.
- Producto: casos nuevos en el arnés y confirmar qué fecha de referencia usan los casos.

Casos: 5, 6, 7, 8, 9, 11. Salida: 3 de 3 y tests del resolvedor en verde.

### M3. La capa del "no" (sábado mediodía)

Objetivo: que el agente sepa no reservar, por el motivo correcto, e informarlo. Con M1 a M3 está la mitad del leaderboard, y debería llegar antes del primer checkpoint.

- Agentes: RF-15, RF-16, RF-17, RF-18, RF-25.
- Voz: rechazos claros y breves, y tono adecuado en la derivación a médico.
- Producto: primera versión del panel con el resultado de cada llamada (RNF-4) y ejecución para puntuar antes del checkpoint.

Casos: 10, 16, 21. Salida: 3 de 3 y ninguna llamada sin informe.

### M4. Identidad, ciclo de vida de la cita y carga (sábado tarde)

Objetivo: resolver quién es el paciente y qué cita se toca, y aguantar diez llamadas a la vez. Todo es código que controlamos.

- Agentes: RF-2, RF-4, RF-5, RF-6, RF-12, RF-13.
- Voz: RF-22 y la prueba de carga de RNF-1, que depende sobre todo del pipeline de voz y de los límites de los proveedores.
- Producto: el arnés lanza diez llamadas en paralelo y comprueba que no hay huecos duplicados ni estados mezclados.

Casos: 2, 3, 4, 12, 13, 14, 15. Salida: 3 de 3 y prueba de carga pasada.

### M5. Robustez de conversación (sábado noche, domingo temprano)

Objetivo: que la conversación aguante a gente real.

- Agentes: RF-14.
- Voz: RF-19, RF-20, RF-21. La prueba de qué idiomas soporta el reconocimiento de voz se hace el viernes, no aquí.
- Producto: escuchar las grabaciones de los fallos y, si da tiempo, un llamante simulado propio para variar ruido e interrupciones.

Casos: 17, 18, 19, 20. Salida: 3 de 3 y alguien de fuera del equipo ha llamado intentando romperlo.

### M6. Demo y cierre (domingo mañana)

Objetivo: tener algo que enseñar y no romper nada antes de la final.

- Producto: panel pulido (RNF-4), guion de demo con una llamada en directo y el panel en pantalla, un diagrama de cómo se orquesta una llamada, un fallo real que encontramos y cómo lo arreglamos, y los números del arnés como respuesta a "cómo sabéis que funciona".
- Voz: pulido para la llamada del jurado (latencia, naturalidad, uso del nombre del paciente cuando ya se conoce).
- Todos: ensayo con alguien haciendo de jurado, congelación del código y última ejecución para puntuar.

Casos: ninguno nuevo. Salida: demo ensayada y código congelado.

## 11. Supuestos y preguntas abiertas

Se resuelven el viernes. Hasta entonces, el documento asume lo indicado.

1. Formato exacto del informe y canal por el que se envía. Sin esto no puntúa nada.
2. Qué acepta cada caso, sobre todo paciente nuevo y agenda llena.
3. Hora de los dos checkpoints.
4. Si las ejecuciones para puntuar son ilimitadas y si cuenta la mejor o la última. Hasta saberlo, solo se corre con la regresión en verde.
5. Si la ejecución para puntuar lanza las llamadas en paralelo. Si es así, la prueba de carga sube a M1.
6. Qué fecha de referencia usan los casos para "next Thursday": la real del día de la llamada o una fija de los datos de prueba.
7. Idioma base de la clínica. Supuesto: inglés, porque el track habla de "callers not speaking English".
8. Si los casos de práctica se pueden lanzar por API o solo a mano. Si es a mano, el arnés compara automáticamente pero alguien lanza las llamadas.
9. Qué expone el starter kit: datos de pacientes, agenda, reglas, y si da el número del llamante. Si lo da, sirve para que la clínica "sepa quién llama" desde el saludo, siempre verificando antes de revelar nada.
10. Límites de llamadas simultáneas de los proveedores de voz y del LLM.

## 12. Riesgos

| Riesgo | Mitigación |
|---|---|
| Un informe mal formado tumba todos los casos a la vez | Formato resuelto en M0 y generado por código desde M1 |
| Una regresión justo antes de un checkpoint | No se corre para puntuar sin regresión en verde |
| El reconocimiento de voz no soporta euskera o gallego | Prueba el viernes y salida digna en RF-20 |
| Latencia alta al encadenar LLM y tools | Medida desde M1 y visible en el panel |
| Agentes concentra casi todos los requisitos en una persona | Una de las dos personas de voz pasa a agentes el sábado, cuando el pipeline esté estable |