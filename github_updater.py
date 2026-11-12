import json
import base64
import time
import requests
from urllib.parse import quote

API_URL = "https://api.github.com"


# ============================================================
# Headers
# ============================================================
def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _file_url(repo: str, file_path: str) -> str:
    return f"{API_URL}/repos/{repo}/contents/{quote(file_path, safe='/')}"


# ============================================================
# Branch + SHA helpers
# ============================================================
def get_default_branch(repo: str, token: str, fallback: str = "main") -> str:
    """Repo এর default branch ফেচ করে। Fallback: 'main'."""
    try:
        r = requests.get(
            f"{API_URL}/repos/{repo}",
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("default_branch") or fallback
    except Exception:
        pass
    return fallback


def _get_sha(repo: str, file_path: str, branch: str, token: str):
    """File এর sha রিটার্ন করে, না থাকলে None."""
    try:
        r = requests.get(
            _file_url(repo, file_path),
            headers=_headers(token),
            params={"ref": branch},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("sha")
        return None
    except Exception:
        return None


# ============================================================
# Token / Repo verification
# ============================================================
def verify_github_token(token: str):
    """Returns (ok, username_or_error)."""
    if not token:
        return False, "Empty token"
    try:
        r = requests.get(f"{API_URL}/user", headers=_headers(token), timeout=15)
        if r.status_code == 200:
            return True, r.json().get("login", "unknown")
        if r.status_code == 401:
            return False, "Invalid token (401 Unauthorized)"
        return False, f"HTTP {r.status_code}: {r.text[:150]}"
    except Exception as e:
        return False, str(e)


def check_repo_access(repo: str, token: str):
    """Returns (ok, default_branch_or_error)."""
    try:
        r = requests.get(
            f"{API_URL}/repos/{repo}",
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            branch = data.get("default_branch", "main")
            perms = data.get("permissions") or {}
            if perms and not perms.get("push", True):
                return False, "No push permission on this repo"
            return True, branch

        if r.status_code == 404:
            return False, "Repository not found (or no access)"
        if r.status_code == 401:
            return False, "Invalid GitHub token"
        if r.status_code == 403:
            return False, "Access forbidden (403)"
        return False, f"HTTP {r.status_code}: {r.text[:150]}"
    except Exception as e:
        return False, str(e)


# ============================================================
# URL parser
# ============================================================
def parse_repo_url(url: str):
    """
    Accept:
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git
      owner/repo
    """
    if not url:
        return None
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        url = url.replace(":", "/", 1).replace("git@", "")
    if "github.com/" in url:
        url = url.split("github.com/")[-1]
    parts = url.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


# ============================================================
# Main: update file on GitHub
# ============================================================
def update_token_file(repo: str, file_path: str, new_content,
                      github_token: str, branch: str = None,
                      max_retries: int = 3) -> dict:
    """
    Create or update a file in a GitHub repo.

    - repo       : "owner/name"
    - file_path  : "token_bd.json" or "data/tokens.json" (nested supported)
    - new_content: list / dict / any JSON-serializable
    - branch     : None → auto-detect default branch
    - max_retries: on 409/422/network errors, retries with backoff
    """
    # ---- validation ----
    if not repo or "/" not in repo:
        return {"ok": False, "repo": repo,
                "error": "Invalid repo format (expected owner/name)"}
    if not file_path:
        return {"ok": False, "repo": repo, "error": "Empty file_path"}
    if not github_token:
        return {"ok": False, "repo": repo, "error": "Missing GitHub token"}

    # ---- auto-detect branch ----
    if not branch:
        branch = get_default_branch(repo, github_token, "main")

    # ---- serialize ----
    try:
        json_bytes = json.dumps(new_content, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception as e:
        return {"ok": False, "repo": repo,
                "error": f"JSON serialization failed: {e}"}
    encoded = base64.b64encode(json_bytes).decode()

    last_error = "Unknown error"

    for attempt in range(max_retries):
        # ---- fetch current sha (if file exists) ----
        sha = _get_sha(repo, file_path, branch, github_token)

        payload = {
            "message": f"auto-update: refresh {file_path}",
            "content": encoded,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        # ---- PUT ----
        try:
            r = requests.put(
                _file_url(repo, file_path),
                headers=_headers(github_token),
                json=payload,
                timeout=30,
            )
        except Exception as e:
            last_error = f"Network error: {e}"
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "repo": repo, "error": last_error,
                    "branch": branch}

        # ---- success ----
        if r.status_code in (200, 201):
            try:
                new_sha = r.json().get("content", {}).get("sha")
            except Exception:
                new_sha = None
            return {"ok": True, "repo": repo, "branch": branch, "sha": new_sha}

        # ---- conflict (sha mismatch) → retry ----
        if r.status_code == 409:
            last_error = "SHA conflict (409), retrying"
            time.sleep(2 ** attempt)
            continue

        # ---- unprocessable (file exists but no sha, or bad branch) → retry ----
        if r.status_code == 422:
            last_error = "Unprocessable (422): likely file exists without sha or bad branch"
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue

        # ---- other errors ----
        try:
            err_json = r.json()
            err_msg = err_json.get("message", r.text[:200])
        except Exception:
            err_msg = r.text[:200]

        return {
            "ok": False,
            "repo": repo,
            "error": err_msg,
            "status": r.status_code,
            "branch": branch,
        }

    return {
        "ok": False,
        "repo": repo,
        "error": last_error,
        "status": 409,
        "branch": branch,
    }