<?php
require_once __DIR__ . '/lib/layout.php';

try {
    $dishes = db()->query('SELECT name, category, price FROM dishes ORDER BY category, id')->fetchAll();
} catch (Throwable $ex) {
    http_response_code(503);
    render('Iniciando', '<div class="notification is-warning">Conectando con la base de datos…</div>');
    exit;
}

$cards = '';
foreach ($dishes as $d) {
    $cards .= '<div class="column is-3"><div class="card"><div class="card-content">
        <span class="tag is-success is-light mb-2">' . e($d['category']) . '</span>
        <p class="title is-6">' . e($d['name']) . '</p>
        <p class="subtitle is-5 has-text-weight-bold">$' . number_format((float) $d['price'], 2) . '</p>
    </div></div></div>';
}
render('Menú', '<h1 class="title">Menú del día</h1><div class="columns is-multiline">' . $cards . '</div>');
