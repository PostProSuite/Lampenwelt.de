<?php
/**
 * PostPro Suite — Authentication (JWT + SAML)
 *
 * Dev Mode:  Auto-login als Admin (kein SSO noetig)
 * Prod Mode: SAML SSO via Microsoft Entra ID
 */

require_once __DIR__ . '/../config/config.php';
require_once __DIR__ . '/database.php';

// ═══ JWT (einfach, kein externes Paket noetig) ═══

function jwt_base64url_encode(string $data): string {
    return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
}

function jwt_base64url_decode(string $data): string {
    return base64_decode(strtr($data, '-_', '+/'));
}

function create_token(string $email, string $name, string $role): string {
    $header = json_encode(['alg' => 'HS256', 'typ' => 'JWT']);
    $payload = json_encode([
        'email' => $email,
        'name'  => $name,
        'role'  => $role,
        'exp'   => time() + (JWT_EXPIRY_HOURS * 3600),
    ]);

    $segments = jwt_base64url_encode($header) . '.' . jwt_base64url_encode($payload);
    $signature = hash_hmac('sha256', $segments, JWT_SECRET, true);

    return $segments . '.' . jwt_base64url_encode($signature);
}

function decode_token(string $token): ?array {
    $parts = explode('.', $token);
    if (count($parts) !== 3) return null;

    [$header, $payload, $sig] = $parts;

    // Verify signature
    $expected = jwt_base64url_encode(
        hash_hmac('sha256', "$header.$payload", JWT_SECRET, true)
    );
    if (!hash_equals($expected, $sig)) return null;

    $data = json_decode(jwt_base64url_decode($payload), true);
    if (!$data) return null;

    // Check expiry
    if (isset($data['exp']) && $data['exp'] < time()) return null;

    return $data;
}

// ═══ SESSION / COOKIE ═══

function get_app_current_user(): ?array {
    // Check cookie first
    $token = $_COOKIE[COOKIE_NAME] ?? null;
    if ($token) {
        $payload = decode_token($token);
        if ($payload) {
            return [
                'email' => $payload['email'],
                'name'  => $payload['name'],
                'role'  => $payload['role'],
            ];
        }
    }

    // Dev mode: auto-login without cookie
    if (DEV_MODE) {
        return [
            'email' => ADMIN_EMAIL,
            'name'  => 'Gerry (Dev)',
            'role'  => 'admin',
        ];
    }

    return null;
}

function require_auth(): array {
    $user = get_app_current_user();
    if (!$user) {
        http_response_code(401);
        echo json_encode(['error' => 'Not authenticated']);
        exit;
    }
    return $user;
}

function require_admin(): array {
    $user = require_auth();
    if ($user['role'] !== 'admin') {
        http_response_code(403);
        echo json_encode(['error' => 'Admin access required']);
        exit;
    }
    return $user;
}

function login_user(string $email, string $name): void {
    $user = Database::upsertUser($email, $name);
    $token = create_token($user['email'], $user['name'], $user['role']);
    setcookie(COOKIE_NAME, $token, [
        'expires'  => time() + (JWT_EXPIRY_HOURS * 3600),
        'path'     => '/',
        'httponly'  => true,
        'samesite' => 'Lax',
    ]);
}

function logout_user(): void {
    setcookie(COOKIE_NAME, '', ['expires' => 1, 'path' => '/']);
}

function is_allowed_email(string $email): bool {
    $domain = strtolower(explode('@', $email)[1] ?? '');
    return in_array($domain, ALLOWED_DOMAINS);
}

// ═══ SAML HELPERS ═══

function get_saml_login_url(): string {
    if (!SAML_ENABLED || !SAML_SSO_URL) {
        return '/auth/login.php?dev=1';
    }

    // Build SAML AuthnRequest
    $id = '_' . bin2hex(random_bytes(16));
    $issueInstant = gmdate('Y-m-d\TH:i:s\Z');
    $acsUrl = get_base_url() . '/auth/saml-acs.php';

    $request = <<<XML
<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
    ID="{$id}"
    Version="2.0"
    IssueInstant="{$issueInstant}"
    Destination="{SAML_SSO_URL}"
    AssertionConsumerServiceURL="{$acsUrl}"
    ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">
    <saml:Issuer>{SAML_ENTITY_ID}</saml:Issuer>
    <samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" AllowCreate="true"/>
</samlp:AuthnRequest>
XML;

    $encoded = base64_encode(gzdeflate($request));
    $urlEncoded = urlencode($encoded);

    return SAML_SSO_URL . '?SAMLRequest=' . $urlEncoded;
}

function parse_saml_response(string $samlResponse): ?array {
    $xml = base64_decode($samlResponse);
    if (!$xml) return null;

    // Suppress warnings for potentially malformed XML
    libxml_use_internal_errors(true);
    $doc = new DOMDocument();
    if (!$doc->loadXML($xml)) return null;

    $xpath = new DOMXPath($doc);
    $xpath->registerNamespace('samlp', 'urn:oasis:names:tc:SAML:2.0:protocol');
    $xpath->registerNamespace('saml', 'urn:oasis:names:tc:SAML:2.0:assertion');

    // Check status
    $statusCode = $xpath->query('//samlp:StatusCode/@Value')->item(0);
    if ($statusCode && strpos($statusCode->nodeValue, 'Success') === false) {
        return null;
    }

    // Extract attributes
    $email = null;
    $name = null;

    // Try emailaddress claim
    $emailNodes = $xpath->query('//saml:Attribute[@Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"]/saml:AttributeValue');
    if ($emailNodes->length > 0) {
        $email = $emailNodes->item(0)->textContent;
    }

    // Fallback: NameID
    if (!$email) {
        $nameId = $xpath->query('//saml:NameID')->item(0);
        if ($nameId) $email = $nameId->textContent;
    }

    // Name
    $nameNodes = $xpath->query('//saml:Attribute[@Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"]/saml:AttributeValue');
    if ($nameNodes->length > 0) {
        $name = $nameNodes->item(0)->textContent;
    }

    // Fallback: displayname
    if (!$name) {
        $displayNodes = $xpath->query('//saml:Attribute[@Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"]/saml:AttributeValue');
        if ($displayNodes->length > 0) $name = $displayNodes->item(0)->textContent;
    }

    if (!$email) return null;

    return [
        'email' => strtolower(trim($email)),
        'name'  => $name ?: explode('@', $email)[0],
    ];
}

function get_base_url(): string {
    $scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
    return "{$scheme}://{$host}";
}
