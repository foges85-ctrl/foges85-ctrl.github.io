#!/usr/bin/env python3
"""
IT Study Hub — Link Checker
Scans all HTML files for broken internal links, Amazon Associates links,
Dion Training / UpPromote links, and all external links.
Outputs a self-contained HTML report.
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────────

SITE_ROOT = "."           # Path to your local site repo (change if needed)
BASE_URL   = "https://itstudyhub.org"  # Live base URL for resolving internal links
OUTPUT     = "link_report.html"
TIMEOUT    = 10            # Seconds per request
WORKERS    = 8             # Parallel HTTP threads
USER_AGENT = "Mozilla/5.0 (compatible; ITStudyHubLinkChecker/1.0)"

AMAZON_PATTERN   = re.compile(r"amazon\.com", re.I)
DION_PATTERN     = re.compile(r"(diontraining\.com|uppromotehq\.com|ref=10655873)", re.I)

# ── Link extraction ───────────────────────────────────────────────────────────

def find_html_files(root):
    return sorted(Path(root).rglob("*.html"))

def extract_links(filepath, root):
    """Return list of (href, link_text, line_number) from an HTML file."""
    links = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            text = tag.get_text(strip=True)[:80] or "[no text]"
            # Approximate line number
            pos = content.find(str(tag))
            line = content[:pos].count("\n") + 1 if pos != -1 else 0
            links.append((href, text, line))
    except Exception as e:
        pass
    return links

def classify_link(href, source_file, root):
    """Return (resolved_url, category) for a given href."""
    if href.startswith("mailto:") or href.startswith("javascript:") or href == "#":
        return None, "skip"

    if href.startswith("#"):
        return None, "anchor"

    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if parsed.netloc.replace("www.", "") == urlparse(BASE_URL).netloc.replace("www.", ""):
            return href, "internal"
        if AMAZON_PATTERN.search(href):
            return href, "amazon"
        if DION_PATTERN.search(href):
            return href, "dion"
        return href, "external"

    # Relative link — resolve against source file location
    rel_base = BASE_URL.rstrip("/") + "/" + str(source_file.relative_to(root)).replace("\\", "/")
    resolved = urljoin(rel_base, href)
    return resolved, "internal"

# ── HTTP checking ─────────────────────────────────────────────────────────────

_checked_cache = {}

def check_url(url):
    """Return (status_code, error_msg). Cached per URL."""
    if url in _checked_cache:
        return _checked_cache[url]
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.head(url, timeout=TIMEOUT, headers=headers, allow_redirects=True)
        if r.status_code == 405:
            r = requests.get(url, timeout=TIMEOUT, headers=headers, allow_redirects=True, stream=True)
        result = (r.status_code, None)
    except requests.exceptions.SSLError as e:
        result = (None, f"SSL Error: {e}")
    except requests.exceptions.ConnectionError as e:
        result = (None, f"Connection Error")
    except requests.exceptions.Timeout:
        result = (None, "Timeout")
    except Exception as e:
        result = (None, str(e)[:80])
    _checked_cache[url] = result
    return result

def check_internal_file(url, root):
    """Check if an internal link resolves to an existing local file."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    candidates = [
        Path(root) / path.lstrip("/"),
        Path(root) / (path.lstrip("/") + ".html"),
        Path(root) / path.lstrip("/") / "index.html",
    ]
    for c in candidates:
        if c.exists():
            return (200, None)
    return (404, "File not found locally")

# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan(root):
    root = Path(root).resolve()
    html_files = find_html_files(root)
    print(f"Found {len(html_files)} HTML files. Extracting links...")

    all_links = []  # (source_file, href_original, resolved_url, category, link_text, line)

    for filepath in html_files:
        links = extract_links(filepath, root)
        for href, text, line in links:
            resolved, category = classify_link(href, filepath, root)
            if category == "skip":
                continue
            all_links.append({
                "source": str(filepath.relative_to(root)),
                "href": href,
                "resolved": resolved,
                "category": category,
                "text": text,
                "line": line,
                "status": None,
                "error": None,
            })

    # Separate internal (local check) vs external (HTTP check)
    to_http_check = [l for l in all_links if l["category"] in ("external", "amazon", "dion") and l["resolved"]]
    to_local_check = [l for l in all_links if l["category"] == "internal" and l["resolved"]]

    print(f"Checking {len(to_local_check)} internal links locally...")
    for link in to_local_check:
        status, error = check_internal_file(link["resolved"], root)
        link["status"] = status
        link["error"] = error

    unique_urls = list({l["resolved"] for l in to_http_check})
    print(f"Checking {len(unique_urls)} unique external URLs (this may take a minute)...")

    url_results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_to_url = {executor.submit(check_url, url): url for url in unique_urls}
        done = 0
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            url_results[url] = future.result()
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(unique_urls)} checked...")

    for link in to_http_check:
        if link["resolved"] in url_results:
            link["status"], link["error"] = url_results[link["resolved"]]

    return all_links, html_files

# ── HTML report ───────────────────────────────────────────────────────────────

def status_label(status, error):
    if error:
        return "error", error
    if status == 200:
        return "ok", "200 OK"
    if status in (301, 302, 307, 308):
        return "redirect", f"{status} Redirect"
    if status == 404:
        return "broken", "404 Not Found"
    if status == 403:
        return "warn", "403 Forbidden"
    if status is None:
        return "error", "No response"
    if status >= 400:
        return "broken", f"{status} Error"
    return "ok", str(status)

def build_report(all_links, html_files, output_path):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total = len(all_links)
    broken = [l for l in all_links if status_label(l["status"], l["error"])[0] in ("broken", "error")]
    redirects = [l for l in all_links if status_label(l["status"], l["error"])[0] == "redirect"]
    ok = [l for l in all_links if status_label(l["status"], l["error"])[0] == "ok"]
    amazon_links = [l for l in all_links if l["category"] == "amazon"]
    dion_links = [l for l in all_links if l["category"] == "dion"]
    amazon_broken = [l for l in amazon_links if status_label(l["status"], l["error"])[0] in ("broken", "error")]
    dion_broken = [l for l in dion_links if status_label(l["status"], l["error"])[0] in ("broken", "error")]

    def rows(links):
        if not links:
            return "<tr><td colspan='6' style='text-align:center;color:#888'>None found</td></tr>"
        out = []
        for l in links:
            cls, label = status_label(l["status"], l["error"])
            badge = {
                "ok": "#22c55e", "redirect": "#f59e0b",
                "broken": "#ef4444", "error": "#ef4444", "warn": "#f97316"
            }.get(cls, "#888")
            out.append(f"""
            <tr>
              <td><code>{l['source']}</code></td>
              <td>{l['line']}</td>
              <td class="link-text">{l['text']}</td>
              <td><a href="{l['href']}" target="_blank" title="{l.get('resolved','')}">{l['href'][:70]}{'…' if len(l['href'])>70 else ''}</a></td>
              <td><span style="background:{badge};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;white-space:nowrap">{label}</span></td>
              <td>{l['category']}</td>
            </tr>""")
        return "".join(out)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IT Study Hub — Link Report</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }}
  h1 {{ color: #38bdf8; margin-bottom: 4px; }}
  .meta {{ color: #94a3b8; font-size: 14px; margin-bottom: 28px; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
  .card {{ background: #1e293b; border-radius: 10px; padding: 18px 24px; min-width: 140px; }}
  .card .num {{ font-size: 36px; font-weight: 700; }}
  .card .lbl {{ font-size: 13px; color: #94a3b8; margin-top: 2px; }}
  .red {{ color: #ef4444; }} .green {{ color: #22c55e; }} .yellow {{ color: #f59e0b; }}
  .orange {{ color: #f97316; }} .blue {{ color: #38bdf8; }}
  section {{ margin-bottom: 40px; }}
  h2 {{ font-size: 18px; border-bottom: 1px solid #334155; padding-bottom: 8px; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead tr {{ background: #1e293b; }}
  th {{ text-align: left; padding: 8px 10px; color: #94a3b8; font-weight: 600; white-space: nowrap; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #1e293b; vertical-align: top; }}
  tr:hover td {{ background: #1e293b88; }}
  .link-text {{ color: #94a3b8; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  a {{ color: #38bdf8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ background: #1e293b; padding: 1px 5px; border-radius: 4px; font-size: 12px; color: #7dd3fc; }}
  .empty {{ color: #64748b; font-style: italic; }}
  details summary {{ cursor: pointer; user-select: none; }}
</style>
</head>
<body>
<h1>🔗 IT Study Hub — Link Report</h1>
<p class="meta">Generated: {now} &nbsp;|&nbsp; {len(html_files)} HTML files scanned &nbsp;|&nbsp; {total} links found</p>

<div class="cards">
  <div class="card"><div class="num red">{len(broken)}</div><div class="lbl">Broken / Error</div></div>
  <div class="card"><div class="num yellow">{len(redirects)}</div><div class="lbl">Redirects</div></div>
  <div class="card"><div class="num green">{len(ok)}</div><div class="lbl">OK</div></div>
  <div class="card"><div class="num orange">{len(amazon_broken)}</div><div class="lbl">Amazon Broken</div></div>
  <div class="card"><div class="num orange">{len(dion_broken)}</div><div class="lbl">Dion Broken</div></div>
  <div class="card"><div class="num blue">{total}</div><div class="lbl">Total Links</div></div>
</div>

<section>
  <h2>🚨 Broken & Error Links ({len(broken)})</h2>
  <table>
    <thead><tr><th>Source File</th><th>Line</th><th>Link Text</th><th>URL</th><th>Status</th><th>Type</th></tr></thead>
    <tbody>{rows(broken)}</tbody>
  </table>
</section>

<section>
  <h2>🛒 Amazon Associates Links ({len(amazon_links)}) — {len(amazon_broken)} broken</h2>
  <table>
    <thead><tr><th>Source File</th><th>Line</th><th>Link Text</th><th>URL</th><th>Status</th><th>Type</th></tr></thead>
    <tbody>{rows(amazon_links)}</tbody>
  </table>
</section>

<section>
  <h2>🎓 Dion Training / UpPromote Links ({len(dion_links)}) — {len(dion_broken)} broken</h2>
  <table>
    <thead><tr><th>Source File</th><th>Line</th><th>Link Text</th><th>URL</th><th>Status</th><th>Type</th></tr></thead>
    <tbody>{rows(dion_links)}</tbody>
  </table>
</section>

<section>
  <details>
    <summary><h2 style="display:inline">↩️ Redirects ({len(redirects)})</h2></summary>
    <table>
      <thead><tr><th>Source File</th><th>Line</th><th>Link Text</th><th>URL</th><th>Status</th><th>Type</th></tr></thead>
      <tbody>{rows(redirects)}</tbody>
    </table>
  </details>
</section>

<section>
  <details>
    <summary><h2 style="display:inline">✅ All OK Links ({len(ok)})</h2></summary>
    <table>
      <thead><tr><th>Source File</th><th>Line</th><th>Link Text</th><th>URL</th><th>Status</th><th>Type</th></tr></thead>
      <tbody>{rows(ok)}</tbody>
    </table>
  </details>
</section>

</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"\n✅ Report saved to: {output_path}")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IT Study Hub Link Checker")
    parser.add_argument("--root", default=SITE_ROOT, help="Path to site root (default: current dir)")
    parser.add_argument("--output", default=OUTPUT, help="Output HTML file name")
    args = parser.parse_args()

    all_links, html_files = run_scan(args.root)
    build_report(all_links, html_files, args.output)
