<?php
require_once __DIR__ . '/lib/db.php';

current_user();
$_SESSION = [];
session_destroy();
header('Location: /');
