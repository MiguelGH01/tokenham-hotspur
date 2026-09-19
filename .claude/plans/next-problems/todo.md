# Lista de trabajo (19 sep) — en orden, una cosa cada vez

Estado: PR-01 y PR-03 en commit `70e5314`. PR-06 + catálogo desde la API hechos, **sin commit**.
Regla: antes de cualquier pasada de más de ~1 min, avisar.

## 1. Evals con audio  ⬜

Objetivo: que el "paciente" del eval HABLE, para ejercitar VAD + Soniox + ElevenLabs + turnos.

- [ ] 1a. Sonda (10 min): pasar UN eval a audio (Josefa) y ver si corre contra nuestro bot.
      Mirar en la doc de Pipecat Evals el bloque exacto (`user: modality: audio`, `judge: modality: audio`
      + `transcription`). Voz del paciente: Kokoro, local, sin clave.
      → Si NO funciona: anotar por qué, saltar al paso 2.
- [ ] 1b. Si funciona: versión audio de 4 escenarios que cubren lo frágil al oído:
      `josefa` (DNI), `josefa_chunked_id` (DNI a trozos con pausas), `ignacio` (teléfono),
      `emilio` (Sáez vs Sáenz). Carpeta aparte `evals/audio/` para no ralentizar los de texto.
- [ ] 1c. Leer los logs: ¿qué transcribió Soniox?, ¿el bot se adelantó en el DNI a trozos?,
      ¿cuánto tarda ElevenLabs en empezar a hablar?
- [ ] 1d. Solo si el bot se adelanta: ajustar el fin de turno, medir otra vez. Si no, no tocar.

## 2. Diez llamadas a la vez  ⬜

Objetivo: comprobar que 10 llamadas simultáneas por la ruta Twilio real no se pisan ni tumban la máquina.

- [ ] 2a. Llevar `fake_harness.py` (hoy en carpeta temporal) al repo: `server/evals/fake_harness.py`.
- [ ] 2b. Lanzador de N copias a la vez, cada una con su `call_id`, contra un bot `-t twilio`
      en un puerto que NO sea el 7860.
- [ ] 2c. Pasada con 10. Mirar: las 10 reciben saludo, el vigilante de silencio salta en las 10,
      cada una envía SU `NO_ACTION` (estado aislado), sin errores, y cuánto tarda el saludo con carga.
      Límite conocido: estos pacientes no hablan; mide aguante, no si reserva bien.

## 3. Probar y commit  ⬜

- [ ] 3a. `uv run pytest` + `ruff` + `evals/run_all.sh evals/PR-01 evals/PR-03 evals/PR-06` (~6 min, avisar).
- [ ] 3b. Si la plataforma va: una práctica de PR-06 (Gloria o Teresa) y una de PR-03 (Andrés).
      Antes, reiniciar el bot del 7860 (lleva código anterior a PR-06).
- [ ] 3c. Commit (lo hace Adolfo, o lo preparo yo y él confirma): PR-06, catálogo desde la API,
      evals de audio, prueba de 10 llamadas.

## 4. PR-05 — Cuándo Exactamente  ⬜

Pasos en `plan.md` (sección PR-05). Tests primero. Resumen: parámetro `day`/`date` que el código
convierte en fecha, `pick_offer(not_before=…)`, ventana que empieza en la fecha pedida, nota `closed_day`.
Faltan 2 de 5 evals: Chloe (domingo) y Amelia (12 oct).

## 5. PR-04 — Paciente Nuevo  ⬜

Pasos en `plan.md` (sección PR-04). El más dependiente del oído: aquí los evals de audio del paso 1
son los que de verdad lo prueban (emails y apellidos dictados).

## 6. Después

- Comprobar el envío final en los evals (qué `BOOK`/`NO_ACTION` sale), para todos los PR.
- Paciente simulado por LLM (guion no fijo).
- PR-07 en adelante cuando los abran.
