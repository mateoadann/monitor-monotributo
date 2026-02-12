# Deploy — Monitor Monotributo

## Requisitos previos

- Docker Engine 24+
- Docker Compose v2 (`docker compose`)

## Configuracion inicial

```bash
# 1. Copiar variables de entorno
cp .env.example .env

# 2. Editar .env con valores reales (especialmente en PROD)
#    - MONITOR_DB_PASSWORD → contraseña segura
#    - MONITOR_SECRET_KEY  → string aleatorio largo
```

---

## Desarrollo (DEV)

```bash
# Levantar todo con hot-reload y puertos expuestos
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

# Solo reconstruir web
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build web

# Detener
docker compose -f docker-compose.yml -f docker-compose.dev.yml down

# Ejecutar tests
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec web pytest tests/
```

**Puertos expuestos en DEV:**

| Servicio | Puerto host   | Configurable con            |
|----------|---------------|-----------------------------|
| Web      | 5000          | `MONITOR_WEB_PORT`          |
| Postgres | 5432          | `MONITOR_PG_PORT`           |
| Redis    | 6379          | `MONITOR_REDIS_PORT`        |

---

## Produccion (PROD)

### 1. Crear la red externa (una sola vez en el VPS)

```bash
docker network create proxy_net
```

### 2. Levantar

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

### 3. Ver logs

```bash
# Todos los servicios
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f

# Solo web
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f web
```

### 4. Detener

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```

### 5. Actualizar (deploy)

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

**Servicios en PROD:**

| Servicio | container_name | Red          | Puertos expuestos al host |
|----------|----------------|--------------|---------------------------|
| web      | `monitor_web`  | default + proxy_net | Ninguno (expose 5000)  |
| worker   | (auto)         | default      | Ninguno                   |
| postgres | (auto)         | default      | Ninguno                   |
| redis    | (auto)         | default      | Ninguno                   |

---

## Nginx Proxy Manager (NPM)

Configurar un Proxy Host en NPM apuntando a:

- **Domain**: `monitor.tudominio.com`
- **Forward Hostname**: `monitor_web`
- **Forward Port**: `5000`
- **Scheme**: `http`

NPM resuelve `monitor_web` porque ambos estan conectados a `proxy_net`.

### Con Cloudflare

Si usas Cloudflare como DNS:
1. Crear registro A apuntando al IP del VPS
2. Activar proxy (nube naranja) para proteccion DDoS y cache
3. En NPM habilitar SSL con Let's Encrypt (Cloudflare puede manejar SSL en modo Full)

---

## Volumenes persistentes

| Volumen                | Uso                            |
|------------------------|--------------------------------|
| `monitor_postgres_data`| Datos de PostgreSQL            |
| `uploads_data`         | PDFs y archivos subidos        |

Para backup de postgres:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec postgres pg_dump -U monitor monitor > backup_$(date +%Y%m%d).sql
```

---

## Aliases utiles (opcional)

Agregar a `~/.bashrc` o `~/.zshrc`:

```bash
alias monitor-dev="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
alias monitor-prod="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
```

Uso:
```bash
monitor-dev up --build
monitor-prod up --build -d
monitor-prod logs -f web
```
