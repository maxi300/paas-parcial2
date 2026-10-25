<?php
// Panel privado: CRUD completo de platillos (crear, leer, editar y borrar)
require_once __DIR__ . '/lib/layout.php';

$user = current_user();
if (!$user) {
    header('Location: /login.php');
    exit;
}
if (empty($_SESSION['csrf'])) {
    $_SESSION['csrf'] = bin2hex(random_bytes(16));
}
$csrf = $_SESSION['csrf'];
$msg = '';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!hash_equals($csrf, $_POST['csrf'] ?? '')) {
        http_response_code(400);
        exit('Solicitud no válida');
    }
    $action = $_POST['action'] ?? '';
    $id = (int) ($_POST['id'] ?? 0);
    $name = mb_substr(trim($_POST['name'] ?? ''), 0, 120);
    $cat = mb_substr(trim($_POST['category'] ?? ''), 0, 60);
    $price = $_POST['price'] ?? '';
    $valid = $name !== '' && $cat !== '' && is_numeric($price) && (float) $price >= 0;

    if ($action === 'create' && $valid) {
        db()->prepare('INSERT INTO dishes (name, category, price) VALUES (?, ?, ?)')->execute([$name, $cat, (float) $price]);
        $msg = 'Platillo creado.';
    } elseif ($action === 'update' && $id > 0 && $valid) {
        db()->prepare('UPDATE dishes SET name = ?, category = ?, price = ? WHERE id = ?')->execute([$name, $cat, (float) $price, $id]);
        $msg = 'Platillo actualizado.';
    } elseif ($action === 'delete' && $id > 0) {
        db()->prepare('DELETE FROM dishes WHERE id = ?')->execute([$id]);
        $msg = 'Platillo eliminado.';
    } else {
        $msg = 'Datos incompletos o no válidos.';
    }
    $_SESSION['flash'] = $msg;
    header('Location: /panel.php');
    exit;
}

$flash = $_SESSION['flash'] ?? '';
unset($_SESSION['flash']);
$users = (int) db()->query('SELECT COUNT(*) FROM users')->fetchColumn();
$rows = db()->query('SELECT id, name, category, price FROM dishes ORDER BY category, id')->fetchAll();
$pod = e(gethostname());
$token = '<input type="hidden" name="csrf" value="' . e($csrf) . '">';

$table = '';
foreach ($rows as $d) {
    $table .= '<tr><td colspan="4"><form method="post" class="columns is-mobile is-vcentered mb-0">' . $token . '
      <input type="hidden" name="id" value="' . (int) $d['id'] . '">
      <div class="column is-5"><input class="input is-small" name="name" value="' . e($d['name']) . '" required maxlength="120"></div>
      <div class="column is-3"><input class="input is-small" name="category" value="' . e($d['category']) . '" required maxlength="60"></div>
      <div class="column is-2"><input class="input is-small" type="number" step="0.01" min="0" name="price" value="' . e($d['price']) . '" required></div>
      <div class="column is-2 buttons are-small">
        <button class="button is-info" name="action" value="update">Guardar</button>
        <button class="button is-danger is-light" name="action" value="delete" formnovalidate
                onclick="return confirm(\'¿Eliminar este platillo?\')">Eliminar</button>
      </div></form></td></tr>';
}

$notice = $flash ? '<div class="notification is-success is-light">' . e($flash) . '</div>' : '';
render('Panel', '<h1 class="title">Hola, ' . e($user['name']) . '</h1>
<p class="subtitle is-6">Sesión PHP con cookie <code>httpOnly</code>; contraseñas con <code>password_hash()</code>.</p>' . $notice . '
<div class="columns">
  <div class="column"><div class="box"><p class="heading">Usuarios</p><p class="title">' . $users . '</p></div></div>
  <div class="column"><div class="box"><p class="heading">Platillos</p><p class="title">' . count($rows) . '</p></div></div>
  <div class="column"><div class="box"><p class="heading">Atendido por el pod</p><p class="is-size-6"><code>' . $pod . '</code></p></div></div>
</div>
<div class="box"><h2 class="title is-5">Agregar platillo</h2>
<form method="post" class="columns">' . $token . '<input type="hidden" name="action" value="create">
  <div class="column is-5"><input class="input" name="name" placeholder="Nombre" required maxlength="120"></div>
  <div class="column is-3"><input class="input" name="category" placeholder="Categoría" required maxlength="60"></div>
  <div class="column is-2"><input class="input" type="number" step="0.01" min="0" name="price" placeholder="Precio" required></div>
  <div class="column is-2"><button class="button is-success is-fullwidth">Agregar</button></div>
</form></div>
<div class="box"><h2 class="title is-5">Platillos (editar o eliminar)</h2>
<table class="table is-fullwidth"><thead><tr><th>Nombre · Categoría · Precio · Acciones</th></tr></thead><tbody>' . $table . '</tbody></table></div>');
