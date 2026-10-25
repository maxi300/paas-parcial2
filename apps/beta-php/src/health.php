<?php
// Liveness: responde si Apache + PHP están vivos. ?ready=1 también verifica la base de datos (readiness).
require_once __DIR__ . '/lib/db.php';

header('Content-Type: application/json');
if (isset($_GET['ready'])) {
    try {
        db()->query('SELECT 1');
        echo json_encode(['status' => 'ready', 'db' => 'ok']);
    } catch (Throwable $ex) {
        http_response_code(503);
        echo json_encode(['status' => 'not-ready', 'db' => $ex->getMessage()]);
    }
    exit;
}
echo json_encode(['status' => 'ok']);
