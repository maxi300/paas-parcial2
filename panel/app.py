# PaaS tipo Render · Parcial 2 (Informática en la Nube)
#   http://IP:5000/        -> Portal del CLIENTE: crear cuenta, conectar Git, desplegar, ver su servicio
#   http://IP:5000/admin   -> Panel del DUEÑO de la plataforma: todos los clientes, planes y configuración completa
# El cliente da 4 valores (nombre/dominio, repositorio Git, runtime/puerto y base de datos);
# la plataforma completa el resto con el chart de Helm (namespace, cuota, TLS, BD, Secrets, HPA, middleware).
import base64, hashlib, json, os, re, shutil, subprocess, threading, time
from datetime import datetime
from functools import wraps
from pathlib import Path

import yaml
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

HOME = Path.home()
CHART = Path(os.environ.get("PAAS_CHART", HOME / "paas-app-chart-v2"))
BUILDS = Path(os.environ.get("PAAS_BUILDS", HOME / "paas-builds"))
CERTS = Path(os.environ.get("PAAS_CERTS", HOME / "certs"))   # CA propia (OpenSSL) para firmar certificados
REGISTRO = CHART / "clientes.json"                           # clientes creados desde el portal
DEPLOYS = CHART / ".deploys"                                 # historial de despliegues por cliente
REPO = "https://github.com/maxi300/paas-parcial2.git"
ADMIN_PASSWORD = os.environ.get("PANEL_PASSWORD", "paas2026")
CLAVES_FIJAS = {"alpha": os.environ.get("ALPHA_PASSWORD", "alpha2026"),
                "beta": os.environ.get("BETA_PASSWORD", "beta2026")}

CLIENTES = {
    "alpha": {"nombre": "Alpha Tech", "release": "mi-app", "ns": "portal-clientes", "plan": "pro",
              "archivo": CHART / "values-portal.yaml", "path": "apps/alpha-node"},
    "beta": {"nombre": "Beta POS", "release": "mi-app-pos", "ns": "pos-ventas", "plan": "basico",
             "archivo": CHART / "values-pos.yaml", "path": "apps/beta-php"},
}
# Planes (como los "instance types" de Render): escala, disco, límite de solicitudes y cuota del namespace
PLANES = {
    "basico": {"label": "Básico", "hpa": (1, 3), "storage": "1Gi", "rpm": 120,
               "quota": {"pods": "8", "requests.cpu": "2", "requests.memory": "2Gi", "limits.cpu": "4",
                         "limits.memory": "4Gi", "requests.storage": "5Gi"}},
    "pro": {"label": "Pro", "hpa": (2, 5), "storage": "2Gi", "rpm": 600,
            "quota": {"pods": "10", "requests.cpu": "2", "requests.memory": "4Gi", "limits.cpu": "4",
                      "limits.memory": "8Gi", "requests.storage": "10Gi"}},
}
RESERVADOS = {"admin", "app", "login", "logout", "registro", "default", "monitoring", "ingress-nginx",
              "portal-clientes", "pos-ventas"}
RUNTIMES = {
    "node": {"label": "Node.js", "port": 3000, "live": "/health", "ready": "/ready"},
    "php": {"label": "PHP + Apache", "port": 8080, "live": "/health.php", "ready": "/health.php?ready=1"},
    "nginx": {"label": "Sitio estático (NGINX)", "port": 80, "live": "/", "ready": "/"},
}
MOTORES = {"postgres": "PostgreSQL", "mysql": "MySQL"}
PUERTOS_BD = {"postgres": 5432, "mysql": 3306}

RE = {
    "host": re.compile(r"^[a-z0-9]([a-z0-9.-]{0,200}[a-z0-9])?$"),
    "repo": re.compile(r"^https://[A-Za-z0-9._/-]+$"),
    "ref": re.compile(r"^[A-Za-z0-9._/-]{1,100}$"),
    "cpu": re.compile(r"^\d+(\.\d+)?m?$"),
    "mem": re.compile(r"^\d+(Mi|Gi)$"),
    "env": re.compile(r"^[A-Z_][A-Z0-9_]*$"),
    "body": re.compile(r"^\d+[km]$"),
    "tag": re.compile(r"^[A-Za-z0-9._-]{1,60}$"),
    "slug": re.compile(r"^[a-z][a-z0-9-]{2,18}[a-z0-9]$"),
    "color": re.compile(r"^#[0-9a-fA-F]{6}$"),
}

app = Flask(__name__)
app.secret_key = os.environ.get("PANEL_SECRET") or hashlib.sha256(("paas" + ADMIN_PASSWORD).encode()).hexdigest()
LOCK = threading.Lock()   # un despliegue a la vez (el clúster es pequeño)
JOBS = {}                 # cliente -> trabajo actual (con su log en vivo)


# ================================================================== utilidades
def run(cmd, timeout=60, cwd=None, entrada=None):
    """Ejecuta un comando sin shell y devuelve (código, salida)."""
    if shutil.which(cmd[0]) is None:
        return 127, f"'{cmd[0]}' no está instalado."
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, input=entrada)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "Tiempo de espera agotado."


def leer_json(ruta, defecto):
    try:
        return json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return defecto


def leer_registro():
    return leer_json(REGISTRO, {})


def escribir_registro(reg):
    REGISTRO.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def clientes():
    """Clientes fijos (Alpha y Beta) + los creados desde el portal."""
    todos = dict(CLIENTES)
    for cid, r in leer_registro().items():
        todos[cid] = {**r, "archivo": CHART / r["archivo"]}
    return todos


def cliente(cid):
    todos = clientes()
    if cid not in todos:
        abort(404)
    return todos[cid]


def cargar(cid):
    with open(cliente(cid)["archivo"], encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def guardar(cid, v):
    c = cliente(cid)
    hist = CHART / ".historial"
    hist.mkdir(exist_ok=True)
    if c["archivo"].exists():
        shutil.copy(c["archivo"], hist / f"{cid}-{datetime.now():%Y%m%d-%H%M%S}.yaml")
    cab = f"# Valores del cliente {c['nombre']} · editado desde el PaaS el {datetime.now():%Y-%m-%d %H:%M}\n"
    with open(c["archivo"], "w", encoding="utf-8") as fh:
        fh.write(cab + yaml.safe_dump(v, sort_keys=False, allow_unicode=True))


def helm_cmd(cid, extra=()):
    c = cliente(cid)
    return ["helm", "upgrade", "--install", c["release"], str(CHART), "-f", str(c["archivo"]), "-n", c["ns"], *extra]


def fuente(v, cid):
    s = v.get("source") or {}
    return {"repo": s.get("repo", REPO), "branch": s.get("branch", "main"),
            "path": s.get("path", cliente(cid)["path"]), "commit": s.get("commit", "")}


def clave_valida(cid, clave):
    if cid in CLAVES_FIJAS:
        return clave == CLAVES_FIJAS[cid]
    r = leer_registro().get(cid)
    return bool(r) and check_password_hash(r["clave"], clave)


def pods_app(cid):
    """Pods de la app (sin la BD ni Adminer): [(nombre, listos, estado, reinicios, edad)]."""
    c = cliente(cid)
    sel = f"app.kubernetes.io/instance={c['release']},app.kubernetes.io/name=paas-app-chart"
    _, out = run(["kubectl", "get", "pods", "-n", c["ns"], "-l", sel, "--no-headers"], 20)
    return [l.split() for l in out.splitlines() if l.strip() and not l.startswith("No resources")]


def uso_pods(cid):
    """CPU y memoria actuales de cada pod del namespace (metrics-server)."""
    code, out = run(["kubectl", "top", "pods", "-n", cliente(cid)["ns"], "--no-headers"], 20)
    return [l.split() for l in out.splitlines() if l.strip()] if code == 0 else []


def credenciales_bd(cid):
    c = cliente(cid)
    code, out = run(["kubectl", "get", "secret", f"{c['release']}-paas-app-chart-db-credentials",
                     "-n", c["ns"], "-o", "json"], 20)
    datos = {}
    if code == 0:
        for k, val in json.loads(out).get("data", {}).items():
            if k in ("DB_USER", "DB_PASSWORD"):
                datos[k] = base64.b64decode(val).decode()
    return datos


# ================================================================== despliegues (en segundo plano + historial)
def historial(cid):
    hist = leer_json(DEPLOYS / f"{cid}.json", [])
    vivo = JOBS.get(cid)
    if vivo:
        hist = [vivo if h["id"] == vivo["id"] else h for h in hist]
    return hist


def registrar(cid, job):
    DEPLOYS.mkdir(parents=True, exist_ok=True)
    ruta = DEPLOYS / f"{cid}.json"
    hist = [h for h in leer_json(ruta, []) if h["id"] != job["id"]]
    copia = {**job, "log": job["log"][-400:]}
    ruta.write_text(json.dumps([copia] + hist[:19], ensure_ascii=False, indent=1), encoding="utf-8")


def lanzar(cid, tipo, pasos, meta=None):
    if JOBS.get(cid, {}).get("estado") == "corriendo":
        return None
    meta = meta if meta is not None else {}
    job = {"id": str(int(time.time() * 1000)), "tipo": tipo, "estado": "corriendo", "log": [],
           "inicio": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "fin": "", "dur": 0, "meta": meta, "t0": time.time()}
    JOBS[cid] = job
    registrar(cid, job)

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
                job["fin"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                job["dur"] = int(time.time() - job["t0"])
                registrar(cid, job)

    threading.Thread(target=worker, daemon=True).start()
    return job


def paso_cmd(cmd, timeout=600, cwd=None):
    def fn():
        code, out = run(cmd, timeout=timeout, cwd=cwd)
        return code == 0, "$ " + " ".join(cmd) + ("\n" + out if out else "")
    return fn


def pasos_aplicar(cid):
    c = cliente(cid)
    dep = f"deploy/{c['release']}-paas-app-chart"
    return [
        ("helm upgrade --install (aplicar values.yaml)", paso_cmd(helm_cmd(cid, ["--wait", "--timeout", "8m"]), 560)),
        ("Esperar a que los pods nuevos estén listos",
         paso_cmd(["kubectl", "rollout", "status", dep, "-n", c["ns"], "--timeout=300s"], 320)),
        ("Estado final", paso_cmd(["kubectl", "get", "pods", "-n", c["ns"]], 30)),
    ]


def pasos_git(cid, meta):
    v = cargar(cid)
    src = fuente(v, cid)
    destino = BUILDS / f"{cid}-{int(time.time())}"

    def clonar():
        BUILDS.mkdir(parents=True, exist_ok=True)
        code, out = run(["git", "clone", "--depth", "1", "-b", src["branch"], src["repo"], str(destino)], 180)
        if code != 0:
            return False, out
        _, sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=destino)
        _, msg = run(["git", "log", "-1", "--pretty=%s"], cwd=destino)
        meta.update(commit=sha.strip(), mensaje=msg.strip(), tag=f"git-{sha.strip()}")
        return True, f"Commit {meta['commit']}: {meta['mensaje']}"

    def construir():
        ctx = destino / src["path"]
        if not (ctx / "Dockerfile").exists():
            return False, f"No hay Dockerfile en {src['path']}"
        _, nodos = run(["kubectl", "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"], 30)
        if not nodos.split():
            return False, "No pude listar los nodos del clúster."
        salida = []
        imagen = f"{v['image']['repository']}:{meta['tag']}"
        for nodo in nodos.split():
            code, out = run(["minikube", "image", "build", "-t", imagen, "-n", nodo, str(ctx)], 900)
            salida.append(f"$ minikube image build -t {imagen} -n {nodo} {src['path']}\n" + out[-1500:])
            if code != 0:
                return False, "\n".join(salida)
        return True, "\n".join(salida)

    def actualizar_values():
        nuevo = cargar(cid)
        nuevo["image"]["tag"] = meta["tag"]
        nuevo["source"] = {**src, "commit": meta["commit"], "deployed": f"{datetime.now():%Y-%m-%d %H:%M}"}
        guardar(cid, nuevo)
        return True, f"image.tag = {meta['tag']}"

    def limpiar():
        shutil.rmtree(destino, ignore_errors=True)
        return True, ""

    return [(f"git clone {src['repo']} (rama {src['branch']})", clonar),
            ("Construir la imagen con el Dockerfile del repositorio (en cada nodo)", construir),
            ("Actualizar values.yaml con la nueva imagen", actualizar_values),
            *pasos_aplicar(cid),
            ("Limpiar carpeta temporal", limpiar)]


# ================================================================== alta de un servicio nuevo
def gobierno_yaml(cid, plan):
    """Cuota del plan, límites por defecto y aislamiento de red del namespace del cliente."""
    q = "\n".join(f'    {k}: "{v}"' for k, v in PLANES[plan]["quota"].items())
    return f"""apiVersion: v1
kind: ResourceQuota
metadata: {{name: {cid}-quota}}
spec:
  hard:
{q}
---
apiVersion: v1
kind: LimitRange
metadata: {{name: paas-defaults}}
spec:
  limits:
  - type: Container
    default: {{cpu: 500m, memory: 256Mi}}
    defaultRequest: {{cpu: 100m, memory: 128Mi}}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {{name: isolate-namespace}}
spec:
  podSelector: {{}}
  policyTypes: [Ingress]
  ingress:
  - from:
    - podSelector: {{}}
    - namespaceSelector: {{matchLabels: {{kubernetes.io/metadata.name: ingress-nginx}}}}
    - namespaceSelector: {{matchLabels: {{kubernetes.io/metadata.name: monitoring}}}}
"""


def values_nuevos(cid, nombre, repo, branch, path, runtime, port, motor, plan, color):
    r, p = RUNTIMES[runtime], PLANES[plan]
    base = cid.replace("-", "_")
    db = {"enabled": True, "engine": motor, "version": "16-alpine" if motor == "postgres" else "8.4",
          "name": f"{base}_db", "user": f"{base}_user", "storage": p["storage"]}
    if motor == "mysql":
        db["resources"] = {"requests": {"cpu": "100m", "memory": "384Mi"}, "limits": {"cpu": "500m", "memory": "768Mi"}}
    return {
        "runtime": runtime, "image": {"repository": f"{cid}-app", "tag": "pendiente"}, "containerPort": port,
        "domain": f"{cid}.paas.local",
        "env": {"APP_NAME": nombre, "TENANT": cid, "APP_ENV": "produccion", "BRAND_COLOR": color},
        "autoscaling": {"enabled": True, "minReplicas": p["hpa"][0], "maxReplicas": p["hpa"][1], "targetCPU": 70},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
        "livenessProbe": {"httpGet": {"path": r["live"], "port": "http"}, "initialDelaySeconds": 10},
        "readinessProbe": {"httpGet": {"path": r["ready"], "port": "http"}, "initialDelaySeconds": 5},
        "database": db, "dbAdmin": {"enabled": True}, "tls": {"enabled": True, "secretName": f"{cid}-tls"},
        "middleware": {"sslRedirect": False, "rateLimitRPM": p["rpm"], "stickySessions": False,
                       "proxyBodySize": "2m", "cors": False},
        "source": {"repo": repo, "branch": branch, "path": path},
    }


def pasos_crear(cid, plan, meta):
    dominio = f"{cid}.paas.local"

    def namespace():
        code, out = run(["kubectl", "create", "namespace", cid], 30)
        if code != 0:
            return False, out
        _, out2 = run(["kubectl", "label", "namespace", cid, f"paas/tenant={cid}", f"paas/plan={plan}"], 30)
        return True, out + "\n" + out2

    def gobierno():
        code, out = run(["kubectl", "apply", "-n", cid, "-f", "-"], 30, entrada=gobierno_yaml(cid, plan))
        return code == 0, out

    def certificado():
        ca, cakey = CERTS / "ca.crt", CERTS / "ca.key"
        if not (ca.exists() and cakey.exists()):
            return False, f"No encuentro la CA en {CERTS} (ca.crt y ca.key)."
        key, csr, crt, ext = (CERTS / f"{cid}.{e}" for e in ("key", "csr", "crt", "ext"))
        ext.write_text(f"subjectAltName=DNS:{dominio},DNS:db.{dominio}\nbasicConstraints=CA:FALSE\n"
                       "keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        salida = []
        for cmd in (["openssl", "genrsa", "-out", str(key), "2048"],
                    ["openssl", "req", "-new", "-key", str(key), "-subj", f"/C=SV/O=PaaS Local/CN={dominio}", "-out", str(csr)],
                    ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(cakey), "-CAcreateserial",
                     "-out", str(crt), "-days", "365", "-sha256", "-extfile", str(ext)],
                    ["kubectl", "create", "secret", "tls", f"{cid}-tls", f"--cert={crt}", f"--key={key}", "-n", cid]):
            code, out = run(cmd, 60)
            salida.append("$ " + " ".join(cmd[:4]) + " …\n" + out)
            if code != 0:
                return False, "\n".join(salida)
        return True, "\n".join(salida)

    return [(f"Crear namespace {cid} (aislado)", namespace),
            (f"Cuota del plan {PLANES[plan]['label']}, LimitRange y NetworkPolicy", gobierno),
            (f"Certificado SSL para {dominio} y db.{dominio} firmado por la CA propia (OpenSSL)", certificado),
            *pasos_git(cid, meta)]


def crear_servicio(f):
    """Valida el formulario de alta y lanza la creación. Devuelve (cid, job) o (None, errores)."""
    err = []
    cid = f.get("cid", "").strip().lower()
    if not RE["slug"].match(cid) or cid in RESERVADOS or cid in clientes() or cid.startswith("kube-"):
        err.append("Ese nombre no es válido o ya está en uso (4-20 letras minúsculas, números o guiones).")
    elif run(["kubectl", "get", "namespace", cid], 20)[0] == 0:
        err.append(f"Ya existe un namespace llamado {cid}.")
    nombre = f.get("nombre", "").strip()[:40] or cid
    clave = f.get("clave", "")
    if len(clave) < 6:
        err.append("La contraseña debe tener al menos 6 caracteres.")
    repo, branch, path = f.get("repo", "").strip(), f.get("branch", "main").strip(), f.get("path", "").strip()
    if not RE["repo"].match(repo) or not RE["ref"].match(branch) or not RE["ref"].match(path):
        err.append("Revisa el repositorio (https://…), la rama y la carpeta.")
    runtime, motor, plan = f.get("runtime"), f.get("motor"), f.get("plan")
    if runtime not in RUNTIMES or motor not in MOTORES or plan not in PLANES:
        err.append("Elige runtime, base de datos y plan.")
    color = f.get("color", "#7c5cff")
    color = color if RE["color"].match(color) else "#7c5cff"
    try:
        port = int(f.get("port") or RUNTIMES.get(runtime, {}).get("port", 80))
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        err.append("Puerto no válido.")
    if err:
        return None, err
    archivo = f"values-{cid}.yaml"
    reg = leer_registro()
    reg[cid] = {"nombre": nombre, "release": cid, "ns": cid, "plan": plan, "archivo": archivo, "path": path,
                "clave": generate_password_hash(clave), "creado": f"{datetime.now():%Y-%m-%d %H:%M}"}
    escribir_registro(reg)
    (CHART / archivo).write_text(f"# Valores del cliente {nombre} · creado desde el portal\n"
                                 + yaml.safe_dump(values_nuevos(cid, nombre, repo, branch, path, runtime, port,
                                                                motor, plan, color), sort_keys=False, allow_unicode=True),
                                 encoding="utf-8")
    meta = {}
    return cid, lanzar(cid, "Creación del servicio", pasos_crear(cid, plan, meta), meta)


# ================================================================== formulario completo (solo el dueño)
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

    v["domain"] = texto("domain", RE["host"], "Dominio")
    v["source"] = {**(v.get("source") or {}), "repo": texto("repo", RE["repo"], "Repositorio Git"),
                   "branch": texto("branch", RE["ref"], "Rama"), "path": texto("path", RE["ref"], "Carpeta")}
    err += aplicar_runtime(f, v)
    err += aplicar_env(f.get("env", ""), v)
    v["image"]["tag"] = texto("tag", RE["tag"], "Etiqueta de imagen")
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


def aplicar_runtime(f, v):
    rt = f.get("runtime")
    if rt not in RUNTIMES:
        return ["Runtime no válido."]
    if rt != v.get("runtime"):
        r = RUNTIMES[rt]
        v["runtime"] = rt
        v["livenessProbe"] = {"httpGet": {"path": r["live"], "port": "http"}, "initialDelaySeconds": 10}
        v["readinessProbe"] = {"httpGet": {"path": r["ready"], "port": "http"}, "initialDelaySeconds": 5}
    try:
        port = int(f.get("port", ""))
        if not 1 <= port <= 65535:
            raise ValueError
        v["containerPort"] = port
    except ValueError:
        return ["Puerto no válido."]
    return []


def aplicar_env(texto, v):
    err, env = [], {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        k, sep, val = linea.partition("=")
        if not sep or not RE["env"].match(k.strip()):
            err.append(f"Variable no válida: {linea} (formato CLAVE=valor)")
            continue
        env[k.strip()] = val.strip()
    v["env"] = env
    return err


# ================================================================== seguridad
@app.before_request
def seguridad():
    if request.path.startswith("/admin"):
        a = request.authorization
        if not a or a.username != "admin" or a.password != ADMIN_PASSWORD:
            return Response("Panel del dueño de la plataforma: acceso restringido.", 401,
                            {"WWW-Authenticate": 'Basic realm="Administracion PaaS"'})
    return None


def login_requerido(fn):
    @wraps(fn)
    def envoltura(*a, **kw):
        cid = session.get("cid")
        if not cid or cid not in clientes():
            session.clear()
            return redirect(url_for("portada"))
        g.cid = cid
        return fn(*a, **kw)
    return envoltura


# ================================================================== PORTAL DEL CLIENTE (tipo Render)
def resumen_servicio(cid):
    c, v = cliente(cid), cargar(cid)
    pods = pods_app(cid)
    listos = sum(1 for p in pods if len(p) > 2 and p[2] == "Running" and p[1].split("/")[0] == p[1].split("/")[-1])
    hist = historial(cid)
    ultimo = hist[0] if hist else None
    if ultimo and ultimo["estado"] == "corriendo":
        estado = ("Desplegando", "warn")
    elif listos:
        estado = ("Live", "ok")
    elif ultimo and ultimo["estado"] == "falló":
        estado = ("Falló el despliegue", "err")
    else:
        estado = ("Sin instancias", "err")
    return {"c": c, "v": v, "src": fuente(v, cid), "pods": pods, "listos": listos, "hist": hist,
            "ultimo": ultimo, "estado": estado, "plan": PLANES.get(c.get("plan"), PLANES["basico"]),
            "nuevo": cid not in CLIENTES}


@app.route("/")
def portada():
    if session.get("cid") in clientes():
        return redirect(url_for("servicio"))
    return render_template("p_portada.html")


@app.route("/login", methods=["POST"])
def login():
    cid = request.form.get("usuario", "").strip().lower()
    if cid in clientes() and clave_valida(cid, request.form.get("clave", "")):
        session.clear()
        session["cid"] = cid
        return redirect(url_for("servicio"))
    flash("Usuario o contraseña incorrectos.", "error")
    return redirect(url_for("portada"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("portada"))


@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        cid, res = crear_servicio(request.form)
        if cid is None:
            for e in res:
                flash(e, "error")
            return render_template("p_registro.html", f=request.form, runtimes=RUNTIMES, motores=MOTORES, planes=PLANES)
        session.clear()
        session["cid"] = cid
        flash("¡Servicio creado! Estamos construyendo y desplegando tu app desde Git.", "ok")
        return redirect(url_for("evento", jid=res["id"]))
    return render_template("p_registro.html", f={}, runtimes=RUNTIMES, motores=MOTORES, planes=PLANES)


@app.route("/app")
@login_requerido
def servicio():
    return render_template("p_servicio.html", s=resumen_servicio(g.cid), uso=uso_pods(g.cid),
                           runtimes=RUNTIMES, motores=MOTORES, ip=request.host.split(":")[0], seccion="resumen")


@app.route("/app/desplegar", methods=["POST"])
@login_requerido
def p_desplegar():
    meta = {}
    job = lanzar(g.cid, "Despliegue desde Git", pasos_git(g.cid, meta), meta)
    if not job:
        flash("Ya hay un despliegue en curso.", "warn")
        return redirect(url_for("eventos"))
    return redirect(url_for("evento", jid=job["id"]))


@app.route("/app/eventos")
@login_requerido
def eventos():
    return render_template("p_eventos.html", s=resumen_servicio(g.cid), seccion="eventos")


@app.route("/app/eventos/<jid>")
@login_requerido
def evento(jid):
    s = resumen_servicio(g.cid)
    job = next((h for h in s["hist"] if h["id"] == jid), None)
    if not job:
        abort(404)
    return render_template("p_evento.html", s=s, job=job, seccion="eventos")


@app.route("/app/logs")
@login_requerido
def logs():
    c = cliente(g.cid)
    sel = f"app.kubernetes.io/instance={c['release']},app.kubernetes.io/name=paas-app-chart"
    _, out = run(["kubectl", "logs", "-n", c["ns"], "-l", sel, "--prefix", "--tail=80", "--max-log-requests=10"], 30)
    return render_template("p_logs.html", s=resumen_servicio(g.cid), out=out, seccion="logs")


@app.route("/app/entorno", methods=["GET", "POST"])
@login_requerido
def entorno():
    v = cargar(g.cid)
    if request.method == "POST":
        err = aplicar_env(request.form.get("env", ""), v)
        if err:
            for e in err:
                flash(e, "error")
            return redirect(url_for("entorno"))
        guardar(g.cid, v)
        job = lanzar(g.cid, "Cambio de variables de entorno", pasos_aplicar(g.cid))
        if not job:
            flash("Guardado. Ya hay un despliegue en curso; se aplicará en el próximo.", "warn")
            return redirect(url_for("entorno"))
        return redirect(url_for("evento", jid=job["id"]))
    env_txt = "\n".join(f"{k}={val}" for k, val in (v.get("env") or {}).items())
    return render_template("p_entorno.html", s=resumen_servicio(g.cid), env_txt=env_txt, seccion="entorno")


@app.route("/app/bd")
@login_requerido
def p_bd():
    s = resumen_servicio(g.cid)
    v, c = s["v"], s["c"]
    datos = credenciales_bd(g.cid)
    host = f"{c['release']}-paas-app-chart-db"
    motor = v["database"]["engine"]
    esquema = "postgresql" if motor == "postgres" else "mysql"
    url = (f"{esquema}://{datos.get('DB_USER', '')}:{datos.get('DB_PASSWORD', '')}@{host}:{PUERTOS_BD[motor]}/"
           f"{v['database']['name']}")
    _, pvc = run(["kubectl", "get", "pvc", "-n", c["ns"], "--no-headers"], 20)
    return render_template("p_bd.html", s=s, datos=datos, host=host, puerto=PUERTOS_BD[motor], url=url,
                           motores=MOTORES, pvc=pvc, seccion="bd")


@app.route("/app/config", methods=["GET", "POST"])
@login_requerido
def config():
    v = cargar(g.cid)
    if request.method == "POST":
        f = request.form
        err = []
        repo, branch, path = f.get("repo", "").strip(), f.get("branch", "").strip(), f.get("path", "").strip()
        if not RE["repo"].match(repo) or not RE["ref"].match(branch) or not RE["ref"].match(path):
            err.append("Revisa el repositorio, la rama y la carpeta.")
        err += aplicar_runtime(f, v)
        if err:
            for e in err:
                flash(e, "error")
            return redirect(url_for("config"))
        v["source"] = {**(v.get("source") or {}), "repo": repo, "branch": branch, "path": path}
        guardar(g.cid, v)
        flash("Configuración guardada. Usa «Desplegar último commit» para construir con estos datos.", "ok")
        return redirect(url_for("config"))
    return render_template("p_config.html", s=resumen_servicio(g.cid), runtimes=RUNTIMES, seccion="config")


# ================================================================== PANEL DEL DUEÑO (/admin)
@app.route("/admin")
def inicio():
    tarjetas = []
    for cid, c in clientes().items():
        v = cargar(cid)
        _, pods = run(["kubectl", "get", "pods", "-n", c["ns"], "--no-headers"], 20)
        lineas = [l.split() for l in pods.splitlines() if l.strip() and not l.startswith("No resources")]
        listos = sum(1 for p in lineas if len(p) > 2 and p[2] == "Running" and p[1].split("/")[0] == p[1].split("/")[-1])
        _, rev = run(["helm", "status", c["release"], "-n", c["ns"], "-o", "json"], 20)
        try:
            info = json.loads(rev)
            helm = f"revisión {info['version']} · {info['info']['status']}"
        except Exception:  # noqa: BLE001
            helm = "sin información"
        h = historial(cid)
        tarjetas.append({"id": cid, "c": c, "v": v, "src": fuente(v, cid), "pods": len(lineas), "listos": listos,
                         "helm": helm, "job": h[0] if h else None, "nuevo": cid not in CLIENTES,
                         "plan": PLANES.get(c.get("plan"), {}).get("label", "")})
    return render_template("inicio.html", tarjetas=tarjetas, runtimes=RUNTIMES, motores=MOTORES,
                           ip=request.host.split(":")[0])


@app.route("/admin/c/<cid>", methods=["GET", "POST"])
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
            if not lanzar(cid, "Cambio de configuración (dueño)", pasos_aplicar(cid)):
                flash("Ya hay un despliegue en curso para este cliente.", "warn")
            return redirect(url_for("trabajo", cid=cid))
        return redirect(url_for("editar", cid=cid))
    env_txt = "\n".join(f"{k}={val}" for k, val in (v.get("env") or {}).items())
    raw = c["archivo"].read_text(encoding="utf-8")
    return render_template("editar.html", cid=cid, c=c, v=v, src=fuente(v, cid), env_txt=env_txt, raw=raw,
                           runtimes=RUNTIMES, motores=MOTORES)


@app.route("/admin/c/<cid>/desplegar", methods=["POST"])
def desplegar(cid):
    cliente(cid)
    meta = {}
    if not lanzar(cid, "Despliegue desde Git (dueño)", pasos_git(cid, meta), meta):
        flash("Ya hay un despliegue en curso para este cliente.", "warn")
    return redirect(url_for("trabajo", cid=cid))


@app.route("/admin/c/<cid>/trabajo")
def trabajo(cid):
    c = cliente(cid)
    h = historial(cid)
    return render_template("trabajo.html", cid=cid, c=c, job=h[0] if h else None)


@app.route("/admin/c/<cid>/estado")
def estado(cid):
    c = cliente(cid)
    cmd = ["kubectl", "get", "pods,svc,ingress,hpa,pvc,resourcequota", "-n", c["ns"], "-o", "wide"]
    code, out = run(cmd, 30)
    _, hist = run(["helm", "history", c["release"], "-n", c["ns"], "--max", "6"], 30)
    return render_template("salida.html", c=c, cid=cid, titulo=f"Estado del namespace {c['ns']}",
                           cmd=" ".join(cmd), code=code, out=out + "\n\n$ helm history " + c["release"] + "\n" + hist)


@app.route("/admin/c/<cid>/bd")
def credenciales(cid):
    c = cliente(cid)
    return render_template("bd.html", c=c, cid=cid, v=cargar(cid), datos=credenciales_bd(cid), motores=MOTORES,
                           secreto=f"{c['release']}-paas-app-chart-db-credentials",
                           servidor=f"{c['release']}-paas-app-chart-db")


@app.route("/admin/c/<cid>/eliminar", methods=["POST"])
def eliminar(cid):
    c = cliente(cid)
    if cid in CLIENTES:
        abort(403)   # Alpha y Beta están protegidos
    run(["helm", "uninstall", c["release"], "-n", c["ns"]], 120)
    run(["kubectl", "delete", "namespace", c["ns"], "--wait=false"], 60)
    reg = leer_registro()
    reg.pop(cid, None)
    escribir_registro(reg)
    if c["archivo"].exists():
        c["archivo"].rename(c["archivo"].with_suffix(".eliminado"))
    JOBS.pop(cid, None)
    flash(f"Cliente {c['nombre']} eliminado (namespace {c['ns']} en borrado).", "ok")
    return redirect(url_for("inicio"))


# ================================================================== plantillas · PANEL DEL DUEÑO (oscuro)
BASE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{% block t %}Administración PaaS{% endblock %}</title>
<style>
:root{--bg:#0b1020;--card:#141b2d;--line:#26314a;--text:#e5e9f2;--muted:#8d99b3;--acc:#7c5cff}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{padding:14px 28px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center}
header a.brand{color:var(--text);text-decoration:none;font-weight:800;font-size:18px}main{max-width:1150px;margin:0 auto;padding:26px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:15px;margin:0 0 12px;color:#b9a8ff;text-transform:uppercase;letter-spacing:.04em}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input,select,textarea{width:100%;padding:9px 11px;border-radius:7px;border:1px solid var(--line);background:#0a0f1c;color:var(--text);font:inherit}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:13px}input:disabled{opacity:.45}.chk{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:14px}.chk input{width:auto}
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
<header><a class="brand" href="{{ url_for('inicio') }}">⎈ Administración PaaS</a><span class="muted">Panel del dueño de la plataforma · Kubernetes · Helm · Ingress TLS</span>
<span style="margin-left:auto"></span><a class="btn" style="font-size:13px;padding:7px 12px" href="{{ url_for('registro') }}" target="_blank">＋ Nuevo cliente</a>
<span class="tag">admin · dueño</span></header>
<main>{% with m = get_flashed_messages(with_categories=true) %}{% for cat, msg in m %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}{% endwith %}
{% block c %}{% endblock %}</main></body></html>"""

INICIO = """{% extends "base.html" %}{% block c %}
<h1>Clientes de la plataforma</h1><p class="muted">Cada cliente vive en su propio namespace, con su base de datos, su certificado y la cuota de su plan.</p>
<div class="grid">{% for t in tarjetas %}{% set v = t.v %}
<div class="card" style="border-top:4px solid {{ v.env.BRAND_COLOR | default('#7c5cff') }}">
 <h1 style="font-size:20px">{{ t.c.nombre }}</h1>
 <p>{% if t.plan %}<span class="tag" style="border-color:#7c5cff">Plan {{ t.plan }}</span>{% endif %}<span class="tag">{{ runtimes[v.runtime].label if v.runtime in runtimes else v.runtime }}</span><span class="tag">{{ motores[v.database.engine] }} {{ v.database.version }}</span><span class="tag">ns {{ t.c.ns }}</span></p>
 <table>
  <tr><td class="muted">Estado</td><td><span class="dot" style="background:{{ '#22c55e' if t.pods and t.listos == t.pods else '#f59e0b' }}"></span>{{ t.listos }}/{{ t.pods }} pods listos · {{ t.helm }}</td></tr>
  <tr><td class="muted">Sitio</td><td><a href="http://{{ v.domain }}" target="_blank">http://{{ v.domain }}</a><br><a href="https://{{ v.domain }}" target="_blank">https://{{ v.domain }}</a></td></tr>
  <tr><td class="muted">Base de datos</td><td><a href="https://db.{{ v.domain }}" target="_blank">https://db.{{ v.domain }}</a> (Adminer)</td></tr>
  <tr><td class="muted">Código</td><td>{{ t.src.repo | replace('https://github.com/', '') }} · {{ t.src.branch }} · {{ t.src.path }}</td></tr>
  <tr><td class="muted">Imagen</td><td><code>{{ v.image.repository }}:{{ v.image.tag }}</code></td></tr>
  <tr><td class="muted">Escala</td><td>HPA {{ v.autoscaling.minReplicas }}–{{ v.autoscaling.maxReplicas }} réplicas · {{ v.resources.limits.cpu }} CPU / {{ v.resources.limits.memory }}</td></tr>
  <tr><td class="muted">Middleware</td><td>{{ v.middleware.rateLimitRPM }} sol/min · {{ 'HTTPS forzado' if v.middleware.sslRedirect else 'HTTP y HTTPS' }}{{ ' · CORS' if v.middleware.cors }}{{ ' · sesión fija' if v.middleware.stickySessions }}</td></tr>
 </table>
 {% if t.job %}<p class="muted">Último despliegue: <a href="{{ url_for('trabajo', cid=t.id) }}">{{ t.job.tipo }} · {{ t.job.estado }}</a></p>{% endif %}
 <div class="acts"><a class="btn" href="{{ url_for('editar', cid=t.id) }}">Configurar</a>
  <form method="post" action="{{ url_for('desplegar', cid=t.id) }}"><button class="btn ok">Desplegar desde Git</button></form>
  <a class="btn sec" href="{{ url_for('estado', cid=t.id) }}">Estado</a><a class="btn sec" href="{{ url_for('credenciales', cid=t.id) }}">Credenciales BD</a>
  {% if t.nuevo %}<form method="post" action="{{ url_for('eliminar', cid=t.id) }}" onsubmit="return confirm('¿Eliminar {{ t.c.nombre }} y todo su namespace?')"><button class="btn sec" style="color:#fca5a5">Eliminar</button></form>{% endif %}</div>
 {% if t.nuevo %}<p class="muted">Archivo hosts: <code>{{ ip }} {{ v.domain }} db.{{ v.domain }}</code></p>{% endif %}
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
 <p class="muted">El motor se elige al crear el cliente (cambiarlo crearía una base vacía).</p></div>
<div class="card"><h2>Variables de entorno</h2><textarea name="env" rows="6">{{ env_txt }}</textarea>
 <p class="muted">Una por línea, CLAVE=valor. Las credenciales de la BD vienen del Secret.</p>
 <label>Etiqueta de la imagen</label><input name="tag" value="{{ v.image.tag }}"></div>
<div class="card"><h2>Escalado y recursos (plan)</h2>
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

TRABAJO = """{% extends "base.html" %}{% block c %}
{% if job and job.estado == 'corriendo' %}<meta http-equiv="refresh" content="3">{% endif %}
<h1>{{ c.nombre }} · {{ job.tipo if job else 'sin despliegues' }}</h1>
{% if job %}<p><span class="dot" style="background:{{ {'corriendo':'#f59e0b','listo':'#22c55e'}.get(job.estado, '#ef4444') }}"></span><b>{{ job.estado }}</b>
<span class="muted"> · inició {{ job.inicio }}{% if job.fin %} · duró {{ job.dur }} s{% endif %}{% if job.estado == 'corriendo' %} · la página se actualiza sola{% endif %}</span></p>
<pre>{{ job.log | join('\\n') }}</pre>{% endif %}
<div class="acts"><a class="btn" href="{{ url_for('inicio') }}">Volver al panel</a></div>{% endblock %}"""

SALIDA = """{% extends "base.html" %}{% block c %}<h1>{{ titulo }}</h1><p class="muted">$ {{ cmd }} · código {{ code }}</p><pre>{{ out }}</pre>
<div class="acts"><a class="btn" href="{{ url_for('inicio') }}">Volver al panel</a><a class="btn sec" href="{{ url_for('editar', cid=cid) }}">Configurar</a></div>{% endblock %}"""

BD = """{% extends "base.html" %}{% block c %}<h1>{{ c.nombre }} · base de datos administrada</h1>
<p class="muted">Datos leídos del Secret <code>{{ secreto }}</code> (nunca están en Git).</p>
<div class="card" style="max-width:620px"><table>
<tr><td class="muted">Adminer</td><td><a href="https://db.{{ v.domain }}" target="_blank">https://db.{{ v.domain }}</a></td></tr>
<tr><td class="muted">Motor</td><td>{{ motores[v.database.engine] }} {{ v.database.version }}</td></tr>
<tr><td class="muted">Servidor</td><td><code>{{ servidor }}</code></td></tr>
<tr><td class="muted">Usuario</td><td><code>{{ datos.DB_USER | default('—') }}</code></td></tr>
<tr><td class="muted">Contraseña</td><td><code>{{ datos.DB_PASSWORD | default('—') }}</code></td></tr>
<tr><td class="muted">Base de datos</td><td><code>{{ v.database.name }}</code></td></tr></table></div>
<div class="acts"><a class="btn" href="{{ url_for('inicio') }}">Volver al panel</a></div>{% endblock %}"""

# ================================================================== plantillas · PORTAL DEL CLIENTE (claro, tipo Render)
P_BASE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{% block t %}NubePaaS{% endblock %}</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--line:#e4e7ef;--text:#14161f;--muted:#6b7280;--acc:#5b3df5;--ok:#16a34a;--warn:#d97706;--err:#dc2626}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
.top{height:58px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;padding:0 24px}
.logo{font-weight:800;font-size:19px;color:var(--text);text-decoration:none}.logo span{color:var(--acc)}
.layout{display:grid;grid-template-columns:230px 1fr;min-height:calc(100vh - 58px)}
nav.side{background:#fff;border-right:1px solid var(--line);padding:18px 12px}
nav.side a{display:block;padding:9px 12px;border-radius:8px;color:#374151;text-decoration:none;font-size:14px;margin-bottom:2px}
nav.side a.on,nav.side a:hover{background:#eef0ff;color:var(--acc);font-weight:600}
nav.side .grupo{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 12px 6px}
main{padding:28px 34px;max-width:1100px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}
h1{font-size:26px;margin:0}h2{font-size:16px;margin:0 0 12px}h3{font-size:13px;color:var(--muted);margin:0 0 6px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.muted{color:var(--muted);font-size:13px}code{font-family:ui-monospace,Consolas,monospace;font-size:13px;background:#f1f2f7;padding:2px 6px;border-radius:5px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:99px;font-size:13px;font-weight:700}
.pill.ok{background:#dcfce7;color:#166534}.pill.warn{background:#fef3c7;color:#92400e}.pill.err{background:#fee2e2;color:#991b1b}
.pill i{width:8px;height:8px;border-radius:50%;background:currentColor;display:inline-block}
.btn{display:inline-block;padding:9px 16px;border-radius:8px;border:1px solid transparent;background:var(--acc);color:#fff;font-weight:600;cursor:pointer;text-decoration:none;font-size:14px}
.btn.sec{background:#fff;border-color:var(--line);color:var(--text)}
label{display:block;font-size:13px;color:#374151;margin:12px 0 5px;font-weight:600}
input,select,textarea{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #d6d9e3;background:#fff;color:var(--text);font:inherit}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
pre{background:#0d1117;color:#d6deeb;padding:16px;border-radius:10px;overflow:auto;font-size:12.5px;white-space:pre-wrap;line-height:1.45;max-height:560px}
.flash{padding:11px 14px;border-radius:8px;margin-bottom:14px;font-size:14px}.flash.ok{background:#dcfce7;color:#166534}.flash.error{background:#fee2e2;color:#991b1b}.flash.warn{background:#fef3c7;color:#92400e}
table{border-collapse:collapse;width:100%}td,th{padding:9px 6px;border-bottom:1px solid var(--line);font-size:14px;text-align:left}th{color:var(--muted);font-size:12px;text-transform:uppercase}
a{color:var(--acc)}.cab{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.chip{display:inline-block;padding:3px 10px;border-radius:99px;font-size:12px;background:#f1f2f7;color:#374151;margin:6px 6px 0 0}
</style></head><body>
<div class="top"><a class="logo" href="{{ url_for('portada') }}">☁ Nube<span>PaaS</span></a>
<span class="muted">Despliega desde Git · HTTPS · Bases de datos administradas</span><span style="margin-left:auto"></span>
{% if session.cid %}<span class="chip">👤 {{ session.cid }}</span><a class="btn sec" href="{{ url_for('logout') }}">Cerrar sesión</a>{% endif %}</div>
{% block cuerpo %}{% endblock %}</body></html>"""

P_FLASH = """{% with m = get_flashed_messages(with_categories=true) %}{% for cat, msg in m %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}{% endwith %}"""

P_SERVICIO_BASE = """{% extends "p_base.html" %}{% block cuerpo %}<div class="layout">
<nav class="side"><div class="grupo">Servicio web</div>
<a href="{{ url_for('servicio') }}" class="{{ 'on' if seccion == 'resumen' }}">Resumen</a>
<a href="{{ url_for('eventos') }}" class="{{ 'on' if seccion == 'eventos' }}">Eventos</a>
<a href="{{ url_for('logs') }}" class="{{ 'on' if seccion == 'logs' }}">Logs</a>
<a href="{{ url_for('entorno') }}" class="{{ 'on' if seccion == 'entorno' }}">Variables de entorno</a>
<a href="{{ url_for('config') }}" class="{{ 'on' if seccion == 'config' }}">Configuración</a>
<div class="grupo">Base de datos</div><a href="{{ url_for('p_bd') }}" class="{{ 'on' if seccion == 'bd' }}">{{ {'postgres':'PostgreSQL','mysql':'MySQL'}[s.v.database.engine] }}</a></nav>
<main>""" + P_FLASH + """
<div class="cab"><div style="flex:1"><h3>Servicio web · {{ s.c.nombre }}</h3>
<h1>{{ s.c.release if s.nuevo else s.v.domain.split('.')[0] }} <span class="pill {{ s.estado[1] }}"><i></i>{{ s.estado[0] }}</span></h1>
<p style="margin:8px 0 0"><a href="https://{{ s.v.domain }}" target="_blank">https://{{ s.v.domain }}</a>
<span class="chip">{{ s.src.repo | replace('https://github.com/', '') | replace('.git', '') }} · {{ s.src.branch }}</span>
<span class="chip">Plan {{ s.plan.label }}</span></p></div>
<form method="post" action="{{ url_for('p_desplegar') }}"><button class="btn">⟳ Desplegar último commit</button></form></div>
{% block seccion %}{% endblock %}</main></div>{% endblock %}"""

P_PORTADA = """{% extends "p_base.html" %}{% block cuerpo %}
<div style="max-width:1050px;margin:50px auto;padding:0 24px;display:grid;grid-template-columns:1.2fr 1fr;gap:40px;align-items:center">
<div><h1 style="font-size:40px;line-height:1.15">Despliega tu app desde Git<br>en minutos.</h1>
<p style="font-size:17px;color:#4b5563;line-height:1.6">Conecta tu repositorio, elige tu runtime y tu base de datos. Nosotros construimos la imagen,
la publicamos con <b>HTTPS</b>, te damos una <b>base de datos administrada</b> y la escalamos sola.</p>
<p><span class="chip">Node.js</span><span class="chip">PHP + Apache</span><span class="chip">PostgreSQL</span><span class="chip">MySQL</span><span class="chip">Certificado SSL</span><span class="chip">Autoescalado</span></p>
<p style="margin-top:22px"><a class="btn" href="{{ url_for('registro') }}">Crear cuenta y desplegar →</a></p></div>
<div class="card" style="padding:26px">""" + P_FLASH + """<h2>Inicia sesión</h2>
<form method="post" action="{{ url_for('login') }}"><label>Usuario (nombre de tu servicio)</label><input name="usuario" placeholder="alpha" required>
<label>Contraseña</label><input name="clave" type="password" required>
<p style="margin-top:18px"><button class="btn" style="width:100%">Entrar</button></p></form>
<p class="muted" style="text-align:center">¿No tienes cuenta? <a href="{{ url_for('registro') }}">Créala aquí</a></p></div></div>{% endblock %}"""

P_REGISTRO = """{% extends "p_base.html" %}{% block cuerpo %}<main style="margin:0 auto">""" + P_FLASH + """
<h1>Nuevo servicio web</h1><p class="muted">Conecta tu repositorio y nosotros hacemos el resto: imagen, HTTPS, base de datos y escalado.</p>
<form method="post"><div class="grid" style="margin-top:18px">
<div class="card"><h2>1. Tu cuenta</h2>
 <label>Nombre del servicio</label><input name="cid" placeholder="gamma-store" value="{{ f.cid }}" required>
 <p class="muted">Será tu usuario y tu dirección: https://<b>nombre</b>.paas.local</p>
 <label>Nombre visible</label><input name="nombre" placeholder="Gamma Store" value="{{ f.nombre }}">
 <label>Contraseña</label><input name="clave" type="password" required>
 <label>Color de tu marca</label><input name="color" type="color" value="{{ f.color or '#5b3df5' }}" style="height:44px;padding:4px"></div>
<div class="card"><h2>2. Conecta tu repositorio</h2>
 <label>Repositorio Git (público)</label><input name="repo" value="{{ f.repo or 'https://github.com/maxi300/paas-parcial2.git' }}">
 <div class="row"><div><label>Rama</label><input name="branch" value="{{ f.branch or 'main' }}"></div>
 <div><label>Carpeta raíz (con Dockerfile)</label><input name="path" value="{{ f.path or 'apps/alpha-node' }}"></div></div>
 <div class="row"><div><label>Runtime</label><select name="runtime">{% for k, r in runtimes.items() %}<option value="{{ k }}" {{ 'selected' if f.runtime == k }}>{{ r.label }}</option>{% endfor %}</select></div>
 <div><label>Puerto (opcional)</label><input name="port" type="number" placeholder="automático" value="{{ f.port }}"></div></div></div>
<div class="card"><h2>3. Base de datos y plan</h2>
 <label>Base de datos administrada</label><select name="motor">{% for k, m in motores.items() %}<option value="{{ k }}" {{ 'selected' if f.motor == k }}>{{ m }}</option>{% endfor %}</select>
 <label>Plan</label>{% for k, p in planes.items() %}
 <label style="font-weight:400;display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0">
 <input type="radio" name="plan" value="{{ k }}" style="width:auto;margin-top:3px" {{ 'checked' if (f.plan or 'basico') == k }}>
 <span><b>{{ p.label }}</b><br><span class="muted">{{ p.hpa[0] }}–{{ p.hpa[1] }} instancias · disco {{ p.storage }} · {{ p.rpm }} solicitudes/min</span></span></label>{% endfor %}</div>
</div><p><button class="btn">Crear servicio web</button> <a class="btn sec" href="{{ url_for('portada') }}">Cancelar</a></p></form></main>{% endblock %}"""

P_SERVICIO = """{% extends "p_servicio_base.html" %}{% block seccion %}
{% if s.nuevo %}<div class="flash warn">Para abrir tu sitio en este equipo agrega a tu archivo <b>hosts</b>: <code>{{ ip }} {{ s.v.domain }} db.{{ s.v.domain }}</code></div>{% endif %}
<div class="grid">
<div class="card"><h3>Último despliegue</h3>{% if s.ultimo %}
 <p style="margin:4px 0"><b>{{ s.ultimo.tipo }}</b> · <span class="pill {{ {'listo':'ok','corriendo':'warn'}.get(s.ultimo.estado, 'err') }}"><i></i>{{ {'listo':'Live','corriendo':'En curso'}.get(s.ultimo.estado, 'Falló') }}</span></p>
 {% if s.ultimo.meta.commit %}<p class="muted">Commit <code>{{ s.ultimo.meta.commit }}</code> {{ s.ultimo.meta.mensaje }}</p>{% endif %}
 <p class="muted">{{ s.ultimo.inicio }}{% if s.ultimo.dur %} · {{ s.ultimo.dur }} s{% endif %} · <a href="{{ url_for('evento', jid=s.ultimo.id) }}">ver log</a></p>
 {% else %}<p class="muted">Aún no hay despliegues desde el portal. Usa «Desplegar último commit».</p>{% endif %}</div>
<div class="card"><h3>Instancias</h3><p style="font-size:30px;font-weight:800;margin:4px 0">{{ s.listos }} <span class="muted" style="font-size:14px;font-weight:400">de {{ s.pods|length }} activas</span></p>
 <p class="muted">Autoescalado {{ s.v.autoscaling.minReplicas }}–{{ s.v.autoscaling.maxReplicas }} · imagen <code>{{ s.v.image.tag }}</code></p></div>
<div class="card"><h3>Plan {{ s.plan.label }}</h3><p class="muted" style="line-height:1.7">{{ s.v.resources.limits.cpu }} CPU · {{ s.v.resources.limits.memory }} RAM por instancia<br>
 Disco de base de datos {{ s.v.database.storage }}<br>{{ s.v.middleware.rateLimitRPM }} solicitudes/min por IP<br>HTTPS {{ 'forzado' if s.v.middleware.sslRedirect else 'y HTTP' }} · certificado {{ s.v.tls.secretName }}</p></div></div>
<div class="card"><h2>Uso actual de recursos</h2>{% if uso %}<table><tr><th>Pod</th><th>CPU</th><th>Memoria</th></tr>
 {% for u in uso %}<tr><td><code>{{ u[0] }}</code></td><td>{{ u[1] }}</td><td>{{ u[2] }}</td></tr>{% endfor %}</table>
 {% else %}<p class="muted">Métricas no disponibles en este momento.</p>{% endif %}</div>
<div class="card"><h2>Detalles</h2><table>
 <tr><td class="muted">Runtime</td><td>{{ runtimes[s.v.runtime].label if s.v.runtime in runtimes else s.v.runtime }} · puerto {{ s.v.containerPort }}</td></tr>
 <tr><td class="muted">Dirección</td><td><a href="https://{{ s.v.domain }}" target="_blank">https://{{ s.v.domain }}</a> · <a href="http://{{ s.v.domain }}" target="_blank">http://{{ s.v.domain }}</a></td></tr>
 <tr><td class="muted">Repositorio</td><td>{{ s.src.repo }} · rama {{ s.src.branch }} · carpeta {{ s.src.path }}</td></tr>
 <tr><td class="muted">Base de datos</td><td>{{ motores[s.v.database.engine] }} {{ s.v.database.version }} · <a href="{{ url_for('p_bd') }}">ver conexión</a></td></tr></table></div>{% endblock %}"""

P_EVENTOS = """{% extends "p_servicio_base.html" %}{% block seccion %}<div class="card"><h2>Eventos</h2>
{% if s.hist %}<table>{% for h in s.hist %}<tr>
<td style="width:34px">{{ {'listo':'✅','corriendo':'⏳'}.get(h.estado, '❌') }}</td>
<td><a href="{{ url_for('evento', jid=h.id) }}"><b>{{ h.tipo }}</b></a> {{ {'listo':'· Live','corriendo':'· en curso'}.get(h.estado, '· falló') }}
{% if h.meta.commit %}<br><span class="muted">Commit <code>{{ h.meta.commit }}</code> {{ h.meta.mensaje }}</span>{% endif %}</td>
<td class="muted" style="white-space:nowrap">{{ h.inicio }}{% if h.dur %}<br>{{ h.dur }} s{% endif %}</td></tr>{% endfor %}</table>
{% else %}<p class="muted">Todavía no hay eventos.</p>{% endif %}</div>{% endblock %}"""

P_EVENTO = """{% extends "p_servicio_base.html" %}{% block seccion %}
{% if job.estado == 'corriendo' %}<meta http-equiv="refresh" content="3">{% endif %}
<div class="card"><h2>{{ job.tipo }} <span class="pill {{ {'listo':'ok','corriendo':'warn'}.get(job.estado, 'err') }}"><i></i>{{ {'listo':'Live','corriendo':'En curso'}.get(job.estado, 'Falló') }}</span></h2>
<p class="muted">Inició {{ job.inicio }}{% if job.fin %} · terminó {{ job.fin }} ({{ job.dur }} s){% endif %}
{% if job.meta.commit %} · commit <code>{{ job.meta.commit }}</code> {{ job.meta.mensaje }}{% endif %}{% if job.estado == 'corriendo' %} · se actualiza solo{% endif %}</p>
<pre>{{ job.log | join('\\n') }}</pre>
{% if job.estado == 'listo' %}<p><a class="btn" href="https://{{ s.v.domain }}" target="_blank">Abrir https://{{ s.v.domain }}</a></p>{% endif %}</div>{% endblock %}"""

P_LOGS = """{% extends "p_servicio_base.html" %}{% block seccion %}<div class="card"><h2>Logs de la aplicación</h2>
<p class="muted">Últimas líneas de cada instancia (el prefijo indica el pod). <a href="{{ url_for('logs') }}">Actualizar</a></p>
<pre>{{ out or 'Sin registros todavía.' }}</pre></div>{% endblock %}"""

P_ENTORNO = """{% extends "p_servicio_base.html" %}{% block seccion %}<div class="card"><h2>Variables de entorno</h2>
<p class="muted">Una por línea, <code>CLAVE=valor</code>. Las credenciales de tu base de datos se inyectan solas desde un Secret: no las pongas aquí.</p>
<form method="post"><textarea name="env" rows="10">{{ env_txt }}</textarea>
<p><button class="btn">Guardar y desplegar</button></p></form></div>{% endblock %}"""

P_CONFIG = """{% extends "p_servicio_base.html" %}{% block seccion %}<div class="card"><h2>Compilación y despliegue</h2>
<form method="post"><label>Repositorio Git</label><input name="repo" value="{{ s.src.repo }}">
<div class="row"><div><label>Rama</label><input name="branch" value="{{ s.src.branch }}"></div><div><label>Carpeta raíz (con Dockerfile)</label><input name="path" value="{{ s.src.path }}"></div></div>
<div class="row"><div><label>Runtime</label><select name="runtime">{% for k, r in runtimes.items() %}<option value="{{ k }}" {{ 'selected' if s.v.runtime == k }}>{{ r.label }}</option>{% endfor %}</select></div>
<div><label>Puerto</label><input name="port" type="number" value="{{ s.v.containerPort }}"></div></div>
<p><button class="btn">Guardar</button></p></form></div>
<div class="card"><h2>Plan {{ s.plan.label }}</h2><p class="muted">Escalado, recursos, disco y límites los define tu plan. Para cambiarlo, contacta al administrador de la plataforma.</p></div>{% endblock %}"""

P_BD = """{% extends "p_servicio_base.html" %}{% block seccion %}<div class="card"><h2>{{ motores[s.v.database.engine] }} {{ s.v.database.version }} · base de datos administrada</h2>
<p class="muted">Solo tu app puede conectarse por la red interna (NetworkPolicy). Para administrarla usa Adminer.</p><table>
<tr><td class="muted">Hostname interno</td><td><code>{{ host }}</code></td></tr>
<tr><td class="muted">Puerto</td><td><code>{{ puerto }}</code></td></tr>
<tr><td class="muted">Base de datos</td><td><code>{{ s.v.database.name }}</code></td></tr>
<tr><td class="muted">Usuario</td><td><code>{{ datos.DB_USER | default('—') }}</code></td></tr>
<tr><td class="muted">Contraseña</td><td><details><summary class="muted" style="cursor:pointer">Mostrar</summary><code>{{ datos.DB_PASSWORD | default('—') }}</code></details></td></tr>
<tr><td class="muted">URL interna</td><td><details><summary class="muted" style="cursor:pointer">Mostrar</summary><code style="word-break:break-all">{{ url }}</code></details></td></tr>
<tr><td class="muted">Almacenamiento</td><td>{{ s.v.database.storage }} (volumen persistente)</td></tr></table>
<p style="margin-top:16px"><a class="btn" href="https://db.{{ s.v.domain }}" target="_blank">Abrir Adminer</a></p></div>
<div class="card"><h2>Volumen</h2><pre>{{ pvc }}</pre></div>{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "inicio.html": INICIO, "editar.html": EDITAR, "trabajo.html": TRABAJO,
    "salida.html": SALIDA, "bd.html": BD,
    "p_base.html": P_BASE, "p_servicio_base.html": P_SERVICIO_BASE, "p_portada.html": P_PORTADA,
    "p_registro.html": P_REGISTRO, "p_servicio.html": P_SERVICIO, "p_eventos.html": P_EVENTOS,
    "p_evento.html": P_EVENTO, "p_logs.html": P_LOGS, "p_entorno.html": P_ENTORNO, "p_config.html": P_CONFIG,
    "p_bd.html": P_BD,
})

if __name__ == "__main__":
    print("NubePaaS listo:\n  Portal de clientes  -> http://0.0.0.0:5000/\n  Panel del dueño     -> http://0.0.0.0:5000/admin")
    app.run(host="0.0.0.0", port=int(os.environ.get("PANEL_PORT", "5000")), threaded=True)
