# command-gate (`cgate`)

Middleware/CLI que se interpone entre asistentes de IA (Claude Code, Sisyphus, opencode, etc.) y los servidores que administra Wanderson, para que la IA **proponga** comandos pero un humano **decida** qué se ejecuta, con **auditoría** completa de qué se corrió, dónde, por qué y con qué resultado.

> Este documento es el punto de partida para continuar el desarrollo en otra sesión (Claude Code / opencode). Recoge las decisiones ya tomadas y el nivel de detalle necesario para empezar a construir sin tener que re-derivar el diseño.

## Motivación

Hoy la IA genera comandos que el operador copia y corre a mano en cada servidor. Eso es lento y no deja rastro. `cgate` busca:

- Mantener al humano como punto de decisión final sobre qué se ejecuta.
- Dar velocidad: revisar y aprobar sin salir del flujo de trabajo.
- Dejar auditoría: qué se corrió, en qué servidor, por qué, y con qué resultado — útil tanto para revisión como para cuando algo falla.

## Principios de diseño

1. **La IA nunca ejecuta directamente.** Solo puede *proponer* un comando. La ejecución ocurre en un paso separado, disparado por el humano.
2. **Comandos libres, no un catálogo cerrado.** No se whitelistea qué se puede pedir — el control real es la revisión humana antes de ejecutar.
3. **Identidad propia, no cuentas compartidas.** Cada operador ejecuta con su propia identidad (dominio, SSH, o credencial explícita que ya tenía) — el CLI no otorga acceso nuevo, solo estructura y audita el acceso que la persona ya tiene.
4. **Sin secretos nuevos que proteger cuando se puede evitar.** Kerberos passthrough cuando la máquina está en el dominio; solo si no aplica, se piden credenciales explícitas, y siempre van a un almacén nativo del sistema operativo, nunca texto plano.
5. **Mínimo componente central posible.** El diseño favorece que cada máquina de operador sea autosuficiente; lo central se limita a lo que realmente necesita coordinación entre personas (identidad verificada, auditoría consolidada).

---

## Fase 1 — Personal, local, sin infraestructura central

**Objetivo de esta fase:** que Wanderson pueda usar `cgate` desde su propia máquina (o cualquier otra que use) para mediar la ejecución de comandos que Claude Code u opencode proponen, sin depender de ningún servicio de la empresa. Debe poder instalarse igual en Windows, Linux o macOS.

### Stack elegido: Python

Comparado contra C#/.NET y Node/TypeScript, Python es la única opción donde las tres piezas críticas del proyecto tienen librerías maduras simultáneamente:

| Necesidad | Librería | Nota |
|---|---|---|
| Servidor MCP | SDK oficial de Anthropic para Python | Igual de maduro que el de Node; no existe SDK oficial en C#. |
| Ejecución en Windows | `pywinrm` | WinRM/PowerShell Remoting. |
| Ejecución en Linux | `paramiko` | SSH. |
| Credenciales seguras cross-OS | `keyring` | Abstrae Windows Credential Manager, macOS Keychain y Secret Service (Linux) con una sola API — exactamente lo que necesita este proyecto. |
| Persistencia local | `sqlite3` (stdlib) | Sin dependencias externas. |
| Empaquetado a binario único | PyInstaller o Nuitka | Paso extra de build, pero evita pedirle al operador que instale un runtime de Python. |

En C# se pierde el SDK oficial de MCP; en Node se pierde la integración nativa de WinRM. Python es el único punto donde ninguna de las tres piezas críticas es una solución de segunda clase.

### Nombre del proyecto

- **Repositorio:** `command-gate`
- **Comando CLI:** `cgate`
- Verificado libre en npm y PyPI al momento de definir el nombre (no hay colisión de paquete, y el nombre de repo en GitHub no requiere unicidad global).
- Se descartó `ch` (muy genérico) y `ss` (colisiona con el comando `ss` de `iproute2` en Linux).

### Componentes de Fase 1

```
Claude Code / opencode
        │  (MCP: propose_command)
        ▼
  Servidor MCP local (Python)
        │  escribe propuesta
        ▼
  Cola local (SQLite)
        │  cgate watch la lee
        ▼
  cgate watch — aprobación interactiva (y/n/a/r)
        │  al aprobar
        ▼
  Ejecutor (WinRM o SSH, según el tipo de servidor)
        │  resultado
        ▼
  Auditoría (misma base SQLite)
        ▲
        │  (MCP: check_status) — la IA puede consultar el resultado
Claude Code / opencode
```

Todo corre en la máquina del operador. No hay ningún servicio de red compartido en esta fase.

### Comandos del CLI

```
cgate connections add <alias> <hostname>     # agrega una conexión
cgate connections remove <alias>
cgate connections list
cgate watch                                   # cola de aprobación interactiva
```

No existe `cgate login` en esta fase — no hay SSO todavía. El campo "usuario" en la auditoría se llena con el usuario del sistema operativo, salvo que la conexión use credenciales explícitas (ver abajo), en cuyo caso se registra el identificador de esa credencial.

### Conexiones: `cgate connections add <alias> <hostname>`

1. **Detección automática del tipo de servidor**, probando el puerto/protocolo:
   - Responde WinRM (5985/5986) → se marca como Windows.
   - Responde SSH (22) → se marca como Linux.
   - Si responde a ambos (ambiguo — p. ej. un Windows con OpenSSH Server habilitado), se le pregunta al usuario cuál usar en el momento. Ninguno de los servidores Windows actuales (`srv-bidev`, `srv-prdfrnt`, `srv-prdsvcs`, `srv-iasapp`) tiene SSH habilitado hoy, pero puede cambiar — por eso la detección debe ser dinámica, no hardcodeada.
   - El tipo detectado (`windows` / `linux`) se guarda como metadato fijo del alias y se expone a la IA vía `list_connections`, para que el comando propuesto use el dialecto correcto (PowerShell vs. Bash) desde el inicio.

2. **Autenticación, por conexión, no global:**
   - Si la máquina donde corre `cgate` está unida al dominio → usa Kerberos passthrough automáticamente (sin credencial guardada).
   - Si no está unida al dominio → `cgate connections add` pide usuario/contraseña (WinRM) o llave/contraseña (SSH), y se guarda con `keyring` en el almacén nativo del sistema — nunca en un archivo de texto plano. Quien agrega la conexión ya tenía esa credencial; `cgate` no otorga acceso nuevo, solo la persiste de forma segura para no tener que reintroducirla cada vez.

### El servidor MCP

Tools expuestos a la IA:

- **`propose_command(server_alias, command, batch_id?, batch_title, batch_description?, reason?)`** — registra la propuesta en la cola; nunca ejecuta. `batch_title` es obligatorio (ver "Título y descripción de lote" abajo); `batch_description` es opcional.
- **`list_connections()`** — devuelve los alias disponibles junto con su tipo (`windows`/`linux`), para que la IA sepa qué dialecto de comando usar.
- **`check_status(batch_id)`** — devuelve el estado y resultado de cada comando del lote (pendiente/aprobado/rechazado/ejecutado/fallido), para que la IA pueda continuar su plan sin que el operador tenga que copiar y pegar la salida manualmente.

> Pendiente de confirmar: si Sisyphus habla MCP de forma nativa. Si no, se necesitaría un adaptador aparte que hable el protocolo propio de Sisyphus contra la misma cola/lógica.

### Título y descripción de lote

Cada lote de comandos que llega de una misma invocación de la IA debe traer:

- **Título (obligatorio):** una línea que identifica el propósito del lote de un vistazo. Ej.: *"Reinicio de servicio X en srv-prdsvcs"*.
- **Descripción (opcional):** una sola línea con el "por qué", solo cuando el título no basta. Ej.: *"El servicio dejó de responder tras el despliegue de las 3pm"*.

La descripción es opcional a propósito: si fuera obligatoria, la IA terminaría rellenando descripciones vacías de contenido ("se ejecutan comandos de diagnóstico") que no aportan nada.

### Cola de aprobación: `cgate watch`

- **Cola de dos niveles:** los lotes se atienden en FIFO; dentro de cada lote, los comandos también en FIFO.
- **`cgate watch` solo muestra el lote activo** (el primero sin resolver por completo). Si llega un lote nuevo mientras se revisa el actual, se encola detrás — nunca se intercalan comandos de lotes distintos en la vista.
- **Aviso no intrusivo** cuando hay un lote esperando: `▲ N lote(s) nuevo(s) en espera — se muestran al terminar el actual`.
- **Un lote se considera resuelto** cuando todos sus comandos tienen estado final (ejecutado o rechazado), ya sea uno por uno o usando `a`/`r`. Solo entonces se pasa al siguiente lote de la cola.
- **Controles por comando:**
  - `y` — aprobar y ejecutar este comando; se ve el resultado antes de pasar al siguiente.
  - `n` — rechazar este comando (no se ejecuta).
  - `a` — aprobar el resto de comandos pendientes del lote actual, en orden, sin volver a preguntar.
  - `r` — rechazar el resto de comandos pendientes del lote actual.
  - `a`/`r` solo afectan lo que **todavía está pendiente** desde donde está el cursor — lo ya ejecutado antes no se deshace.

**Trade-off aceptado conscientemente:** si dos sesiones de IA proponen lotes en servidores completamente distintos y sin relación, la segunda espera a que se resuelva la primera, aunque no haya conflicto real entre ellas. Se prioriza la claridad (nunca se mezclan comandos de tareas distintas en la vista) sobre el paralelismo. Si en el uso real esto se siente lento, se puede reconsiderar un mecanismo explícito para "saltar" a otro lote sin cambiar el comportamiento por defecto.

### Colores en `cgate watch`

| Elemento | Color | Nota |
|---|---|---|
| Título del lote | Teal | Lo primero que se lee. |
| Descripción del lote | Gris, cursiva | Deliberadamente discreta. |
| Comando pendiente (el que se está revisando) | Blanco/texto claro, con marcador `●` amarillo | Debe resaltar sobre el resto. |
| Comando ejecutado con éxito | Verde, con `✓` | |
| Comando rechazado | Rojo, con `✗` | |
| Prompt `y/n/a/r` | Azul | |
| Badge de tipo de servidor (`WIN` / `LNX`) junto al alias | Azul claro / verde, como etiqueta compacta | Para saber de un vistazo qué dialecto de comando se está revisando. |
| Aviso de lote en espera | Amarillo, con `▲` | |

Los símbolos (`✓ ✗ ● ▲`) siempre acompañan al color — el color nunca es la única señal, para que funcione igual con daltonismo.

### Esquema de datos (SQLite)

Diseñado para que en Fase 2 sea un `export`/migración de datos a SQL Server, no un rediseño — mismas columnas conceptuales.

```sql
CREATE TABLE batches (
  id TEXT PRIMARY KEY,           -- UUID
  title TEXT NOT NULL,
  description TEXT,
  requested_by_agent TEXT,       -- 'claude-code' | 'sisyphus' | otro
  created_at TEXT NOT NULL,      -- ISO 8601 UTC
  resolved_at TEXT
);

CREATE TABLE commands (
  id TEXT PRIMARY KEY,           -- UUID
  batch_id TEXT NOT NULL REFERENCES batches(id),
  position INTEGER NOT NULL,     -- orden dentro del lote
  server_alias TEXT NOT NULL,
  server_type TEXT NOT NULL,     -- 'windows' | 'linux'
  command TEXT NOT NULL,
  status TEXT NOT NULL,          -- 'pending' | 'approved' | 'rejected' | 'executed' | 'failed'
  result TEXT,                   -- stdout/stderr/exit code combinados o serializados
  approved_by TEXT,              -- usuario de OS o identificador de credencial
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
```

### Fuera de alcance en Fase 1 (explícitamente)

- Sin SSO / login.
- Sin base de datos central — todo vive en SQLite local.
- Sin multiusuario — una persona, una máquina (aunque puede ser cualquier máquina, dentro o fuera del dominio).

---

## Fase 2 — Equipo, identidad centralizada, auditoría consolidada

**Objetivo de esta fase:** que el resto del equipo pueda instalar `cgate` en sus propias máquinas, y que la auditoría de todas las personas quede consolidada en un solo lugar, con la identidad de cada operación verificada por el login corporativo (Keycloak self-hosted) — sin que eso implique centralizar la ejecución ni las credenciales de servidor.

### Qué cambia respecto a Fase 1

- La ejecución **sigue pasando en la máquina de cada operador**, con su propia identidad (dominio o credencial explícita) — eso no se centraliza.
- Lo que se centraliza es el **reporte de auditoría**: cada resolución de comando (ejecutado/rechazado) se reporta a una API central que escribe en SQL Server, autenticada con el token de Keycloak de quien aprobó.
- Se agrega `cgate login`.

### `cgate login`

- Flujo OIDC contra Keycloak: Authorization Code + PKCE, con un listener local (`http://localhost:PUERTO/callback`) para recibir la redirección.
- El token se cachea cifrado usando `keyring`, bajo una clave como `command-gate:keycloak-token`.
- El CLI debe manejar el refresh de token de forma que no dependa de refrescos muy frecuentes — ya hubo un problema de degradación de performance por refresh de Keycloak cada 3–4 segundos en otro servicio (`srv-bidev`); conviene diseñar el cacheo de token de este CLI para evitar ese mismo patrón.

### Tabla central de auditoría (SQL Server)

```sql
CREATE TABLE AuditLog (
  Id UNIQUEIDENTIFIER PRIMARY KEY DEFAULT NEWID(),
  BatchId UNIQUEIDENTIFIER NOT NULL,
  BatchTitle NVARCHAR(200) NOT NULL,
  BatchDescription NVARCHAR(500) NULL,
  ServerAlias NVARCHAR(100) NOT NULL,
  ServerType NVARCHAR(20) NOT NULL,          -- 'windows' | 'linux'
  Command NVARCHAR(MAX) NOT NULL,
  RequestedByAgent NVARCHAR(50) NULL,        -- 'claude-code' | 'sisyphus'
  ApprovedByUser NVARCHAR(255) NOT NULL,     -- claim 'email' del token Keycloak
  Status NVARCHAR(20) NOT NULL,              -- 'approved' | 'rejected' | 'executed' | 'failed'
  Result NVARCHAR(MAX) NULL,
  CreatedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
  ResolvedAt DATETIME2 NULL
);
```

### API central

- **`POST /api/audit`** — requiere `Authorization: Bearer <token Keycloak>`. Valida el claim `email` del token, inserta la fila en `AuditLog`. Responde `401` si el token expiró y no hay refresh válido — el CLI debe pedir `cgate login` de nuevo en ese caso, sin perder la propuesta local (se reintenta el reporte, no se pierde el resultado de la ejecución).
- No expone ninguna ruta de ejecución — es exclusivamente un receptor de auditoría. La API central nunca tiene credenciales de servidor ni ejecuta nada.

### Qué se mantiene igual que en Fase 1

- Detección automática de tipo de servidor.
- Cola local por lotes en SQLite (sigue existiendo como buffer local — el reporte a la API central ocurre después de resolver cada comando, no reemplaza la cola local).
- Los tools MCP (`propose_command`, `list_connections`, `check_status`) — mismos contratos, ahora con el reporte de auditoría central como paso adicional al resolver.
- El modelo de autenticación por conexión (dominio o credencial explícita con `keyring`) — Keycloak nunca autentica contra los servidores, solo identifica quién aprobó en el log.

---

## Fase 3 — Dirección a explorar (visión, no especificación cerrada)

Esta fase no se ha diseñado en detalle todavía — se incluye como brújula de hacia dónde podría evolucionar el proyecto una vez que Fase 2 esté en uso real por el equipo. Ninguno de estos puntos es una decisión tomada; son posibles direcciones a validar cuando llegue el momento:

- **Panel de consulta sobre `AuditLog`** — una vista (web o CLI) para responder preguntas tipo "¿qué se corrió en `srv-prdsvcs` la semana pasada?" sin tener que escribir SQL a mano cada vez.
- **Reglas de auto-aprobación acotadas** — para comandos de solo lectura/diagnóstico claramente identificables (ej. `Get-Service`, `systemctl status`), permitir que el operador marque de antemano una categoría como "no necesita confirmación interactiva", sin abrir la puerta a comandos de escritura libres. Esto tendría que diseñarse con mucho cuidado para no erosionar el principio central del proyecto (nada se ejecuta sin revisión humana).
- **Catálogo central de conexiones opcional** — si el equipo crece, considerar un directorio compartido de alias → hostname (sin credenciales) para no tener que redefinir los mismos servidores en cada máquina, sin perder el modelo de "cada quien con su propia identidad" para ejecutar.
- **Soporte para otros agentes además de Claude Code / opencode / Sisyphus**, si aparecen nuevos clientes MCP en el flujo de trabajo del equipo.
- **Posible apertura fuera de JI Cohen** — si el proyecto demuestra valor internamente, evaluar si tiene sentido proponerlo como herramienta de uso más amplio. Esto es puramente especulativo por ahora.

---

## Preguntas abiertas para la siguiente sesión de construcción

1. ¿Sisyphus soporta MCP nativamente, o necesita un adaptador aparte?
2. ¿Dónde vivirá el repositorio (usuario personal de GitHub vs. organización de la empresa) y será público o privado?
3. Canal de distribución del binario empaquetado (PyInstaller/Nuitka): ¿descarga manual desde releases de GitHub, o algo más automatizado (winget/homebrew/script de instalación)? No definido — puede quedar como decisión de la sesión de construcción.
