# TFT AUTO-ACCEPT

Autoaceptador de Windows con ventana compacta y bandeja. Utiliza la API interna de Riot Client y TFT, sin mover el ratón, pulsar teclas, capturar pantalla ni usar OCR. Puede conectarse con TFT ya abierto; no necesita reiniciarlo ni observar su arranque. Conserva el adaptador LCU del cliente anterior, sin filtros de modalidad.

El código es público. El ejecutable se distribuye en la sección **Releases** de este repositorio.

## Uso

1. Descarga `TFT-AUTO-ACCEPT.zip` y extrae `TFT AUTO-ACCEPT.exe`. No requiere instalar Python. GitHub normaliza el EXE directo como `TFT.AUTO-ACCEPT.exe`.
2. Abre el autoaceptador y pulsa **Activar**. Puedes hacerlo antes o después de abrir TFT, con tu sesión de Riot iniciada.
3. Inicia la búsqueda desde TFT. Ante una confirmación pendiente, espera **1 segundo**, la vuelve a comprobar y envía la aceptación interna. Puedes dejarlo en la bandeja; TFT no necesita estar en primer plano.
4. **Desactivar** cancela las esperas. La X pregunta **Ocultar en bandeja**, **Salir** o **Cancelar**. Ocultar conserva el estado. La bandeja ofrece Activar/Desactivar, Mostrar y Salir, con tick verde o X roja.

La ventana mide 260 × 160 y comienza desactivada. La aplicación no inicia búsquedas ni reinicia el juego durante su uso normal.

## Conexión interna

La aplicación descubre las credenciales locales de Riot Client y consulta la autorización específica de `teamfighttactics-client`. Si falta, solicita una autorización OAuth independiente para la sesión ya iniciada, con `keepAlive: false`. No pide tu contraseña ni crea otra sesión de juego; conserva la autenticación global de Riot Client.

Antes de aceptar, consulta el jugador propio y comprueba su membresía TFT. La API puede mostrar una party de LoL en `currentParty` y, al mismo tiempo, la membresía correcta de TFT en `parties[]`; se valida esta última sin confundir ambos productos.

Los tokens permanecen en RAM. Las conexiones remotas verifican TLS y no usan proxies, redirecciones ni reintentos automáticos. La conexión local se limita a 127.0.0.1. El programa final prescinde del proxy SDK del juego, de la preparación YAML, de WAMP y de la cache de funnel.

## Privacidad

Cada copia utiliza la sesión de Riot iniciada en el equipo donde se ejecuta. El código y el EXE no incluyen credenciales, identificadores de cuenta, nombres de jugador ni rutas personales del desarrollador. Los ejemplos de las pruebas son ficticios.

No hay telemetría, actualizaciones automáticas ni envíos a servidores del proyecto. La aplicación solo hace peticiones al Riot Client local y a los servicios HTTPS de Riot autorizados por el código. No abre un servidor de acceso remoto ni instala servicios o controladores.

Las credenciales no se guardan en archivos propios. El registro local contiene fechas, estados y tipos de error, con rotación; no guarda tokens ni identificadores de cuenta y no se envía. Los diagnósticos omiten el texto arbitrario de las excepciones para evitar copiar datos sensibles.

Descarga el ejecutable de los releases de este repositorio y comprueba las sumas SHA256 publicadas. El código fuente y los tests permiten revisar el comportamiento; la revisión no constituye una garantía absoluta frente a cualquier vulnerabilidad.

## Confirmaciones y errores

El estado del lobby se obtiene del registro incremental de TFT. Solo un aviso reciente y pendiente permite actuar. Antes del POST se revalidan activación, fase, identidad de la confirmación y respuestas manuales registradas, también después de consultar la identidad de la cuenta.

Se permite un solo envío por confirmación del TFT independiente. Si queda incierto por timeout o desconexión, no se repite basándose en un estado anterior. Aparece un aviso para revisar el juego. Desactivar/reactivar no permite duplicar ese intento; una confirmación posterior tiene su propia identidad.

El cliente anterior conserva su API LCU y hasta dos intentos condicionados tras un timeout, porque dispone de una consulta de la respuesta del jugador.

## Consumo, pausa y cierre

Desactivado no consulta al cliente. Durante la partida cierra sus conexiones y espera cambios del registro o cierre del proceso mediante notificaciones nativas cancelables. Al volver al lobby reanuda solo si sigue vigente la misma activación.

Cuando falta el cliente, una espera de archivo permite detectar su arranque. Los textos e iconos solo se actualizan al cambiar. No se promete consumo nulo.

Salir cancela las esperas, cierra HTTP y libera los handles y procesos propios. Se conserva un límite de cinco segundos si un hilo no responde. Una petición ya iniciada puede terminar; no habilita nuevas aceptaciones ni sobrescribe «Desactivado». El programa no termina TFT.

Los registros propios están en `%LOCALAPPDATA%\TFT AUTO-ACCEPT\logs\TFT AUTO-ACCEPT.log`, con 1 MB por archivo y dos copias. No contienen tokens, encabezados de autorización ni respuestas completas del cliente.

## Validación y alcance

Las pruebas automatizadas cubren autenticación, HTTP, membresías, cancelación, deduplicación, pausa nativa y cierre; las peticiones de aceptación de esas pruebas están simuladas.

Con autorización del usuario se realizaron pruebas reales en la práctica de Tictoco, cola 1220:

- El trabajador completo anterior, con token GAPS, envió un único POST a los 1,548 segundos, detectó el comienzo de la partida, entró en pausa nativa y cerró correctamente.
- La autorización RSO específica obtenida desde Riot Client permitió aceptar mediante API: POST 200 y comienzo de partida comprobado. Esta prueba confirma la vía de autenticación que usa la nueva implementación.

**Ejecutable actualizado validado:** se abrió con TFT ya iniciado, conectó sin cache del SDK ni reiniciar el cliente, aceptó automáticamente una práctica con un único envío y entró en pausa al comenzar. Salir terminó sus dos procesos y conservó abierto el mismo proceso de TFT. Las pruebas automatizadas también aprobaron, incluidas las de privacidad de los diagnósticos.

Detalles: [NEW_CLIENT.md](NEW_CLIENT.md). Consumo y verificación: [PERFORMANCE.md](PERFORMANCE.md). Auditoría: [AUDIT.md](AUDIT.md).

## Desarrollo

Windows 10/11 x64 y Python 3.12 con Tkinter:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe "TFT AUTO-ACCEPT.py"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

Las dependencias de ejecución están en `requirements.txt`; las de empaquetado, en `requirements-lock.txt`. Se retiraron PyYAML y websocket-client al eliminar la dependencia del proxy SDK. Se conserva el antiguo `venv` sin utilizarlo.

`build.ps1` construye en una carpeta temporal, ejecuta los diagnósticos del EXE, comprueba la terminación de sus dos procesos y guarda una copia del ejecutable anterior antes de sustituirlo. Los diagnósticos gráficos usan un escritorio aislado.

Cada publicación en `main` genera un release nuevo con EXE, ZIP y SHA256, conservando los anteriores. Los entornos, registros y herramientas de investigación quedan excluidos del repositorio.
