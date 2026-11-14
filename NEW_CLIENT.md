# TFT independiente: conexión con el juego ya abierto — 14 de noviembre de 2026

## Arquitectura final

La aplicación utiliza la API local de Riot Client para recuperar la autorización específica de TFT y la API remota de confirmación. Puede abrirse y activarse con TFT ya iniciado, sin reiniciar el cliente, capturar un evento de arranque ni crear otra sesión de juego.

El descubrimiento utiliza las credenciales locales de Riot Client, sin `wmic`. La autorización se consulta mediante:

```text
GET /rso-auth/v2/authorizations/teamfighttactics-client
```

Si todavía no existe, se solicita una autorización OAuth independiente para `clientId: teamfighttactics-client`, con `keepAlive: false`, dentro de la sesión ya autenticada. No se consumen claves de arranque ni se modifica la autorización predeterminada. No se pide contraseña ni se llama a la creación de sesiones GAPS.

La aplicación obtiene el token actual y lo conserva únicamente en RAM. HTTP local se limita a 127.0.0.1; HTTPS remoto verifica certificados. No hay proxies, redirecciones ni reintentos automáticos de POST.

## Falso positivo corregido en la auditoría

La investigación anterior descartó RSO porque el GET del jugador devolvía una `currentParty` de LoL distinta de la party de TFT. Esa conclusión era un falso positivo: se había utilizado un campo insuficiente para decidir la compatibilidad.

El objeto del jugador incluye `parties[]`. Con la autorización específica de TFT obtenida desde Riot Client se comprobó que esa lista contenía una membresía propia con `partyProduct: tft`, cuyo `partyId` coincidía con el registro nativo de TFT. La party predeterminada de LoL no invalida esa membresía.

La nueva validación comprueba el PUUID propio, los UUID de las membresías y su producto, y rechaza datos inválidos, miembros ajenos y duplicados. No transforma ni presenta una party de LoL como TFT. Conserva la validación anterior de `currentParty.product == tft` para respuestas compatibles que la acrediten directamente.

El token predeterminado del Riot Client y su autorización específica de TFT tampoco deben confundirse: en la prueba actual, el primero devolvió 403 y la autorización específica permitió leer la membresía TFT y aceptar una práctica.

## Contrato de confirmación

La ruta observada en el cliente y comprobada en las pruebas reales es:

```text
POST /team-builder-ledge/v3/indicateAfkReadiness
```

Cuerpo de aceptación:

```json
{"additionalInventoryJWT":"","afkReady":true,"initialSpellIds":[],"lastSelectedSkinIdByChampionId":{},"simplifiedInventoryJWT":""}
```

El rechazo usa la misma ruta con `afkReady: false`. Iniciar o cancelar la búsqueda mediante `startAction`/`stopAction` es una operación distinta de aceptar o rechazar su confirmación. La aplicación final solo acepta confirmaciones; el usuario inicia la búsqueda.

Antes de enviar, se consulta otra vez el jugador propio con el mismo token y se revalidan activación, fase y confirmación. Se conserva la espera fija de un segundo y el límite de un envío por confirmación.

Los callbacks `Accepted match` y `Declined match` del registro no confirman de forma fiable un POST externo. Una foto RMS anterior con `afkReady: false` tampoco demuestra que siga pendiente tras un timeout. Un envío incierto se muestra como tal y no se repite, incluso al desactivar/reactivar.

## Evidencia real y límites

El usuario autorizó aceptar prácticas de Tictoco, cola 1220, para validar la integración. Esa autorización no se extiende a aceptaciones de otras colas durante el desarrollo.

- Una búsqueda Normal y su rechazo por API respondieron 200; se comprobó la detención de la búsqueda. No se aceptó una partida Normal.
- El trabajador completo anterior, usando GAPS, realizó un único POST a los 1,548 segundos de detectar la confirmación. El cliente pasó a `InProgress`, el trabajador entró en pausa nativa y terminó limpiamente.
- Una prueba con la autorización específica recuperada desde Riot Client envió la aceptación, obtuvo 200 y comprobó el inicio de la práctica. Esta prueba acredita el camino RSO sin depender del funnel SDK.
- Las pruebas automatizadas utilizan respuestas simuladas para los POST y procesos/archivos auxiliares propios para las esperas nativas.

**Ejecutable actualizado validado:** se abrió con TFT ya iniciado, conectó sin cache del SDK ni reiniciar el cliente, aceptó automáticamente una práctica con un único envío y entró en pausa al comenzar. Salir terminó sus dos procesos y conservó abierto el mismo proceso de TFT. Las pruebas automatizadas, incluidas las de privacidad, también aprobaron.

## Vías retiradas

Durante la investigación se habilitó y comprobó un proxy local del SDK de TFT, se observó su funnel mediante WAMP y se obtuvo el token GAPS. Ese mecanismo dependía de conocer un identificador que no se pudo recuperar de forma general al conectar tarde.

La arquitectura final retira proxy SDK, preparación YAML, WAMP y cache de funnel. No requiere PyYAML ni websocket-client ni impone abrir el bot antes del juego. Los experimentos permanecen excluidos del repositorio; sus pasos no son instrucciones de instalación.

Los intentos visuales anteriores también se retiraron. El permiso de controlar la pantalla se utilizó solo durante la investigación. El programa final no utiliza ratón, teclado, capturas, OCR, memoria del juego ni inyección. Una lectura de memoria denegada se detuvo sin obtener bytes ni eludir protecciones.

Referencias de contexto: [documentación oficial de TFT](https://developer.riotgames.com/docs/tft), [cliente de protocolo del autor](https://github.com/Mathss18/lol-headless-client/blob/master/src/suppliers/session.supplier.ts) y [AbstractLedgeEndpoint](https://github.com/DrMaxis/custom-league-client/blob/main/Core/src/main/java/com/hawolt/client/resources/ledge/AbstractLedgeEndpoint.java). La solución actual se contrastó con la sesión y el comportamiento reales; no depende de trasladar a ciegas contratos antiguos de LoL.
