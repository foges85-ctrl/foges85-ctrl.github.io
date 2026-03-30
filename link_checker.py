#!/usr/bin/env python3
"""
IT Study Hub - Link Checker
Scans all HTML files for broken internal links, Amazon Associates links,
Dion Training / UpPromote links, and all external links.
Emails an HTML report only when broken links are found.
"""

import re
import sys
import smtplib
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────

SITE_ROOT = "."
BASE_URL  = "https://itstudyhub.org"

# ── Email config — fill these in ──────────────────────────────────────────────
GMAIL_ADDRESS = "contact@itstudyhub.org"
GMAIL_APP_PW  = "zfoy rafe svbx mpxo"
EMAIL_TO      = "contact@itstudyhub.org"

TIMEOUT    = 10
WORKERS    = 8
USER_AGENT = "Mozilla/5.0 (compatible; ITStudyHubLinkChecker/1.0)"

AMAZON_PATTERN = re.compile(r"amazon\.com", re.I)
DION_PATTERN   = re.compile(r"(diontraining\.com|uppromotehq\.com|ref=10655873)", re.I)

# ── Link extraction ────────────────────────────────────────────────────────────

def find_html_files(root):
    return sorted(p for p in Path(root).rglob("*.html") if "venv" not in p.parts)

def extract_links(filepath, root):
    links = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            text = tag.get_text(strip=True)[:80] or "[no text]"
            pos  = content.find(str(tag))
            line = content[:pos].count("\n") + 1 if pos != -1 else 0
            links.append((href, text, line))
    except Exception:
        pass
    return links

def classify_link(href, source_file, root):
    # Skip non-navigable and false-positive links
    if href.startswith(("mailto:", "javascript:", "tel:")):
        return None, "skip"
    if href.startswith("#"):
        return None, "skip"
    if "cdn-cgi" in href:
        return None, "skip"

    if href.startswith(("http://", "https://")):
        parsed     = urlparse(href)
        netloc     = parsed.netloc.replace("www.", "")
        base_netloc = urlparse(BASE_URL).netloc.replace("www.", "")
        if netloc == base_netloc:
            return href, "internal"
        if AMAZON_PATTERN.search(href):
            return href, "amazon"
        if DION_PATTERN.search(href):
            return href, "dion"
        return href, "external"

    # Relative link
    rel_base = BASE_URL.rstrip("/") + "/" + str(source_file.relative_to(root)).replace("\\", "/")
    resolved = urljoin(rel_base, href)
    return resolved, "internal"

# ── HTTP checking ──────────────────────────────────────────────────────────────

_cache = {}

def check_url(url):
    if url in _cache:
        return _cache[url]
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.head(url, timeout=TIMEOUT, headers=headers, allow_redirects=True)
        if r.status_code == 405:
            r = requests.get(url, timeout=TIMEOUT, headers=headers, allow_redirects=True, stream=True)
        result = (r.status_code, None)
    except requests.exceptions.SSLError:
        result = (None, "SSL Error")
    except requests.exceptions.ConnectionError:
        result = (None, "Connection Error")
    except requests.exceptions.Timeout:
        result = (None, "Timeout")
    except Exception as e:
        result = (None, str(e)[:80])
    _cache[url] = result
    return result

def check_internal_file(url, root):
    parsed = urlparse(url)
    path   = parsed.path.rstrip("/")
    candidates = [
        Path(root) / path.lstrip("/"),
        Path(root) / (path.lstrip("/") + ".html"),
        Path(root) / path.lstrip("/") / "index.html",
    ]
    for c in candidates:
        if c.exists():
            return (200, None)
    return (404, "File not found locally")

# ── Main scan ──────────────────────────────────────────────────────────────────

def run_scan(root):
    root       = Path(root).resolve()
    html_files = find_html_files(root)
    print(f"Found {len(html_files)} HTML files. Extracting links...")

    all_links = []
    for filepath in html_files:
        for href, text, line in extract_links(filepath, root):
            resolved, category = classify_link(href, filepath, root)
            if category == "skip":
                continue
            all_links.append({
                "source": str(filepath.relative_to(root)),
                "href": href, "resolved": resolved,
                "category": category, "text": text,
                "line": line, "status": None, "error": None,
            })

    to_local  = [l for l in all_links if l["category"] == "internal" and l["resolved"]]
    to_remote = [l for l in all_links if l["category"] in ("external", "amazon", "dion") and l["resolved"]]

    print(f"Checking {len(to_local)} internal links locally...")
    for link in to_local:
        link["status"], link["error"] = check_internal_file(link["resolved"], root)

    unique_urls = list({l["resolved"] for l in to_remote})
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

    for link in to_remote:
        if link["resolved"] in url_results:
            link["status"], link["error"] = url_results[link["resolved"]]

    return all_links, html_files

# ── Report builder ─────────────────────────────────────────────────────────────

def status_label(status, error):
    if error:       return "error",    error
    if status == 200: return "ok",     "200 OK"
    if status in (301, 302, 307, 308): return "redirect", f"{status} Redirect"
    if status == 404: return "broken", "404 Not Found"
    if status == 403: return "warn",   "403 Forbidden"
    if status is None: return "error", "No response"
    if status >= 400:  return "broken", f"{status} Error"
    return "ok", str(status)

def make_rows(links):
    if not links:
        return "<tr><td colspan='6' style='text-align:center;color:#888'>None found</td></tr>"
    out = []
    colors = {"ok": "#22c55e", "redirect": "#f59e0b", "broken": "#ef4444", "error": "#ef4444", "warn": "#f97316"}
    for l in links:
        cls, label = status_label(l["status"], l["error"])
        badge = colors.get(cls, "#888")
        url   = l["href"]
        short = url[:70] + ("..." if len(url) > 70 else "")
        out.append(
            f"<tr><td><code>{l['source']}</code></td><td>{l['line']}</td>"
            f"<td style='color:#94a3b8'>{l['text']}</td>"
            f"<td><a href='{url}' target='_blank' style='color:#38bdf8'>{short}</a></td>"
            f"<td><span style='background:{badge};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px'>{label}</span></td>"
            f"<td>{l['category']}</td></tr>"
        )
    return "".join(out)

def build_report(all_links, html_files):
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total  = len(all_links)
    broken = [l for l in all_links if status_label(l["status"], l["error"])[0] in ("broken", "error")]
    redirects    = [l for l in all_links if status_label(l["status"], l["error"])[0] == "redirect"]
    ok           = [l for l in all_links if status_label(l["status"], l["error"])[0] == "ok"]
    amazon_links = [l for l in all_links if l["category"] == "amazon"]
    dion_links   = [l for l in all_links if l["category"] == "dion"]
    amazon_broken = [l for l in amazon_links if status_label(l["status"], l["error"])[0] in ("broken", "error")]
    dion_broken   = [l for l in dion_links   if status_label(l["status"], l["error"])[0] in ("broken", "error")]

    def section(title, links, collapsible=False):
        table = (
            f"<table><thead><tr><th>Source File</th><th>Line</th><th>Link Text</th>"
            f"<th>URL</th><th>Status</th><th>Type</th></tr></thead>"
            f"<tbody>{make_rows(links)}</tbody></table>"
        )
        if collapsible:
            return f"<section><details><summary><h2 style='display:inline'>{title}</h2></summary>{table}</details></section>"
        return f"<section><h2>{title}</h2>{table}</section>"

    cards = "".join([
        f"<div class='card'><div class='num' style='color:#ef4444'>{len(broken)}</div><div class='lbl'>Broken / Error</div></div>",
        f"<div class='card'><div class='num' style='color:#f59e0b'>{len(redirects)}</div><div class='lbl'>Redirects</div></div>",
        f"<div class='card'><div class='num' style='color:#22c55e'>{len(ok)}</div><div class='lbl'>OK</div></div>",
        f"<div class='card'><div class='num' style='color:#f97316'>{len(amazon_broken)}</div><div class='lbl'>Amazon Broken</div></div>",
        f"<div class='card'><div class='num' style='color:#f97316'>{len(dion_broken)}</div><div class='lbl'>Dion Broken</div></div>",
        f"<div class='card'><div class='num' style='color:#38bdf8'>{total}</div><div class='lbl'>Total Links</div></div>",
    ])

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>IT Study Hub - Link Report</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
h1{{color:#38bdf8}} .meta{{color:#94a3b8;font-size:14px;margin-bottom:28px}}
.cards{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:32px}}
.card{{background:#1e293b;border-radius:10px;padding:18px 24px;min-width:140px}}
.card .num{{font-size:36px;font-weight:700}} .card .lbl{{font-size:13px;color:#94a3b8}}
section{{margin-bottom:40px}} h2{{font-size:18px;border-bottom:1px solid #334155;padding-bottom:8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} thead tr{{background:#1e293b}}
th{{text-align:left;padding:8px 10px;color:#94a3b8}} td{{padding:7px 10px;border-bottom:1px solid #1e293b;vertical-align:top}}
tr:hover td{{background:#1e293b88}} code{{background:#1e293b;padding:1px 5px;border-radius:4px;font-size:12px;color:#7dd3fc}}
details summary{{cursor:pointer}}
</style></head><body>
<h1>IT Study Hub - Link Report</h1>
<p class="meta">Generated: {now} | {len(html_files)} HTML files | {total} links checked</p>
<div class="cards">{cards}</div>
{section(f"Broken and Error Links ({len(broken)})", broken)}
{section(f"Amazon Associates Links ({len(amazon_links)}) - {len(amazon_broken)} broken", amazon_links)}
{section(f"Dion Training Links ({len(dion_links)}) - {len(dion_broken)} broken", dion_links)}
{section(f"Redirects ({len(redirects)})", redirects, collapsible=True)}
</body></html>"""

# ── Email sender ───────────────────────────────────────────────────────────────

def send_email(html, broken_count):
    now     = datetime.now().strftime("%Y-%m-%d")
    subject = f"IT Study Hub - {broken_count} Broken Link(s) Found ({now})"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = EMAIL_TO

    msg.attach(MIMEText(
        f"IT Study Hub link checker found {broken_count} broken link(s) on {now}.\n"
        f"See the attached HTML report for details.",
        "plain", "utf-8"
    ))

    attachment = MIMEText(html, "html", "utf-8")
    attachment.add_header("Content-Disposition", f"attachment; filename=link_report_{now}.html")
    msg.attach(attachment)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PW)
            server.sendmail(GMAIL_ADDRESS, EMAIL_TO, msg.as_string())
        print(f"Email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"Failed to send email: {e}")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IT Study Hub Link Checker")
    parser.add_argument("--root", default=SITE_ROOT)
    parser.add_argument("--force-email", action="store_true",
                        help="Send email even with no broken links (use to test Gmail setup)")
    args = parser.parse_args()

    all_links, html_files = run_scan(args.root)
    broken = [l for l in all_links if status_label(l["status"], l["error"])[0] in ("broken", "error")]

    if not broken and not args.force_email:
        print("No broken links found. No email sent.")
    else:
        count = len(broken)
        print(f"{count} broken link(s) found. Sending email...")
        html = build_report(all_links, html_files)
        send_email(html, count)
