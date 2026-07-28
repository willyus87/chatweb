# Chat interno - Guía rápida

## 1. Instalar dependencias
```
pip install -r requirements.txt
```

## 2. Correr el servidor
```
uvicorn main:app --host 0.0.0.0 --port 8000
```
- `--host 0.0.0.0` es lo que permite que otras PCs de la red se conecten (no solo la tuya).
- Dejá esta ventana abierta (o corrélo como servicio/tarea programada) mientras el chat esté en uso.

## 3. Encontrar tu IP local
En una consola de Windows (`cmd`):
```
ipconfig
```
Buscá "Dirección IPv4" (ej: `192.168.1.50`).

## 4. Que tus compañeros entren
Desde el navegador, cada uno va a:
```
http://TU_IP_LOCAL:8000
```
La primera vez, cada persona tiene que **crear su cuenta** (usuario + contraseña, mínimo 4
caracteres) con el link "¿No tenés cuenta? Creá una". Las próximas veces, inician sesión con
esas mismas credenciales. Las contraseñas se guardan hasheadas en `chat.db` (nunca en texto
plano), y cada nombre de usuario solo se puede registrar una vez — así se sabe con certeza
quién es quién al escribir.

Nota: las sesiones (tokens) viven en memoria del servidor. Si reiniciás `uvicorn`, todos van a
tener que volver a iniciar sesión (no a crear cuenta de nuevo — la cuenta ya queda guardada en
`chat.db`, solo se pierde la sesión activa).

## 4.b Habilitar HTTPS (necesario para el micrófono)

Los navegadores solo permiten usar el micrófono en conexiones seguras (HTTPS) o en `localhost`.
Como acá se entra por IP y HTTP normal, hay que generar un certificado autofirmado:

```
python generar_certificado.py TU_IP_LOCAL
```
Ejemplo: `python generar_certificado.py 192.168.1.50`

Esto crea `cert.pem` y `key.pem` en la misma carpeta. Después corré el servidor así:
```
uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

Y ahora se entra con **https** (no http):
```
https://TU_IP_LOCAL:8000
```

La primera vez, el navegador va a mostrar una advertencia tipo "La conexión no es privada" —
es normal con un certificado autofirmado (no lo emitió una autoridad reconocida). Hay que
hacer clic en "Configuración avanzada" > "Continuar de todas formas" (en Chrome/Edge) una
única vez por PC. Después queda funcionando con el micrófono habilitado.

Si en algún momento cambia la IP de tu PC servidor, hay que volver a correr
`generar_certificado.py` con la IP nueva.

## 5. Firewall de Windows
La primera vez, Windows puede preguntar si permitís la conexión — elegí "Permitir acceso" para redes privadas. Si no aparece el aviso, agregá una regla entrante para el puerto 8000 en el Firewall de Windows Defender.

## 6. Actualizaciones
- Si cambiás `static/index.html`: alcanza con que la gente recargue la página (F5).
- Si cambiás `main.py`: hay que detener uvicorn (Ctrl+C) y volver a correrlo.
- El historial de mensajes vive en `chat.db` (SQLite) y los archivos/audios subidos en la carpeta `uploads/` — no se pierden entre reinicios.

## Estructura del proyecto
```
chatweb/
├── main.py              -> backend FastAPI (WebSockets, historial, subida de archivos)
├── requirements.txt
├── chat.db               -> se crea solo al arrancar (historial)
├── uploads/              -> se crea solo al arrancar (archivos/audios/GIF subidos)
└── static/
    └── index.html        -> cliente (login + interfaz de chat)
```

## 7. Nuevas funciones: borrar conversación y foto de perfil
- **Borrar conversación**: dentro de un chat, el ícono 🗑 en el encabezado borra todo el historial
  con esa persona (para ambos lados) — pide confirmación antes de hacerlo, y no se puede deshacer.
- **Foto de perfil**: haciendo clic en tu propio avatar (arriba a la izquierda) podés subir una
  foto. Se actualiza al instante para vos y para el resto de los conectados.

## 8. Protegerse de un compañero que satura/tira el servidor

Se blindó el código para que **nunca se caiga** por datos malformados o inesperados (antes,
un mensaje raro por WebSocket podía tumbar esa conexión sin limpiarla bien). Además se agregó:
- Un límite de tamaño para subida de archivos (25 MB) y fotos de perfil (5 MB).
- Un límite de pedidos por IP: si una misma IP manda demasiados pedidos HTTP o intentos de
  conexión WebSocket en poco tiempo, se le empieza a responder "demasiados pedidos" en vez de
  saturar el servidor.

Esto ayuda, pero **la protección real y más importante es a nivel de red**, porque nada de lo
anterior evita un flood grande (ej: miles de conexiones TCP simultáneas) — eso hay que cortarlo
antes de que llegue a Python:

1. **Restringí el Firewall de Windows a IPs conocidas**: en vez de dejar el puerto 8000 abierto
   a cualquiera de la red, creá una regla de entrada que solo permita las IPs de tus compañeros
   (Firewall de Windows Defender con seguridad avanzada → Reglas de entrada → nueva regla →
   "Ámbito" → especificás las IPs remotas permitidas). Así, aunque alguien más sepa tu IP y
   puerto, ni siquiera le llega el paquete.
2. **Asigná IP fija a cada compañero** (reserva DHCP por MAC en el router) para que esa lista de
   IPs permitidas no se rompa sola.
3. **Corré el servidor con reinicio automático**: si igual llegara a caerse, conviene que se
   levante solo. Lo más simple en Windows es un `.bat` que lo reinicie si termina:
   ```bat
   :loop
   uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
   timeout /t 2
   goto loop
   ```
4. Si en algún momento el problema persiste o se pone más serio, una opción más robusta es
   correr el chat detrás de **Tailscale** (red privada tipo VPN): ahí el servidor deja de ser
   alcanzable por IP de LAN directamente, solo por los dispositivos que vos autorizás
   explícitamente — aunque sepan tu IP de red local, no podrían ni conectarse.

## 9. Funciones más recientes

- **Mensajes no leídos**: cada contacto muestra un punto rojo con la cantidad de mensajes sin
  leer; desaparece al abrir esa conversación. Además suena una notificación cuando llega un
  mensaje de un contacto que no tenés abierto (o si la pestaña no está en foco).
- **Tarjeta de contacto**: clic en la foto de un contacto (en la lista o en el header del chat)
  muestra su foto en grande, nombre y estado.
- **Ver imagen/GIF en tamaño real**: clic en una imagen o GIF del chat la abre en una pestaña
  nueva del navegador.
- **Tics de mensaje** (estilo WhatsApp): ✓ enviado, ✓✓ gris entregado (el otro está conectado),
  ✓✓ azul leído (el otro abrió la conversación).
- **Videollamada**: botón 📹 en el header del chat para llamar al contacto abierto. Usa WebRTC
  (conexión directa entre navegadores); el servidor solo hace de intermediario para coordinar
  la llamada (oferta/respuesta/candidatos ICE), nunca ve ni graba el video. Requiere HTTPS,
  igual que el micrófono. Si el otro no está conectado, avisa que no está disponible en vez de
  quedar sonando para siempre. Si una PC no tiene cámara (o está en uso por otra app), la
  llamada cae automáticamente a solo audio en vez de fallar, tanto para quien llama como para
  quien contesta.
- **Tema oscuro**: switch en la barra lateral (junto al botón de cerrar sesión). La elección
  queda guardada en el navegador de cada persona.
- **Diseño responsivo**: la ventana del chat se adapta al tamaño de la ventana del navegador.
  Si se achica mucho (menos de ~680px de ancho), pasa a mostrar una sola columna a la vez
  (lista de contactos o conversación abierta) con un botón "←" para volver.

## 10. Chats grupales

- Botón **"+"** junto a "Grupos" en la barra lateral: elegís un nombre y los contactos a
  incluir, y se crea el grupo al instante para todos los miembros.
- Funciona igual que un chat 1 a 1: texto, archivos, GIF, audio — con el nombre de quién
  escribió cada mensaje visible (ya que en un grupo puede ser cualquiera de varios miembros).
- El botón 🗑 borra el historial completo del grupo para todos.
- **Limitaciones de esta primera versión**: no hay tics de entregado/leído por miembro (sería
  mucho más complejo con varias personas), ni videollamada grupal — el botón de videollamada
  se oculta automáticamente al estar en un chat de grupo. Agregar miembros a un grupo ya
  creado es posible vía la API (`/api/groups/add_member`), pero todavía no hay un botón en la
  interfaz para hacerlo directamente desde el chat.

## Próximos pasos posibles
- Acceso remoto (fuera de la red del trabajo) vía Tailscale o similar.
- Botón de "Conectar por Escritorio Remoto" (como en la app de escritorio) integrado en el chat web.
- Buscador de GIFs online (requiere API key de Giphy) en vez de solo subir GIFs propios.
