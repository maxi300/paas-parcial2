<?php
require_once __DIR__ . '/lib/layout.php';

$error = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $email = strtolower(trim($_POST['email'] ?? ''));
    $stmt = db()->prepare('SELECT id, name, email, password_hash FROM users WHERE email = ?');
    $stmt->execute([$email]);
    $user = $stmt->fetch();
    if ($user && password_verify($_POST['password'] ?? '', $user['password_hash'])) {
        current_user();
        session_regenerate_id(true);
        $_SESSION['user'] = ['id' => $user['id'], 'name' => $user['name'], 'email' => $user['email']];
        header('Location: /panel.php');
        exit;
    }
    http_response_code(401);
    $error = 'Correo o contraseña incorrectos.';
}

$alert = $error ? '<div class="notification is-danger is-light">' . e($error) . '</div>' : '';
render('Iniciar sesión', '<div class="columns is-centered"><div class="column is-5"><div class="box">
  <h1 class="title is-4">Iniciar sesión</h1>' . $alert . '
  <form method="post">
    <div class="field"><input class="input" type="email" name="email" placeholder="Correo" required maxlength="120"></div>
    <div class="field"><input class="input" type="password" name="password" placeholder="Contraseña" required></div>
    <button class="button is-success is-fullwidth">Entrar</button>
  </form></div></div></div>');
