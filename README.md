# Single-Cell Omics Daily

Single-Cell Omics Daily is an automated static site that aggregates the latest
research papers, tools and datasets across the single-cell ecosystem. A daily
GitHub Actions workflow fetches content from CrossRef, Europe PMC, arXiv and
GitHub, summarises each highlight, and rebuilds the GitHub Pages site.

## Repository structure

```
content/                  # JSON + Markdown payloads and tabular exports
scripts/                  # Data fetching and site generation scripts
templates/                # Jinja2 templates for HTML, RSS, sitemap, robots
assets/                   # Stylesheet, search index and other static assets
.github/workflows/        # CI workflow definitions
index.html                # Placeholder (overwritten by generator)
archive.html              # Placeholder archive page
tags.html                 # Placeholder tags page
requirements.txt          # Python dependencies
```

## Local setup

1. Create and activate a Python 3.11 virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Fetch data (optional API hints below):

   ```bash
   python scripts/fetch_sources.py --days 7
   ```

4. Build pages from the collected payloads:

   ```bash
   python scripts/summarize_and_build.py --window 30
   ```

The generation step rewrites `index.html`, `archive.html`, `tags.html`, `feed.xml`,
`sitemap.xml`, `robots.txt`, `assets/search-index.json` and Markdown files under
`content/`.

## Required secrets

Add the following secrets under **Settings → Secrets and variables → Actions**
for the repository:

| Secret | Purpose |
| ------ | ------- |
| `CROSSREF_MAILTO` | Optional email to access the CrossRef polite pool. |
| `GH_READ_TOKEN` | Optional GitHub token for higher search rate limits. Falls back to `GITHUB_TOKEN`. |
| `PERSONAL_TOKEN` | Only required if Actions needs to push to protected branches. |

No secrets are stored in the repository; scripts only read from environment
variables.

## Scheduled automation

The workflow `.github/workflows/daily_update.yml` runs daily at `07:00`
America/Los_Angeles (`15:00 UTC`). Adjust the CRON in the workflow file to
change the schedule.

## Troubleshooting

- **API rate limiting**: Provide `CROSSREF_MAILTO` and `GH_READ_TOKEN` secrets to
  reduce throttling. The scripts already retry transient failures.
- **Empty result sets**: The generator still produces valid pages. Consider
  widening the keyword list or increasing the `--days` parameter.
- **Git push failures**: Ensure the workflow token has `contents: write`
  permission or supply a PAT via `PERSONAL_TOKEN`.

## License

This project is provided under the MIT License.
