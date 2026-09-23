# Running Agent Capital on a Windows PC as an always-on server

The goal: a spare Windows computer stays on at home, runs the backend and your
Claude Code sessions, and your phone reaches it from anywhere over Tailscale.
Your Mac becomes just another client.

The backend needs tmux and a Unix shell, so it runs inside **WSL2 (Ubuntu)**.
Windows itself only has three jobs: stay awake, boot Ubuntu after a reboot, and
run Tailscale.

Three questions up front:

- **Same Wi-Fi?** No. Tailscale connects the phone and the PC over an encrypted
  tunnel from anywhere (cellular included). Nothing is opened on your router and
  nothing is public — only devices signed in to your Tailscale account can see it.
- **Internet all the time?** Yes, for the PC. Claude Code calls Anthropic on every
  turn and Tailscale needs the internet to connect the two devices. Use Ethernet if
  you can; Wi-Fi is the usual reason a home server "randomly" disappears.
- **How does it never stop?** Four things can stop it and each has its own fix
  below: sleep/hibernate, Windows Update reboots, WSL shutting down, the backend
  crashing.

## Part 1 — Windows: install WSL and Ubuntu

Open **PowerShell as Administrator**:

```powershell
wsl --install -d Ubuntu
```

Reboot if asked. Ubuntu opens and asks for a Unix username and password — this is
the account the backend runs as. Then keep WSL current:

```powershell
wsl --update
```

## Part 2 — Ubuntu: clone and run the setup script

In the Ubuntu window:

```bash
sudo apt-get install -y git
git clone https://github.com/charvi-pattila/agent-capital.git ~/code/my-claude/agent-capital
cd ~/code/my-claude/agent-capital
scripts/setup-linux.sh
```

The script installs tmux, Node, the Claude Code CLI and the Python venv, builds
the UI, asks you for a dashboard password, and enables systemd in WSL. **The
first run ends by asking you to restart WSL** (systemd only starts on a fresh
boot). In PowerShell:

```powershell
wsl --shutdown
```

Reopen Ubuntu and run `scripts/setup-linux.sh` again — it skips everything that
is done and installs the service. You should see
`OK: agent-capital is running ... on http://localhost:8888`.

Then log Claude Code in on this machine, once:

```bash
claude
```

Type `/login`, open the printed URL on any device, sign in, paste the code back,
then quit with `/exit`. Sessions started from the dashboard fail until this is done.

Check from Windows: open <http://localhost:8888> in a browser on the PC. You
should get the login page. (WSL forwards its ports to Windows' localhost by
default; if this doesn't load, see *Troubleshooting*.)

## Part 3 — Windows: never sleep

PowerShell as Administrator:

```powershell
powercfg /change standby-timeout-ac 0     # never sleep on mains power
powercfg /change hibernate-timeout-ac 0
powercfg /h off                           # disable hibernate entirely
powercfg /change monitor-timeout-ac 10    # screen off is fine
```

If it is a laptop: Settings → System → Power → *Lid, power & sleep button
controls* → **When I close the lid: Do nothing** (plugged in). Keep it on mains.

Windows Update will still reboot occasionally. You cannot fully prevent that;
set **Settings → Windows Update → Advanced → Active hours** to cover your working
day, and rely on Parts 4 and 5 to bring everything back after the reboot.

## Part 4 — Windows: keep Ubuntu running and boot it automatically

WSL does not start at boot on its own, and it **shuts Ubuntu down a few seconds
after the last Ubuntu window closes** — systemd running inside does not keep it
alive (verified 2026-09-09: closing the terminal took the service down). So one
scheduled task does both jobs: it starts Ubuntu at logon and holds a tiny
process open inside it forever. PowerShell **as Administrator** (`-RunLevel
Highest` fails with "Access denied" from a normal shell):

```powershell
$action   = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu --exec sleep infinity"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Agent Capital WSL keepalive" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest
Start-ScheduledTask -TaskName "Agent Capital WSL keepalive"
```

`-ExecutionTimeLimit 0` matters: the default kills a task after 3 days. Also
stop WSL from idling out the whole VM — create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
vmIdleTimeout=-1
```

Check: close every Ubuntu window, wait 30 seconds, open <http://localhost:8888>
on the PC. It should still load.

That runs at **logon**, so Windows has to sign in by itself after a reboot:

1. Press Win+R, run `netplwiz`.
2. Untick *Users must enter a user name and password*, enter your password.
   (If the box is missing: Settings → Accounts → Sign-in options → turn off
   *For improved security, only allow Windows Hello sign-in*, then retry.)

The PC will sit at an unlocked desktop after a reboot; that is the trade-off for
unattended recovery. Lock it by hand (Win+L) when you walk away — a locked
session keeps everything running.

Test it: reboot the PC, wait a minute, open <http://localhost:8888> on it.

## Part 5 — Tailscale: reach it from the phone

On the **Windows** side (not inside Ubuntu):

1. Install Tailscale from <https://tailscale.com/download/windows> and sign in.
   It runs as a Windows service, so it starts before login after every reboot.
2. In the admin console <https://login.tailscale.com/admin/dns>: enable
   **MagicDNS** and **HTTPS Certificates**.
3. PowerShell:

   ```powershell
   tailscale serve --bg 8888
   ```

   This publishes `https://<pc-name>.<tailnet>.ts.net` (port 443, real
   certificate, renewed automatically) and forwards it to the backend on
   localhost:8888. `--bg` makes it persist across reboots. `tailscale serve status`
   shows the exact URL.

4. Tell the dashboard its address so the About tab shows it with a QR code
   (Ubuntu window):

   ```bash
   cd ~/code/my-claude/agent-capital
   scripts/setup-linux.sh --public-url https://<pc-name>.<tailnet>.ts.net
   ```

On the **iPhone**: install Tailscale from the App Store, sign in with the same
account, toggle it on (it stays on; battery impact is negligible). Open the
`https://...ts.net` URL in Safari, log in with the dashboard password, then
Share → **Add to Home Screen**. Open it from the Home Screen icon, tap
**Alerts** in the bottom bar, type your name, **Turn on**, allow notifications,
then **Send test** — that is the "needs your response" push (README → Phone
notifications). It only works from the installed app, not a Safari tab.

Since the certificate and TLS are handled by `tailscale serve`, the
`scripts/tailscale-https.sh` route from `docs/REMOTE-ACCESS.md` is not needed
here; the backend stays plain HTTP on localhost inside WSL.

## Part 6 — Your projects

Claude Code works on repositories that live on the server. Either clone them
there by hand under `~/code/my-claude/` (the default home for new projects), or
create them from the dashboard's **New Project** — a relative path resolves
under that directory.

Bringing the Mac's dashboard state over is optional. `data/` holds the project
list, memory and reports; copying it works, but each project's `path` in
`data/projects/<id>/meta.json` still points at the Mac (`/Users/...`) and has to
be edited to the Ubuntu path (`/home/<you>/...`) before the project can start.
Recreating the projects on the server is usually less work.

## Day to day (Ubuntu window)

```bash
journalctl -u agent-capital -f                 # live log
systemctl status agent-capital                 # running? pid?
sudo systemctl restart agent-capital           # after git pull (closes running sessions first, like Close All)
tmux attach -t claude_<project-id>             # look at a session directly; Ctrl-B d to detach
scripts/uninstall-service-linux.sh             # remove the service
```

Updating: `git pull`, then `scripts/setup-linux.sh` (rebuilds the UI and
restarts the service).

## Troubleshooting

- **`http://localhost:8888` works on the PC but the Tailscale URL gives a 502.**
  Windows → WSL localhost forwarding dropped (it sometimes does after
  `wsl --shutdown`). Restarting the service fixes it: `sudo systemctl restart
  agent-capital`. If it keeps happening, switch WSL to mirrored networking: create
  `C:\Users\<you>\.wslconfig` with

  ```ini
  [wsl2]
  networkingMode=mirrored
  ```

  then `wsl --shutdown` and reopen Ubuntu. In mirrored mode WSL shares Windows'
  network interfaces directly, including Tailscale's.
- **Phone shows the page but the terminal never updates.** The terminal is a
  server-sent event stream. `tailscale serve` passes those through unbuffered;
  if you put anything else in front (a different reverse proxy), it must not
  buffer `text/event-stream` responses.
- **Sessions fail to start / "claude: not found" in the log.** Claude is not
  logged in, or not installed for the user the service runs as. Run `claude` in
  the Ubuntu window as that user.
- **Ubuntu is not running after a reboot.** Check the scheduled task ran
  (Task Scheduler → Task Scheduler Library → *Agent Capital WSL keepalive* → Last Run
  Result), and that Windows actually signed in (Part 4).
- **The service is up but the About tab has no public address.** Re-run
  `scripts/setup-linux.sh --public-url https://...` — or the backend can read it
  from Tailscale itself if `tailscale.exe` is at its default install path.
