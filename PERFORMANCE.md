# Consumo, pausa y cierre

El trabajador permanece en espera cuando está desactivado. Con TFT abierto lee únicamente los bytes nuevos de su registro; las peticiones de identidad y autenticación se concentran en conectar y confirmar una aceptación.

Durante la partida cierra HTTP y espera notificaciones nativas de archivos o cierre del proceso. No consulta la API mientras esa pausa está activa. La interfaz y la bandeja conservan su revisión de mensajes para responder a las acciones del usuario.

La CPU y la RAM dependen del equipo, del empaquetado y del estado de Windows. No se promete consumo nulo ni compatibilidad con cualquier configuración de hardware.

## Validación

Las pruebas comprueban ausencia de consultas durante la pausa, reanudación en la misma activación, cancelación, cierre de handles y terminación de los dos procesos del EXE. Las esperas se prueban también con procesos auxiliares propios.

Se verificó una aceptación interna real en una práctica de TFT, seguida de la pausa nativa. Salir terminó los procesos propios y dejó abierto el cliente del juego.

Los informes de rendimiento se generan localmente y no se incorporan al repositorio ni al release. Contienen contadores operativos de CPU, RAM y procesos para el diagnóstico; no se transmiten.

## Reproducir

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

El script ejecuta los diagnósticos en un escritorio aislado, comprueba que no queden procesos y conserva una copia del EXE anterior antes de sustituirlo. Los diagnósticos no se ejecutan durante el uso normal.
