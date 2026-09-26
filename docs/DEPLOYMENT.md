# Despliegue y personalización

## Componentes

- `app/` (raíz del repositorio): Hermes Agent upstream completo, exportado del checkout activo del VPS.
- `custom/plugins/`: plugins instalados desde `$HOME/hermes/plugins`, sin datos de ejecución.
- `config-examples/`: plantillas públicas revisadas; sustituye todos los valores de ejemplo.
- `deploy/systemd/`: unidades de ejemplo para adaptar a tu usuario y rutas.

## Instalación general

1. Instala las dependencias documentadas por Hermes Agent y el runtime de Python requerido por el proyecto.
2. Crea una configuración privada local a partir de la documentación de Hermes. No copies secretos a Git.
3. Instala solo los plugins que necesites desde `custom/plugins/` en el directorio de plugins activo de Hermes.
4. Configura Alexa/HA mediante credenciales locales y los endpoints accesibles desde tu red. Configura el helper ADB en Home Assistant con su propia clave de emparejamiento privada.
5. Ajusta las unidades de servicio de `deploy/systemd/` y valida identidad, rutas y permisos antes de habilitarlas.

Las personalizaciones se integran como plugins Hermes; el repo incluye el código activo, no una imagen completa del VPS ni datos de usuarios.

## Telegram y comandos

Hermes recibe mensajes a través del gateway de Telegram. Los comandos disponibles dependen de los plugins activados y del registro de comandos de cada versión. Consulta los `plugin.yaml` y los manejadores de `custom/plugins/`. Los comandos de plugins registrados en la copia activa incluyen `/descarga`, `/descarga-archivo`, `/descarga-media`, `/descargas-inicio`, `/descargas-status`, `/descargas-stop`, `/gopeed-status`, `/guarda_esto`, `/impresora-status`, `/imprimir-archivo`, `/imprimir-texto` y `/remember`. Sus argumentos y descripción están en `custom/plugins/*/plugin.yaml` y en el código del plugin. Los comandos de Hermes integrados dependen de la versión instalada. Los valores de token y destinatario se deben configurar en el entorno privado.

## Integraciones

- **Hermy HQ**: plugin cliente para consultar el estado y enviar solicitudes a la cola de agentes. La aplicación completa vive en el repo `hermy-hq-personalized`.
- **Home Assistant**: plugins de Alexa/presencia y flujos de HA documentados en `home-assistant-personalized`.
- **ADB / Fire TV**: órdenes que el helper ADB de Home Assistant traduce a comandos del dispositivo. Empareja ADB localmente y nunca publiques sus llaves.
- **Presencia**: `custom/network-ingest/` recibe informes autenticados y enlaza la notificación con Home Assistant/Telegram. Configura tokens localmente y habilita el unit template solo tras revisar rutas.
- **video2implementation**: `custom/video2implementation/` contiene el flujo activo de extracción, contrato Markdown y adaptadores; añade las credenciales del proveedor fuera de Git.
- **Telegram**: transporta comandos y respuestas de Hermes; el bot se autentica con secreto local.

## Revisión de privacidad

Antes de instalar, repón nombres, IDs, rutas y direcciones ficticias por valores de tu instalación. Añade secretos mediante el mecanismo local de Hermes y Home Assistant. Nunca subas volcados, conversaciones, archivos de credenciales ni directorios de perfil.
