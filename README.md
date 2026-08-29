# AbyssBeacon

**Search deeper. Find models sooner.**

A local model discovery and download manager for Hugging Face, CivitAI, CivitAI Red, ModelScope, TensorHub Art, and SeaArt.

AbyssBeacon brings multiple model sources into one local interface so you can find new releases, track updates, inspect previews and metadata, follow creators, and manage downloads without checking each site individually.

<img width="2353" height="1235" alt="main feed" src="https://github.com/user-attachments/assets/36df0552-cdea-4e99-823a-6c942b427f75" />

## Features

- Scan multiple supported model sources from one interface.
- Track new, updated, viewed, favorite, and downloaded models.
- Filter by source, architecture, model type, access state, favorites, and download status.
- Browse model images and video previews.
- Search supported sources directly without changing the normal scan registry.
- Use Discovery scans and creator tools to find models outside normal architecture searches.
- Track downloadable, gated, paid, and early-access states where the source provides enough information.
- Download through the browser or use the Local Installer for a ComfyUI model library.
- Pause and resume supported local downloads, including across AbyssBeacon restarts.
- Keep model information and preview images beside locally installed models.
- Configure scan limits and retention windows to control how deeply sources are searched.
- Hide mature content by default, with user-controlled visibility settings.
- Block creators from individual sources or use supported universal creator exclusions.
- Run locally with a SQLite database and local settings.

<img width="1559" height="1116" alt="card_details" src="https://github.com/user-attachments/assets/499768cb-044f-43f1-88e0-7a269aa114bc" />

## Supported Sources

| Source | Normal Scan | Search / Discovery | Downloads |
| --- | --- | --- | --- |
| Hugging Face | Yes | Yes | Yes |
| CivitAI | Yes | Yes | Yes |
| CivitAI Red | Yes | Yes | Yes |
| ModelScope | Yes | Yes | Yes |
| TensorHub Art | Yes | Yes | Yes |
| SeaArt | Yes | Yes | Source-dependent |

Source websites, APIs, authentication requirements, and page structures can change without notice. A source may temporarily require an AbyssBeacon update if its service changes.

## Getting Started

### Requirements

- Windows
- Python 3 available from the command line
- Internet access
- Firefox, Chrome, or Microsoft Edge if you use the SeaArt browser-session connection

Windows is the tested platform for the initial release.

### Start AbyssBeacon

1. Download or clone the repository.
2. Open the `AbyssBeacon` folder.
3. Double-click `Start AbyssBeacon.bat`.
4. On first launch, AbyssBeacon creates a local Python virtual environment and installs the packages listed in `requirements.txt`.
5. Open `http://127.0.0.1:5000` in your browser when the terminal says AbyssBeacon is ready.

The first startup can take longer while Python packages are installed.

## First Run

Fresh installations start with conservative defaults:

- All supported sources and tested model families are available.
- No sources or model families are preselected in the Scan window.
- Normal scans use a global maximum of 150 results per source and architecture search.
- Automatic Retention is off by default.
- Mature models are hidden by default.
- Downloads use the browser by default.
- The Local Installer has no ComfyUI folder configured until you choose one.

Open Settings to change these defaults before your first large scan.

## Scanning

Use the `SCAN` button to choose the sources and model families for a normal scan.

<img width="1072" height="1184" alt="scan_details" src="https://github.com/user-attachments/assets/acfae5da-0ffa-4f0b-bf55-3b5cbef4250c" />

AbyssBeacon supports centralized scan limits:

- A finite result limit stops a source and architecture search at that result ceiling.
- If Automatic Retention is enabled, the scan stops at the result limit or retention-day boundary, whichever comes first.
- Unlimited result mode uses the retention window as the stopping boundary.
- Individual sources can use lower limits than the global setting.

Individual services can still impose their own backend or pagination limits.

## Search Sources and Discovery

Normal scans use the tested model-family definitions configured in AbyssBeacon.

The main search box filters models already in your AbyssBeacon feed.

Use Search Sources when you want to look for an arbitrary model name or keyword without adding it to the normal scan registry. Type `search` in the main search box to open Search Sources.

Discovery tools can use source-specific tags and creator information to find models outside the normal architecture searches. Type `discover` in the main search box to open Discovery.

Additional explanations are available by hovering over controls inside AbyssBeacon.

## Downloads and ComfyUI

The default download behavior sends files to your browser.

To use the Local Installer:

1. Open Settings.
2. Go to Library.
3. Set your ComfyUI folder.
4. Choose `Install to local library` as the download behavior.

The default Local Installer layout is:

`AbyssBeacon / model`

AbyssBeacon keeps original filenames by default and keeps both files if an older version already exists.

When enabled, model info and preview images are saved beside installed models.

## SeaArt

SeaArt discovery uses a local browser session because its website signs requests in the frontend.

AbyssBeacon can launch an isolated local browser profile for this connection. Firefox is the default browser choice. The saved browser profile stays on your machine and is excluded from the repository by `.gitignore`.

## Privacy and Local Data

AbyssBeacon is designed as a local application.

Local runtime data can include:

- `models.db`
- `settings.json`
- `secrets.json`
- browser-session profiles
- cached previews
- download state

The public repository excludes credentials, browser profiles, databases, caches, partial downloads, and other machine-specific runtime data.

If you add source credentials, they remain local unless you deliberately copy or publish those files yourself.

## Mature Content

Fresh installations default to `Hide Mature`.

Users can change mature-content visibility in Settings. Source metadata is not always consistent, so no automated classification should be treated as perfect.

## Current Scope

AbyssBeacon is focused on discovering, tracking, inspecting, and downloading models from the currently supported sources.

The first public release is Windows-focused and has been built primarily around local ComfyUI workflows. Source integrations may need maintenance as third-party websites and APIs change.

## Reporting Problems

If something breaks, an issue is most useful when it includes:

- the source being scanned
- the model or creator involved, when relevant
- what you expected to happen
- what happened instead
- terminal output around the failure
- a screenshot when the problem is visual

Do not post API keys, cookies, session tokens, passwords, or your `secrets.json` file in a public issue.

## Project Status

AbyssBeacon is actively developed. This is the first public release, so issues may still appear, especially in integrations that depend on third-party websites and APIs.

## License

AbyssBeacon is licensed under the **GNU General Public License v3.0 only (GPL-3.0-only)**.

You may use, study, modify, and redistribute the project under the terms of GPL-3.0. If you distribute a modified version or other derivative covered by the GPL, the corresponding source must remain available under the GPL terms.

The GPL does not prohibit charging money for copies or distributions. It does require distributed GPL-covered derivatives to preserve the license and source-code freedoms.

See [`LICENSE`](LICENSE) for the complete license text.

Copyright (C) 2026 AbyssBeacon contributors.
