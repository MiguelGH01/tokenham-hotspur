# Features de producto (jurado + vídeo)

Backlog ordenado de Adolfo. Cada feature la coge **una sesión de Claude aparte**, en
paralelo. Si eres esa sesión: lee "Cómo se trabaja" entero antes de tocar nada.

Contexto: el board solo puntúa el registro enviado (`PD-20`, `SC-binary`); estas features
son para el jurado (`JR-*`, [07-jury-and-platform](requirements/07-jury-and-platform.md))
y el vídeo de 2 min. Lo que no se enseña funcionando no puntúa (`JR-not-slide`).

## Cómo se trabaja

**Rama.** Una sola rama compartida por todas las sesiones: `adolfo/product-features`,
creada desde `main`. Sin ramas hijas ni worktrees (decisión de Adolfo). No se mergea a
`main` sin que Adolfo lo diga.

Como varias sesiones comparten directorio y rama a la vez:

- Toca solo los ficheros de tu feature. Si necesitas cambiar uno compartido
  (`server/bot.py`, `server/flows/common.py`, este doc), hazlo en un commit pequeño y
  cuanto antes.
- Commits pequeños y frecuentes, con `git add <tus ficheros>`; nunca `git add -A` ni
  `git commit -a`: se llevaría el trabajo a medias de otra sesión.
- Nada de `git stash`, `git reset`, `git checkout -- <fichero>` ni cambios de rama: le
  quitan el suelo a las demás sesiones.
- **Un solo run de evals a la vez.** `make evals` y `make eval-one` usan puertos fijos
  (7860 / 7861) y hacen `pkill -f "bot.py -t eval"`: lanzarlo mata el bot de eval de otra
  sesión. Avisa a Adolfo antes de lanzar uno.

**Proceso por feature, en este orden, con las skills de Adolfo:**

1. **Brainstorm** — solo si la feature lo marca abajo (`Brainstorm: sí`). Skill
   `/explore`: evidencia + alternativas con recomendación. No se decide nada; decide Adolfo.
2. **Preguntas** — resolver las "Preguntas abiertas" de la feature con Adolfo. En
   decisiones de verdad: dar opciones con lo que cuesta cada una y la recomendada.
   En trabajo mecánico: hacerlo sin preguntar.
3. **Plan** — skill `/plan`: plan corto en prosa con contrato de aceptación, en
   `.claude/plans/<feature>/plan.md`.
4. **Implementar** — skill `/implement`: baseline limpio, tests en RED primero, tareas
   incrementales con Adolfo en el bucle.
5. **Revisar y verificar** — `/review` cuando Adolfo lo pida; `/verify` para el examen
   de aceptación.

**Regla que no se negocia: no romper el board.** Antes y después de cualquier cambio que
toque `server/bot.py`, `server/flows/` o `server/rules.py`: `make test` y `make evals`
(escenarios `server/evals/PR-*`). Si un escenario que pasaba deja de pasar, se para.
Pedir permiso a Adolfo antes de runs largos.

**Al terminar una feature:** actualizar su `Estado` aquí y dejar escrito cómo se enseña
funcionando (el guion de demo).

---

## 1. Pack de rigor

- **Estado:** en curso en otra sesión, en otra rama. **Pendiente: moverlo a la rama de features.**
- **Brainstorm:** no.
- **Qué es:** demostrar con números que sabemos si funciona: repetir cada escenario N
  veces (¿pasa 5 de 5 o 3 de 5?) y coste de una llamada en euros y segundos.
- **Criterio:** `JR-rigour`. Además re-verifica `PR-04` y `PR-05`, cuyos últimos logs
  (19-sep 08:3x, anteriores a tres merges) eran FAIL.
- **Reutiliza:** métricas de uso ya activas en [bot.py:544](../server/bot.py#L544);
  `make evals` y `make oracle` en el [Makefile](../Makefile); `server/evals/corpus/judge.py`.
- **Riesgo para el board:** ninguno, es offline.
- **Preguntas abiertas:** precios por proveedor (LLM, STT, TTS) para la fórmula de coste;
  en qué rama está hoy el trabajo.
- **Demo:** tabla escenario × N runs con los modos de fallo nombrados, más €/llamada.
- **Mitad coste: hecha (19-sep), falta verla con audio.** Plan y mediciones en
  [.claude/plans/call-cost/plan.md](../.claude/plans/call-cost/plan.md). Cada llamada deja
  en su `audit-<call_id>.ndjson` lo que consumió (`service_usage`, `service_latency`,
  `call_started`, `call_ended`) y `make cost` lo valora en € y segundos con p50/p95.
  **No es offline:** toca `server/bot.py` (+4/−1 líneas, un observer fuera del camino del
  audio). `make evals` no se corrió (decisión de Adolfo); sí `make test` (266 en verde) y
  una llamada real en modo texto.
  - *Guion de demo:* hacer una llamada (webrtc o Twilio) → `make cost` → tabla con
    `call_id`, segundos, € y latencia del LLM; al pie, la fecha de los precios y el cambio
    del BCE. Frase: "esta llamada ha costado X céntimos y ha durado Y segundos".
  - *Pendiente de Adolfo:* una llamada con audio para ver STT y TTS en el trail; confirmar
    a ojo los precios de `server/call_cost.py` contra las URLs que lleva al lado.

## 2. Reconocer al paciente con su ficha

- **Estado:** sin empezar.
- **Brainstorm:** corto (solo la parte de "recordar").
- **Objetivo:** que el agente suene a que conoce a quien llama y le personalice la
  oferta, en vez de interrogarle.
- **Criterio:** `JR-personal`, `JR-experience`; guías `CL-guide-read-chart`,
  `CL-guide-personal-offer`, `CL-guide-go-further`.
- **De dónde salen los datos (todo lectura de la API de la clínica):**
  - **Nota del paciente:** campo `note` del registro que devuelve
    `GET /api/v1/directory` (`CL-note`, [api/02-clinic.md](api/02-clinic.md)). Ya llega
    al identificar al paciente; hoy no se usa en ningún sitio.
  - **Historial:** `GET /api/v1/patients/{id}/appointments?when=past`, hasta 8 visitas de
    2024–2025 (`CL-past`). Cliente ya hecho:
    [clinic_client.py:158](../server/clients/clinic_client.py#L158). Hoy solo lo usa
    `resolution.py` al final de la llamada.
  - Pacientes con `has_visited_before=false` no tienen historial (`CL-no-past-new`).
- **Punto de enganche:** [flows/identification.py:71](../server/flows/identification.py#L71),
  que hoy solo distingue "returning / first-time".
- **Riesgo para el board:** ALTO. Es la única feature que toca el camino de `PR-01`.
  Añade una llamada a la API y turnos más largos contra el límite de 3 min
  (`SC-three-minutes`). La personalización nunca puede cambiar el registro: el tipo de
  cita sale de la ficha, no de la conversación (`CL-type-rule`).
- **Preguntas abiertas:**
  - "Recordar llamadas anteriores con nosotros" no sale de la API (la clínica es solo
    lectura). ¿Queremos un almacén propio por paciente, o basta con nota + historial?
  - ¿La oferta personal ("la Dra. Ortiz, que es quien le suele ver") puede desviar del
    hueco más temprano que espera el board? Si sí, ¿solo en modo demo?
  - ¿Se hace antes o después del freeze (`SC-freeze`, domingo 20-sep 06:00)?
- **Demo:** llamada de un paciente habitual: saludo por nombre, sin "¿ha venido antes?",
  oferta con su médico de siempre.

## 3. Escalar las red flags de verdad

- **Estado:** sin empezar.
- **Brainstorm:** sí.
- **Qué es:** ante un síntoma de urgencia el agente no da cita: escala a un humano
  (`ESCALATE(medical_emergency)`), y no se deja convencer de lo contrario.
- **Criterio:** `JR-safety`. Puntos de board solo si abren `PR-10` (peso 3) y `PR-14`
  (peso 4): **comprobar en el dashboard si están abiertos.**
- **Las cinco red flags publicadas** están en `PR-10`,
  [06-problems.md](requirements/06-problems.md). Las derivaciones a referral se
  mantienen fuera (`PR-10` es ortogonal a `PR-06`).
- **Reutiliza:** `set_escalate` en [submission.py:231](../server/submission.py#L231)
  existe pero nadie lo llama desde la conversación; fallback en
  [resolution.py:54](../server/resolution.py#L54).
- **Idea de Adolfo:** usar Jev, de TypeSafe AI, como detector. Mirado el 19-sep en su
  documentación: no genera texto; se le manda lo que ha dicho el paciente y una lista de
  preguntas, y devuelve para cada una una probabilidad (`Noul`: sí/no entre 0 y 1;
  `Score`: nivel 0–3; `choice`: una opción). SDK `typesafe-sdk`, modelo `jev-1.12`, clave
  en `TYPESAFE_API_KEY`. Anuncian 70–500 ms y $0,042 por millón de tokens de entrada.
  **Sin comprobar:** que tengamos acceso (está en early access con lista de espera), que
  entienda español (la documentación no lo dice) y que el SDK tenga cliente asíncrono.
- **Ampliación propuesta (19-sep):** el caso "pasar a una persona según el tema" del
  agent builder (feature 4) se hace aquí, como un guardarraíl único: urgencias y temas
  que recepción quiera derivar son preguntas de la misma lista.
- **Preguntas abiertas:**
  - ¿Detector externo, reglas propias sobre las 5 frases, o los dos (externo + red de
    seguridad determinista)? El error caro es el falso negativo.
  - ¿Qué pasa si el servicio externo tarda o se cae a mitad de llamada? (corte por
    silencio, `SC-silence-cut`)
  - ¿Falsos positivos? Un "me duele el pecho al toser" que escala rompe un `BOOK` de `PR-10`.
- **Demo:** "me aprieta el pecho y me cuesta respirar" → escala, no ofrece cita; después,
  intento de convencerle de que dé cita igualmente.

## 4. Agent builder

- **Estado:** sin empezar.
- **Brainstorm:** sí, obligatorio. Es la feature menos definida.
- **Qué es:** que la persona de la clínica adapte su agente a su caso actual sin tocar
  código. No podemos escribir en la clínica (la API es solo lectura y lo nuestro son
  POST de envío), pero el agente sí es nuestro y se puede editar.
- **Ejemplos de Adolfo:**
  - Excepciones operativas: "hoy falta tal médico" → el agente lo sabe y recoloca.
  - Tono conversacional: más formal, más cercano, etc.
  - Más casos según se nos ocurran (apuntarlos aquí).
- **Criterio:** `JR-platform` ("surprise them"), `JR-discretion`.
- **Reutiliza (a confirmar en el brainstorm):** la baja de un médico ya existe como
  concepto en [rules.py:382](../server/rules.py#L382) (`_on_leave`, hoy alimentado por el
  catálogo); textos y tono en `server/flows/common.py` y los nodos de `server/flows/`.
- **Riesgo para el board:** ALTO si está mal acotado. El board calcula la respuesta
  esperada con la API de la clínica (`SC-expected-via-api`): cualquier excepción local
  que cambie el registro enviado hace fallar casos. Hay que decidir cómo se garantiza que
  en el carril puntuado no hay personalizaciones activas.
- **Preguntas abiertas:**
  - ¿Qué se puede editar? ¿Solo tono (prompt) o también reglas que cambian la decisión?
  - ¿Cómo edita la persona: formulario, fichero, hablándole al agente?
  - ¿Dónde vive la configuración y cuándo se aplica (siguiente llamada, en caliente)?
  - ¿Cómo se valida una edición antes de activarla (p. ej. pasando los evals)?
  - ¿Qué no se puede tocar nunca? (reglas de seguridad de la feature 3, privacidad)
- **Demo:** en pantalla se añade "el Dr. X no viene mañana" y se cambia el tono; la
  siguiente llamada lo refleja.
- **Casos decididos por Adolfo (19-sep, tras `/explore`):**
  - Entran: médico ausente (el `leave` que `rules.py` ya interpreta); día cerrado
    (`closure_days`, clínica entera); un médico deja de aceptar un seguro
    (`refused_insurers`); avisos que el agente dice en voz alta al confirmar (solo si
    coinciden centro y fecha); tono (una frase añadida al system prompt).
  - Para todos: cada aviso caduca solo, y cuando influye en una llamada queda apuntado
    en su `audit-<call_id>.ndjson` (responde "¿por qué dijo eso?").
  - Fuera: saludo editable.
  - Guardado para luego: preguntas frecuentes (riesgo: que invente lo que no está escrito).
  - Sin decidir: pasar a una persona según el tema (se solapa con la feature 3);
    repartir carga y ausencia de medio día (lógica nueva sobre el camino puntuado).
  - Los tres primeros cambian el registro enviado: solo activos con el marcador cerrado
    (`SC-freeze`). Avisos hablados y tono no lo cambian (`SC-conversation-unscored`).
- **Frontend:** no hay ninguno en el repo ni en ninguna rama remota (comprobado 19-sep;
  lo único web es el dashboard de evals, solo lectura, en
  `origin/merge/flows-agentic-improvements`). La pantalla de edición va al final de la
  cola hasta que se suba; antes se hace el motor (fichero de avisos + que el agente lo
  respete), que no depende de ella.

---

## Aparcadas (propuestas, no pedidas)

- Consola en vivo y "¿por qué dijo eso?" (`JR-live-console`, `JR-explain`): ya hay un
  dashboard en `origin/merge/flows-agentic-improvements` (`server/scripts/dashboard/`).
- Diez llamadas a la vez y número marcable (`JR-concurrency-demo`, `JR-dialable`):
  `make concurrency` ya existe; nunca se ha guardado un resultado.
