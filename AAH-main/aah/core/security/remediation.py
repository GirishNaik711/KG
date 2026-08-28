#!/usr/bin/env python3
"""Remediation guidance database for common security findings.

Maps CWE IDs and tool-specific rule patterns to actionable fix guidance
including before/after code examples, reference links, and effort estimates.
"""

REMEDIATION_DB: dict[str, dict] = {
    "CWE-89": {
        "title": "SQL Injection",
        "fix": "Use parameterized queries instead of string formatting",
        "before": 'conn.execute(f"SELECT * FROM users WHERE id = {user_id}")',
        "after": 'conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))',
        "reference": "https://owasp.org/www-community/attacks/SQL_Injection",
        "effort": "small",
    },
    "CWE-798": {
        "title": "Hardcoded Credentials",
        "fix": "Move secrets to environment variables or a secrets manager",
        "before": 'API_KEY = "EXAMPLE-KEY-REPLACE-ME"',
        "after": 'API_KEY = os.environ["API_KEY"]',
        "reference": "https://cwe.mitre.org/data/definitions/798.html",
        "effort": "small",
    },
    "CWE-778": {
        "title": "Insufficient Logging",
        "fix": "Add logging to security-sensitive operations (auth, data access, deletion)",
        "before": "def login(email, password):\n    user = db.query(email, password)\n    return user",
        "after": "def login(email, password):\n    logger.info(f\"Login attempt for {email}\")\n    user = db.query(email, password)\n    logger.info(f\"Login {'success' if user else 'failed'} for {email}\")\n    return user",
        "reference": "https://cwe.mitre.org/data/definitions/778.html",
        "effort": "small",
    },
    "CWE-862": {
        "title": "Missing Authorization",
        "fix": "Add authorization checks before accessing resources",
        "before": "@app.get(\"/users/{user_id}\")\nasync def get_user(user_id: int):\n    return db.get_user(user_id)",
        "after": "@app.get(\"/users/{user_id}\")\nasync def get_user(user_id: int, current_user = Depends(get_current_user)):\n    if current_user.id != user_id and not current_user.is_admin:\n        raise HTTPException(403)\n    return db.get_user(user_id)",
        "reference": "https://cwe.mitre.org/data/definitions/862.html",
        "effort": "medium",
    },
    "CWE-79": {
        "title": "Cross-Site Scripting (XSS)",
        "fix": "Escape user input before rendering in HTML; use a templating engine with auto-escaping",
        "before": 'return f"<h1>Hello {name}</h1>"',
        "after": 'from markupsafe import escape\nreturn f"<h1>Hello {escape(name)}</h1>"',
        "reference": "https://owasp.org/www-community/attacks/xss/",
        "effort": "small",
    },
    "CWE-22": {
        "title": "Path Traversal",
        "fix": "Validate and sanitize file paths; use Path.resolve() and check against allowed directories",
        "before": 'path = base_dir / user_input\nreturn path.read_text()',
        "after": 'path = (base_dir / user_input).resolve()\nif not str(path).startswith(str(base_dir.resolve())):\n    raise ValueError("Path traversal attempt")\nreturn path.read_text()',
        "reference": "https://cwe.mitre.org/data/definitions/22.html",
        "effort": "small",
    },
    "CWE-327": {
        "title": "Use of Broken Cryptographic Algorithm",
        "fix": "Replace weak algorithms (MD5, SHA1, DES) with strong alternatives (SHA-256, AES-256-GCM)",
        "before": "import hashlib\nhash = hashlib.md5(password.encode()).hexdigest()",
        "after": "import hashlib\nhash = hashlib.sha256(password.encode()).hexdigest()\n# Better: use bcrypt or argon2 for passwords",
        "reference": "https://cwe.mitre.org/data/definitions/327.html",
        "effort": "small",
    },
    "CWE-502": {
        "title": "Deserialization of Untrusted Data",
        "fix": "Never deserialize untrusted data with pickle/yaml.load; use safe alternatives",
        "before": "import pickle\nobj = pickle.loads(user_data)",
        "after": "import json\nobj = json.loads(user_data)  # JSON is safe to deserialize",
        "reference": "https://cwe.mitre.org/data/definitions/502.html",
        "effort": "medium",
    },
    "CWE-918": {
        "title": "Server-Side Request Forgery (SSRF)",
        "fix": "Validate URLs against an allowlist; block internal/private IP ranges",
        "before": "response = requests.get(user_provided_url)",
        "after": "from urllib.parse import urlparse\nparsed = urlparse(user_provided_url)\nif parsed.hostname not in ALLOWED_HOSTS:\n    raise ValueError(\"URL not allowed\")\nresponse = requests.get(user_provided_url)",
        "reference": "https://owasp.org/www-community/attacks/Server_Side_Request_Forgery",
        "effort": "medium",
    },
    "CWE-611": {
        "title": "XML External Entity (XXE) Injection",
        "fix": "Disable external entity processing in XML parsers",
        "before": "from xml.etree.ElementTree import parse\ntree = parse(user_xml)",
        "after": "import defusedxml.ElementTree as ET\ntree = ET.parse(user_xml)",
        "reference": "https://cwe.mitre.org/data/definitions/611.html",
        "effort": "small",
    },
    "CWE-200": {
        "title": "Exposure of Sensitive Information",
        "fix": "Remove sensitive data from error messages, logs, and API responses",
        "before": 'raise HTTPException(500, detail=f"DB error: {str(e)}")',
        "after": 'logger.error(f"DB error: {e}")\nraise HTTPException(500, detail="Internal server error")',
        "reference": "https://cwe.mitre.org/data/definitions/200.html",
        "effort": "small",
    },
    "CWE-352": {
        "title": "Cross-Site Request Forgery (CSRF)",
        "fix": "Add CSRF tokens to state-changing requests; validate Origin/Referer headers",
        "before": "@app.post(\"/transfer\")\nasync def transfer(amount: int, to: str): ...",
        "after": "from fastapi_csrf_protect import CsrfProtect\n@app.post(\"/transfer\")\nasync def transfer(amount: int, to: str, csrf: CsrfProtect = Depends()): ...",
        "reference": "https://owasp.org/www-community/attacks/csrf",
        "effort": "medium",
    },
    "CWE-287": {
        "title": "Improper Authentication",
        "fix": "Use established auth libraries; enforce strong password policies; implement MFA",
        "before": "if user.password == submitted_password: ...",
        "after": "from passlib.hash import bcrypt\nif bcrypt.verify(submitted_password, user.password_hash): ...",
        "reference": "https://cwe.mitre.org/data/definitions/287.html",
        "effort": "medium",
    },
    "CWE-306": {
        "title": "Missing Authentication for Critical Function",
        "fix": "Add authentication middleware to all sensitive endpoints",
        "before": "@app.delete(\"/users/{id}\")\nasync def delete_user(id: int): ...",
        "after": "@app.delete(\"/users/{id}\")\nasync def delete_user(id: int, user = Depends(require_auth)): ...",
        "reference": "https://cwe.mitre.org/data/definitions/306.html",
        "effort": "medium",
    },
    "CWE-532": {
        "title": "Information Exposure Through Log Files",
        "fix": "Sanitize sensitive data before logging; never log passwords, tokens, or PII",
        "before": 'logger.info(f"User login: {email}, password: {password}")',
        "after": 'logger.info(f"User login attempt: {email}")',
        "reference": "https://cwe.mitre.org/data/definitions/532.html",
        "effort": "small",
    },
    "CWE-345": {
        "title": "Insufficient Verification of Data Authenticity",
        "fix": "Validate signatures, checksums, or MACs on data from external sources",
        "before": "token = request.headers['Authorization']\npayload = jwt.decode(token, options={'verify_signature': False})",
        "after": "token = request.headers['Authorization']\npayload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])",
        "reference": "https://cwe.mitre.org/data/definitions/345.html",
        "effort": "small",
    },
    "CWE-863": {
        "title": "Incorrect Authorization",
        "fix": "Verify resource ownership before granting access; don't rely on client-provided IDs alone",
        "before": "user = db.get_user(request.params['user_id'])",
        "after": "user = db.get_user(request.params['user_id'])\nif user.owner_id != current_user.id:\n    raise HTTPException(403, 'Forbidden')",
        "reference": "https://cwe.mitre.org/data/definitions/863.html",
        "effort": "small",
    },
}


def get_remediation(cwe: str, rule_id: str = "") -> dict:
    """Look up remediation guidance for a finding.

    Tries CWE first, then falls back to rule-pattern matching.
    Returns a dict with fix, before, after, reference, effort fields,
    or a generic fallback if no match found.
    """
    if cwe and cwe in REMEDIATION_DB:
        return REMEDIATION_DB[cwe]

    cwe_base = cwe.split(":")[0].strip() if cwe else ""
    if cwe_base and cwe_base in REMEDIATION_DB:
        return REMEDIATION_DB[cwe_base]

    if not cwe and rule_id:
        rule_lower = rule_id.lower()
        if "sql" in rule_lower or "injection" in rule_lower:
            return REMEDIATION_DB["CWE-89"]
        if "secret" in rule_lower or "credential" in rule_lower or "api-key" in rule_lower:
            return REMEDIATION_DB["CWE-798"]
        if "log" in rule_lower and "auth" in rule_lower:
            return REMEDIATION_DB["CWE-778"]
        if "authz" in rule_lower or "permission" in rule_lower or "login-required" in rule_lower:
            return REMEDIATION_DB["CWE-862"]
        if "xss" in rule_lower or "cross-site" in rule_lower:
            return REMEDIATION_DB["CWE-79"]
        if "path" in rule_lower and "traversal" in rule_lower:
            return REMEDIATION_DB["CWE-22"]
        if "deserializ" in rule_lower or "pickle" in rule_lower:
            return REMEDIATION_DB["CWE-502"]
        if "ssrf" in rule_lower:
            return REMEDIATION_DB["CWE-918"]

    ref = f"https://cwe.mitre.org/data/definitions/{cwe_base.replace('CWE-', '')}.html" if cwe_base else ""
    return {
        "title": cwe_base or "Security Finding",
        "fix": "Review the finding and apply the appropriate fix",
        "before": "",
        "after": "",
        "reference": ref,
        "effort": "medium",
    }
