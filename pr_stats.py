#!/usr/bin/env python3
"""
GitHub PR Reviewer Statistics
사용법: python pr_stats.py [--token xxx] [--state closed] [--max-prs 200] [--port 0]

흐름:
  1. 로컬 서버 시작 → 브라우저 즉시 열림
  2. 브라우저에서 저장소 추가 + 팀원 선택 + "분석 시작"
  3. PR 목록을 빠르게 스캔 → 선택된 팀원이 리뷰한 PR만 상세 수집
  4. SSE 로 실시간 스트리밍 → 차트 점진 업데이트
"""

import os
import sys
import io
import json
import argparse
import subprocess
import threading
import urllib.request
import urllib.parse
import urllib.error
import socket
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "pr_stats_config.json")


# ──────────────────────────────────────────────
# Proxy
# ──────────────────────────────────────────────

def setup_proxy():
    """git config 의 http.proxy 를 읽어 urllib 환경변수에 적용."""
    # 이미 환경변수로 설정된 경우 그대로 사용
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        print(f"[프록시] 환경변수: {os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')}")
        return
    for key in ("http.proxy", "https.proxy"):
        try:
            r = subprocess.run(
                ["git", "config", "--global", key],
                capture_output=True, text=True, timeout=5,
            )
            proxy = r.stdout.strip()
            if proxy:
                os.environ["HTTPS_PROXY"] = proxy
                os.environ["HTTP_PROXY"]  = proxy
                print(f"[프록시] git config {key}: {proxy}")
                return
        except Exception:
            pass


# ──────────────────────────────────────────────
# Token
# ──────────────────────────────────────────────

def resolve_token(token_arg):
    if token_arg:
        return token_arg
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        t = r.stdout.strip()
        if t:
            print("[토큰] gh CLI")
            return t
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                t = line[len("password="):].strip()
                if t:
                    print("[토큰] git credential")
                    return t
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────
# GitHub Client
# ──────────────────────────────────────────────

class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token):
        self.token = token
        self.req_count = 0

    def get(self, path, params=None):
        self.req_count += 1
        url = f"{self.BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.mockingbird-preview+json",
            "User-Agent": "pr-stats",
        })
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("[ERROR] 토큰 인증 실패")
                sys.exit(1)
            raise

    def paginate(self, path, params=None, max_items=None, stop_before=None):
        """stop_before: datetime — 이 시각보다 오래된 항목이 나오면 조기 종료."""
        params = dict(params or {})
        params["per_page"] = 100
        items, page = [], 1
        while True:
            params["page"] = page
            data = self.get(path, params)
            if not data:
                break
            if stop_before:
                filtered = []
                for item in data:
                    if parse_dt(item.get("created_at")) >= stop_before:
                        filtered.append(item)
                    else:
                        items.extend(filtered)
                        return items
                items.extend(filtered)
            else:
                items.extend(data)
                if max_items and len(items) >= max_items:
                    return items[:max_items]
            if len(data) < 100:
                break
            page += 1
        return items


# ──────────────────────────────────────────────
# Data Store  (thread-safe SSE queue)
# ──────────────────────────────────────────────

class DataStore:
    def __init__(self):
        self.prs   = []
        self.done  = False
        self.error = None
        self._cond = threading.Condition()

    def push(self, event, data):
        with self._cond:
            self.prs.append((event, data))
            self._cond.notify_all()

    def finish(self, data=None):
        with self._cond:
            self.prs.append(("done", data or {}))
            self.done = True
            self._cond.notify_all()

    def fail(self, msg):
        with self._cond:
            self.prs.append(("error", {"message": msg}))
            self.done = True
            self._cond.notify_all()

    def wait(self, since, timeout=30):
        with self._cond:
            self._cond.wait_for(lambda: len(self.prs) > since or self.done, timeout=timeout)

    def reset(self):
        with self._cond:
            self.prs   = []
            self.done  = False
            self.error = None
            self._cond.notify_all()


# ──────────────────────────────────────────────
# Fetch worker
# ──────────────────────────────────────────────

def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def iso(dt):
    return dt.isoformat() if dt else None


def fetch_worker(store, client, repos, state, max_prs,
                 target_users, since_str, until_str):
    try:
        since_dt = parse_dt(since_str + "T00:00:00Z") if since_str else None
        until_dt = parse_dt(until_str + "T23:59:59Z") if until_str else None

        all_relevant = []

        for repo_full in repos:
            owner, repo = repo_full.split("/", 1)

            # ── 1. PR 목록 (빠름)
            print(f"[1] {repo_full} PR 목록 가져오는 중 ...")
            try:
                if since_dt:
                    # 날짜 지정 시 max_prs 제한 없이 since_dt 이전 PR에서 자동 종료
                    prs_raw = client.paginate(
                        f"/repos/{owner}/{repo}/pulls",
                        {"state": state, "sort": "created", "direction": "desc"},
                        stop_before=since_dt,
                    )
                else:
                    prs_raw = client.paginate(
                        f"/repos/{owner}/{repo}/pulls",
                        {"state": state, "sort": "created", "direction": "desc"},
                        max_prs,
                    )
            except urllib.error.HTTPError as e:
                store.fail(f"저장소 접근 실패: {repo_full} (HTTP {e.code})")
                return

            # until 필터 (since는 paginate에서 처리됨)
            if until_dt:
                prs_raw = [
                    pr for pr in prs_raw
                    if parse_dt(pr["created_at"]) <= until_dt
                ]

            print(f"  → {len(prs_raw)}개 PR (날짜 필터 후)")
            store.push("meta", {"owner": owner, "repo": repo, "total": len(prs_raw)})

            # ── 2. reviews 빠른 스캔 → 타겟 유저 관련 PR만 추림
            print(f"[2] {repo_full} 리뷰 스캔 중 (타겟: {', '.join(target_users)}) ...")
            relevant = []
            for idx, pr in enumerate(prs_raw):
                num = pr["number"]
                try:
                    reviews = client.get(f"/repos/{owner}/{repo}/pulls/{num}/reviews")
                except Exception:
                    reviews = []

                reviewers = {(r.get("user") or {}).get("login", "") for r in reviews}
                reviewers |= {r.get("login","") for r in pr.get("requested_reviewers",[])}

                if reviewers & target_users:
                    relevant.append((owner, repo, pr, reviews))

                if (idx + 1) % 20 == 0 or (idx + 1) == len(prs_raw):
                    print(f"  스캔 {idx+1}/{len(prs_raw)}, 관련 PR: {len(relevant)}개")

                store.push("scan_progress", {
                    "repo":     repo_full,
                    "scanned":  idx + 1,
                    "total":    len(prs_raw),
                    "relevant": len(relevant),
                })

            all_relevant.extend(relevant)

        print(f"[3] 관련 PR {len(all_relevant)}개 상세 수집 중 ...")
        store.push("relevant_total", {"total": len(all_relevant)})

        # ── 3. 관련 PR만 상세 수집 (timeline + detail)
        for idx, (owner, repo, pr, reviews) in enumerate(all_relevant):
            num    = pr["number"]
            author = pr["user"]["login"]

            try:
                detail = client.get(f"/repos/{owner}/{repo}/pulls/{num}")
            except Exception:
                detail = pr

            try:
                timeline = client.paginate(f"/repos/{owner}/{repo}/issues/{num}/timeline")
            except Exception:
                timeline = []

            # ── 리뷰어별 이벤트 수집 (timeline + reviews 합산)
            rev_raw = {}  # login -> [(t, type, state)]
            for ev in timeline:
                evt = ev.get("event")
                if evt in ("review_requested", "review_request_removed"):
                    req   = ev.get("requested_reviewer") or {}
                    login = req.get("login", "")
                    t     = parse_dt(ev.get("created_at"))
                    if login and login != author and t:
                        rev_raw.setdefault(login, []).append((t, evt, ""))

            comment_count = 0
            for rev in reviews:
                login   = (rev.get("user") or {}).get("login", "")
                state_r = rev.get("state", "")
                body    = rev.get("body", "") or ""
                t       = parse_dt(rev.get("submitted_at"))
                if not login or login == author or t is None:
                    continue
                if state_r == "COMMENTED" and body:
                    comment_count += 1
                rev_raw.setdefault(login, []).append((t, "review", state_r))

            # ── 시간순 정렬 후 요청-응답 쌍 구성
            requested_at  = {}  # login -> 마지막 요청 (참여율 계산용)
            first_reviewed = {}
            first_approved = {}
            pairs_by_login = {}  # login -> [{requested_at, reviewed_at}]

            for login, events in rev_raw.items():
                events.sort(key=lambda x: x[0])
                cur_req = None
                for t, etype, state in events:
                    if etype == "review_requested":
                        cur_req = t
                        if login not in requested_at or t > requested_at[login]:
                            requested_at[login] = t
                    elif etype == "review_request_removed":
                        cur_req = None
                    elif etype == "review":
                        if login not in first_reviewed:
                            first_reviewed[login] = t
                        if state == "APPROVED" and login not in first_approved:
                            first_approved[login] = t
                        if cur_req is not None:
                            pairs_by_login.setdefault(login, []).append({
                                "requested_at": iso(cur_req),
                                "reviewed_at":  iso(t),
                            })
                            cur_req = None  # 응답 후 다음 재요청을 기다림

            all_logins = set(requested_at) | set(first_reviewed)
            reviewer_events = [
                {
                    "login":             l,
                    "requested_at":      iso(requested_at.get(l)),
                    "first_reviewed_at": iso(first_reviewed.get(l)),
                    "first_approved_at": iso(first_approved.get(l)),
                    "req_resp_pairs":    pairs_by_login.get(l, []),
                }
                for l in all_logins
            ]

            store.push("pr", {
                "repo":            f"{owner}/{repo}",
                "number":          num,
                "title":           pr["title"],
                "author":          author,
                "created_at":      pr["created_at"],
                "merged_at":       pr.get("merged_at"),
                "additions":       detail.get("additions", 0),
                "deletions":       detail.get("deletions", 0),
                "changed_files":   detail.get("changed_files", 0),
                "commits":         detail.get("commits", 0),
                "comment_count":   comment_count,
                "reviewer_events": reviewer_events,
            })

            if (idx + 1) % 5 == 0 or (idx + 1) == len(all_relevant):
                print(f"  상세 {idx+1}/{len(all_relevant)} (API: {client.req_count})")

        store.finish({"total": len(all_relevant), "api_count": client.req_count})
        print(f"\n[완료] 총 API 요청: {client.req_count}회")

    except Exception as e:
        store.fail(str(e))
        raise


# ──────────────────────────────────────────────
# App state (shared across requests)
# ──────────────────────────────────────────────

APP = {
    "state":   "closed",
    "max_prs": 200,
    "token":   "",
    "store":   None,
}


# ──────────────────────────────────────────────
# HTTP Handler
# ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
                self._serve(200, "text/html; charset=utf-8", f.read().encode())
        elif self.path == "/stream":
            self._sse()
        elif self.path == "/config":
            self._get_config()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/analyze":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            self._start_analysis(body)
        elif self.path == "/add-repo":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            self._add_repo(body)
        elif self.path == "/config":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            self._save_config(body)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Config load / save
    def _get_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self._serve(200, "application/json", f.read().encode("utf-8"))
        except FileNotFoundError:
            self._serve(200, "application/json", b"{}")

    def _save_config(self, body):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=2)
            self._serve(200, "application/json", b'{"ok":true}')
        except Exception as e:
            self._serve(500, "application/json", json.dumps({"error": str(e)}).encode())

    # ── Add repo (fetch collaborators)
    def _add_repo(self, body):
        repo_full = body.get("repo", "").strip()
        if not repo_full or "/" not in repo_full:
            self._serve(400, "application/json", b'{"error":"invalid repo"}')
            return
        owner, repo = repo_full.split("/", 1)
        client = GitHubClient(APP["token"])
        collabs = fetch_collaborators(client, owner, repo)
        result = json.dumps({"collaborators": collabs}, ensure_ascii=False).encode()
        self._serve(200, "application/json", result)

    # ── Start / restart analysis
    def _start_analysis(self, body):
        repos      = [r.strip() for r in body.get("repos", []) if r.strip()]
        users      = set(u.strip() for u in body.get("users", []) if u.strip())
        since_str  = body.get("since", "")
        until_str  = body.get("until", "")

        if not repos:
            self._serve(400, "application/json", b'{"error":"repos required"}')
            return
        if not users:
            self._serve(400, "application/json", b'{"error":"users required"}')
            return

        store = DataStore()
        APP["store"] = store

        client = GitHubClient(APP["token"])
        worker = threading.Thread(
            target=fetch_worker,
            args=(store, client, repos,
                  APP["state"], APP["max_prs"],
                  users, since_str, until_str),
            daemon=True,
        )
        worker.start()

        self._serve(200, "application/json", b'{"ok":true}')

    # ── SSE stream
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        store = APP.get("store")
        if store is None:
            # 아직 분석 시작 전
            msg = 'event: waiting\ndata: {}\n\n'
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass
            return

        sent = 0

        def send(event, data):
            msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()

        try:
            while True:
                store.wait(sent, timeout=30)
                events = store.prs[sent:]
                for (ev, data) in events:
                    send(ev, data)
                    sent += 1
                if store.done and sent >= len(store.prs):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

# (HTML은 index.html 참조)


def fetch_collaborators(client, owner, repo):
    """저장소 collaborator 목록을 가져옵니다. 실패 시 빈 리스트 반환."""
    try:
        members = client.paginate(f"/repos/{owner}/{repo}/collaborators")
        logins = sorted(m["login"] for m in members if m.get("login"))
        print(f"[팀원] collaborator {len(logins)}명: {', '.join(logins)}")
        return logins
    except urllib.error.HTTPError as e:
        # 403: 권한 없음 (외부 contributor 계정 등)
        # org repo는 /orgs/{org}/members 로 시도
        if e.code == 403:
            org = owner
            try:
                members = client.paginate(f"/orgs/{org}/members")
                logins = sorted(m["login"] for m in members if m.get("login"))
                print(f"[팀원] org member {len(logins)}명: {', '.join(logins)}")
                return logins
            except Exception:
                pass
        print(f"[팀원] collaborator 조회 실패 (HTTP {e.code}). --users 옵션으로 직접 지정하세요.")
        return []
    except Exception as e:
        print(f"[팀원] collaborator 조회 오류: {e}")
        return []


def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def parse_args():
    p = argparse.ArgumentParser(
        description="GitHub PR Reviewer Statistics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python pr_stats.py
  python pr_stats.py --state all --max-prs 500
""")
    p.add_argument("--token",   default=None)
    p.add_argument("--state",   default="closed", choices=["open","closed","all"])
    p.add_argument("--max-prs", default=200, type=int)
    p.add_argument("--port",    default=0, type=int)
    return p.parse_args()


def main():
    args = parse_args()

    setup_proxy()
    token = resolve_token(args.token)
    if not token:
        print("[ERROR] GitHub token을 찾을 수 없습니다.")
        print("  방법 1: GITHUB_TOKEN 환경변수")
        print("  방법 2: --token ghp_xxx")
        print("  방법 3: gh auth login")
        sys.exit(1)

    APP.update({
        "repos":   [],
        "state":   args.state,
        "max_prs": args.max_prs,
        "token":   token,
        "store":   None,
    })

    port = args.port if args.port else find_free_port()
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"[서버] {url}  (종료: Ctrl+C)")

    import webbrowser
    t = threading.Timer(0.4, lambda: (
        os.startfile(url) if sys.platform == "win32" else webbrowser.open(url)
    ))
    t.daemon = True
    t.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[종료]")
        server.shutdown()


if __name__ == "__main__":
    main()
