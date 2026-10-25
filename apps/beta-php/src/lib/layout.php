<?php
require_once __DIR__ . '/db.php';

function render(string $title, string $body): void
{
    $user = current_user();
    $app = e(env('APP_NAME', 'Beta POS'));
    $color = e(env('BRAND_COLOR', '#16a34a'));
    $tenant = e(env('TENANT', 'pos-ventas'));
    $entorno = e(env('APP_ENV', 'desarrollo'));
    $pod = e(gethostname());
    $nav = $user
        ? '<a class="button is-light is-small" href="/panel.php">' . e($user['name']) . '</a>
           <a class="button is-white is-outlined is-small" href="/logout.php">Salir</a>'
        : '<a class="button is-light is-small" href="/login.php">Iniciar sesión</a>
           <a class="button is-white is-outlined is-small" href="/register.php">Registrarse</a>';
    $t = e($title);
    echo <<<HTML
<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{$t} · {$app}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bulma@1.0.2/css/bulma.min.css">
<style>.brand{background:{$color}}</style>
</head><body>
<nav class="navbar brand is-spaced"><div class="container">
  <div class="navbar-brand"><a class="navbar-item has-text-white has-text-weight-bold" href="/">{$app}
    <span class="tag is-light ml-2">PHP + MySQL</span></a></div>
  <div class="navbar-end"><div class="navbar-item"><div class="buttons">{$nav}</div></div></div>
</div></nav>
<section class="section"><div class="container">{$body}</div></section>
<footer class="footer py-4"><div class="container is-size-7 has-text-grey">
  Tenant <b>{$tenant}</b> · entorno {$entorno} · pod <code>{$pod}</code> · <a href="/info.php">/info.php</a>
</div></footer>
</body></html>
HTML;
}
