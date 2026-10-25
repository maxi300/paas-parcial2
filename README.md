# NubePaaS · Parcial 2 (Informática en la Nube)

Plataforma como Servicio **multi-tenant tipo Render** sobre Kubernetes (minikube, 2 nodos).
El cliente da **4 valores** —nombre/dominio, repositorio Git, runtime/puerto y motor de base de datos— y la
plataforma crea el resto con Helm: namespace aislado, cuota del plan, certificado SSL (OpenSSL), base de datos con
volumen persistente y contraseñas en Secrets, Ingress con HTTPS, autoescalado (HPA) y middleware.

| Cliente | Namespace | Stack | Base de datos | Plan |
|---|---|---|---|---|
| Alpha Tech | `portal-clientes` | Node.js 22 + Express (API REST) | PostgreSQL 16 · 2 GiB | Pro · HPA 2–5 · 600 sol/min |
| Beta POS | `pos-ventas` | PHP 8.3 + Apache (CRUD) | MySQL 8.4 · 1 GiB | Básico · HPA 1–3 · 120 sol/min |
| TecnoMorazán | `tecno-morazan` | Node.js (creado desde el portal) | PostgreSQL 16 · 1 GiB | Básico |

## Estructura

```
apps/alpha-node/   Cliente 1: Dockerfile, package.json, src/server.js, src/api.js
apps/beta-php/     Cliente 2: Dockerfile, src/*.php
chart/             Chart Helm paas-app-chart v0.2.0 + values-portal.yaml + values-pos.yaml
panel/app.py       NubePaaS: portal del cliente ( / ) y panel del dueño ( /admin )
monitoreo/         ingress-metrics.yaml (ServiceMonitor) · dashboard-paas-parcial2.json (Grafana)
pruebas/           carga.sh (velocidad y carga) · resultados-carga.txt
docs/              Informe técnico del parcial
```

## Arranque

```bash
minikube start
# acceso público (VM con adaptador puente)
sudo kubectl --kubeconfig $HOME/.kube/config port-forward --address 0.0.0.0 \
     -n ingress-nginx svc/ingress-nginx-controller 80:80 443:443
# NubePaaS en :5000  (las contraseñas se pasan por variables de entorno, no están en el repo)
PANEL_PASSWORD='…' ALPHA_PASSWORD='…' BETA_PASSWORD='…' python3 panel/app.py
# Grafana en :3000
kubectl port-forward --address 0.0.0.0 -n monitoring svc/prometheus-grafana 3000:80
```

Desplegar o actualizar un cliente a mano:

```bash
helm upgrade --install mi-app chart -f chart/values-portal.yaml -n portal-clientes
helm upgrade --install mi-app-pos chart -f chart/values-pos.yaml -n pos-ventas
```

## Informe y evidencias

- Informe técnico: [`docs/Informe_Parcial2_NubePaaS.pdf`](docs/Informe_Parcial2_NubePaaS.pdf)
- Dashboard de Grafana para importar: [`monitoreo/dashboard-paas-parcial2.json`](monitoreo/dashboard-paas-parcial2.json)
- Snapshots de Grafana (solo lectura, datos reales de las pruebas):
  [1](https://snapshots.raintank.io/dashboard/snapshot/huJoq5WdpJWrukH1xKgUB5lf5eE1TLmC) ·
  [2](https://snapshots.raintank.io/dashboard/snapshot/tFk1QqEkFbtxYd98AOvbIqy7frWWTc4w) ·
  [3](https://snapshots.raintank.io/dashboard/snapshot/MtH8jq55CDBMEklOycikfEcOUv4c3j5W) ·
  [4](https://snapshots.raintank.io/dashboard/snapshot/GKpBBpqGnl194pjiMCxc1CuzDVsR4V5R) ·
  [5](https://snapshots.raintank.io/dashboard/snapshot/LqhVeLd8USPjSU3ecRHZo4YM9k7OHrli)

## Seguridad

- Ninguna llave privada ni contraseña en Git (`.gitignore`: `*.key`, `*.pem`, `certs/`).
- Contraseñas de base de datos y JWT generadas al instalar y guardadas en Secrets de Kubernetes.
- NetworkPolicy por namespace: un cliente no puede conectarse a la base de datos de otro.
