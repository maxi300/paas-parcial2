<?php
require_once __DIR__ . '/lib/db.php';

header('Content-Type: application/json');
try {
    $dbVersion = 'MySQL ' . db()->query('SELECT VERSION()')->fetchColumn();
} catch (Throwable $ex) {
    $dbVersion = 'sin conexión';
}
echo json_encode([
    'cliente' => env('APP_NAME', 'Beta POS'),
    'tenant' => env('TENANT', 'pos-ventas'),
    'entorno' => env('APP_ENV', 'desarrollo'),
    'runtime' => 'PHP ' . PHP_VERSION,
    'servidor_web' => $_SERVER['SERVER_SOFTWARE'] ?? 'Apache',
    'base_de_datos' => $dbVersion,
    'db_host' => env('DB_HOST', 'localhost'),
    'pod' => gethostname(),
], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
