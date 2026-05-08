# Traefik + Cloudflare Origin Certificate Setup for Dokploy

Use this guide when deploying a new service on a Dokploy VPS under a subdomain of `ndequity.org`.

The wildcard Cloudflare origin certificate (`ndequity.org` + `*.ndequity.org`) already exists and Cloudflare SSL is already set to **Full (strict)**. You just need to upload the cert to the new Dokploy instance and configure Traefik to use it as the default.

The setup involves three locations that must all be correct:
1. **Dokploy UI** — upload the cert + verify Traefik static config
2. **VPS filesystem** — create a dynamic Traefik config file that makes the cert the default
3. **docker-compose.yml** — add Traefik labels to each service

---

## Step 1: Upload the Certificate to Dokploy

You need the PEM certificate and private key from the existing Cloudflare origin cert (created in the ndequity.org project).

1. In the **Dokploy UI**, go to **Settings → Certificates**
2. Click **Add Certificate**
3. Paste in the certificate (PEM) and private key
4. Save

Dokploy stores the cert files at `/etc/dokploy/traefik/dynamic/certificates/` on the VPS with generated filenames.

---

## Step 2: Find the Certificate Filenames on the VPS

SSH into the VPS and list the cert directory:

```bash
ssh root@<your-vps-ip>
ls -la /etc/dokploy/traefik/dynamic/certificates/
```

Note the exact filenames — you'll need them in Step 4.

---

## Step 3: Verify the Traefik Static Config Has a File Provider

1. In **Dokploy UI → Advanced → Traefik**, confirm the static config contains:

```yaml
providers:
  file:
    directory: /etc/dokploy/traefik/dynamic
    watch: true
```

If it's missing, add it and restart the Traefik container from the Dokploy UI.

The `websecure` entrypoint will likely look like this by default:

```yaml
entryPoints:
  websecure:
    address: :443
    http:
      tls:
        certResolver: letsencrypt
```

Leave the `certResolver: letsencrypt` line as-is — the dynamic config in Step 4 overrides which cert is actually served without touching the static config.

---

## Step 4: Create the Default Certificate Dynamic Config

This is the step Dokploy does **not** do for you. The Certificates UI uploads the files but never tells Traefik to use them as the default. Without this file, Traefik serves its self-signed "TRAEFIK DEFAULT CERT" for any router with `tls=true`.

SSH into the VPS and create `/etc/dokploy/traefik/dynamic/default-cert.yml`:

```yaml
tls:
  stores:
    default:
      defaultCertificate:
        certFile: /etc/dokploy/traefik/dynamic/certificates/<your-cert-filename>.crt
        keyFile: /etc/dokploy/traefik/dynamic/certificates/<your-cert-filename>.key
```

Replace `<your-cert-filename>` with the actual filenames from Step 2.

Traefik watches the `dynamic/` directory and picks up the file automatically — no restart needed.

---

## Step 5: Configure Traefik Labels in docker-compose.yml

For each service you want to expose, add labels like these:

```yaml
services:
  myapp:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.myapp.rule=Host(`search.ndequity.org`)"
      - "traefik.http.routers.myapp.entrypoints=websecure"
      - "traefik.http.routers.myapp.tls=true"
      - "traefik.http.services.myapp.loadbalancer.server.port=3000"
    networks:
      - dokploy-network

networks:
  dokploy-network:
    external: true
```

Key things to note:
- `tls=true` without `certresolver` — tells Traefik to use TLS but look for the cert in the default store (set in Step 4)
- The service must be on `dokploy-network` for Traefik to discover it
- No ports need to be published to the host — Traefik routes to the container via the Docker network

**If your app uses Django with `SECURE_SSL_REDIRECT`**, add an `X-Forwarded-Proto` header so Django knows the upstream request was HTTPS (Traefik doesn't forward this by default, which causes redirect loops):

```yaml
- "traefik.http.middlewares.myapp-headers.headers.customrequestheaders.X-Forwarded-Proto=https"
- "traefik.http.routers.myapp.middlewares=myapp-headers"
```

---

## Step 6: Verify It's Working

```bash
curl -I https://search.ndequity.org
```

The cert issuer should be "Cloudflare Inc ECC CA-3" (or similar), not "TRAEFIK DEFAULT CERT".

If you still see the Traefik self-signed cert, check:
- **Wrong filenames** in `default-cert.yml` — re-check Step 2
- **YAML syntax error** in `default-cert.yml` — check Traefik logs: `docker logs <traefik-container-id>`
- **Missing network** — the service isn't attached to `dokploy-network`

---

## Summary

| Location | What | How |
|---|---|---|
| Dokploy UI → Certificates | Cert + key uploaded | Dokploy UI |
| Dokploy UI → Advanced → Traefik | File provider present in static config | Dokploy UI |
| `/etc/dokploy/traefik/dynamic/default-cert.yml` | Sets Cloudflare cert as Traefik's default TLS store | SSH + text editor |
| `docker-compose.yml` | Traefik labels on each service | Code |
