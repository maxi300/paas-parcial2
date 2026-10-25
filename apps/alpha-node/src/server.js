// Alpha Tech - Cliente 1 del PaaS multi-tenant (namespace portal-clientes)
// Stack: Node.js + Express + PostgreSQL
// Toda la configuración llega por variables de entorno (values.yaml -> Deployment -> env / Secret).

const os = require("os");
const express = require("express");
const cookieParser = require("cookie-parser");
const bcrypt = require("bcryptjs");
const jwt = require("jsonwebtoken");
const { Pool } = require("pg");

const cfg = {
  port: parseInt(process.env.PORT || "3000", 10),
  appName: process.env.APP_NAME || "Alpha Tech",
  tenant: process.env.TENANT || "portal-clientes",
  appEnv: process.env.APP_ENV || "desarrollo",
  brandColor: process.env.BRAND_COLOR || "#2563eb",
  jwtSecret: process.env.JWT_SECRET || "cambiar-este-secreto",
  db: {
    host: process.env.DB_HOST || "localhost",
    port: parseInt(process.env.DB_PORT || "5432", 10),
    database: process.env.DB_NAME || "alpha_db",
    user: process.env.DB_USER || "alpha_user",
    password: process.env.DB_PASSWORD || "",
  },
};

const pool = new Pool({ ...cfg.db, max: 5, connectionTimeoutMillis: 3000 });
let dbReady = false;

async function migrate() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS users (
      id SERIAL PRIMARY KEY,
      name VARCHAR(80) NOT NULL,
      email VARCHAR(120) UNIQUE NOT NULL,
      password_hash VARCHAR(100) NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS products (
      id SERIAL PRIMARY KEY,
      name VARCHAR(120) NOT NULL,
      price NUMERIC(10,2) NOT NULL,
      stock INT NOT NULL DEFAULT 0,
      created_by INT REFERENCES users(id) ON DELETE SET NULL,
      created_at TIMESTAMPTZ DEFAULT now()
    );`);
  const { rows } = await pool.query("SELECT count(*)::int AS n FROM products");
  if (rows[0].n === 0) {
    await pool.query(
      `INSERT INTO products (name, price, stock) VALUES
       ('Laptop Ultrabook 14"', 899.00, 12),
       ('Monitor 27" 4K', 329.99, 8),
       ('Teclado mecánico', 79.50, 25),
       ('Mouse inalámbrico', 24.90, 40)`
    );
  }
}

async function connectWithRetry(attempt = 1) {
  try {
    await pool.query("SELECT 1");
    await migrate();
    dbReady = true;
    console.log(`[db] conectado a PostgreSQL en ${cfg.db.host}:${cfg.db.port}`);
  } catch (err) {
    dbReady = false;
    const wait = Math.min(attempt * 2, 15);
    console.log(`[db] intento ${attempt} falló (${err.message}); reintento en ${wait}s`);
    setTimeout(() => connectWithRetry(attempt + 1), wait * 1000);
  }
}

// ---------- utilidades ----------
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function currentUser(req) {
  const token = req.cookies.tn_session;
  if (!token) return null;
  try {
    return jwt.verify(token, cfg.jwtSecret);
  } catch {
    return null;
  }
}

function requireAuth(req, res, next) {
  const user = currentUser(req);
  if (!user) return res.redirect("/login");
  req.user = user;
  next();
}

function page(title, user, body) {
  return `<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)} · ${esc(cfg.appName)}</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>.brand{background:${esc(cfg.brandColor)}}</style>
</head><body class="bg-light">
<nav class="navbar navbar-dark brand mb-4"><div class="container">
  <a class="navbar-brand fw-bold" href="/">${esc(cfg.appName)} <span class="badge bg-light text-dark ms-2">Node.js + PostgreSQL</span></a>
  <div class="d-flex gap-2">
    ${user
      ? `<a class="btn btn-sm btn-light" href="/dashboard">${esc(user.name)}</a>
         <form method="post" action="/logout"><button class="btn btn-sm btn-outline-light">Salir</button></form>`
      : `<a class="btn btn-sm btn-light" href="/login">Iniciar sesión</a>
         <a class="btn btn-sm btn-outline-light" href="/register">Registrarse</a>`}
  </div></div></nav>
<main class="container">${body}</main>
<footer class="container text-muted small my-4">
  Tenant <b>${esc(cfg.tenant)}</b> · entorno ${esc(cfg.appEnv)} · pod <code>${esc(os.hostname())}</code> ·
  <a href="/api/info">/api/info</a>
</footer>
</body></html>`;
}

// ---------- app ----------
const app = express();
app.set("trust proxy", true); // detrás del Ingress NGINX (HTTPS termina en el Ingress)
app.use(express.urlencoded({ extended: false }));
app.use(cookieParser());

require("./api")(app, { pool, jwt, bcrypt, cfg }); // API REST: /api/products (CRUD)

app.get("/health", (_req, res) => res.json({ status: "ok" }));
app.get("/ready", async (_req, res) => {
  try {
    await pool.query("SELECT 1");
    res.json({ status: "ready", db: "ok" });
  } catch (err) {
    res.status(503).json({ status: "not-ready", db: err.message });
  }
});

app.get("/api/info", async (_req, res) => {
  let dbVersion = "sin conexión";
  try {
    dbVersion = (await pool.query("SELECT version()")).rows[0].version;
  } catch {}
  res.json({
    cliente: cfg.appName,
    tenant: cfg.tenant,
    entorno: cfg.appEnv,
    runtime: `Node.js ${process.version}`,
    framework: "Express 4",
    base_de_datos: dbVersion,
    db_host: cfg.db.host,
    pod: os.hostname(),
  });
});

app.get("/", async (req, res) => {
  const user = currentUser(req);
  if (!dbReady) return res.status(503).send(page("Iniciando", user, `<div class="alert alert-warning">Conectando con la base de datos…</div>`));
  const { rows } = await pool.query("SELECT name, price, stock FROM products ORDER BY id");
  const cards = rows
    .map(
      (p) => `<div class="col-md-3"><div class="card h-100 shadow-sm"><div class="card-body">
        <h6 class="card-title">${esc(p.name)}</h6>
        <p class="fs-5 fw-bold mb-1">$${Number(p.price).toFixed(2)}</p>
        <span class="text-muted small">${p.stock} en existencia</span>
      </div></div></div>`
    )
    .join("");
  res.send(
    page("Catálogo", user, `
      <h1 class="h3 mb-3">Catálogo de tecnología</h1>
      <div class="row g-3">${cards}</div>`)
  );
});

app.get("/register", (req, res) => res.send(page("Registro", currentUser(req), authForm("register"))));
app.get("/login", (req, res) => res.send(page("Iniciar sesión", currentUser(req), authForm("login"))));

function authForm(mode, error = "") {
  const isReg = mode === "register";
  return `<div class="row justify-content-center"><div class="col-md-5"><div class="card shadow-sm"><div class="card-body">
    <h1 class="h4 mb-3">${isReg ? "Crear cuenta" : "Iniciar sesión"}</h1>
    ${error ? `<div class="alert alert-danger">${esc(error)}</div>` : ""}
    <form method="post" action="/${mode}">
      ${isReg ? `<input class="form-control mb-2" name="name" placeholder="Nombre" required maxlength="80">` : ""}
      <input class="form-control mb-2" type="email" name="email" placeholder="Correo" required maxlength="120">
      <input class="form-control mb-3" type="password" name="password" placeholder="Contraseña" required minlength="6">
      <button class="btn btn-primary w-100">${isReg ? "Registrarme" : "Entrar"}</button>
    </form></div></div></div></div>`;
}

function setSession(res, user, req) {
  const token = jwt.sign({ id: user.id, name: user.name, email: user.email }, cfg.jwtSecret, { expiresIn: "8h" });
  res.cookie("tn_session", token, { httpOnly: true, sameSite: "lax", secure: req.secure, maxAge: 8 * 3600 * 1000 });
}

app.post("/register", async (req, res) => {
  const { name, email, password } = req.body;
  if (!name || !email || !password || password.length < 6)
    return res.status(400).send(page("Registro", null, authForm("register", "Completa todos los campos (contraseña mínima de 6 caracteres).")));
  try {
    const hash = await bcrypt.hash(password, 10);
    const { rows } = await pool.query(
      "INSERT INTO users (name, email, password_hash) VALUES ($1, $2, $3) RETURNING id, name, email",
      [name.trim(), email.trim().toLowerCase(), hash]
    );
    setSession(res, rows[0], req);
    res.redirect("/dashboard");
  } catch (err) {
    const msg = err.code === "23505" ? "Ese correo ya está registrado." : "No se pudo crear la cuenta.";
    res.status(400).send(page("Registro", null, authForm("register", msg)));
  }
});

app.post("/login", async (req, res) => {
  const { email, password } = req.body;
  const { rows } = await pool.query("SELECT id, name, email, password_hash FROM users WHERE email = $1", [
    String(email || "").trim().toLowerCase(),
  ]);
  const user = rows[0];
  if (!user || !(await bcrypt.compare(String(password || ""), user.password_hash)))
    return res.status(401).send(page("Iniciar sesión", null, authForm("login", "Correo o contraseña incorrectos.")));
  setSession(res, user, req);
  res.redirect("/dashboard");
});

app.post("/logout", (_req, res) => {
  res.clearCookie("tn_session");
  res.redirect("/");
});

app.get("/dashboard", requireAuth, async (req, res) => {
  const users = (await pool.query("SELECT count(*)::int AS n FROM users")).rows[0].n;
  const products = (await pool.query("SELECT count(*)::int AS n FROM products")).rows[0].n;
  res.send(
    page("Panel", req.user, `
    <h1 class="h3">Hola, ${esc(req.user.name)}</h1>
    <p class="text-muted">Sesión autenticada con JWT en cookie <code>httpOnly</code>; contraseñas con bcrypt.</p>
    <div class="row g-3 mb-4">
      <div class="col-md-4"><div class="card"><div class="card-body"><div class="text-muted small">Usuarios</div><div class="fs-3 fw-bold">${users}</div></div></div></div>
      <div class="col-md-4"><div class="card"><div class="card-body"><div class="text-muted small">Productos</div><div class="fs-3 fw-bold">${products}</div></div></div></div>
      <div class="col-md-4"><div class="card"><div class="card-body"><div class="text-muted small">Atendido por el pod</div><div class="fw-bold"><code>${esc(os.hostname())}</code></div></div></div></div>
    </div>
    <div class="card"><div class="card-body">
      <h2 class="h5">Agregar producto</h2>
      <form method="post" action="/products" class="row g-2">
        <div class="col-md-6"><input class="form-control" name="name" placeholder="Nombre" required maxlength="120"></div>
        <div class="col-md-3"><input class="form-control" name="price" type="number" step="0.01" min="0" placeholder="Precio" required></div>
        <div class="col-md-2"><input class="form-control" name="stock" type="number" min="0" placeholder="Stock" required></div>
        <div class="col-md-1"><button class="btn btn-primary w-100">+</button></div>
      </form></div></div>`)
  );
});

app.post("/products", requireAuth, async (req, res) => {
  const { name, price, stock } = req.body;
  if (name && !isNaN(price) && !isNaN(stock))
    await pool.query("INSERT INTO products (name, price, stock, created_by) VALUES ($1, $2, $3, $4)", [
      String(name).slice(0, 120),
      Number(price),
      parseInt(stock, 10),
      req.user.id,
    ]);
  res.redirect("/");
});

app.use((err, _req, res, _next) => {
  console.error(err);
  res.status(500).send("Error interno");
});

app.listen(cfg.port, () => {
  console.log(`${cfg.appName} escuchando en el puerto ${cfg.port} (tenant ${cfg.tenant})`);
  connectWithRetry();
});
