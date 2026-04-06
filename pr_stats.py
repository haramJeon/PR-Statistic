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

    def paginate(self, path, params=None, max_items=None):
        params = dict(params or {})
        params["per_page"] = 100
        items, page = [], 1
        while True:
            params["page"] = page
            data = self.get(path, params)
            if not data:
                break
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
                prs_raw = client.paginate(
                    f"/repos/{owner}/{repo}/pulls",
                    {"state": state, "sort": "created", "direction": "desc"},
                    max_prs,
                )
            except urllib.error.HTTPError as e:
                store.fail(f"저장소 접근 실패: {repo_full} (HTTP {e.code})")
                return

            # 날짜 필터
            if since_dt or until_dt:
                prs_raw = [
                    pr for pr in prs_raw
                    if (not since_dt or parse_dt(pr["created_at"]) >= since_dt)
                    and (not until_dt or parse_dt(pr["created_at"]) <= until_dt)
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

            requested_at = {}
            for ev in timeline:
                if ev.get("event") == "review_requested":
                    req   = ev.get("requested_reviewer") or {}
                    login = req.get("login", "")
                    t     = parse_dt(ev.get("created_at"))
                    if login and login != author and t:
                        if login not in requested_at or t < requested_at[login]:
                            requested_at[login] = t

            first_reviewed = {}
            first_approved = {}
            comment_count  = 0
            for rev in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
                login   = (rev.get("user") or {}).get("login", "")
                state_r = rev.get("state", "")
                body    = rev.get("body","") or ""
                t       = parse_dt(rev.get("submitted_at"))
                if not login or login == author or t is None:
                    continue
                if state_r == "COMMENTED" and body:
                    comment_count += 1
                if login not in first_reviewed:
                    first_reviewed[login] = t
                if state_r == "APPROVED" and login not in first_approved:
                    first_approved[login] = t

            all_logins = set(requested_at) | set(first_reviewed)
            reviewer_events = [
                {
                    "login":             l,
                    "requested_at":      iso(requested_at.get(l)),
                    "first_reviewed_at": iso(first_reviewed.get(l)),
                    "first_approved_at": iso(first_approved.get(l)),
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
    "repos":   [],        # list of "owner/repo" strings
    "state":   "closed",
    "max_prs": 200,
    "token":   "",
    "store":   None,
    "worker":  None,
}


# ──────────────────────────────────────────────
# HTTP Handler
# ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve(200, "text/html; charset=utf-8", HTML_PAGE.encode())
        elif self.path == "/stream":
            self._sse()
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
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

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
        APP["worker"] = worker
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
# HTML
# ──────────────────────────────────────────────

HTML_PAGE = ""   # filled in main()

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PR Stats</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--side:#1a1d2e;--card:#1a1d2e;--border:#2a2d3e;
  --text:#e0e0e0;--muted:#888;--accent:#3498db;
  --green:#2ecc71;--orange:#e67e22;--red:#e74c3c;--purple:#9b59b6;--teal:#1abc9c;
}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:var(--bg);color:var(--text);display:flex;min-height:100vh}
#sidebar{width:270px;min-width:270px;background:var(--side);
  border-right:1px solid var(--border);padding:22px 16px;
  display:flex;flex-direction:column;gap:16px;
  position:sticky;top:0;height:100vh;overflow-y:auto}
#sidebar h2{font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:2px}
.sep{border:none;border-top:1px solid var(--border);margin:0}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:.75rem;color:var(--muted)}
input[type=text]{
  background:#0f1117;border:1px solid var(--border);color:var(--text);
  border-radius:6px;padding:6px 10px;font-size:.83rem;width:100%}
input:focus{outline:none;border-color:var(--accent)}
.row{display:flex;gap:6px}
.btn{flex:1;padding:7px 6px;border:1px solid var(--border);background:transparent;
     color:var(--text);border-radius:6px;font-size:.78rem;cursor:pointer;transition:.15s}
.btn:hover{background:#22263a}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.btn-primary:hover{background:#2980b9}
.btn-primary:disabled{background:#2a2d3e;border-color:#2a2d3e;color:#555;cursor:not-allowed}
#member-list{display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto}
.mitem{display:flex;align-items:center;gap:7px;padding:4px 7px;border-radius:5px;font-size:.83rem;cursor:pointer}
.mitem:hover{background:#22263a}
.mitem input{width:14px;height:14px;accent-color:var(--accent)}
#sel-chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px;min-height:0}
#sel-chips:empty{display:none}
.chip{display:inline-flex;align-items:center;gap:4px;padding:2px 8px 2px 9px;
  background:#22263a;border:1px solid var(--border);border-radius:99px;
  font-size:.73rem;color:var(--text);white-space:nowrap}
.chip-x{background:none;border:none;color:var(--muted);cursor:pointer;
  font-size:.8rem;line-height:1;padding:0;margin-left:1px}
.chip-x:hover{color:var(--red)}
#main{flex:1;padding:26px 30px;overflow-x:hidden;min-width:0}
#header{margin-bottom:18px}
#header h1{font-size:1.35rem;font-weight:700}
#subtitle{font-size:.82rem;color:var(--muted);margin-top:3px}
#progress-wrap{margin-bottom:18px}
#pbg{background:#2a2d3e;border-radius:99px;height:5px;overflow:hidden}
#pbar{height:100%;background:var(--accent);border-radius:99px;width:0;transition:width .4s}
#ptxt{font-size:.75rem;color:var(--muted);margin-top:5px}
#cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:13px;text-align:center}
.cval{font-size:1.45rem;font-weight:700}
.clbl{font-size:.7rem;color:var(--muted);margin-top:3px}
.csub{font-size:.67rem;color:#555;margin-top:2px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.full{grid-column:1/-1}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px}
.box{background:var(--card);border:1px solid var(--border);border-radius:9px;overflow:hidden}
#tbl{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:16px;margin-bottom:14px}
#tbl h2{font-size:.88rem;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:var(--bg);color:var(--muted);text-align:left;padding:6px 9px;
   border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:6px 9px;border-bottom:1px solid #1e2130}
tr:hover td{background:#1e2130}
.badge{display:inline-block;padding:1px 6px;border-radius:99px;font-size:.72rem;font-weight:600}
/* date picker */
.dp-wrap{position:relative}
.dp-input{cursor:pointer!important;caret-color:transparent}
.dp-cal{position:fixed;z-index:9999;background:#1a1d2e;border:1px solid var(--border);
  border-radius:9px;padding:12px;width:232px;box-shadow:0 8px 32px rgba(0,0,0,.6);
  display:none}
.dp-cal.open{display:block}
.dp-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.dp-nav{background:none;border:none;color:var(--muted);cursor:pointer;font-size:1.1rem;
  padding:2px 6px;line-height:1;border-radius:4px}
.dp-nav:hover{background:#22263a;color:var(--text)}
.dp-title{font-size:.82rem;font-weight:600;color:var(--text)}
.dp-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
.dp-dow{text-align:center;font-size:.68rem;color:var(--muted);padding:3px 0}
.dp-day{text-align:center;padding:5px 2px;border-radius:5px;font-size:.78rem;
  cursor:pointer;transition:.1s;color:var(--text)}
.dp-day:hover:not(.dp-empty):not(.dp-other){background:#22263a}
.dp-day.dp-today{color:var(--accent);font-weight:600}
.dp-day.dp-sel{background:var(--accent);color:#fff!important}
.dp-day.dp-other{color:#444;cursor:default}
.dp-day.dp-empty{cursor:default}
.dp-clear{margin-top:8px;text-align:right}
.dp-clear button{background:none;border:none;color:var(--muted);font-size:.72rem;
  cursor:pointer;padding:0}
.dp-clear button:hover{color:var(--text)}
/* initial state */
#initial{display:flex;align-items:center;justify-content:center;height:60vh;
         flex-direction:column;gap:12px;color:var(--muted);text-align:center}
#initial h2{font-size:1rem;color:var(--text)}
@media(max-width:900px){
  #sidebar{width:210px;min-width:210px}
  .grid2,.grid3{grid-template-columns:1fr}.full{grid-column:1}
}
</style>
</head>
<body>
<div id="sidebar">
  <div>
    <div style="font-size:.95rem;font-weight:700;color:var(--text)">PR Stats</div>
  </div>
  <hr class="sep">

  <div>
    <h2>저장소</h2>
    <div class="row" style="margin-top:6px">
      <input type="text" id="repo-input" placeholder="https://github.com/owner/repo.git"
        style="flex:1;background:#0f1117;border:1px solid var(--border);color:var(--text);
               border-radius:6px;padding:6px 10px;font-size:.83rem"
        onkeydown="if(event.key==='Enter')addRepo()">
      <button id="repo-add-btn" class="btn" onclick="addRepo()"
        style="flex:0 0 auto;padding:6px 12px;white-space:nowrap">추가</button>
    </div>
    <div id="repo-list" style="margin-top:6px;display:flex;flex-direction:column;gap:3px"></div>
  </div>
  <hr class="sep">

  <div>
    <h2>날짜 범위</h2>
    <div class="row" style="margin-top:6px">
      <div class="field"><label>시작일</label><input type="text" id="since" placeholder="YYYY-MM-DD" readonly class="dp-input"></div>
      <div class="field"><label>종료일</label><input type="text" id="until" placeholder="YYYY-MM-DD" readonly class="dp-input"></div>
    </div>
  </div>
  <hr class="sep">

  <div>
    <h2>팀원</h2>
    <input type="text" id="member-search" placeholder="검색..."
      style="width:100%;background:#0f1117;border:1px solid var(--border);color:var(--text);
             border-radius:6px;padding:5px 10px;font-size:.82rem;margin:6px 0"
      oninput="filterMembers(this.value)">
    <div class="row" style="margin-bottom:6px">
      <button class="btn" onclick="selAll(true)">전체</button>
      <button class="btn" onclick="selAll(false)">해제</button>
    </div>
    <div id="member-list"></div>
    <div id="sel-chips"></div>
  </div>
  <hr class="sep">

  <div style="margin-top:auto">
    <button class="btn btn-primary" id="btn-analyze" onclick="startAnalysis()"
      style="width:100%;height:auto">
      분석 시작
    </button>
    <div style="font-size:.68rem;color:#444;margin-top:8px" id="footer"></div>
  </div>
</div>

<div id="main">
  <div id="header">
    <h1>PR Reviewer Statistics</h1>
    <div id="subtitle"></div>
  </div>

  <div id="progress-wrap" style="display:none">
    <div id="pbg"><div id="pbar"></div></div>
    <div id="ptxt"></div>
  </div>

  <div id="initial">
    <h2>저장소를 추가하고 분석을 시작하세요</h2>
    <div style="font-size:.84rem">좌측에서 저장소 추가 → 팀원 선택 → <strong>분석 시작</strong></div>
  </div>

  <div id="results" style="display:none">
    <div id="cards"></div>
    <div class="grid2"><div class="box full"><div id="ch1"></div></div></div>
    <div class="grid2">
      <div class="box"><div id="ch2"></div></div>
      <div class="box"><div id="ch3"></div></div>
    </div>
    <div class="grid3">
      <div class="box"><div id="ch4"></div></div>
      <div class="box"><div id="ch5"></div></div>
      <div class="box"><div id="ch6"></div></div>
    </div>
    <div class="grid2">
      <div class="box"><div id="ch7"></div></div>
      <div class="box"><div id="ch8"></div></div>
    </div>
    <div class="grid2"><div class="box full"><div id="ch9"></div></div></div>
    <div id="tbl">
      <h2>리뷰어 상세 통계</h2>
      <table><thead><tr>
        <th>리뷰어</th><th>PR 생성</th><th>리뷰 참여</th><th>참여율</th>
        <th>평균 응답시간</th><th>평균 승인시간</th><th>중간값 응답시간</th><th>리뷰한 PR</th>
      </tr></thead><tbody id="tbody"></tbody></table>
    </div>
  </div>
</div>

<script>
// ── State
let RAW = [];
let IS_DONE = false;
let SCAN_TOTAL = 0;
let RELEVANT_TOTAL = 0;
let es = null;
let renderTimer = null;
let REPOS = [];

// ── Utils
const $ = id => document.getElementById(id);
const fmtH = h => {
  if (h==null||isNaN(h)) return 'N/A';
  if (h<1) return `${Math.round(h*60)}분`;
  if (h<24) return `${h.toFixed(1)}h`;
  return `${(h/24).toFixed(1)}일`;
};
const avg = a => a.length ? a.reduce((s,v)=>s+v,0)/a.length : null;
const median = a => { if(!a.length) return null; const s=[...a].sort((x,y)=>x-y); return s[Math.floor(s.length/2)]; };
const diffH = (a,b) => { if(!a||!b) return null; const d=(new Date(b)-new Date(a))/3600000; return d>=0?d:null; };

// ── Repo management
// REPOS: [{name, count}]
function parseRepoName(input) {
  const s = input.trim();
  // https://github.com/owner/repo.git  or  git@github.com:owner/repo.git
  const m = s.match(/github\.com[/:]([^/\s]+\/[^/\s.]+?)(?:\.git)?\s*$/i);
  if (m) return m[1];
  // plain owner/repo
  if (/^[^/\s]+\/[^/\s]+$/.test(s)) return s;
  return null;
}

function addRepo() {
  const inp = $('repo-input');
  const addBtn = $('repo-add-btn');
  const name = parseRepoName(inp.value);
  if (!name) { alert('GitHub clone URL 또는 owner/repo 형식으로 입력하세요.\n예) https://github.com/medit-desktop-app/applications.git'); return; }
  if (REPOS.find(r => r.name === name)) { inp.value = ''; return; }
  const val = name;
  inp.disabled = true;
  addBtn.disabled = true;
  addBtn.textContent = '...';
  fetch('/add-repo', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({repo: val}),
  }).then(r => r.json()).then(r => {
    if (r.error) { alert('오류: ' + r.error); return; }
    const collabs = r.collaborators || [];
    REPOS.push({name: val, count: collabs.length});
    renderRepoList();
    rebuildMembers(collabs);
    inp.value = '';
  }).catch(e => alert('서버 오류: ' + e))
    .finally(() => {
      inp.disabled = false;
      addBtn.disabled = false;
      addBtn.textContent = '추가';
      inp.focus();
    });
}

function removeRepo(name) {
  REPOS = REPOS.filter(r => r.name !== name);
  renderRepoList();
}

function renderRepoList() {
  const list = $('repo-list');
  list.innerHTML = REPOS.map(r =>
    `<div style="display:flex;align-items:center;justify-content:space-between;
                 padding:4px 8px;background:#0f1117;border-radius:5px;font-size:.8rem">
       <div style="min-width:0">
         <div style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.name}</div>
         <div style="color:var(--muted);font-size:.7rem">${r.count}명</div>
       </div>
       <button onclick="removeRepo('${r.name}')"
         style="background:none;border:none;color:var(--muted);cursor:pointer;
                font-size:.85rem;padding:0 4px;flex-shrink:0" title="제거">&#x2715;</button>
     </div>`
  ).join('');
}

// ── Member list
function updateSelChips() {
  const chips = $('sel-chips');
  chips.innerHTML = '';
  $('member-list').querySelectorAll('input:checked').forEach(cb => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `${cb.value}<button class="chip-x" title="선택 해제">✕</button>`;
    chip.querySelector('.chip-x').addEventListener('click', () => {
      cb.checked = false;
      updateSelChips();
    });
    chips.appendChild(chip);
  });
}

function rebuildMembers(logins) {
  const list = $('member-list');
  const existing = new Set([...list.querySelectorAll('input')].map(i=>i.value));
  logins.forEach(login => {
    if (existing.has(login)) return;
    const d = document.createElement('div');
    d.className = 'mitem';
    d.innerHTML = `<input type="checkbox" id="cb-${login}" value="${login}" checked>
                   <label for="cb-${login}" style="cursor:pointer">${login}</label>`;
    d.querySelector('input').addEventListener('change', updateSelChips);
    list.appendChild(d);
  });
  updateSelChips();
}

function selAll(v) {
  $('member-list').querySelectorAll('.mitem:not([style*="display:none"]) input').forEach(cb => cb.checked = v);
  updateSelChips();
}

function filterMembers(q) {
  const kw = q.trim().toLowerCase();
  $('member-list').querySelectorAll('.mitem').forEach(el => {
    const login = el.querySelector('input').value.toLowerCase();
    el.style.display = (!kw || login.includes(kw)) ? '' : 'none';
  });
}

function getSelectedUsers() {
  return [...$('member-list').querySelectorAll('input:checked')].map(cb=>cb.value);
}

// ── Analysis
function startAnalysis() {
  if (!REPOS.length) { alert('저장소를 추가하세요.'); return; }
  const repoNames = REPOS.map(r => r.name);
  const users = getSelectedUsers();
  if (!users.length) { alert('팀원을 선택하세요.'); return; }

  // 이전 SSE 닫기
  if (es) { es.close(); es = null; }
  RAW = []; IS_DONE = false; SCAN_TOTAL = 0; RELEVANT_TOTAL = 0;

  $('initial').style.display = 'none';
  $('results').style.display = 'block';
  $('progress-wrap').style.display = 'block';
  $('pbar').style.width = '0%';
  $('ptxt').textContent = '분석 요청 중...';
  $('btn-analyze').disabled = true;
  $('btn-analyze').textContent = '분석 중...';

  fetch('/analyze', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      repos: repoNames,
      users,
      since: $('since').value,
      until: $('until').value,
    }),
  }).then(r => r.json()).then(r => {
    if (r.ok) connectSSE();
    else { $('ptxt').textContent = '오류: ' + (r.error||'unknown'); resetBtn(); }
  }).catch(e => { $('ptxt').textContent = '서버 오류: ' + e; resetBtn(); });
}

function resetBtn() {
  $('btn-analyze').disabled = false;
  $('btn-analyze').textContent = '분석 시작';
}

function connectSSE() {
  es = new EventSource('/stream');

  es.addEventListener('meta', e => {
    const d = JSON.parse(e.data);
    SCAN_TOTAL += d.total;
    $('ptxt').textContent = `[${d.owner}/${d.repo}] PR ${d.total}개 목록 확인. 리뷰 스캔 중...`;
  });

  es.addEventListener('scan_progress', e => {
    const d = JSON.parse(e.data);
    const pct = Math.round(d.scanned / d.total * 50);
    $('pbar').style.width = pct + '%';
    $('ptxt').textContent = `[${d.repo}] 리뷰 스캔 ${d.scanned}/${d.total} — 관련 PR: ${d.relevant}개`;
  });

  es.addEventListener('relevant_total', e => {
    RELEVANT_TOTAL = JSON.parse(e.data).total;
    $('ptxt').textContent = `관련 PR ${RELEVANT_TOTAL}개 상세 수집 중...`;
  });

  es.addEventListener('pr', e => {
    RAW.push(JSON.parse(e.data));
    const pct = 50 + Math.round(RAW.length / (RELEVANT_TOTAL||1) * 50); // 50~100%
    $('pbar').style.width = Math.min(pct, 99) + '%';
    $('ptxt').textContent = `상세 수집 ${RAW.length}/${RELEVANT_TOTAL}`;

    // 처음 3개는 즉시, 이후 5개마다 렌더
    if (RAW.length <= 3 || RAW.length % 5 === 0) schedRender();
  });

  es.addEventListener('done', e => {
    const d = JSON.parse(e.data);
    IS_DONE = true;
    $('pbar').style.width = '100%';
    $('ptxt').textContent = `완료! ${d.total}개 PR 분석 (API ${d.api_count}회)`;
    $('footer').textContent = `API ${d.api_count}회`;
    setTimeout(() => $('progress-wrap').style.display='none', 2000);
    render(); resetBtn();
    es.close(); es = null;
  });

  es.addEventListener('error', e => {
    if (e.data) $('ptxt').textContent = '오류: ' + JSON.parse(e.data).message;
    resetBtn(); es.close(); es = null;
  });
}

// ── Compute
function computeStats() {
  const since = $('since').value;
  const until = $('until').value;
  const sinceD = since ? new Date(since+'T00:00:00Z') : null;
  const untilD = until ? new Date(until+'T23:59:59Z') : null;
  const selected = new Set(getSelectedUsers());

  const uStats = {};
  selected.forEach(l => { uStats[l]={pr_count:0,requested:0,participated:0,respT:[],apprT:[],prs:new Set()}; });

  const prs = [];
  for (const pr of RAW) {
    const c = new Date(pr.created_at);
    if (sinceD && c < sinceD) continue;
    if (untilD && c > untilD) continue;
    // PR 생성 수 카운트
    if (uStats[pr.author]) uStats[pr.author].pr_count++;

    const rvTimes = {};
    for (const ev of pr.reviewer_events) {
      const {login:l} = ev;
      if (!selected.has(l) || l===pr.author) continue;
      rvTimes[l] = ev;
      const u = uStats[l]; if (!u) continue;
      if (ev.requested_at) {
        u.requested++;
        if (ev.first_reviewed_at) {
          u.participated++;
          const rt = diffH(ev.requested_at, ev.first_reviewed_at);
          if (rt!=null) u.respT.push(rt);
        }
        if (ev.first_approved_at) { const at=diffH(ev.requested_at,ev.first_approved_at); if(at!=null) u.apprT.push(at); }
      } else if (ev.first_reviewed_at) { u.participated++; }
      if (ev.first_reviewed_at) u.prs.add(pr.number);
    }

    const revL = Object.keys(rvTimes).filter(l=>rvTimes[l].first_reviewed_at);
    const firstRev = revL.reduce((m,l)=>{ const t=rvTimes[l].first_reviewed_at; return (!m||t<m)?t:m; }, null);
    prs.push({
      number:pr.number, title:pr.title, author:pr.author,
      created_at:pr.created_at, merged_at:pr.merged_at, merged:!!pr.merged_at,
      additions:pr.additions, deletions:pr.deletions,
      time_to_first_review_h: diffH(pr.created_at, firstRev),
      time_to_merge_h: pr.merged_at ? diffH(pr.created_at, pr.merged_at) : null,
      time_review_to_merge_h: (firstRev&&pr.merged_at) ? diffH(firstRev,pr.merged_at) : null,
    });
  }

  const rows = Object.entries(uStats).map(([l,s])=>({
    login:l, pr_count:s.pr_count, participated:s.participated,
    part_rate: s.requested>0 ? s.participated/s.requested*100 : 0,
    avg_resp: avg(s.respT), med_resp: median(s.respT), avg_appr: avg(s.apprT),
    prs_reviewed: s.prs.size,
  })).sort((a,b)=>b.participated-a.participated);

  return {rows, prs};
}

// ── Render
const LO = {paper_bgcolor:'#1a1d2e',plot_bgcolor:'#1a1d2e',font:{color:'#e0e0e0',size:11},
             margin:{l:44,r:14,t:38,b:52},legend:{bgcolor:'rgba(0,0,0,0)',orientation:'h',y:1.08},
             xaxis:{gridcolor:'#2a2d3e',color:'#888'},yaxis:{gridcolor:'#2a2d3e',color:'#888'}};
const cfg = {responsive:true,displayModeBar:false};
const lo = (h,ex={}) => ({...LO,...ex,height:h,
  xaxis:{...LO.xaxis,...(ex.xaxis||{})},yaxis:{...LO.yaxis,...(ex.yaxis||{})}});
function vline(layout,val,lbl){
  layout.shapes=[{type:'line',x0:val,x1:val,y0:0,y1:1,yref:'paper',line:{color:'#e74c3c',dash:'dash',width:1.5}}];
  layout.annotations=[{x:val,y:1,yref:'paper',text:lbl,showarrow:false,font:{color:'#e74c3c',size:10},xanchor:'left'}];
}

function render() {
  const {rows, prs} = computeStats();
  const merged = prs.filter(p=>p.merged&&p.time_to_merge_h!=null);
  const rtPRs  = prs.filter(p=>p.time_to_first_review_h!=null);
  const r2mPRs = prs.filter(p=>p.time_review_to_merge_h!=null);

  const since = $('since').value, until = $('until').value;
  $('subtitle').textContent =
    `기간: ${since||'전체'} ~ ${until||'오늘'}  |  PR: ${prs.length}개 (머지: ${merged.length}개)`;

  // cards
  const avgM = avg(merged.map(p=>p.time_to_merge_h));
  const avgR = avg(rtPRs.map(p=>p.time_to_first_review_h));
  const avgP = avg(rows.map(r=>r.part_rate));
  const top  = rows[0];
  const fast = [...rows].filter(r=>r.avg_resp!=null).sort((a,b)=>a.avg_resp-b.avg_resp)[0];
  $('cards').innerHTML = [
    {l:'전체 PR',v:`${prs.length}개`,s:'',c:'#3498db'},
    {l:'머지된 PR',v:`${merged.length}개`,s:`${prs.length?Math.round(merged.length/prs.length*100):0}%`,c:'#2ecc71'},
    {l:'평균 머지 소요',v:fmtH(avgM),s:'',c:'#9b59b6'},
    {l:'첫 리뷰 응답',v:fmtH(avgR),s:'',c:'#e67e22'},
    {l:'평균 참여율',v:avgP!=null?`${avgP.toFixed(0)}%`:'N/A',s:'',c:'#3498db'},
    {l:'최다 리뷰어',v:top?.login||'-',s:`${top?.participated||0}건`,c:'#e74c3c'},
    {l:'가장 빠른 리뷰어',v:fast?.login||'-',s:fmtH(fast?.avg_resp),c:'#1abc9c'},
  ].map(c=>`<div class="card"><div class="cval" style="color:${c.c}">${c.v}</div>
    <div class="clbl">${c.l}</div>${c.s?`<div class="csub">${c.s}</div>`:''}</div>`).join('');

  if (!rows.length) { $('tbody').innerHTML=''; return; }
  const L = rows.map(r=>r.login);

  Plotly.react('ch1',[
    {type:'bar',name:'리뷰 참여',x:L,y:rows.map(r=>r.participated),marker:{color:'#2ecc71'},text:rows.map(r=>r.participated),textposition:'auto'},
    {type:'bar',name:'PR 생성',x:L,y:rows.map(r=>r.pr_count),marker:{color:'#3498db',opacity:.45},text:rows.map(r=>r.pr_count),textposition:'auto'},
  ],lo(360,{title:{text:'리뷰 참여 수 vs PR 생성 수',font:{color:'#e0e0e0',size:12}},barmode:'group'}),cfg);

  const pc=rows.map(r=>r.part_rate>=80?'#2ecc71':r.part_rate>=50?'#e67e22':'#e74c3c');
  Plotly.react('ch2',[{type:'bar',x:L,y:rows.map(r=>+r.part_rate.toFixed(1)),marker:{color:pc},
    text:rows.map(r=>`${r.part_rate.toFixed(0)}%`),textposition:'auto'}],
    lo(340,{title:{text:'참여율',font:{color:'#e0e0e0',size:12}},yaxis:{range:[0,110],gridcolor:'#2a2d3e',color:'#888'},
      shapes:[{type:'line',x0:-.5,x1:L.length-.5,y0:80,y1:80,line:{color:'#2ecc71',dash:'dash',width:1.5}}],
      annotations:[{x:L.length-1,y:80,text:'80%',showarrow:false,font:{color:'#2ecc71',size:10},yshift:8}]}),cfg);

  const rtR=[...rows].filter(r=>r.avg_resp!=null).sort((a,b)=>a.avg_resp-b.avg_resp), rtL=rtR.map(r=>r.login);
  Plotly.react('ch3',[
    {type:'bar',name:'평균 응답',x:rtL,y:rtR.map(r=>r.avg_resp),marker:{color:'#9b59b6'},text:rtR.map(r=>fmtH(r.avg_resp)),textposition:'auto'},
    {type:'scatter',name:'평균 승인',x:rtL,y:rtR.map(r=>r.avg_appr),mode:'markers+lines',marker:{size:8,color:'#e67e22'},line:{color:'#e67e22'}},
  ],lo(340,{title:{text:'응답 / 승인 시간',font:{color:'#e0e0e0',size:12}}}),cfg);

  const rH=rtPRs.map(p=>p.time_to_first_review_h);
  const rL=lo(300,{title:{text:'첫 리뷰 응답시간 분포',font:{color:'#e0e0e0',size:12}},xaxis:{title:'h',gridcolor:'#2a2d3e',color:'#888'},yaxis:{title:'PR수',gridcolor:'#2a2d3e',color:'#888'}});
  if(rH.length) vline(rL,median(rH),`중간값: ${fmtH(median(rH))}`);
  Plotly.react('ch4',[{type:'histogram',x:rH,nbinsx:20,marker:{color:'#f39c12'}}],rL,cfg);

  const mH=merged.map(p=>p.time_to_merge_h/24);
  const mL=lo(300,{title:{text:'머지 소요시간',font:{color:'#e0e0e0',size:12}},xaxis:{title:'일',gridcolor:'#2a2d3e',color:'#888'},yaxis:{title:'PR수',gridcolor:'#2a2d3e',color:'#888'}});
  if(mH.length) vline(mL,median(mH),`중간값: ${fmtH(median(mH)*24)}`);
  Plotly.react('ch5',[{type:'histogram',x:mH,nbinsx:20,marker:{color:'#1abc9c'}}],mL,cfg);

  const r2H=r2mPRs.map(p=>p.time_review_to_merge_h/24);
  const r2L=lo(300,{title:{text:'리뷰 후 머지까지',font:{color:'#e0e0e0',size:12}},xaxis:{title:'일',gridcolor:'#2a2d3e',color:'#888'},yaxis:{title:'PR수',gridcolor:'#2a2d3e',color:'#888'}});
  if(r2H.length) vline(r2L,median(r2H),`중간값: ${fmtH(median(r2H)*24)}`);
  Plotly.react('ch6',[{type:'histogram',x:r2H,nbinsx:20,marker:{color:'#3498db'}}],r2L,cfg);

  const mm={};merged.forEach(p=>{const k=p.merged_at.slice(0,7);mm[k]=(mm[k]||0)+1;});
  const mons=Object.keys(mm).sort();
  Plotly.react('ch7',[{type:'scatter',x:mons,y:mons.map(m=>mm[m]),mode:'lines+markers+text',
    text:mons.map(m=>mm[m]),textposition:'top center',marker:{color:'#3498db'},
    fill:'tozeroy',fillcolor:'rgba(52,152,219,0.12)'}],
    lo(320,{title:{text:'월별 머지 PR',font:{color:'#e0e0e0',size:12}},xaxis:{gridcolor:'#2a2d3e',color:'#888'},yaxis:{gridcolor:'#2a2d3e',color:'#888'}}),cfg);

  Plotly.react('ch8',[{type:'scatter',mode:'markers',
    x:merged.map(p=>p.additions+p.deletions), y:merged.map(p=>p.time_to_merge_h/24),
    marker:{color:merged.map(p=>p.time_to_merge_h),colorscale:'Viridis',showscale:true,size:7,opacity:.7},
    customdata:merged.map(p=>[p.number,p.title]),
    hovertemplate:'PR #%{customdata[0]}<br>%{customdata[1]}<br>변경: %{x:,}줄<br>머지: %{y:.1f}일<extra></extra>'}],
    lo(320,{title:{text:'코드 변경량 vs 머지 소요시간',font:{color:'#e0e0e0',size:12}},
             xaxis:{title:'변경 라인',gridcolor:'#2a2d3e',color:'#888'},yaxis:{title:'머지 소요(일)',gridcolor:'#2a2d3e',color:'#888'}}),cfg);

  // ── 산점도: 응답시간 vs 참여율 (naver/pr-stats 스타일)
  const scRows = rows.filter(r => r.avg_resp != null && r.requested > 0);
  if (scRows.length >= 2) {
    const xs = scRows.map(r => r.avg_resp);
    const ys = scRows.map(r => r.part_rate);
    const medResp = median(xs);
    const medPart = median(ys);
    const xMax = Math.max(...xs) * 1.15 || 1;
    const scColors = scRows.map(r =>
      r.avg_resp <= medResp && r.part_rate >= medPart ? '#2ecc71' :
      r.avg_resp >  medResp && r.part_rate >= medPart ? '#e67e22' :
      r.avg_resp <= medResp && r.part_rate <  medPart ? '#3498db' :
      '#e74c3c'
    );
    const scLayout = lo(420, {
      title: {text: '리뷰어 산점도: 응답시간 vs 참여율', font: {color: '#e0e0e0', size: 12}},
      xaxis: {title: '평균 응답시간 (h)', gridcolor: '#2a2d3e', color: '#888', rangemode: 'tozero'},
      yaxis: {title: '참여율 (%)', range: [-5, 115], gridcolor: '#2a2d3e', color: '#888'},
      shapes: [
        {type:'line',x0:medResp,x1:medResp,y0:-5,y1:115,line:{color:'#444',dash:'dot',width:1.2}},
        {type:'line',x0:0,x1:xMax,y0:medPart,y1:medPart,line:{color:'#444',dash:'dot',width:1.2}},
      ],
      annotations: [
        {x:0,    y:115, xref:'paper', yref:'y', text:'빠름 + 높은 참여',  showarrow:false, font:{color:'#2ecc71',size:9}, xanchor:'left'},
        {x:1,    y:115, xref:'paper', yref:'y', text:'느림 + 높은 참여',  showarrow:false, font:{color:'#e67e22',size:9}, xanchor:'right'},
        {x:0,    y:-2,  xref:'paper', yref:'y', text:'빠름 + 낮은 참여',  showarrow:false, font:{color:'#3498db',size:9}, xanchor:'left'},
        {x:1,    y:-2,  xref:'paper', yref:'y', text:'느림 + 낮은 참여',  showarrow:false, font:{color:'#e74c3c',size:9}, xanchor:'right'},
        {x:medResp, y:115, xref:'x', yref:'y', text:`중간값 ${fmtH(medResp)}`, showarrow:false, font:{color:'#666',size:9}, yshift:0},
      ],
      margin: {l:52, r:20, t:48, b:56},
    });
    Plotly.react('ch9', [{
      type:'scatter', mode:'markers+text',
      x: xs, y: ys,
      text: scRows.map(r => r.login),
      textposition: 'top center',
      textfont: {size: 10, color: '#e0e0e0'},
      marker: {size: 13, color: scColors, line: {color: '#0f1117', width: 1.5}, opacity: 0.9},
      hovertemplate: '<b>%{text}</b><br>응답시간: %{x:.1f}h<br>참여율: %{y:.1f}%<extra></extra>',
    }], scLayout, cfg);
  } else {
    $('ch9').innerHTML = '';
  }

  $('tbody').innerHTML=rows.map(r=>{
    const p=r.part_rate, c=p>=80?'#2ecc71':p>=50?'#e67e22':'#e74c3c';
    return `<tr><td><strong>${r.login}</strong></td><td>${r.pr_count}</td><td>${r.participated}</td>
      <td><span class="badge" style="background:${c}22;color:${c}">${p.toFixed(0)}%</span></td>
      <td style="color:#9b59b6">${fmtH(r.avg_resp)}</td><td style="color:#e67e22">${fmtH(r.avg_appr)}</td>
      <td>${fmtH(r.med_resp)}</td><td>${r.prs_reviewed}</td></tr>`;
  }).join('');
}

function schedRender() { clearTimeout(renderTimer); renderTimer=setTimeout(render,300); }

// ── Date Picker
const DP = {};
const DP_DOWS = ['일','월','화','수','목','금','토'];

function dpPad(n){return String(n).padStart(2,'0');}
function dpToStr(y,m,d){return `${y}-${dpPad(m+1)}-${dpPad(d)}`;}

function dpBuild(id) {
  const inp = $(id);
  inp.readOnly = true;
  const wrap = document.createElement('div');
  wrap.className = 'dp-wrap';
  wrap.style.flex = '1';
  inp.parentNode.style.flex = '1';
  inp.parentNode.insertBefore(wrap, inp);
  wrap.appendChild(inp);
  const cal = document.createElement('div');
  cal.className = 'dp-cal';
  cal.addEventListener('click', e => e.stopPropagation());
  wrap.appendChild(cal);
  const today = new Date();
  DP[id] = {cal, value:'', viewY:today.getFullYear(), viewM:today.getMonth()};
  inp.addEventListener('click', e => { e.stopPropagation(); dpToggle(id); });
  dpRender(id);
}

function dpToggle(id) {
  Object.keys(DP).forEach(k => { if(k!==id) DP[k].cal.classList.remove('open'); });
  const dp = DP[id];
  if (!dp.cal.classList.contains('open')) {
    const rect = $(id).getBoundingClientRect();
    dp.cal.style.left = rect.left + 'px';
    dp.cal.style.top  = (rect.bottom + 4) + 'px';
  }
  dp.cal.classList.toggle('open');
}

function dpNav(id, dir) {
  const dp = DP[id]; dp.viewM += dir;
  if(dp.viewM<0){dp.viewM=11;dp.viewY--;}
  if(dp.viewM>11){dp.viewM=0;dp.viewY++;}
  dpRender(id);
}

function dpSelect(id, y, m, d) {
  const dp = DP[id];
  dp.value = dpToStr(y, m, d);
  $(id).value = dp.value;
  dp.cal.classList.remove('open');
  dpRender(id);
}

function dpClear(id) {
  DP[id].value = '';
  $(id).value = '';
  dpRender(id);
}

function dpRender(id) {
  const dp = DP[id]; const {viewY:y, viewM:m} = dp;
  const today = new Date();
  const todayStr = dpToStr(today.getFullYear(), today.getMonth(), today.getDate());
  const first = new Date(y, m, 1).getDay();
  const days = new Date(y, m+1, 0).getDate();
  const title = new Date(y, m).toLocaleDateString('ko-KR', {year:'numeric',month:'long'});
  let cells = DP_DOWS.map(d=>`<div class="dp-dow">${d}</div>`).join('');
  for(let i=0;i<first;i++) cells+=`<div class="dp-day dp-empty"></div>`;
  for(let d=1;d<=days;d++){
    const s=dpToStr(y,m,d);
    const cls=['dp-day',s===todayStr?'dp-today':'',s===dp.value?'dp-sel':''].join(' ').trim();
    cells+=`<div class="${cls}" onclick="dpSelect('${id}',${y},${m},${d})">${d}</div>`;
  }
  dp.cal.innerHTML=`
    <div class="dp-head">
      <button class="dp-nav" onclick="dpNav('${id}',-1)">&#8249;</button>
      <span class="dp-title">${title}</span>
      <button class="dp-nav" onclick="dpNav('${id}',1)">&#8250;</button>
    </div>
    <div class="dp-grid">${cells}</div>
    ${dp.value?`<div class="dp-clear"><button onclick="dpClear('${id}')">지우기</button></div>`:''}
  `;
}

document.addEventListener('click', () => {
  Object.keys(DP).forEach(id => DP[id].cal.classList.remove('open'));
});

// ── Init
dpBuild('since');
dpBuild('until');
// 기본값: 오늘 ~ 2주 전
(function() {
  const today = new Date();
  const twoWeeksAgo = new Date(today);
  twoWeeksAgo.setDate(twoWeeksAgo.getDate() - 14);
  function dpSetVal(id, date) {
    const dp = DP[id];
    dp.value = dpToStr(date.getFullYear(), date.getMonth(), date.getDate());
    $(id).value = dp.value;
    dp.viewY = date.getFullYear();
    dp.viewM = date.getMonth();
    dpRender(id);
  }
  dpSetVal('since', twoWeeksAgo);
  dpSetVal('until', today);
})();
// (repos and members are populated via UI)
</script>
</body>
</html>"""


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

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
    global HTML_PAGE

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

    HTML_PAGE = HTML_TEMPLATE

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
