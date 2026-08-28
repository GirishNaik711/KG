#!/usr/bin/env python3
"""
Generate professional release summary documents (HTML and/or DOCX) from structured PR data.

Usage:
    python generate_document.py --data-file data.json --format both --output-dir ./output
    python generate_document.py --data '<json string>' --format html --output-dir ./output
"""

import argparse
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SCRIPT_DIR.parent / "assets"
BANNER_PATH = ASSETS_DIR / "banner.png"


def _get_banner_base64() -> str:
    """Read the banner image and return as a base64 data URI."""
    if not BANNER_PATH.exists():
        return ""
    with open(BANNER_PATH, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def generate_html(data: dict, output_dir: str) -> str:
    """Generate a styled HTML release summary document."""

    branch = data.get("branch", "develop")
    start_date = data.get("start_date", "")
    end_date = data.get("end_date", "")
    total_prs = data.get("total_prs", 0)
    contributors = data.get("contributors", [])
    executive_summary = data.get("executive_summary", "")
    themes = data.get("themes", [])

    date_range = f"{_format_date(start_date)} &ndash; {_format_date(end_date)}"

    # Build theme sections
    theme_sections = ""
    for theme in themes:
        pr_rows = ""
        for pr in theme.get("prs", []):
            pr_link = f'<a href="{pr.get("url", "#")}" target="_blank">#{pr["number"]}</a>'
            pr_rows += f"""
                <tr>
                    <td class="pr-number">{pr_link}</td>
                    <td>{_esc(pr.get("title", ""))}</td>
                    <td class="pr-author">{_esc(pr.get("author", ""))}</td>
                    <td class="pr-summary">{_esc(pr.get("summary", ""))}</td>
                    <td class="pr-stats">
                        <span class="additions">+{pr.get("additions", 0)}</span>
                        <span class="deletions">-{pr.get("deletions", 0)}</span>
                    </td>
                </tr>"""

        theme_sections += f"""
        <div class="theme-section">
            <h2 class="theme-title">{_esc(theme.get("name", ""))}</h2>
            <p class="theme-narrative">{_esc(theme.get("narrative", ""))}</p>
            <table class="pr-table">
                <thead>
                    <tr>
                        <th>PR</th>
                        <th>Title</th>
                        <th>Author</th>
                        <th>Summary</th>
                        <th>Changes</th>
                    </tr>
                </thead>
                <tbody>{pr_rows}
                </tbody>
            </table>
        </div>"""

    # Build full PR list
    all_prs = []
    for theme in themes:
        all_prs.extend(theme.get("prs", []))
    all_prs.sort(key=lambda p: p.get("merged_at", ""), reverse=True)

    pr_list_rows = ""
    for pr in all_prs:
        pr_link = f'<a href="{pr.get("url", "#")}" target="_blank">#{pr["number"]}</a>'
        merged = _format_datetime(pr.get("merged_at", ""))
        pr_list_rows += f"""
                <tr>
                    <td class="pr-number">{pr_link}</td>
                    <td>{_esc(pr.get("title", ""))}</td>
                    <td class="pr-author">{_esc(pr.get("author", ""))}</td>
                    <td>{merged}</td>
                    <td class="pr-stats">
                        <span class="additions">+{pr.get("additions", 0)}</span>
                        <span class="deletions">-{pr.get("deletions", 0)}</span>
                    </td>
                </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Release Summary &mdash; {_esc(branch)} &mdash; {date_range}</title>
    <style>
        :root {{
            --primary: #1a365d;
            --primary-light: #2b6cb0;
            --accent: #3182ce;
            --bg: #f7fafc;
            --card-bg: #ffffff;
            --text: #2d3748;
            --text-light: #718096;
            --border: #e2e8f0;
            --success: #38a169;
            --danger: #e53e3e;
        }}

        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
        }}

        .container {{
            max-width: 960px;
            margin: 0 auto;
            padding: 40px 24px;
        }}

        .banner {{
            width: 100%;
            border-radius: 12px 12px 0 0;
            display: block;
        }}

        .header {{
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
            color: white;
            padding: 0;
            border-radius: 12px;
            margin-bottom: 32px;
            overflow: hidden;
        }}

        .header-text {{
            padding: 24px 40px 32px;
        }}

        .header h1 {{
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
        }}

        .header .meta {{
            font-size: 15px;
            opacity: 0.9;
        }}

        .header .meta span {{
            margin-right: 24px;
        }}

        .stats-bar {{
            display: flex;
            gap: 16px;
            margin-bottom: 32px;
        }}

        .stat-card {{
            flex: 1;
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            text-align: center;
        }}

        .stat-card .number {{
            font-size: 32px;
            font-weight: 700;
            color: var(--primary);
        }}

        .stat-card .label {{
            font-size: 13px;
            color: var(--text-light);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .section {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 32px;
            margin-bottom: 24px;
        }}

        .section h2 {{
            font-size: 20px;
            color: var(--primary);
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid var(--border);
        }}

        .executive-summary {{
            font-size: 16px;
            line-height: 1.8;
            color: var(--text);
        }}

        .theme-section {{
            margin-bottom: 32px;
        }}

        .theme-title {{
            font-size: 18px;
            color: var(--primary-light);
            margin-bottom: 8px;
            padding-bottom: 6px;
            border-bottom: 1px solid var(--border);
        }}

        .theme-narrative {{
            font-size: 15px;
            color: var(--text);
            margin-bottom: 16px;
            line-height: 1.7;
        }}

        .pr-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}

        .pr-table th {{
            text-align: left;
            padding: 10px 12px;
            background: var(--bg);
            color: var(--text-light);
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 2px solid var(--border);
        }}

        .pr-table td {{
            padding: 10px 12px;
            border-bottom: 1px solid var(--border);
            vertical-align: top;
        }}

        .pr-table tr:last-child td {{
            border-bottom: none;
        }}

        .pr-number a {{
            color: var(--accent);
            text-decoration: none;
            font-weight: 600;
        }}

        .pr-number a:hover {{
            text-decoration: underline;
        }}

        .pr-author {{
            color: var(--text-light);
            white-space: nowrap;
        }}

        .pr-summary {{
            color: var(--text);
            max-width: 280px;
        }}

        .pr-stats {{
            white-space: nowrap;
        }}

        .additions {{
            color: var(--success);
            font-weight: 600;
            margin-right: 6px;
        }}

        .deletions {{
            color: var(--danger);
            font-weight: 600;
        }}

        .footer {{
            text-align: center;
            font-size: 13px;
            color: var(--text-light);
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid var(--border);
        }}

        @media print {{
            body {{ background: white; }}
            .container {{ padding: 0; }}
            .header {{ border-radius: 0; }}
            .section {{ break-inside: avoid; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            {"<img class='banner' src='" + _get_banner_base64() + "' alt='AAH Banner' />" if _get_banner_base64() else ""}
            <div class="header-text">
                <h1>Release Summary</h1>
                <div class="meta">
                    <span>Branch: <strong>{_esc(branch)}</strong></span>
                    <span>Period: <strong>{date_range}</strong></span>
                </div>
            </div>
        </div>

        <div class="stats-bar">
            <div class="stat-card">
                <div class="number">{total_prs}</div>
                <div class="label">Pull Requests Merged</div>
            </div>
            <div class="stat-card">
                <div class="number">{len(contributors)}</div>
                <div class="label">Contributors</div>
            </div>
            <div class="stat-card">
                <div class="number">{len(themes)}</div>
                <div class="label">Feature Themes</div>
            </div>
        </div>

        <div class="section">
            <h2>Executive Summary</h2>
            <p class="executive-summary">{_esc(executive_summary)}</p>
        </div>

        <div class="section">
            <h2>Changes by Theme</h2>
            {theme_sections}
        </div>

        <div class="section">
            <h2>Complete PR List</h2>
            <table class="pr-table">
                <thead>
                    <tr>
                        <th>PR</th>
                        <th>Title</th>
                        <th>Author</th>
                        <th>Merged</th>
                        <th>Changes</th>
                    </tr>
                </thead>
                <tbody>{pr_list_rows}
                </tbody>
            </table>
        </div>

        <div class="footer">
            Generated on {datetime.now().strftime("%B %d, %Y at %I:%M %p")}
        </div>
    </div>
</body>
</html>"""

    filename = f"release-summary-{branch}-{start_date}-to-{end_date}.html"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


def generate_docx(data: dict, output_dir: str) -> str:
    """Generate a styled Word (.docx) release summary document."""
    try:
        from docx import Document
        from docx.shared import Inches, Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
    except ImportError:
        print("ERROR: python-docx is required for .docx generation.", file=sys.stderr)
        print("Install it with: pip install python-docx", file=sys.stderr)
        sys.exit(1)

    branch = data.get("branch", "develop")
    start_date = data.get("start_date", "")
    end_date = data.get("end_date", "")
    total_prs = data.get("total_prs", 0)
    contributors = data.get("contributors", [])
    executive_summary = data.get("executive_summary", "")
    themes = data.get("themes", [])

    doc = Document()

    # -- Page margins --
    for section in doc.sections:
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(2.5)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    # -- Styles --
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)
    font.color.rgb = RGBColor(0x2D, 0x37, 0x48)

    # -- Banner image --
    if BANNER_PATH.exists():
        banner_para = doc.add_paragraph()
        banner_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        banner_para.space_after = Pt(16)
        page_width = section.page_width - section.left_margin - section.right_margin
        banner_run = banner_para.add_run()
        banner_run.add_picture(str(BANNER_PATH), width=page_width)

    # -- Title --
    title = doc.add_heading("Release Summary", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in title.runs:
        run.font.color.rgb = RGBColor(0x1A, 0x36, 0x5D)
        run.font.size = Pt(28)

    # -- Subtitle --
    subtitle = doc.add_paragraph()
    subtitle_run = subtitle.add_run(
        f"Branch: {branch}  |  {_format_date(start_date)} to {_format_date(end_date)}"
    )
    subtitle_run.font.size = Pt(12)
    subtitle_run.font.color.rgb = RGBColor(0x71, 0x80, 0x96)
    subtitle.space_after = Pt(4)

    # -- Stats line --
    stats = doc.add_paragraph()
    stats_run = stats.add_run(
        f"{total_prs} PRs merged  |  {len(contributors)} contributors  |  {len(themes)} themes"
    )
    stats_run.font.size = Pt(11)
    stats_run.font.bold = True
    stats_run.font.color.rgb = RGBColor(0x2B, 0x6C, 0xB0)
    stats.space_after = Pt(20)

    # -- Executive Summary --
    doc.add_heading("Executive Summary", level=1)
    p = doc.add_paragraph(executive_summary)
    p.space_after = Pt(16)

    # -- Themes --
    doc.add_heading("Changes by Theme", level=1)
    for theme in themes:
        doc.add_heading(theme.get("name", ""), level=2)
        narrative = doc.add_paragraph(theme.get("narrative", ""))
        narrative.space_after = Pt(8)

        prs = theme.get("prs", [])
        if prs:
            table = doc.add_table(rows=1, cols=4)
            table.style = "Light List Accent 1"
            table.alignment = WD_TABLE_ALIGNMENT.LEFT

            # Header
            headers = ["PR", "Title", "Author", "Summary"]
            for i, h in enumerate(headers):
                cell = table.rows[0].cells[i]
                cell.text = h
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.bold = True
                        run.font.size = Pt(10)

            # Rows
            for pr in prs:
                row = table.add_row()
                row.cells[0].text = f"#{pr.get('number', '')}"
                row.cells[1].text = pr.get("title", "")
                row.cells[2].text = pr.get("author", "")
                row.cells[3].text = pr.get("summary", "")
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(9)

        doc.add_paragraph("")  # spacer

    # -- Full PR list --
    doc.add_heading("Complete PR List", level=1)
    all_prs = []
    for theme in themes:
        all_prs.extend(theme.get("prs", []))
    all_prs.sort(key=lambda p: p.get("merged_at", ""), reverse=True)

    if all_prs:
        table = doc.add_table(rows=1, cols=5)
        table.style = "Light List Accent 1"
        table.alignment = WD_TABLE_ALIGNMENT.LEFT

        headers = ["PR", "Title", "Author", "Merged", "Changes"]
        for i, h in enumerate(headers):
            cell = table.rows[0].cells[i]
            cell.text = h
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.bold = True
                    run.font.size = Pt(10)

        for pr in all_prs:
            row = table.add_row()
            row.cells[0].text = f"#{pr.get('number', '')}"
            row.cells[1].text = pr.get("title", "")
            row.cells[2].text = pr.get("author", "")
            row.cells[3].text = _format_datetime(pr.get("merged_at", ""))
            additions = pr.get("additions", 0)
            deletions = pr.get("deletions", 0)
            row.cells[4].text = f"+{additions} / -{deletions}"
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(9)

    # -- Footer --
    doc.add_paragraph("")
    footer = doc.add_paragraph()
    footer_run = footer.add_run(
        f"Generated on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}"
    )
    footer_run.font.size = Pt(9)
    footer_run.font.color.rgb = RGBColor(0x71, 0x80, 0x96)
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER

    filename = f"release-summary-{branch}-{start_date}-to-{end_date}.docx"
    filepath = os.path.join(output_dir, filename)
    doc.save(filepath)
    return filepath


# -- Helpers --

def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_date(date_str: str) -> str:
    """Format YYYY-MM-DD to readable date."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return date_str


def _format_datetime(dt_str: str) -> str:
    """Format ISO datetime to readable date."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return dt_str


def main():
    parser = argparse.ArgumentParser(description="Generate release summary documents")
    parser.add_argument("--data", help="JSON string of PR data")
    parser.add_argument("--data-file", help="Path to JSON file with PR data")
    parser.add_argument(
        "--format",
        choices=["html", "docx", "both"],
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument("--output-dir", default=".", help="Output directory")
    parser.add_argument("--title", default="Release Summary", help="Document title")
    parser.add_argument("--branch", help="Override branch name in data")
    parser.add_argument("--start-date", help="Override start date in data")
    parser.add_argument("--end-date", help="Override end date in data")

    args = parser.parse_args()

    # Load data
    if args.data_file:
        with open(args.data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif args.data:
        data = json.loads(args.data)
    else:
        print("ERROR: Provide --data or --data-file", file=sys.stderr)
        sys.exit(1)

    # Apply overrides
    if args.branch:
        data["branch"] = args.branch
    if args.start_date:
        data["start_date"] = args.start_date
    if args.end_date:
        data["end_date"] = args.end_date

    # Ensure output dir exists
    os.makedirs(args.output_dir, exist_ok=True)

    # Generate
    outputs = []
    fmt = args.format

    if fmt in ("html", "both"):
        path = generate_html(data, args.output_dir)
        outputs.append(path)
        print(f"HTML: {path}")

    if fmt in ("docx", "both"):
        path = generate_docx(data, args.output_dir)
        outputs.append(path)
        print(f"DOCX: {path}")

    return outputs


if __name__ == "__main__":
    main()
