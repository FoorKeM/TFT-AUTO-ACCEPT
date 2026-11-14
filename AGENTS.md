# TFT AUTO-ACCEPT

- Nombre visible de la ventana, archivo Python y ejecutable: `TFT AUTO-ACCEPT`.
- El repositorio es público. No introducir nombres personales, rutas de un equipo concreto, datos de cuentas ni credenciales reales en fuentes, fixtures, artefactos o metadatos de commits.
- Usar una identidad neutra del proyecto y un correo noreply en los commits. Resolver las rutas locales a partir del sistema de cada usuario.
- Mantener la aceptación para cualquier cola; no añadir filtros por juego sin una nueva indicación del usuario.
- Mantener una espera fija de 1 segundo, sin slider ni aleatoriedad.
- Al pulsar la X, preguntar si se desea salir u ocultar en la bandeja; ocultar conserva el estado activo/desactivado del bot.
- Conservar la ventana compacta. En la bandeja, ofrecer Activar/Desactivar según el estado, Mostrar y Salir; tick verde al activar y X roja al desactivar.
- Preservar el reposo sin consultas de red y verificar que Salir termine los hilos, los helpers propios y ambos procesos del EXE; no terminar el cliente del juego.
- Cliente anterior: al detectar el proceso de la partida, suspender las consultas mediante una espera nativa cancelable. Reanudar al terminar el proceso solo si la misma activación sigue vigente; respetar desactivación manual y salida. No sondear mientras esa espera está activa.
- Ejecutar las pruebas con `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v` y generar el ejecutable con `build.ps1`.
- Automatizar las pruebas unitarias con respuestas simuladas o consultas de lectura. La autorización explícita del 14 de noviembre permite iniciar y aceptar una práctica de Tictoco (cola 1220) para validar la integración real. No extender esa prueba a otras colas.
- Cada actualización publicada en `main` genera un release mediante `.github/workflows/release.yml`. Comprobar su resultado antes de dar una publicación por terminada.
- No subir entornos virtuales, registros, credenciales ni carpetas de compilación. Los ejecutables se distribuyen como adjuntos de releases.
- No reemplazar ni eliminar releases anteriores para publicar una modificación; crear la siguiente versión.

- El programa final debe aceptar únicamente por API interna; no incorporar ratón, teclado, capturas ni OCR. El 14 de noviembre el usuario autorizó control de pantalla solo para investigar y ratificó esta restricción para el programa. Puede iniciarse y rechazarse una cola de prueba autorizada, además de la aceptación de práctica expresamente autorizada.
- TFT independiente: recuperar desde la API local de Riot Client la autorización `/rso-auth/v2/authorizations/teamfighttactics-client`. Si falta, crear una autorización OAuth independiente con el clientId observado y keepAlive=false, sin crear sesiones de juego GAPS ni cambiar la autenticación global. Mantener tokens únicamente en RAM. No utilizar ni volver a introducir proxy SDK, WAMP, preparación YAML ni cache de funnel.
- En el TFT independiente, la pausa usa cambios del registro y cierre del proceso mediante notificaciones nativas; lobby y partida comparten proceso. Evitar consultas de API durante la partida.
- Ejecutar los diagnósticos gráficos del EXE en un escritorio aislado para no interrumpir el juego del usuario.

- Si el POST del TFT independiente queda incierto, no reenviarlo usando un DTO AFK anterior: el registro local no confirma de forma fiable una aceptación externa. Conservar la deduplicación al desactivar/reactivar. LCU mantiene su consulta autoritativa y reintento condicionado.
- El usuario exige poder abrir y activar el programa con TFT ya iniciado, sin tener que reiniciarlo. Utilizar la sesión de juego existente sin crear otra ni terminar el cliente; una autorización OAuth independiente no es una nueva sesión de juego. Mantener también la espera nativa de arranque para cuando el juego está ausente.

- Validar cuenta propia y membresía TFT en `player.parties[]` mediante puuid, partyId y partyProduct=tft, aunque currentParty seleccione LoL. La exclusión anterior de RSO basada solo en currentParty fue un falso positivo corregido. Conservar la comprobación compatible de currentParty TFT cuando corresponda; no presentar una party LoL como TFT.
- La versión completa del EXE RSO se abrió con TFT ya iniciado, aceptó una práctica 1220 una sola vez, mantuvo el mismo proceso del cliente y entró en pausa. Salir terminó los dos procesos propios sin cerrar TFT. Las simulaciones y las pruebas reales deben describirse por separado.
- El uso normal inicia únicamente la aceptación; el usuario inicia la búsqueda. Las acciones de inicio/cancelación/rechazo durante investigación no deben convertirse en acciones automáticas nuevas del producto sin otra indicación.
