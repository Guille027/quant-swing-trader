# QSTS 24/7 en un servidor gratis (Oracle Cloud + Tailscale)

Así la app funciona sola todo el día: descarga los precios después de cada cierre, manda las órdenes a Alpaca y te
avisa por Telegram, aunque tu ordenador esté apagado. La verás desde el móvil o el PC con Tailscale (una red privada
gratis entre tus aparatos): el servidor **no** queda abierto a internet.

Tiempo: unos 45 minutos la primera vez. Coste: 0 € (Oracle pide una tarjeta solo para verificar que eres una persona).

## 0. En tu ordenador
Actualiza QSTS con **Actualizar QSTS.bat**.

## 1. Tailscale (la red privada)
1. Entra en **tailscale.com**, crea una cuenta gratis (por ejemplo con tu cuenta de Google).
2. Instala la app de Tailscale en tu **PC** y en tu **móvil** e inicia sesión con esa misma cuenta.
3. En la web de Tailscale (panel de administración): **Settings → Keys → Generate auth key** → *Generate key*.
   Copia la clave (empieza por `tskey-auth-`). Guárdala un momento en un bloc de notas.

## 2. Permiso para descargar la app (solo si tu repositorio de GitHub es privado)
1. En **github.com**: tu foto → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**.
2. Nombre: `qsts-servidor`. Caducidad: la más larga. **Repository access → Only select repositories →
   quant-swing-trader**. **Permissions → Contents → Read-only**. *Generate token*.
3. Copia el token (empieza por `github_pat_`) al bloc de notas.

## 3. Cuenta de Oracle Cloud
1. Entra en **oracle.com/cloud/free** → *Start for free*. Elige como región una cercana (por ejemplo *Spain Central
   (Madrid)* o *Germany Central (Frankfurt)*); no se puede cambiar después.
2. Completa el registro (te pedirá la tarjeta para verificar; con el plan gratis no cobra).

## 4. Crear el servidor
En la consola de Oracle: menú ☰ → **Compute → Instances → Create instance**.
1. **Name**: `qsts`.
2. **Image**: *Change image* → **Ubuntu** → **Canonical Ubuntu 24.04** → *Select image*.
3. **Shape**: *Change shape* → **Ampere** → **VM.Standard.A1.Flex** con **2 OCPU y 12 GB** de memoria (debe poner
   *Always Free-eligible*). Si al crear sale «Out of capacity», vuelve a intentarlo más tarde o elige
   **AMD → VM.Standard.E2.1.Micro** (también gratis; más lento, pero sirve).
4. **Networking**: deja lo que viene (crear una red nueva con IP pública: hace falta para salir a internet).
5. **Add SSH keys**: *Generate a key pair for me* y descarga la clave privada (por si un día hace falta).
6. Abajo, **Show advanced options → Management → Paste cloud-init script**. Abre el archivo
   `deploy/oracle/cloud-init.sh` de la carpeta de QSTS con el Bloc de notas, **pega tu clave de Tailscale** entre las
   comillas de `TAILSCALE_KEY=""` (y el token de GitHub en `GITHUB_TOKEN=""` si lo creaste), y copia **todo** el
   texto en esa casilla.
7. **Create**. Espera unos **15 minutos**: se instala todo solo.

## 5. Abrir la app del servidor
1. En el panel de Tailscale aparecerá una máquina llamada **qsts**.
2. Con Tailscale activado en el PC o el móvil, abre en el navegador **http://qsts** (si no abre, usa la dirección
   `100.x.x.x` que muestra Tailscale para esa máquina: `http://100.x.x.x`).
3. Abajo pondrá **servidor 24/7**.

## 6. Pasar tus datos y bots al servidor
1. En la app de tu **PC**: **Ajustes → Servidor 24/7 → Pasar el paper trading al servidor**. Guarda una copia en
   Descargas y el PC deja de mandar órdenes (solo uno puede hacerlo).
2. En la app del **servidor** (http://qsts): **Ajustes → Servidor 24/7 → elegir archivo** → el `qsts-datos.db.gz`
   de tus Descargas → **Cargar estos datos**. El servidor se reinicia con tus datos en uno o dos minutos.
3. En la app del servidor, **Ajustes**: vuelve a poner tus **claves de Alpaca** (paper) y el **token de Telegram**
   (escríbele algo a tu bot y pulsa *Detectar mi chat*; luego *Enviar mensaje de prueba*). Las claves no viajan en
   la copia, por seguridad.

Listo. Desde ahora usa siempre **http://qsts** (también desde el móvil). Tu PC puede seguir abriendo QSTS para mirar
backtests, pero sin paper trading (está desactivado allí).

## Mantenimiento
- **Actualizar**: en la app del servidor, **Ajustes → Actualizar el servidor a la última versión**.
- **Precios**: se descargan solos cada día hacia las 22:45 de España (la línea de abajo dice cómo van).

## Si algo falla
- **Pasados 20 minutos no aparece «qsts» en Tailscale**: casi siempre es la clave de Tailscale mal copiada o ya
  usada. Crea otra clave, y en Oracle borra la instancia (*Terminate*) y créala de nuevo con la clave nueva.
- **Aparece pero http://qsts no abre**: espera 5 minutos más (la instalación de Python tarda) y prueba la dirección
  `http://100.x.x.x`.
- En cualquier otro caso, mándame una captura de lo que veas.

## A tener en cuenta
- Oracle ha avisado de que puede **recuperar servidores gratuitos que pasan mucho tiempo casi sin usar** la CPU.
  QSTS trabaja poco la mayor parte del día. Si un día desaparece, se vuelve a crear con estos mismos pasos (y si
  pasa, la cuenta *Pay As You Go* de Oracle sigue sin cobrar dentro de los límites gratis y no tiene esa regla).
  Esto viene de la información pública de Oracle que conozco: compruébalo en su página del plan gratuito.
- Yahoo Finance a veces limita las descargas desde servidores en la nube. Si los precios no se actualizan, la
  línea de abajo y la página Datos lo dirán: avísame.
