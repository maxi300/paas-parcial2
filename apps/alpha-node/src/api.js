// API RESTful de productos (CRUD) · Cliente 1
// Lectura pública; crear, editar y borrar requieren token JWT (Authorization: Bearer <token>) o la cookie de sesión.
const express = require("express");

// Evita que un error de BD en una ruta async tumbe el proceso (Express 4 no captura promesas)
process.on("unhandledRejection", (err) => console.error("[api] error no controlado:", err.message));

module.exports = function registerApi(app, { pool, jwt, bcrypt, cfg }) {
  const api = express.Router();
  api.use(express.json());

  function auth(req, res, next) {
    const header = req.get("authorization") || "";
    const token = header.startsWith("Bearer ") ? header.slice(7) : req.cookies.tn_session;
    try {
      req.user = jwt.verify(token || "", cfg.jwtSecret);
      next();
    } catch {
      res.status(401).json({ error: "Token inválido o ausente" });
    }
  }

  function validate(body) {
    const name = String(body.name || "").trim();
    const price = Number(body.price);
    const stock = Number.parseInt(body.stock, 10);
    if (!name || name.length > 120 || !Number.isFinite(price) || price < 0 || !Number.isInteger(stock) || stock < 0)
      return null;
    return { name, price, stock };
  }

  const idOf = (req) => Number.parseInt(req.params.id, 10);

  api.post("/login", async (req, res) => {
    const { email, password } = req.body || {};
    const { rows } = await pool.query("SELECT id, name, email, password_hash FROM users WHERE email = $1", [
      String(email || "").trim().toLowerCase(),
    ]);
    const user = rows[0];
    if (!user || !(await bcrypt.compare(String(password || ""), user.password_hash)))
      return res.status(401).json({ error: "Credenciales incorrectas" });
    const token = jwt.sign({ id: user.id, name: user.name, email: user.email }, cfg.jwtSecret, { expiresIn: "8h" });
    res.json({ token, expira_en: "8h" });
  });

  api.get("/products", async (_req, res) => {
    const { rows } = await pool.query("SELECT id, name, price::float AS price, stock FROM products ORDER BY id");
    res.json(rows);
  });

  api.get("/products/:id", async (req, res) => {
    const { rows } = await pool.query("SELECT id, name, price::float AS price, stock FROM products WHERE id = $1", [idOf(req)]);
    rows[0] ? res.json(rows[0]) : res.status(404).json({ error: "No existe" });
  });

  api.post("/products", auth, async (req, res) => {
    const p = validate(req.body || {});
    if (!p) return res.status(400).json({ error: "Datos inválidos: name, price >= 0, stock entero >= 0" });
    const { rows } = await pool.query(
      "INSERT INTO products (name, price, stock, created_by) VALUES ($1, $2, $3, $4) RETURNING id, name, price::float AS price, stock",
      [p.name, p.price, p.stock, req.user.id]
    );
    res.status(201).json(rows[0]);
  });

  api.put("/products/:id", auth, async (req, res) => {
    const p = validate(req.body || {});
    if (!p) return res.status(400).json({ error: "Datos inválidos: name, price >= 0, stock entero >= 0" });
    const { rows } = await pool.query(
      "UPDATE products SET name = $1, price = $2, stock = $3 WHERE id = $4 RETURNING id, name, price::float AS price, stock",
      [p.name, p.price, p.stock, idOf(req)]
    );
    rows[0] ? res.json(rows[0]) : res.status(404).json({ error: "No existe" });
  });

  api.delete("/products/:id", auth, async (req, res) => {
    const { rowCount } = await pool.query("DELETE FROM products WHERE id = $1", [idOf(req)]);
    rowCount ? res.status(204).end() : res.status(404).json({ error: "No existe" });
  });

  app.use("/api", api);
};
