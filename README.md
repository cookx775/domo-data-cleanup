# Domo Cleanup — Start here

Find cleanup opportunities, review them together, and keep a shared decision history in your
Domo instance. The tool writes findings and decisions to AppDB. It never changes or deletes
candidate datasets or dataflows.

## 1. Prepare

You need Python 3.10 to 3.15, your Domo hostname and developer token, and access to your
instance's DomoStats and Domo Governance reports. DataSets and Dataflow Details are mandatory;
the browser checks the rest and explains coverage gaps. Detailed report requirements are in
`app/PREREQUISITES.md` if your administrator needs them.

Your token also needs access to shared cleanup storage. The app automatically finds the
**Cleanup Runs** and **Cleanup Review** collections, or creates them in **Domo Instance Cleanup**
when none are visible. Before another administrator starts, give their account access to the
existing shared store. An inaccessible store cannot be discovered. Ambiguous or incomplete
storage stops setup with an explanation.

## 2. Download and install once

Download **Domo-Cleanup.zip** from the handoff folder. Extract it fully into a permanent local
folder (on Windows: right-click the zip, **Extract All**; never double-click a file from inside
the zip, because Windows then copies only that one file). The extracted `domo-cleanup` folder
contains this guide, two installers, two launchers, and `app/`.

Double-click **Install Domo Cleanup.bat** on Windows or **Install Domo Cleanup.command** on Mac.
The installer finds Python 3.10 or newer, creates a private Python environment in a `.venv`
folder next to `app/`, installs the tool into it, and finishes with `install ok`. If it stops, it
says which step failed; send that text to Dalton. Nothing outside the `domo-cleanup` folder
changes. On a Mac that blocks the file the first time, right-click it and choose **Open**.

**No Python 3.10 or newer?** Install it from python.org; on Windows tick **Add python.exe to PATH**
and keep **py launcher** selected. If Windows has the Python Install Manager, `py install 3.13`
also works. Then close the installer window and double-click the installer again. A `python`
that opens the Microsoft Store is not Python. Run the installer again whenever you install a
different Python or a new release of this tool.

**If the installer cannot run** (for example, company policy blocks it), open Command Prompt
(Windows) or Terminal (Mac) in the `domo-cleanup` folder and run the same steps one line at a
time. On Windows, open the folder in File Explorer, click the address bar, type `cmd`, and press
Enter. Install from this folder with `-e ./app`; `-e .` fails here because the program is in `app/`.

**Windows:**

```text
py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install --prefer-binary --find-links .\app\wheels -e .\app
.venv\Scripts\python -c "import requests, yaml, jsonschema; print('install ok')"
```

Use `python` in place of `py -3` if `py` is not recognized.

**Mac:**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --prefer-binary --find-links ./app/wheels -e ./app
.venv/bin/python -c "import requests, yaml, jsonschema; print('install ok')"
```

The last line must print `install ok`. The launchers use this `.venv` automatically. If company
policy blocks installing Python or running these files, ask IT for help.

## 3. Open and connect

Double-click **Open Domo Cleanup.command** on Mac or **Open Domo Cleanup.bat** on Windows.
Keep the launcher window open while you work. If the browser does not open, use the exact
`http://127.0.0.1:...` address printed in that window. The launcher uses the installer's `.venv`
first. The Windows launcher checks the app folder, Python, and the install, and names the first
one that fails before it pauses; send that text to Dalton. If a double-click closes instantly
with no text, run the launcher from Command Prompt in the `domo-cleanup` folder instead:
`"Open Domo Cleanup.bat"`.

Enter your hostname, developer token, and the requested profile details. Credentials stay on
this computer. Shared decisions are attributed to the token owner's Domo identity.

The app connects to shared storage automatically. You do not need to enter a collection ID.
If findings already exist, open them to continue reviewing without collecting again. For a new
run, choose **Check setup**, confirm the correct report datasets, and run cleanup. If discovery
misses a report, choose **Enter another dataset ID** and validate it before confirming.

## 4. Review and submit

- Read the coverage notes first. Missing evidence does not mean an asset is clean.
- Filter by owner. Expand a candidate to see its evidence and decision controls.
- Hover over dotted glossary terms for definitions.
- Choose decisions, enter reasons, preview the readable summary, and **Submit decisions**.
- Existing decisions show who made them. Discuss changes with that reviewer when needed.

Decisions are saved to AppDB and used by the next administrator and subsequent runs. Reload to
see new shared decisions. If the review changed before you submit, reload and review again.
Submitting requires a working AppDB connection; do not assume an interrupted submission saved.
Mark an asset quarantined or deleted only after someone actually performs that action in Domo.

Reports are browser views. Owner reports are generated by filtering; downloads are optional.
Normal shared runs do not create local findings files, decision ledgers, or report folders.

## Next time, updates, and help

Double-click the same launcher and choose your saved instance. Shared history lives in AppDB;
local settings, tokens, and setup receipts live in `app/instances/`, created on first use.
Keep that folder private. Never upload a used installation or saved token to Box.

**Updating to a new release:** close the launcher. Extract the new release into a new folder.
Copy your existing `app/instances/` folder, which holds each instance's `config.yaml` and `.env`,
into the new folder's `app/`. Then double-click the new folder's installer and open the app from
the new folder. Do not copy `.venv`; the installer creates a fresh one. Keep the old folder
until the new one resumes your instance correctly.

**Updating from the older layout:** extract this release separately. Copy the old installation's
`instances/` into the new `app/` folder, then install and launch the new package. Keep the old
installation until verified. If it contains local findings or decisions, use the browser's
legacy import workflow and verify the imported history. Do not discard the old local history.

For help, send Dalton the error message, operating system, Python version, and run ID.
The installers and launchers have been tested on Mac. The Windows `.bat` files and a second
administrator on a separate machine still need independent acceptance. AppDB access must be
set up for each administrator.

`app/` contains the supporting program files; you do not need to browse or edit them for normal
use. Advanced command documentation and optional AI prompt templates remain in the source
repository. A standalone application without a Python installation is a future exploration.
