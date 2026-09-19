#!/usr/bin/env python3
"""
watch.py — flop-labs と flop.finance の変更を検知して GitHub Issue を立てる。

標準ライブラリのみ。GitHub Actions から1時間ごとに実行する想定。

  python watch.py --seed     初回。現状を記録するだけで Issue は立てない
  python watch.py            通常実行。変化があれば Issue を1本立てる
  python watch.py --dry-run  Issue を立てず、検知内容を標準出力に出す

環境変数:
  GITHUB_TOKEN        Actions が自動で渡す
  GITHUB_REPOSITORY   Issue を立てる先（"owner/repo"）。Actions が自動で渡す
"""

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
UA = "flop-watch (https://github.com/kamils333)"


# ───────────────────────────────── HTTP

def fetch(url, token=None, accept="application/vnd.github+json", retries=3):
    """(status, body_text) を返す。例外は投げない。"""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", accept)
        if token and "api.github.com" in url:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            # レート制限は待って再試行する価値がある
            if e.code in (403, 429) and attempt < retries:
                time.sleep(5 * attempt)
                continue
            return e.code, body
        except Exception as e:
            if attempt < retries:
                time.sleep(3 * attempt)
                continue
            return 0, f"{type(e).__name__}: {e}"
    return 0, "unreachable"


def api(path, token, **params):
    url = "https://api.github.com" + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    status, body = fetch(url, token)
    if status != 200:
        print(f"  ! API {status} {path}: {body[:160]}", file=sys.stderr)
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# ───────────────────────────────── ページ本文の抽出

def page_text(raw_html):
    """タグを落として本文だけにする。ナンスや装飾の揺れで誤検知しないように。"""
    t = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw_html)
    t = re.sub(r"(?is)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def snap_path(url):
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    return os.path.join(SNAP, key + ".txt")


def read_snap(url):
    p = snap_path(url)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def write_snap(url, text):
    os.makedirs(SNAP, exist_ok=True)
    with open(snap_path(url), "w", encoding="utf-8") as f:
        f.write(text)


def make_diff(old, new, context, cap):
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                  lineterm="", n=context))[2:]  # ヘッダ2行を落とす
    if len(d) > cap:
        d = d[:cap] + [f"... (残り {len(d) - cap} 行は省略)"]
    return d


# ───────────────────────────────── 状態

def load_state():
    p = os.path.join(HERE, "state.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(os.path.join(HERE, "state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False, sort_keys=True)


# ───────────────────────────────── 監視

def check_repos(cfg, st, token, findings):
    repos = api(f"/orgs/{cfg['org']}/repos", token,
                per_page=100, sort="created", direction="desc")
    if repos is None:
        return []
    names = [r["name"] for r in repos]
    known = set(st.get("repos", []))

    for r in repos:
        if r["name"] not in known and known:          # 初回は既知として扱う
            findings.append({
                "kind": "新しいリポジトリ",
                "weight": 3,
                "title": f"{cfg['org']}/{r['name']}",
                "body": [f"作成 {r['created_at'][:10]} · {r.get('description') or '(説明なし)'}",
                         r["html_url"]],
            })
    st["repos"] = names
    return repos


def check_releases(cfg, st, token, repos, findings):
    seen = st.setdefault("releases", {})
    for r in repos:
        name = r["name"]
        rel = api(f"/repos/{cfg['org']}/{name}/releases", token, per_page=5)
        if not rel:
            continue
        tag = rel[0].get("tag_name", "")
        # 既に知っているリポジトリで、タグが変わったときだけ報告する。
        # 初見のリポジトリは check_repos が「新しいリポジトリ」として報告済み。
        if name in seen and seen[name] != tag:
            latest = rel[0]
            findings.append({
                "kind": "新しいリリース",
                "weight": 3,
                "title": f"{cfg['org']}/{name} {tag}",
                "body": [latest.get("name") or "", (latest.get("body") or "")[:400],
                         latest.get("html_url", "")],
            })
        seen[name] = tag


def check_commits(cfg, st, token, findings):
    seen = st.setdefault("commits", {})
    for full in cfg["commit_watch"]:
        owner, name = full.split("/")
        cm = api(f"/repos/{owner}/{name}/commits", token, per_page=10)
        if not cm:
            continue
        last = seen.get(full)
        new = []
        for c in cm:
            if c["sha"] == last:
                break
            new.append(c)
        if new and last:
            findings.append({
                "kind": "新しいコミット",
                "weight": 2,
                "title": f"{full} — {len(new)} 件",
                "body": [f"- `{c['sha'][:7]}` {c['commit']['message'].splitlines()[0][:110]}"
                         for c in new[:10]],
            })
        seen[full] = cm[0]["sha"]


def check_issues(cfg, st, token, repos, findings):
    """運営（OWNER / MEMBER / COLLABORATOR）が関与した Issue だけを拾う。"""
    roles = set(cfg["operator_roles"])
    since = st.get("issues_since") or (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    first = "issues_since" not in st

    for r in repos:
        name = r["name"]

        # 運営が付けたコメント（リポジトリ単位で一括取得できる）
        cs = api(f"/repos/{cfg['org']}/{name}/issues/comments", token,
                 since=since, per_page=100, sort="created", direction="desc")
        for c in (cs or []):
            if c.get("author_association") in roles and not first:
                findings.append({
                    "kind": "運営コメント",
                    "weight": 3,
                    "title": f"{name} — @{c['user']['login']} ({c['author_association']})",
                    "body": [c["body"][:700].strip(), c["html_url"]],
                })

        # 運営が立てた Issue
        iss = api(f"/repos/{cfg['org']}/{name}/issues", token,
                  since=since, state="all", per_page=100)
        for i in (iss or []):
            if "pull_request" in i:
                continue
            if i.get("author_association") in roles and not first:
                findings.append({
                    "kind": "運営が立てた Issue",
                    "weight": 3,
                    "title": f"{name} #{i['number']} — {i['title'][:90]}",
                    "body": [(i.get("body") or "")[:700].strip(), i["html_url"]],
                })

    st["issues_since"] = datetime.now(timezone.utc).replace(microsecond=0)\
        .isoformat().replace("+00:00", "Z")


def check_pages(cfg, st, findings):
    meta = st.setdefault("pages", {})
    for url in cfg["pages"]:
        status, raw = fetch(url, accept="text/html,text/plain,*/*")
        if status != 200:
            print(f"  ! ページ {status} {url}", file=sys.stderr)
            continue
        text = page_text(raw)
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        old_digest = meta.get(url, {}).get("sha")

        if old_digest and old_digest != digest:
            old = read_snap(url) or ""
            d = make_diff(old, text, cfg["diff_context_lines"],
                          cfg["max_diff_lines_per_page"])
            findings.append({
                "kind": "ページ更新",
                "weight": 3,
                "title": url,
                "body": [f"{len(old.splitlines())} → {len(text.splitlines())} 行 / "
                         f"`{old_digest}` → `{digest}`",
                         "```diff", *d, "```"],
            })
        meta[url] = {"sha": digest, "lines": len(text.splitlines())}
        write_snap(url, text)


# ───────────────────────────────── Issue を立てる

def open_issue(findings, token, dry):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    kinds = sorted({f["kind"] for f in findings})
    title = f"[flop] {' / '.join(kinds)} — {now}"

    lines = [f"検知時刻: {now}", ""]
    for f in sorted(findings, key=lambda x: -x["weight"]):
        lines.append(f"## {f['kind']}: {f['title']}")
        lines += [str(b) for b in f["body"] if str(b).strip()]
        lines.append("")
    lines.append("---")
    lines.append("自動検知。差分は保存済みスナップショットとの比較です。")
    body = "\n".join(lines)

    if dry:
        print("=" * 70)
        print(title)
        print("=" * 70)
        print(body[:4000])
        return

    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (repo and token):
        print("GITHUB_REPOSITORY / GITHUB_TOKEN が無いので Issue は立てません。", file=sys.stderr)
        print(title); print(body[:2000])
        return

    payload = json.dumps({"title": title[:250], "body": body[:60000]}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues",
                                 data=payload, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("Issue を作成しました:", json.load(r)["html_url"])
    except urllib.error.HTTPError as e:
        print("Issue の作成に失敗:", e.code, e.read().decode("utf-8", "replace")[:300],
              file=sys.stderr)
        sys.exit(1)


# ───────────────────────────────── main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="現状を記録するだけ")
    ap.add_argument("--dry-run", action="store_true", help="Issue を立てず出力する")
    args = ap.parse_args()

    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("警告: GITHUB_TOKEN が無いため未認証（60回/時）で動きます。", file=sys.stderr)

    st = {} if args.seed else load_state()
    findings = []

    repos = check_repos(cfg, st, token, findings)
    if repos:
        check_releases(cfg, st, token, repos, findings)
        check_issues(cfg, st, token, repos, findings)
    check_commits(cfg, st, token, findings)
    check_pages(cfg, st, findings)

    save_state(st)

    if args.seed:
        print(f"初期化しました。リポジトリ {len(st.get('repos', []))} 件、"
              f"ページ {len(st.get('pages', {}))} 件を記録。")
        return

    if not findings:
        print("変化なし。")
        return

    print(f"{len(findings)} 件の変化を検知しました。")
    open_issue(findings, token, args.dry_run)


if __name__ == "__main__":
    main()
