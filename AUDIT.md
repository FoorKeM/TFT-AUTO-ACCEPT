# Auditoría de código y privacidad

## Alcance

Se revisaron las fuentes, dependencias declaradas, pruebas, configuración de empaquetado y contenido descompuesto del EXE. Se contrastaron constantes y archivos con identificadores y credenciales del equipo de prueba únicamente en memoria, sin guardarlos en el informe.

## Resultados

- No se encontraron credenciales reales, identificadores propios de Riot, nombres de jugador, rutas personales ni claves privadas incorporados al EXE.
- No hay telemetría, servidor de acceso remoto, actualización automática ni envío de registros a terceros.
- Las peticiones locales usan la API de Riot Client en 127.0.0.1. Las remotas verifican TLS y se limitan al dominio de Riot validado por el código; no siguen redirecciones ni proxies del entorno.
- Las credenciales se obtienen de la sesión local de cada equipo y permanecen en memoria. La autorización específica de TFT se valida contra la cuenta del cliente y su membresía de partida.
- Los informes y registros propios excluyen tokens, encabezados de autorización y respuestas completas. Los errores inesperados conservan únicamente su tipo.
- Las pruebas usan identidades y tokens ficticios. Dos pruebas de flujo de datos comprueban que los diagnósticos no persistan el texto sensible de una excepción.
- Los callbacks, estructuras nativas y exportaciones compatibles se conservaron después de revisar sus usos indirectos. Se retiró el adaptador SDK sustituido por la autorización de Riot Client.
- Los comentarios y nombres propios del código no contienen emojis ni adornos pictográficos.

## Validación funcional

Se probó el EXE con TFT ya abierto: una aceptación interna en una práctica, el mismo proceso del cliente, pausa durante la partida y cierre completo de los procesos propios. La suite automatizada comprueba también cancelación, desactivación, respuestas manuales, deduplicación y recuperación de errores.

El examen del ejecutable incluyó archivos internos, objetos de código, constantes, nombres de archivo y bibliotecas. Los certificados empaquetados son públicos; no incluyen claves privadas. Las licencias y referencias públicas de las dependencias se conservan.

## Publicación

La publicación pública usa una copia limpia con identidad de autor neutra. El historial anterior se conserva privado; sus metadatos personales no se transfieren al repositorio público. Los entornos, configuraciones locales, registros e informes de diagnóstico no se publican.

Esta revisión verifica el alcance descrito y evita divulgar datos del equipo de desarrollo; no constituye una garantía absoluta frente a cualquier vulnerabilidad futura. Cada usuario debe obtener los binarios desde los releases oficiales y verificar sus sumas SHA256.
