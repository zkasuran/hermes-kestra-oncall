# How the README pictures were made

All four files in `assets/` come from the end-to-end rig (Kestra 2.0.9, a real Hermes gateway and model). The rig is only read.

1. Copy the rig's `state.db` and the plugin store with the SQLite backup API into a scratch `HERMES_HOME` (`/tmp/hko-docs-home-shots`) that also has `plugins/kestra-oncall` linked to this repo and the plugin settings in `config.yaml`.
2. `hermes dashboard` on that scratch home with the Kestra credentials in the environment, then `python docs/screenshots/shot_dashboard.py assets/03-dashboard.png 2 INC-<id>` (1600x1000 CSS px, device scale 2).
3. `python docs/screenshots/render_chat.py --state-db ... --session ... --typed-output ... --typed-text ... --store-db ... --incident ... --out chat.html` then `python docs/screenshots/shot_html.py chat.html assets/01-chat-diagnosis.png 2`. The script reads the reply, the approval prompt and the stored apply result from the recorded data. See its docstring for the arguments.
4. The Kestra picture is the replay execution's Gantt tab in the Kestra UI, captured with Playwright after logging in on the login page (credentials from the rig env file, never on screen), both task rows expanded.
5. `python docs/screenshots/shot_banner.py` renders `banner.html` (2400x1200).

Playwright and Chrome are only needed to regenerate the pictures. The plugin does not use them.

## The run behind the current pictures

Taken on 2026-11-09 after `rig/reset.sh --yes && rig/up.sh`, with `python tests/e2e/run_e2e.py --only S1,S2,S3 --out /tmp/...` (a separate run from the one in `evidence/`, so the ids differ):
incident `INC-8af8a0db`, failing execution `3B4DkIMzndVTxtmb6LVTRW`, base revision 46, fixed revision 47, replay `52WuudaIdSCTsT748URYBd`, webhook session `20261109_130332_bfee8ed3` (model `gpt-6-astra`).
The typed command was `/kestra apply INC-8af8a0db 8a61e96c5ec2`, exactly the command the model printed.
