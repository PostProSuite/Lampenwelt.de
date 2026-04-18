<?php
/**
 * PostPro Suite — SQLite Database Layer
 * Users, Run History, Updates
 */

require_once __DIR__ . '/../config/config.php';

class Database {
    private static ?PDO $pdo = null;

    public static function get(): PDO {
        if (self::$pdo === null) {
            $dir = dirname(DB_PATH);
            if (!is_dir($dir)) mkdir($dir, 0755, true);

            self::$pdo = new PDO('sqlite:' . DB_PATH, null, null, [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            ]);
            self::$pdo->exec("PRAGMA journal_mode=WAL");
            self::init();
        }
        return self::$pdo;
    }

    private static function init(): void {
        self::$pdo->exec("
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL,
                last_login TEXT
            );

            CREATE TABLE IF NOT EXISTS run_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                workflow_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                exit_code INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                seen_by TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                user_email TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                ticket_key TEXT,
                file_size INTEGER,
                created_at TEXT NOT NULL
            );
        ");
    }

    // ── Users ──

    public static function upsertUser(string $email, string $name): array {
        $db = self::get();
        $now = date('c');
        $role = (strtolower($email) === ADMIN_EMAIL) ? 'admin' : 'user';

        $existing = $db->prepare("SELECT * FROM users WHERE email = ?");
        $existing->execute([$email]);
        $user = $existing->fetch();

        if ($user) {
            $db->prepare("UPDATE users SET name=?, last_login=? WHERE email=?")
               ->execute([$name, $now, $email]);
            $role = $user['role']; // Keep existing role
        } else {
            $db->prepare("INSERT INTO users (email, name, role, created_at, last_login) VALUES (?, ?, ?, ?, ?)")
               ->execute([$email, $name, $role, $now, $now]);
        }

        $stmt = $db->prepare("SELECT * FROM users WHERE email = ?");
        $stmt->execute([$email]);
        return $stmt->fetch();
    }

    public static function getAllUsers(): array {
        return self::get()->query("SELECT * FROM users ORDER BY last_login DESC")->fetchAll();
    }

    public static function updateUserRole(int $userId, string $role): void {
        self::get()->prepare("UPDATE users SET role=? WHERE id=?")->execute([$role, $userId]);
    }

    // ── Run History ──

    public static function addRun(string $email, int $workflowId, string $title): int {
        $db = self::get();
        $db->prepare("INSERT INTO run_history (user_email, workflow_id, title, status, started_at) VALUES (?, ?, ?, 'running', ?)")
           ->execute([$email, $workflowId, $title, date('c')]);
        return (int)$db->lastInsertId();
    }

    public static function finishRun(int $runId, string $status, int $exitCode): void {
        self::get()->prepare("UPDATE run_history SET status=?, exit_code=?, finished_at=? WHERE id=?")
                    ->execute([$status, $exitCode, date('c'), $runId]);
    }

    public static function getHistory(?string $email = null, int $limit = 50): array {
        $db = self::get();
        if ($email) {
            $stmt = $db->prepare("SELECT * FROM run_history WHERE user_email=? ORDER BY started_at DESC LIMIT ?");
            $stmt->execute([$email, $limit]);
        } else {
            $stmt = $db->prepare("SELECT * FROM run_history ORDER BY started_at DESC LIMIT ?");
            $stmt->execute([$limit]);
        }
        return $stmt->fetchAll();
    }

    // ── Updates ──

    public static function createUpdate(string $version, string $message): int {
        $db = self::get();
        $db->prepare("INSERT INTO updates (version, message, created_at) VALUES (?, ?, ?)")
           ->execute([$version, $message, date('c')]);
        return (int)$db->lastInsertId();
    }

    public static function getUpdates(): array {
        return self::get()->query("SELECT * FROM updates ORDER BY created_at DESC LIMIT 20")->fetchAll();
    }

    public static function getUnseenCount(string $email): int {
        $rows = self::get()->query("SELECT seen_by FROM updates")->fetchAll();
        $count = 0;
        foreach ($rows as $r) {
            $seen = json_decode($r['seen_by'], true) ?: [];
            if (!in_array($email, $seen)) $count++;
        }
        return $count;
    }

    public static function markUpdateSeen(int $updateId, string $email): void {
        $db = self::get();
        $stmt = $db->prepare("SELECT seen_by FROM updates WHERE id=?");
        $stmt->execute([$updateId]);
        $row = $stmt->fetch();
        if ($row) {
            $seen = json_decode($row['seen_by'], true) ?: [];
            if (!in_array($email, $seen)) {
                $seen[] = $email;
                $db->prepare("UPDATE updates SET seen_by=? WHERE id=?")->execute([json_encode($seen), $updateId]);
            }
        }
    }

    // ── Exports ──

    public static function recordExport(?int $runId, string $email, string $filename, string $filePath, ?string $ticketKey = null): int {
        $db = self::get();
        $size = file_exists($filePath) ? filesize($filePath) : 0;
        $db->prepare("INSERT INTO exports (run_id, user_email, filename, file_path, ticket_key, file_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
           ->execute([$runId, $email, $filename, $filePath, $ticketKey, $size, date('c')]);
        return (int)$db->lastInsertId();
    }

    public static function getExports(?string $email = null, int $limit = 100): array {
        $db = self::get();
        if ($email) {
            $stmt = $db->prepare("SELECT * FROM exports WHERE user_email=? ORDER BY created_at DESC LIMIT ?");
            $stmt->execute([$email, $limit]);
        } else {
            $stmt = $db->prepare("SELECT * FROM exports ORDER BY created_at DESC LIMIT ?");
            $stmt->execute([$limit]);
        }
        return $stmt->fetchAll();
    }

    public static function getExportsByTicket(string $ticketKey): array {
        $db = self::get();
        $stmt = $db->prepare("SELECT * FROM exports WHERE ticket_key=? ORDER BY created_at DESC");
        $stmt->execute([$ticketKey]);
        return $stmt->fetchAll();
    }
}
