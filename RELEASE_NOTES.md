# TFT AUTO-ACCEPT — Conexión interna sin reiniciar TFT

- Puede abrirse y activarse con TFT ya iniciado. Recupera la autorización específica desde Riot Client; no depende de capturar el arranque del juego.
- Retirados proxy SDK, preparación YAML, WAMP, cache de funnel, PyYAML y websocket-client.
- Corregido un falso positivo de la investigación: `currentParty` puede mostrar LoL mientras `parties[]` contiene la membresía propia de TFT. Se valida esa membresía antes del POST.
- Conserva aceptación interna sin ratón, teclado, capturas ni OCR; ventana compacta, bandeja, espera fija de un segundo, cancelación, pausa nativa y cierre de sus procesos.
- Un envío incierto no se duplica, tampoco al desactivar/reactivar. Se conserva el adaptador LCU anterior.

La API RSO recuperada desde Riot Client aceptó una práctica de Tictoco (cola 1220): respuesta 200 y comienzo de partida comprobado. El trabajador completo anterior con GAPS también realizó una aceptación única y entró en pausa nativa. Las aceptaciones automatizadas de las pruebas unitarias son simuladas.

**Ejecutable actualizado validado:** se abrió con TFT ya iniciado, conectó sin cache del SDK ni reiniciar el cliente, aceptó automáticamente una práctica con un único envío y entró en pausa al comenzar. Salir terminó sus dos procesos y conservó abierto el mismo proceso de TFT.

Publicación limpia: sin credenciales ni datos personales incorporados, sin telemetría y con diagnósticos sanitizados. Las pruebas de privacidad verifican que una excepción no copie tokens ni rutas personales a los informes.

Descarga `TFT-AUTO-ACCEPT.zip` y extrae `TFT AUTO-ACCEPT.exe`. No requiere instalar Python. Se conservan los releases anteriores y se publican sumas SHA256.
