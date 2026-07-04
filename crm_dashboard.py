"""
WK Tech Audit - CRM Dashboard
==============================
A full lead-tracking dashboard: finds leads, audits their SEO, and tracks
them through a sales pipeline, all backed by a real database (SQLite).

Run locally with:
    uv run --with streamlit --with requests --with beautifulsoup4 --with pandas --with plotly streamlit run crm_dashboard.py
"""

import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "WKTechLeadGenTool/1.0 (contact: your-email@wktech.pk)"}
DB_PATH = "wktech_crm.db"
PIPELINE_STAGES = ["New Leads", "Qualified", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]

st.set_page_config(page_title="WK Tech Audit", page_icon="📊", layout="wide")

# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            website TEXT,
            address TEXT,
            email TEXT,
            niche TEXT,
            location TEXT,
            seo_score INTEGER,
            performance_score INTEGER,
            seo_sub_score INTEGER,
            best_practices_score INTEGER,
            accessibility_score INTEGER,
            issues TEXT,
            pipeline_stage TEXT DEFAULT 'New Leads',
            lead_source TEXT DEFAULT 'Organic Search',
            date_added TEXT,
            date_converted TEXT,
            audited INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            report_type TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_report(report_type):
    conn = get_conn()
    conn.execute("INSERT INTO reports_log (created_at, report_type) VALUES (?, ?)",
                 (datetime.now().isoformat(), report_type))
    conn.commit()
    conn.close()


def save_lead(lead, audit=None, lead_source="Organic Search"):
    conn = get_conn()
    cur = conn.cursor()
    # Avoid exact duplicate (same name+website)
    existing = cur.execute(
        "SELECT id FROM leads WHERE name = ? AND website = ?", (lead["name"], lead["website"])
    ).fetchone()

    scores = audit["scores"] if audit else {}
    issues_str = "; ".join(audit["issues"]) if audit else ""
    email = audit["email"] if audit else lead.get("email", "")

    if existing:
        cur.execute("""
            UPDATE leads SET phone=?, address=?, email=?, seo_score=?,
                performance_score=?, seo_sub_score=?, best_practices_score=?,
                accessibility_score=?, issues=?, audited=?
            WHERE id=?
        """, (lead["phone"], lead["address"], email,
              scores.get("overall"), scores.get("performance"), scores.get("seo"),
              scores.get("best_practices"), scores.get("accessibility"),
              issues_str, 1 if audit else 0, existing["id"]))
    else:
        cur.execute("""
            INSERT INTO leads (name, phone, website, address, email, niche, location,
                seo_score, performance_score, seo_sub_score, best_practices_score,
                accessibility_score, issues, pipeline_stage, lead_source, date_added, audited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'New Leads', ?, ?, ?)
        """, (lead["name"], lead["phone"], lead["website"], lead["address"], email,
              lead.get("niche", ""), lead.get("location", ""),
              scores.get("overall"), scores.get("performance"), scores.get("seo"),
              scores.get("best_practices"), scores.get("accessibility"),
              issues_str, lead_source, datetime.now().isoformat(), 1 if audit else 0))
    conn.commit()
    conn.close()


def update_pipeline_stage(lead_id, new_stage):
    conn = get_conn()
    converted_at = datetime.now().isoformat() if new_stage == "Closed Won" else None
    conn.execute("UPDATE leads SET pipeline_stage=?, date_converted=? WHERE id=?",
                 (new_stage, converted_at, lead_id))
    conn.commit()
    conn.close()


def load_leads():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM leads ORDER BY date_added DESC", conn)
    conn.close()
    return df


def count_reports():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) as c FROM reports_log").fetchone()["c"]
    conn.close()
    return n


init_db()

# ----------------------------------------------------------------------
# LEAD FINDING (OpenStreetMap)
# ----------------------------------------------------------------------

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]


def geocode_location(location):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": location, "format": "json", "limit": 1}
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Could not find location: {location}")
    return data[0]["boundingbox"]


def find_businesses(niche, location, limit=20):
    south, north, west, east = geocode_location(location)
    overpass_query = f"""
    [out:json][timeout:25];
    (
      node["name"~"{niche}",i]({south},{west},{north},{east});
      way["name"~"{niche}",i]({south},{west},{north},{east});
      relation["name"~"{niche}",i]({south},{west},{north},{east});
    );
    out center {limit * 3};
    """
    last_error = None
    r = None
    for server in OVERPASS_SERVERS:
        try:
            r = requests.post(server, data={"data": overpass_query}, headers=HEADERS, timeout=45)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            last_error = e
            r = None
            continue
    if r is None:
        raise RuntimeError(f"All Overpass servers unavailable right now. Try again shortly. ({last_error})")
    elements = r.json().get("elements", [])
    elements = [el for el in elements if el.get("tags", {}).get("name")][:limit]
    businesses = []
    for el in elements:
        tags = el.get("tags", {})
        businesses.append({
            "name": tags.get("name", ""),
            "phone": tags.get("phone", tags.get("contact:phone", "")),
            "website": tags.get("website", tags.get("contact:website", "")),
            "address": ", ".join(filter(None, [
                tags.get("addr:housenumber", ""), tags.get("addr:street", ""),
                tags.get("addr:city", ""), tags.get("addr:state", ""),
            ])),
        })
    return businesses


# ----------------------------------------------------------------------
# SEO AUDIT (with categorized sub-scores matching the dashboard)
# ----------------------------------------------------------------------

def extract_email(html_text):
    mailto = re.findall(r'mailto:([\w\.-]+@[\w\.-]+)', html_text)
    if mailto:
        return mailto[0]
    emails = re.findall(r'[\w\.-]+@[\w\.-]+\.\w+', html_text)
    emails = [e for e in emails if not any(x in e.lower() for x in [".png", ".jpg", ".css", ".js"])]
    return emails[0] if emails else None


def find_contact_page(soup, base):
    for a in soup.find_all("a", href=True):
        if "contact" in a["href"].lower() or "contact" in a.get_text().lower():
            return urljoin(base, a["href"])
    return None


def audit_seo(url):
    issues = []
    result = {"url": url, "issues": [], "email": "", "scores": {}}
    if not url.startswith("http"):
        url = "https://" + url

    try:
        start = time.time()
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        load_time = time.time() - start
    except requests.RequestException as e:
        result["issues"].append(f"Site unreachable ({e.__class__.__name__})")
        result["scores"] = {"overall": 0, "performance": 0, "seo": 0, "best_practices": 0, "accessibility": 0}
        result["email"] = "(site unreachable)"
        return result

    seo_checks_total, seo_checks_passed = 5, 5
    bp_checks_total, bp_checks_passed = 3, 3
    acc_checks_total, acc_checks_passed = 2, 2

    if not url.startswith("https://") and r.url.startswith("http://"):
        issues.append("No HTTPS/SSL - site is not secure")
        bp_checks_passed -= 1

    soup = BeautifulSoup(r.text, "html.parser")

    title = soup.find("title")
    if not title or not title.text.strip():
        issues.append("Missing <title> tag"); seo_checks_passed -= 1
    elif len(title.text.strip()) > 60:
        issues.append(f"Title tag too long ({len(title.text.strip())} chars)"); seo_checks_passed -= 1
    elif len(title.text.strip()) < 10:
        issues.append("Title tag too short / not descriptive"); seo_checks_passed -= 1

    meta_desc = soup.find("meta", attrs={"name": "description"})
    if not meta_desc or not meta_desc.get("content", "").strip():
        issues.append("Missing meta description"); seo_checks_passed -= 1
    elif len(meta_desc.get("content", "")) > 160:
        issues.append("Meta description too long (over 160 chars)"); seo_checks_passed -= 1

    h1s = soup.find_all("h1")
    if len(h1s) == 0:
        issues.append("No H1 tag found on page"); seo_checks_passed -= 1
    elif len(h1s) > 1:
        issues.append(f"Multiple H1 tags found ({len(h1s)})"); seo_checks_passed -= 1

    text_content = soup.get_text(separator=" ", strip=True)
    word_count = len(text_content.split())
    if word_count < 300:
        issues.append(f"Thin content - only ~{word_count} words on page"); seo_checks_passed -= 1

    if not soup.find("link", attrs={"rel": "canonical"}):
        issues.append("Missing canonical tag"); seo_checks_passed -= 1

    if not soup.find("meta", attrs={"name": "viewport"}):
        issues.append("No mobile viewport meta tag"); acc_checks_passed -= 1

    images = soup.find_all("img")
    missing_alt = [img for img in images if not img.get("alt", "").strip()]
    if images and missing_alt:
        issues.append(f"{len(missing_alt)} of {len(images)} images missing alt text"); acc_checks_passed -= 1

    if load_time > 3:
        issues.append(f"Slow load time ({load_time:.1f}s)")

    parsed = urlparse(r.url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    try:
        robots = requests.get(urljoin(base, "/robots.txt"), headers=HEADERS, timeout=10)
        if robots.status_code != 200:
            issues.append("Missing robots.txt"); bp_checks_passed -= 1
    except requests.RequestException:
        issues.append("Missing robots.txt"); bp_checks_passed -= 1
    try:
        sitemap = requests.get(urljoin(base, "/sitemap.xml"), headers=HEADERS, timeout=10)
        if sitemap.status_code != 200:
            issues.append("Missing sitemap.xml"); bp_checks_passed -= 1
    except requests.RequestException:
        issues.append("Missing sitemap.xml"); bp_checks_passed -= 1

    performance_score = 100 if load_time < 1 else max(0, 100 - int((load_time - 1) * 25))
    seo_score = max(0, round((seo_checks_passed / seo_checks_total) * 100))
    bp_score = max(0, round((bp_checks_passed / bp_checks_total) * 100))
    acc_score = max(0, round((acc_checks_passed / acc_checks_total) * 100))
    overall = round((performance_score + seo_score + bp_score + acc_score) / 4)

    result["scores"] = {
        "overall": overall, "performance": performance_score,
        "seo": seo_score, "best_practices": bp_score, "accessibility": acc_score,
    }
    result["issues"] = issues if issues else ["No major issues found"]

    email = extract_email(r.text)
    if not email:
        contact_link = find_contact_page(soup, base)
        if contact_link:
            try:
                cr = requests.get(contact_link, headers=HEADERS, timeout=15)
                email = extract_email(cr.text)
            except requests.RequestException:
                pass
    result["email"] = email or f"(not found - try info@{parsed.netloc})"
    return result


# ----------------------------------------------------------------------
# UI HELPERS
# ----------------------------------------------------------------------

def time_ago(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return ""
    delta = datetime.now() - dt
    if delta.total_seconds() < 60:
        return "just now"
    if delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() // 60)} min ago"
    if delta.total_seconds() < 86400:
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


# ----------------------------------------------------------------------
# SIDEBAR NAVIGATION
# ----------------------------------------------------------------------

st.sidebar.title("📊 WK Tech Audit")
page = st.sidebar.radio("Navigate", ["Dashboard", "Leads", "Site Audits", "Pipeline", "Reports"], label_visibility="collapsed")

df = load_leads()

# ----------------------------------------------------------------------
# DASHBOARD PAGE
# ----------------------------------------------------------------------

if page == "Dashboard":
    st.title("Dashboard Overview")

    total_leads = len(df)
    audits_completed = int(df["audited"].sum()) if not df.empty else 0
    reports_created = count_reports()
    open_opps = int((df["pipeline_stage"].isin(
        [s for s in PIPELINE_STAGES if s not in ("Closed Won", "Closed Lost")])).sum()) if not df.empty else 0
    clients_converted = int((df["pipeline_stage"] == "Closed Won").sum()) if not df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Leads", total_leads)
    c2.metric("Audits Completed", audits_completed)
    c3.metric("Reports Created", reports_created)
    c4.metric("Open Opportunities", open_opps)
    c5.metric("Clients Converted", clients_converted)

    st.divider()

    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("SEO Score Overview")
        avg_score = df["seo_score"].dropna().mean() if not df.empty and df["seo_score"].notna().any() else 0
        fig = go.Figure(go.Indicator(
            mode="gauge+number", value=avg_score,
            gauge={"axis": {"range": [0, 100]},
                   "bar": {"color": "#1a5f7a"},
                   "steps": [{"range": [0, 30], "color": "#f8d7da"},
                             {"range": [30, 70], "color": "#fff3cd"},
                             {"range": [70, 100], "color": "#d4edda"}]},
            title={"text": "Average Score"}))
        fig.update_layout(height=280, margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.subheader("Website Health")
        if not df.empty and df["audited"].sum() > 0:
            audited_df = df[df["audited"] == 1]
            health = {
                "Performance": audited_df["performance_score"].mean(),
                "SEO": audited_df["seo_sub_score"].mean(),
                "Best Practices": audited_df["best_practices_score"].mean(),
                "Accessibility": audited_df["accessibility_score"].mean(),
            }
            for label, val in health.items():
                st.write(f"{label}: {val:.0f}")
                st.progress(int(val) / 100)
        else:
            st.info("No audits yet.")

    with col3:
        st.subheader("Top Issues")
        if not df.empty:
            all_issues = []
            for issues_str in df["issues"].dropna():
                all_issues.extend([i.strip() for i in issues_str.split(";") if i.strip() and i.strip() != "No major issues found"])
            if all_issues:
                issue_counts = pd.Series(all_issues).value_counts().head(5)
                for issue, count in issue_counts.items():
                    st.write(f"⚠️ {issue}: **{count}**")
            else:
                st.info("No issues logged yet.")
        else:
            st.info("No data yet.")

    st.divider()
    col4, col5, col6 = st.columns(3)

    with col4:
        st.subheader("Recent Leads")
        if not df.empty:
            recent = df.head(5)
            for _, row in recent.iterrows():
                st.write(f"🔵 {row['website'] or row['name']} — {time_ago(row['date_added'])}")
        else:
            st.info("No leads yet — go to 'Leads' to find some.")

    with col5:
        st.subheader("Pipeline Stages")
        if not df.empty:
            stage_counts = df["pipeline_stage"].value_counts().reindex(PIPELINE_STAGES, fill_value=0)
            fig2 = go.Figure(go.Funnel(y=stage_counts.index, x=stage_counts.values))
            fig2.update_layout(height=280, margin=dict(l=20, r=20, t=20, b=20))
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No pipeline data yet.")

    with col6:
        st.subheader("Leads Source")
        if not df.empty:
            source_counts = df["lead_source"].value_counts()
            fig3 = go.Figure(go.Pie(labels=source_counts.index, values=source_counts.values, hole=0.4))
            fig3.update_layout(height=280, margin=dict(l=20, r=20, t=20, b=20), showlegend=True)
            st.plotly_chart(fig3, use_container_width=True)
        else:
            st.info("No source data yet.")

# ----------------------------------------------------------------------
# LEADS PAGE (find + save businesses)
# ----------------------------------------------------------------------

elif page == "Leads":
    st.title("Find & Save Leads")
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        niche = st.text_input("Niche", placeholder="e.g. roofing")
    with col2:
        location = st.text_input("Location", placeholder="e.g. Dallas, Texas")
    with col3:
        limit = st.number_input("Max results", min_value=1, max_value=30, value=10)

    if st.button("Search & Save to Dashboard", type="primary"):
        if not niche or not location:
            st.warning("Enter both a niche and a location.")
        else:
            with st.spinner("Searching..."):
                try:
                    results = find_businesses(niche, location, limit)
                    for b in results:
                        b["niche"] = niche
                        b["location"] = location
                        save_lead(b, lead_source="Organic Search")
                    st.success(f"Found and saved {len(results)} leads to your dashboard.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    st.divider()
    st.subheader("All Leads")
    if not df.empty:
        st.dataframe(df[["id", "name", "phone", "website", "email", "pipeline_stage", "seo_score"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No leads yet.")

# ----------------------------------------------------------------------
# SITE AUDITS PAGE
# ----------------------------------------------------------------------

elif page == "Site Audits":
    st.title("Site Audits")
    st.write("Audit any website. If it matches an existing lead (by name/website), it updates automatically; otherwise it's added as a new lead.")

    audit_name = st.text_input("Business Name", placeholder="e.g. ABC Roofing")
    audit_url = st.text_input("Website URL", placeholder="e.g. https://example.com")

    if st.button("Run Audit", type="primary"):
        if not audit_url:
            st.warning("Enter a website URL.")
        else:
            with st.spinner("Auditing..."):
                try:
                    result = audit_seo(audit_url)
                    st.success(f"Overall Score: {result['scores']['overall']}/100")
                    st.write(f"**Email found:** {result['email']}")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Performance", result["scores"]["performance"])
                    m2.metric("SEO", result["scores"]["seo"])
                    m3.metric("Best Practices", result["scores"]["best_practices"])
                    m4.metric("Accessibility", result["scores"]["accessibility"])
                    st.write("**Issues found:**")
                    for issue in result["issues"]:
                        st.write(f"- {issue}")

                    lead = {"name": audit_name or audit_url, "phone": "", "website": audit_url,
                            "address": "", "email": result["email"], "niche": "", "location": ""}
                    save_lead(lead, audit=result, lead_source="Direct Audit")
                    st.info("Saved to your dashboard leads.")
                except Exception as e:
                    st.error(f"Error: {e}")

# ----------------------------------------------------------------------
# PIPELINE PAGE
# ----------------------------------------------------------------------

elif page == "Pipeline":
    st.title("Pipeline")
    if df.empty:
        st.info("No leads yet — add some from the 'Leads' page first.")
    else:
        for _, row in df.iterrows():
            c1, c2, c3 = st.columns([3, 2, 2])
            c1.write(f"**{row['name']}**  \n{row['website']}")
            c2.write(f"Score: {row['seo_score'] if pd.notna(row['seo_score']) else 'N/A'}")
            new_stage = c3.selectbox("Stage", PIPELINE_STAGES,
                                      index=PIPELINE_STAGES.index(row["pipeline_stage"]) if row["pipeline_stage"] in PIPELINE_STAGES else 0,
                                      key=f"stage_{row['id']}")
            if new_stage != row["pipeline_stage"]:
                update_pipeline_stage(row["id"], new_stage)
                st.rerun()
            st.divider()

# ----------------------------------------------------------------------
# REPORTS PAGE
# ----------------------------------------------------------------------

elif page == "Reports":
    st.title("Reports")
    if df.empty:
        st.info("No data yet to build a report from.")
    else:
        st.write("Export your current leads and audit data as a CSV report.")
        if st.button("Generate CSV Report", type="primary"):
            log_report("CSV Export")
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("Download Report", csv, "wktech_leads_report.csv", "text/csv")
            st.success("Report generated and logged.")
