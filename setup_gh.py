# -*- coding: utf-8 -*-
"""Настройка GitHub-репозитория для рассылки: секреты + публичный доступ + запуск.

Секреты нигде не печатаются. Токен берётся из сохранённых учётных данных Git.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

OWNER, REPO = "tonvpn1234-alt", "tg-outreach"


def get_token():
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def api(token, method, path, payload=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}"), dict(e.headers)


def read_env():
    """Достаёт ключи из .env (API_ID = ..., API_HASH = "...")."""
    vals = {}
    with open(".env", "r", encoding="utf-8-sig") as f:
        for line in f:
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"')
    return vals


def get_session_string():
    out = subprocess.run([sys.executable, "main.py", "--export-session"],
                         capture_output=True, text=True, timeout=120)
    lines = [l.strip() for l in out.stdout.splitlines()
             if l.strip() and not l.strip().startswith("[")]
    return lines[-1] if lines else None


def put_secret(token, key_info, name, value):
    status, key, _ = api(token, "GET", f"/repos/{OWNER}/{REPO}/actions/secrets/public-key")
    if status != 200:
        print(f"  [-] {name}: не получил public-key ({status})")
        return False
    from nacl import encoding, public as nacl_public
    pk = nacl_public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    sealed = nacl_public.SealedBox(pk).encrypt(value.encode())
    b64 = base64.b64encode(sealed).decode()
    status, _, _ = api(token, "PUT",
                       f"/repos/{OWNER}/{REPO}/actions/secrets/{name}",
                       {"encrypted_value": b64, "key_id": key["key_id"]})
    print(f"  {'[+]' if status in (201, 204) else '[-]'} секрет {name}: HTTP {status}")
    return status in (201, 204)


def main():
    token = get_token()
    if not token:
        print("[-] Не нашёл сохранённые учётные данные GitHub (git credential fill пустой).")
        return 1
    print("[+] Токен из учётных данных Git получен")

    status, repo, headers = api(token, "GET", f"/repos/{OWNER}/{REPO}")
    if status != 200:
        print(f"[-] Репозиторий недоступен: HTTP {status} ({repo.get('message')})")
        return 1
    scopes = headers.get("X-OAuth-Scopes") or headers.get("x-oauth-scopes") or ""
    print(f"[+] Репозиторий {OWNER}/{REPO}: private={repo['private']} | scopes токена: {scopes}")
    has_repo_scope = "repo" in scopes.split(", ") if scopes else False
    has_workflow_scope = "workflow" in scopes.split(", ") if scopes else False

    # 1. Секреты
    env = read_env()
    print("[*] Заливаю секреты...")
    secrets = {
        "API_ID": env.get("API_ID", ""),
        "API_HASH": env.get("API_HASH", ""),
        "PHONE_NUMBER": env.get("PHONE_NUMBER", ""),
    }
    ss = get_session_string()
    if ss:
        secrets["SESSION_STRING"] = ss
        print(f"  [+] SESSION_STRING получен (длина {len(ss)})")
    else:
        print("  [!] SESSION_STRING не получен — проверьте вывод --export-session")
    ok = all(put_secret(token, env, name, value) for name, value in secrets.items() if value)

    # 2. Публичный доступ
    if repo["private"]:
        status, resp, _ = api(token, "PATCH", f"/repos/{OWNER}/{REPO}", {"private": False})
        print(f"  {'[+]' if status == 200 else '[-]'} Репозиторий теперь public: HTTP {status}")
        if status == 200:
            repo["private"] = False
    else:
        print("  [+] Репозиторий уже публичный")

    # 3. Запуск воркфлоу
    dispatched = False
    if "workflow" in scopes:
        status, resp, _ = api(token, "POST",
                              f"/repos/{OWNER}/{REPO}/actions/workflows/run.yml/dispatches",
                              {"ref": "main"})
        print(f"  {'[+]' if status == 204 else '[-]'} Запуск воркфлоу: HTTP {status}")
        dispatched = status == 204
    else:
        print("  [!] У токена нет scope 'workflow' — запустите вручную: Actions -> outreach -> Run workflow")

    if not dispatched:
        return 0 if ok else 1

    # 4. Ждём и смотрим статус первого запуска
    print("[*] Жду 60 сек и проверяю статус запуска...")
    time.sleep(60)
    status, runs, _ = api(token, "GET",
                          f"/repos/{OWNER}/{REPO}/actions/workflows/run.yml/runs?per_page=1")
    if status == 200 and runs.get("workflow_runs"):
        run = runs["workflow_runs"][0]
        print(f"[+] Запуск #{run['run_number']}: {run['status']} ({run.get('conclusion')})")
        print(f"    Логи: {run['html_url']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
