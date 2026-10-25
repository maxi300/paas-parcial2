# Panel PaaS (tipo Render) · Parcial 2
# El cliente da 4 valores (dominio, repositorio Git, runtime/puerto y base de datos);
# la plataforma completa el resto con el chart de Helm (TLS, BD, Secrets, HPA, middleware).
import base64, os, re, shutil, subprocess, threading, time
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for
from jinja2 import DictLoader

HOME = Path.home()
CHART = Path(os.environ.get("PAAS_CHART", HOME / "paas-app-chart-v2"))
BUILDS = Path(os.environ.get("PAAS_BUILDS", HOME / "paas-builds"))
REPO = "https://github.com/maxi300/paas-parcial2.git"
PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "paas2026")

CLIENTES = {
    "alpha": {"nombre": "Alpha Tech", "release": "mi-app", "ns": "portal-clientes",
              "archivo": CHART / "values-portal.yaml", "path": "apps/alpha-node"},
    "beta": {"nombre": "Beta POS", "release": "mi-app-pos", "ns": "pos-ventas",
             "archivo": CHART / "values-pos.yaml", "path": "apps/beta-php"},
}
RUNTIMES = {
    "node": {"label": "Node.js", "port": 3000, "live": "/health", "ready": "/ready"},
    "php": {"label": "PHP + Apache", "port": 8080, "live": "/health.php", "ready": "/health.php?ready=1"},
    "nginx": {"label": "Sitio estático (NGINX)", "port": 80, "live": "/", "ready": "/"},
}
MOTORES = {"postgres": "PostgreSQL", "mysql": "MySQL"}

RE = {
    "host": re.compile(r"^[a-z0-9]([a-z0-9.-]{0,200}[a-z0-9])?$"),
    "repo": re.compile(r"^https://[A-Za-z0-9._/-]+$"),
    "ref": re.compile(r"^[A-Za-z0-9._/-]{1,100}$"),
    "cpu": re.compile(r"^\d+(\.\d+)?m?$"),
    "mem": re.compile(r"^\d+(Mi|Gi)$"),
    "env": re.compile(r"^[A-Z_][A-Z0-9_]*$"),
    "body": re.compile(r"^\d+[km]$"),
    "tag": re.compile(r"^[A-Za-z0-9._-]{1,60}$"),
}

app = Flask(__name__)
app.secret_key = os.urandom(16).hex()
LOCK = threading.Lock()   # un despliegue a la vez (el clúster es pequeño)
JOBS = {}                 # cliente -> {"estado", "log", "inicio", "tipo"}


# ------------------------------------------------------------------ utilidades
def run(cmd, timeout=60, cwd=None):
    """Ejecuta un comando sin shell y devuelve (código, salida)."""
    if shutil.which(cmd[0]) is None:
        return 127, f"'{cmd[0]}' no está instalado."
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "Tiempo de espera agotado."


def cliente(cid):
    if cid not in CLIENTES:
        abort(404)
    return CLIENTES[cid]


def cargar(cid):
    with open(cliente(cid)["archivo"], encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def guardar(cid, v):
    c = cliente(cid)
    hist = CHART / ".historial"
    hist.mkdir(exist_ok=True)
    shutil.copy(c["archivo"], hist / f"{cid}-{datetime.now():%Y%m%d-%H%M%S}.yaml")
    cab = f"# Valores del cliente {c['nombre']} · editado desde el Panel PaaS el {datetime.now():%Y-%m-%d %H:%M}\n"
    with open(c["archivo"], "w", encoding="utf-8") as fh:
        fh.write(cab + yaml.safe_dump(v, sort_keys=False, allow_unicode=True))


def helm_cmd(cid, extra=()):
    c = cliente(cid)
    return ["helm", "upgrade", c["release"], str(CHART), "-f", str(c["archivo"]), "-n", c["ns"], *extra]


def fuente(v, cid):
    s = v.get("source") or {}
    return {"repo": s.get("repo", REPO), "branch": s.get("branch", "main"),
            "path": s.get("path", CLIENTES[cid]["path"]), "commit": s.get("commit", "")}


@app.before_request
def autenticar():
    a = request.authorization
    if not a or a.username != PANEL_USER or a.password != PANEL_PASSWORD:
        return Response("Acceso restringido", 401, {"WWW-Authenticate": 'Basic realm="Panel PaaS"'})


# ------------------------------------------------------------------ trabajos en segundo plano
def lanzar(cid, tipo, pasos):
    if JOBS.get(cid, {}).get("estado") == "corriendo":
        return False
    job = {"estado": "corriendo", "log": [], "inicio": datetime.now(), "tipo": tipo}
    JOBS[cid] = job

    def worker():
        with LOCK:
            try:
                for titulo, fn in pasos:
                    job["log"].append(f"\n▶ {titulo}")
                    ok, salida = fn()
                    if salida:
                        job["log"].append(salida)
                    if not ok:
                        job["estado"] = "falló"
                        return
                job["estado"] = "listo"
            except Exception as exc:  # noqa: BLE001
                job["log"].append(f"Error: {exc}")
                job["estado"] = "falló"
            finally:
                job["fin"] = datetime.now()

    threading.Thread(target=worker, daemon=True).start()
    return True


def paso_cmd(cmd, timeout=600, cwd=None):
    def fn():
        code, out = run(cmd, timeout=timeout, cwd=cwd)
        return code == 0, "$ " + " ".join(cmd) + ("\n" + out if out else "")
    return fn


def pasos_aplicar(cid):
    c = cliente(cid)
    dep = f"deploy/{c['release']}-paas-app-chart"
    return [
        ("helm upgrade (aplicar values.yaml)", paso_cmd(helm_cmd(cid, ["--wait", "--timeout", "5m"]), 400)),
        ("Esperar a que los pods nuevos estén listos", paso_cmd(["kubectl", "rollout", "status", dep, "-n", c["ns"], "--timeout=300s"], 320)),
        ("Estado final", paso_cmd(["kubectl", "get", "pods", "-n", c["ns"]], 30)),
    ]


def pasos_git(cid):
    v = cargar(cid)
    src = fuente(v, cid)
    destino = BUILDS / f"{cid}-{int(time.time())}"
    estado = {}

    def clonar():
        BUILDS.mkdir(parents=True, exist_ok=True)
        code, out = run(["git", "clone", "--depth", "1", "-b", src["branch"], src["repo"], str(destino)], 180)
        if code != 0:
            return False, out
        _, sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=destino)
        _, msg = run(["git", "log", "-1", "--pretty=%s"], cwd=destino)
        estado["sha"] = sha.strip()
        estado["tag"] = f"git-{estado['sha']}"
        return True, f"Commit {estado['sha']}: {msg}"

    def construir():
        ctx = destino / src["path"]
        if not (ctx / "Dockerfile").exists():
            return False, f"No hay Dockerfile en {src['path']}"
        _, nodos = run(["kubectl", "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"], 30)
        salida = []
        imagen = f"{v['image']['repository']}:{estado['tag']}"
        for nodo in nodos.split():
            code, out = run(["minikube", "image", "build", "-t", imagen, "-n", nodo, str(ctx)], 900)
            salida.append(f"$ minikube image build -t {imagen} -n {nodo} {src['path']}\n" + out[-1500:])
            if code != 0:
                return False, "\n".join(salida)
        return True, "\n".join(salida)

    def actualizar_values():
        nuevo = cargar(cid)
        nuevo["image"]["tag"] = estado["tag"]
        nuevo["source"] = {**src, "commit": estado["sha"], "deployed": f"{datetime.now():%Y-%m-%d %H:%M}"}
        guardar(cid, nuevo)
        return True, f"image.tag = {estado['tag']}"

    def limpiar():
        shutil.rmtree(destino, ignore_errors=True)
        return True, ""

    return [("git clone " + src["repo"] + f" (rama {src['branch']})", clonar),
            ("Construir la imagen con el Dockerfile del repositorio (en cada nodo)", construir),
            ("Actualizar values.yaml con la nueva imagen", actualizar_values),
            *pasos_aplicar(cid),
            ("Limpiar carpeta temporal", limpiar)]


# ------------------------------------------------------------------ formulario -> values
def aplicar_formulario(f, v):
    err = []

    def texto(campo, regex, etiqueta):
        val = f.get(campo, "").strip()
        if not regex.match(val):
            err.append(f"Valor no válido en «{etiqueta}»: {val or '(vacío)'}")
        return val

    def numero(campo, lo, hi, etiqueta):
        try:
            n = int(f.get(campo, ""))
            if lo <= n <= hi:
                return n
        except ValueError:
            pass
        err.append(f"«{etiqueta}» debe ser un número entre {lo} y {hi}.")
        return lo

    # Los 4 valores del cliente
    v["domain"] = texto("domain", RE["host"], "Dominio")
    v["source"] = {**(v.get("source") or {}),
                   "repo": texto("repo", RE["repo"], "Repositorio Git"),
                   "branch": texto("branch", RE["ref"], "Rama"),
                   "path": texto("path", RE["ref"], "Carpeta de la app")}
    rt = f.get("runtime")
    if rt not in RUNTIMES:
        err.append("Runtime no válido.")
    elif rt != v.get("runtime"):
        r = RUNTIMES[rt]
        v["runtime"] = rt
        v["livenessProbe"] = {"httpGet": {"path": r["live"], "port": "http"}, "initialDelaySeconds": 10}
        v["readinessProbe"] = {"httpGet": {"path": r["ready"], "port": "http"}, "initialDelaySeconds": 5}
    v["containerPort"] = numero("port", 1, 65535, "Puerto")

    # Opciones avanzadas
    v["image"]["tag"] = texto("tag", RE["tag"], "Etiqueta de imagen")
    env = {}
    for linea in f.get("env", "").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        k, sep, val = linea.partition("=")
        if not sep or not RE["env"].match(k.strip()):
            err.append(f"Variable no válida: {linea} (formato CLAVE=valor)")
            continue
        env[k.strip()] = val.strip()
    v["env"] = env
    a = v.setdefault("autoscaling", {})
    a["enabled"] = f.get("hpa") == "on"
    a["minReplicas"] = numero("hpa_min", 1, 10, "Réplicas mínimas")
    a["maxReplicas"] = numero("hpa_max", 1, 10, "Réplicas máximas")
    a["targetCPU"] = numero("hpa_cpu", 10, 95, "CPU objetivo")
    if a["minReplicas"] > a["maxReplicas"]:
        err.append("Las réplicas mínimas no pueden ser mayores que las máximas.")
    v["resources"] = {"requests": {"cpu": texto("req_cpu", RE["cpu"], "CPU solicitada"),
                                   "memory": texto("req_mem", RE["mem"], "Memoria solicitada")},
                      "limits": {"cpu": texto("lim_cpu", RE["cpu"], "CPU límite"),
                                 "memory": texto("lim_mem", RE["mem"], "Memoria límite")}}
    m = v.setdefault("middleware", {})
    m["sslRedirect"] = f.get("ssl_redirect") == "on"
    m["rateLimitRPM"] = numero("rpm", 0, 100000, "Solicitudes por minuto")
    m["stickySessions"] = f.get("sticky") == "on"
    m["cors"] = f.get("cors") == "on"
    m["proxyBodySize"] = texto("body", RE["body"], "Tamaño máximo de petición")
    v.setdefault("tls", {})["enabled"] = f.get("tls") == "on"
    v.setdefault("dbAdmin", {})["enabled"] = f.get("dbadmin") == "on"
    return err


# ------------------------------------------------------------------ rutas
@app.route("/")
def inicio():
    tarjetas = []
    for cid, c in CLIENTES.items():
        v = cargar(cid)
        _, pods = run(["kubectl", "get", "pods", "-n", c["ns"], "--no-headers"], 20)
        lineas = [l.split() for l in pods.splitlines() if l.strip() and not l.startswith("No resources")]
        listos = sum(1 for p in lineas if len(p) > 2 and p[2] == "Running" and p[1].split("/")[0] == p[1].split("/")[-1])
        _, rev = run(["helm", "status", c["release"], "-n", c["ns"], "-o", "json"], 20)
        try:
            import json
            info = json.loads(rev)
            helm = f"revisión {info['version']} · {info['info']['status']}"
        except Exception:  # noqa: BLE001
            helm = "sin información"
        tarjetas.append({"id": cid, "c": c, "v": v, "src": fuente(v, cid), "pods": len(lineas),
                         "listos": listos, "helm": helm, "job": JOBS.get(cid)})
    return render_template("inicio.html", tarjetas=tarjetas, runtimes=RUNTIMES, motores=MOTORES)


@app.route("/c/<cid>", methods=["GET", "POST"])
def editar(cid):
    c = cliente(cid)
    v = cargar(cid)
    if request.method == "POST":
        accion = request.form.get("accion", "guardar")
        if accion == "yaml":
            try:
                v = yaml.safe_load(request.form.get("raw", "")) or {}
                if not isinstance(v, dict):
                    raise ValueError("debe ser un mapa YAML")
            except Exception as exc:  # noqa: BLE001
                flash(f"YAML no válido: {exc}", "error")
                return redirect(url_for("editar", cid=cid))
        else:
            errores = aplicar_formulario(request.form, v)
            if errores:
                for e in errores:
                    flash(e, "error")
                return redirect(url_for("editar", cid=cid))
        guardar(cid, v)
        flash(f"Guardado {c['archivo'].name}.", "ok")
        if accion == "preview":
            code, out = run(["helm", "template", c["release"], str(CHART), "-f", str(c["archivo"]), "-n", c["ns"]], 60)
            return render_template("salida.html", c=c, cid=cid, titulo="Vista previa: manifiestos que generará Helm",
                                   cmd=f"helm template {c['release']} -f {c['archivo'].name}", code=code, out=out)
        if accion in ("aplicar", "yaml"):
            if not lanzar(cid, "Aplicar cambios", pasos_aplicar(cid)):
                flash("Ya hay un despliegue en curso para este cliente.", "warn")
            return redirect(url_for("trabajo", cid=cid))
        return redirect(url_for("editar", cid=cid))
    env_txt = "\n".join(f"{k}={val}" for k, val in (v.get("env") or {}).items())
    raw = c["archivo"].read_text(encoding="utf-8")
    return render_template("editar.html", cid=cid, c=c, v=v, src=fuente(v, cid), env_txt=env_txt, raw=raw,
                           runtimes=RUNTIMES, motores=MOTORES)


@app.route("/c/<cid>/desplegar", methods=["POST"])
def desplegar(cid):
    cliente(cid)
    if not lanzar(cid, "Desplegar desde Git", pasos_git(cid)):
        flash("Ya hay un despliegue en curso para este cliente.", "warn")
    return redirect(url_for("trabajo", cid=cid))


@app.route("/c/<cid>/trabajo")
def trabajo(cid):
    c = cliente(cid)
    return render_template("trabajo.html", cid=cid, c=c, job=JOBS.get(cid))


@app.route("/c/<cid>/estado")
def estado(cid):
    c = cliente(cid)
    cmd = ["kubectl", "get", "pods,svc,ingress,hpa,pvc,resourcequota", "-n", c["ns"], "-o", "wide"]
    code, out = run(cmd, 30)
    _, hist = run(["helm", "history", c["release"], "-n", c["ns"], "--max", "6"], 30)
    return render_template("salida.html", c=c, cid=cid, titulo=f"Estado del namespace {c['ns']}",
                           cmd=" ".join(cmd), code=code, out=out + "\n\n$ helm history " + c["release"] + "\n" + hist)


@app.route("/c/<cid>/bd")
def credenciales(cid):
    c = cliente(cid)
    v = cargar(cid)
    secreto = f"{c['release']}-paas-app-chart-db-credentials"
    code, out = run(["kubectl", "get", "secret", secreto, "-n", c["ns"], "-o", "json"], 20)
    datos = {}
    if code == 0:
        import json
        for k, val in json.loads(out).get("data", {}).items():
            if k in ("DB_USER", "DB_PASSWORD"):
                datos[k] = base64.b64decode(val).decode()
    return render_template("bd.html", c=c, cid=cid, v=v, datos=datos, secreto=secreto, motores=MOTORES,
                           servidor=f"{c['release']}-paas-app-chart-db")


# ------------------------------------------------------------------ plantillas
BASE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{% block t %}Panel PaaS{% endblock %}</title>
<style>
:root{--bg:#0b1020;--card:#141b2d;--line:#26314a;--text:#e5e9f2;--muted:#8d99b3;--acc:#7c5cff;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{padding:14px 28px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center}
header a{color:var(--text);text-decoration:none;font-weight:800;font-size:18px}main{max-width:1150px;margin:0 auto;padding:26px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:15px;margin:0 0 12px;color:#b9a8ff;text-transform:uppercase;letter-spacing:.04em}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input,select,textarea{width:100%;padding:9px 11px;border-radius:7px;border:1px solid var(--line);background:#0a0f1c;color:var(--text);font:inherit}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:13px}.chk{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:14px}.chk input{width:auto}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.btn{display:inline-block;padding:9px 16px;border-radius:7px;border:0;background:var(--acc);color:#fff;font-weight:700;cursor:pointer;text-decoration:none;font-size:14px}
.btn.sec{background:transparent;border:1px solid var(--line);color:var(--text)}.btn.ok{background:#16a34a}
.tag{display:inline-block;padding:3px 9px;border-radius:99px;font-size:12px;background:#0a0f1c;border:1px solid var(--line);margin:0 4px 4px 0}
.flash{padding:10px 14px;border-radius:7px;margin-bottom:10px}.flash.ok{background:#14532d}.flash.error{background:#7f1d1d}.flash.warn{background:#78350f}
pre{background:#05080f;padding:14px;border-radius:9px;overflow:auto;font-size:13px;white-space:pre-wrap;border:1px solid var(--line)}
.muted{color:var(--muted);font-size:13px}.acts{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
a{color:#a78bfa}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.cuatro{border:1px solid #5b47c9;background:#17143a}table{border-collapse:collapse;width:100%}td{padding:6px 4px;border-bottom:1px solid var(--line);font-size:14px}
</style></head><body>
<header><a href="{{ url_for('inicio') }}">⎈ Panel PaaS</a><span class="muted">Despliegue tipo Render sobre Kubernetes · Helm · Ingress TLS</span></header>
<main>{% with m = get_flashed_messages(with_categories=true) %}{% for cat, msg in m %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}{% endwith %}
{% block c %}{% endblock %}</main></body></html>"""

INICIO = """{% extends "base.html" %}{% block c %}
<h1>Clientes de la plataforma</h1><p class="muted">Cada cliente vive en su propio namespace, con su base de datos, su certificado y sus límites.</p>
<div class="grid">{% for t in tarjetas %}{% set v = t.v %}
<div class="card" style="border-top:4px solid {{ v.env.BRAND_COLOR | default('#7c5cff') }}">
 <h1 style="font-size:20px">{{ t.c.nombre }}</h1>
 <p><span class="tag">{{ runtimes[v.runtime].label if v.runtime in runtimes else v.runtime }}</span><span class="tag">{{ motores[v.database.engine] }} {{ v.database.version }}</span><span class="tag">ns {{ t.c.ns }}</span></p>
 <table>
  <tr><td class="muted">Estado</td><td><span class="dot" style="background:{{ '#22c55e' if t.pods and t.listos == t.pods else '#f59e0b' }}"></span>{{ t.listos }}/{{ t.pods }} pods listos · {{ t.helm }}</td></tr>
  <tr><td class="muted">Sitio</td><td><a href="http://{{ v.domain }}" target="_blank">http://{{ v.domain }}</a><br><a href="https://{{ v.domain }}" target="_blank">https://{{ v.domain }}</a></td></tr>
  <tr><td class="muted">Base de datos</td><td><a href="https://db.{{ v.domain }}" target="_blank">https://db.{{ v.domain }}</a> (Adminer)</td></tr>
  <tr><td class="muted">Código</td><td>{{ t.src.repo | replace('https://github.com/', '') }} · {{ t.src.branch }} · {{ t.src.path }}</td></tr>
  <tr><td class="muted">Imagen</td><td><code>{{ v.image.repository }}:{{ v.image.tag }}</code></td></tr>
  <tr><td class="muted">Escala</td><td>HPA {{ v.autoscaling.minReplicas }}–{{ v.autoscaling.maxReplicas }} réplicas · {{ v.resources.limits.cpu }} CPU / {{ v.resources.limits.memory }}</td></tr>
  <tr><td class="muted">Middleware</td><td>{{ v.middleware.rateLimitRPM }} sol/min · {{ 'HTTPS forzado' if v.middleware.sslRedirect else 'HTTP y HTTPS' }}{{ ' · CORS' if v.middleware.cors }}{{ ' · sesión fija' if v.middleware.stickySessions }}</td></tr>
 </table>
 {% if t.job %}<p class="muted">Último trabajo: <a href="{{ url_for('trabajo', cid=t.id) }}">{{ t.job.tipo }} · {{ t.job.estado }}</a></p>{% endif %}
 <div class="acts"><a class="btn" href="{{ url_for('editar', cid=t.id) }}">Configurar</a>
  <form method="post" action="{{ url_for('desplegar', cid=t.id) }}"><button class="btn ok">Desplegar desde Git</button></form>
  <a class="btn sec" href="{{ url_for('estado', cid=t.id) }}">Estado</a><a class="btn sec" href="{{ url_for('credenciales', cid=t.id) }}">Credenciales BD</a></div>
</div>{% endfor %}</div>{% endblock %}"""

EDITAR = """{% extends "base.html" %}{% block c %}
<h1>{{ c.nombre }} <span class="muted">· release {{ c.release }} · namespace {{ c.ns }} · {{ c.archivo.name }}</span></h1>
<form method="post"><div class="grid" style="margin-top:16px">
<div class="card cuatro"><h2>Lo que da el cliente (4 valores)</h2>
 <label>1 · Dominio</label><input name="domain" value="{{ v.domain }}">
 <label>2 · Repositorio Git</label><input name="repo" value="{{ src.repo }}">
 <div class="row"><div><label>Rama</label><input name="branch" value="{{ src.branch }}"></div><div><label>Carpeta con el Dockerfile</label><input name="path" value="{{ src.path }}"></div></div>
 <div class="row"><div><label>3 · Runtime</label><select name="runtime">{% for k, r in runtimes.items() %}<option value="{{ k }}" {{ 'selected' if v.runtime == k }}>{{ r.label }}</option>{% endfor %}</select></div>
 <div><label>Puerto del contenedor</label><input name="port" type="number" value="{{ v.containerPort }}"></div></div>
 <label>4 · Base de datos administrada</label><input value="{{ motores[v.database.engine] }} {{ v.database.version }} · {{ v.database.name }} · {{ v.database.storage }}" disabled>
 <p class="muted">El motor se elige al crear el cliente (cambiarlo crearía una base vacía).</p>
</div>
<div class="card"><h2>Variables de entorno</h2><textarea name="env" rows="6">{{ env_txt }}</textarea>
 <p class="muted">Una por línea, CLAVE=valor. Las credenciales de la BD no van aquí: vienen del Secret.</p>
 <label>Etiqueta de la imagen</label><input name="tag" value="{{ v.image.tag }}"><p class="muted">{{ v.image.repository }}:<b>{{ v.image.tag }}</b> · la cambia “Desplegar desde Git”.</p></div>
<div class="card"><h2>Escalado y recursos</h2>
 <label class="chk"><input type="checkbox" name="hpa" {{ 'checked' if v.autoscaling.enabled }}> Autoescalado (HPA)</label>
 <div class="row3"><div><label>Mín.</label><input name="hpa_min" type="number" value="{{ v.autoscaling.minReplicas }}"></div><div><label>Máx.</label><input name="hpa_max" type="number" value="{{ v.autoscaling.maxReplicas }}"></div><div><label>CPU %</label><input name="hpa_cpu" type="number" value="{{ v.autoscaling.targetCPU }}"></div></div>
 <div class="row"><div><label>CPU solicitada</label><input name="req_cpu" value="{{ v.resources.requests.cpu }}"></div><div><label>Memoria solicitada</label><input name="req_mem" value="{{ v.resources.requests.memory }}"></div></div>
 <div class="row"><div><label>CPU límite</label><input name="lim_cpu" value="{{ v.resources.limits.cpu }}"></div><div><label>Memoria límite</label><input name="lim_mem" value="{{ v.resources.limits.memory }}"></div></div></div>
<div class="card"><h2>Ingress · SSL · Middleware</h2>
 <label class="chk"><input type="checkbox" name="tls" {{ 'checked' if v.tls.enabled }}> HTTPS con certificado {{ v.tls.secretName }}</label>
 <label class="chk"><input type="checkbox" name="ssl_redirect" {{ 'checked' if v.middleware.sslRedirect }}> Forzar HTTPS (redirigir HTTP → HTTPS)</label>
 <label>Límite de solicitudes por minuto por IP (0 = sin límite)</label><input name="rpm" type="number" value="{{ v.middleware.rateLimitRPM }}">
 <label class="chk"><input type="checkbox" name="sticky" {{ 'checked' if v.middleware.stickySessions }}> Sesión fija (cookie de afinidad)</label>
 <label class="chk"><input type="checkbox" name="cors" {{ 'checked' if v.middleware.cors }}> Permitir CORS</label>
 <label>Tamaño máximo de petición</label><input name="body" value="{{ v.middleware.proxyBodySize }}">
 <label class="chk"><input type="checkbox" name="dbadmin" {{ 'checked' if v.dbAdmin.enabled }}> Publicar Adminer en db.{{ v.domain }}</label></div>
</div>
<div class="acts"><button class="btn ok" name="accion" value="aplicar">Guardar y aplicar</button>
<button class="btn sec" name="accion" value="preview">Vista previa (helm template)</button><button class="btn sec" name="accion" value="guardar">Solo guardar</button>
<a class="btn sec" href="{{ url_for('inicio') }}">Volver</a></div></form>
<div class="card" style="margin-top:22px"><h2>values.yaml completo</h2><form method="post"><textarea name="raw" rows="18">{{ raw }}</textarea>
<div class="acts"><button class="btn" name="accion" value="yaml">Guardar YAML y aplicar</button></div></form></div>{% endblock %}"""

TRABAJO = """{% extends "base.html" %}{% block t %}{{ c.nombre }} · despliegue{% endblock %}{% block c %}
{% if job and job.estado == 'corriendo' %}<meta http-equiv="refresh" content="3">{% endif %}
<h1>{{ c.nombre }} · {{ job.tipo if job else 'sin trabajos' }}</h1>
{% if job %}<p><span class="dot" style="background:{{ {'corriendo':'#f59e0b','listo':'#22c55e'}.get(job.estado, '#ef4444') }}"></span><b>{{ job.estado }}</b>
<span class="muted"> · inició {{ job.inicio.strftime('%H:%M:%S') }}{% if job.fin %} · duró {{ (job.fin - job.inicio).seconds }} s{% endif %}{% if job.estado == 'corriendo' %} · la página se actualiza sola{% endif %}</span></p>
<pre>{{ job.log | join('\n') }}</pre>{% endif %}
<div class="acts"><a class="btn" href="{{ url_for('inicio') }}">Volver al panel</a></div>{% endblock %}"""

SALIDA = """{% extends "base.html" %}{% block c %}<h1>{{ titulo }}</h1><p class="muted">$ {{ cmd }} · código {{ code }}</p><pre>{{ out }}</pre>
<div class="acts"><a class="btn" href="{{ url_for('inicio') }}">Volver al panel</a><a class="btn sec" href="{{ url_for('editar', cid=cid) }}">Configurar</a></div>{% endblock %}"""

BD = """{% extends "base.html" %}{% block c %}<h1>{{ c.nombre }} · base de datos administrada</h1>
<p class="muted">Solo el dueño del cliente ve estos datos. Se leen del Secret <code>{{ secreto }}</code> (nunca están en Git).</p>
<div class="card" style="max-width:620px"><table>
<tr><td class="muted">Adminer</td><td><a href="https://db.{{ v.domain }}" target="_blank">https://db.{{ v.domain }}</a></td></tr>
<tr><td class="muted">Motor</td><td>{{ motores[v.database.engine] }} {{ v.database.version }}</td></tr>
<tr><td class="muted">Servidor</td><td><code>{{ servidor }}</code></td></tr>
<tr><td class="muted">Usuario</td><td><code>{{ datos.DB_USER | default('—') }}</code></td></tr>
<tr><td class="muted">Contraseña</td><td><code>{{ datos.DB_PASSWORD | default('—') }}</code></td></tr>
<tr><td class="muted">Base de datos</td><td><code>{{ v.database.name }}</code></td></tr></table></div>
<div class="acts"><a class="btn" href="{{ url_for('inicio') }}">Volver al panel</a></div>{% endblock %}"""

app.jinja_loader = DictLoader({"base.html": BASE, "inicio.html": INICIO, "editar.html": EDITAR,
                               "trabajo.html": TRABAJO, "salida.html": SALIDA, "bd.html": BD})

if __name__ == "__main__":
    print(f"Panel PaaS en http://0.0.0.0:5000  (usuario {PANEL_USER})")
    app.run(host="0.0.0.0", port=int(os.environ.get("PANEL_PORT", "5000")), threaded=True)
