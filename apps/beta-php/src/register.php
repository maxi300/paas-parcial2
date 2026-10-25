<?php
require_once __DIR__ . '/lib/layout.php';

$error = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $name = trim($_POST['name'] ?? '');
    $email = strtolower(trim($_POST['email'] ?? ''));
    $pass = $_POST['password'] ?? '';
    if ($name === '' || !filter_var($email, FILTER_VALIDATE_EMAIL) || strlen($pass) < 6) {
        $error = 'Completa todos los campos (contraseña mínima de 6 caracteres).';
    } else {
        try {
            $stmt = db()->prepare('INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)');
            $stmt->execute([mb_substr($name, 0, 80), $email, password_hash($pass, PASSWORD_DEFAULT)]);
            current_user();
            session_regenerate_id(true);
            $_SESSION['user'] = ['id' => (int) db()->lastInsertId(), 'name' => $name, 'email' => $email];
            header('Location: /panel.php');
            exit;
        } catch (PDOException $ex) {
            $error = $ex->getCode() === '23000' ? 'Ese correo ya está registrado.' : 'No se pudo crear la cuenta.';
        }
    }
    http_response_code(400);
}

$alert = $error ? '<div class="notification is-danger is-light">' . e($error) . '</div>' : '';
render('Registro', '<div class="columns is-centered"><div class="column is-5"><div class="box">
  <h1 class="title is-4">Crear cuenta</h1>' . $alert . '
  <form method="post">
    <div class="field"><input class="input" name="name" placeholder="Nombre" required maxlength="80"></div>
    <div class="field"><input class="input" type="email" name="email" placeholder="Correo" required maxlength="120"></div>
    <div class="field"><input class="input" type="password" name="password" placeholder="Contraseña" required minlength="6"></div>
    <button class="button is-success is-fullwidth">Registrarme</button>
  </form></div></div></div>');
