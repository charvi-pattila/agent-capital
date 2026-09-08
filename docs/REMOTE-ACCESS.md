# Remote access over Tailscale (stable https:// URL)

## Why

Agent Capitol is served by the Flask backend on port 8888 and reached from an
iPhone. On a plain LAN that has two recurring problems:

- **The Mac's LAN IP drifts.** Every new DHCP lease or network change means a
  new `http://192.168.x.y:8888` URL, so Home Screen bookmarks go stale.
- **Campus / hotel Wi-Fi isolates clients.** Many managed networks block
  device-to-device traffic, so the phone cannot reach the Mac at all even when
  both are on the same SSID.

[Tailscale](https://tailscale.com) fixes both: each device gets a private
address that never changes and a MagicDNS name such as
`charvis-mac.tail1234.ts.net`, and the two devices reach each other through an
encrypted peer-to-peer tunnel (relayed if the network blocks direct traffic).
`tailscale cert` then issues a real Let's Encrypt certificate for that name, so
the phone gets genuine HTTPS with no self-signed-certificate warnings and Safari
treats the app as a secure origin.

Nothing is exposed to the public internet: only devices signed in to your
tailnet can see the Mac.

> Hosting on a separate Windows PC (WSL2) instead of this Mac? Use
> [`WINDOWS-SERVER.md`](./WINDOWS-SERVER.md) — there Tailscale runs on the Windows
> side and `tailscale serve` handles HTTPS, so the certificate script below is not needed.

## One-time setup on the Mac

1. **Install Tailscale.** `brew install --cask tailscale` (it asks for your
   macOS password because the installer package runs as root), or download
   from <https://tailscale.com/download/mac>.
2. **Open Tailscale.app and sign in.** Use the account (Google/GitHub/Apple/
   email) you will also use on the iPhone. Leave the app running; it starts at
   login by default.
3. **Admin console toggles** at <https://login.tailscale.com/admin/dns>:
   - **MagicDNS** -> Enable (gives the Mac its `*.ts.net` name)
   - **HTTPS Certificates** -> Enable (allows `tailscale cert`)
4. **Run the script** from the repo root:

   ```bash
   scripts/tailscale-https.sh
   ```

   It finds the CLI, reads the Mac's MagicDNS name, fetches the certificate
   into `~/.agent-capitol/certs/`, and writes `SSL_CERT=` / `SSL_KEY=` into
   `backend/.env`. If Tailscale is not installed or not signed in it stops and
   prints exactly what to do. Re-running it is always safe.

5. **Restart the server** so it picks up the cert (the backend reads
   `PORT`, `HOST`, `SSL_CERT`, `SSL_KEY` from its environment at startup):
   - **launchd service:** `backend/.env` is merged into the service's
     environment by `scripts/install-service.sh`, so run that once after the
     script, then `launchctl kickstart -k gui/$(id -u)/com.agent-capitol.server`
     for any later restart.
   - **Foreground:** stop it with Ctrl-C and start it again with the variables
     loaded, e.g. `set -a; source backend/.env; set +a; ./venv/bin/python backend/server.py`.

The script prints the final URL, e.g. `https://charvis-mac.tail1234.ts.net:8888`.

## On the iPhone

1. Install **Tailscale** from the App Store and sign in with the same account.
   Toggle the VPN on (it stays on; battery impact is negligible).
2. Open the `https://<mac-name>.tail1234.ts.net:8888` URL in Safari.
3. Share -> **Add to Home Screen**. Because the name never changes, this
   bookmark keeps working at home, on campus, and on cellular.

The old `http://<mac-name>.local:8888` LAN URL keeps working on your home
network; Tailscale adds a second route rather than replacing it. (Once the
server runs with a certificate it speaks HTTPS only, so use `https://` with
the `.local` name too, and expect a certificate-name warning there because the
cert is issued for the `.ts.net` name.)

## Certificate renewal

Let's Encrypt certificates issued by Tailscale are valid for ~90 days. Renew by
re-running the script and restarting the server:

```bash
scripts/tailscale-https.sh --renew
launchctl kickstart -k gui/$(id -u)/com.agent-capitol.server   # or restart the foreground server
```

The script prints the expiry date each time it runs. A cron/launchd job that
runs it monthly is a reasonable follow-up.

## Troubleshooting

| Symptom | Check |
|---|---|
| Page never loads from the phone | Tailscale on the phone is toggled on and both devices show as online at <https://login.tailscale.com/admin/machines>. On the Mac: `/Applications/Tailscale.app/Contents/MacOS/Tailscale status`. |
| Script says "not connected to a tailnet" | Open Tailscale.app and sign in; the menu-bar icon should be solid, not greyed out. |
| Script says it cannot read a MagicDNS name | MagicDNS is not enabled in the admin console, or the app has not refreshed yet; wait ~10 s and re-run. |
| `tailscale cert` fails | HTTPS Certificates is not enabled in the admin console, or the Mac has no internet (Let's Encrypt is contacted at issue time). |
| Page loads but the terminal mirror stays blank | The SSE stream (`/api/projects/<id>/terminal`) is being buffered or cut. Confirm you are hitting the Flask server directly (no proxy in front of it); it already sends `Cache-Control: no-cache` and `X-Accel-Buffering: no`. Verified locally: `curl -k -N https://127.0.0.1:8888/api/projects/<id>/terminal` streams `data:` lines over TLS. |
| Browser warns that the certificate expired | Run `scripts/tailscale-https.sh --renew` and restart the server. |
| Browser warns about the certificate name | You opened the `.local` or IP URL; the cert is only valid for the `.ts.net` name. Use the Tailscale URL. |
| Server does not start after adding SSL_* | Check `SSL_CERT`/`SSL_KEY` in `backend/.env` point at existing files (`ls -l ~/.agent-capitol/certs/`) and that the launchd env was re-merged with `scripts/install-service.sh`. |

## Fallback: ngrok (public tunnel)

`ngrok` is already installed (`/opt/homebrew/bin/ngrok`). `ngrok http 8888`
gives an https URL that works from anywhere without Tailscale on the phone, but:

- it exposes the app to the **public internet** (anyone with the URL can reach
  the login gate, and sessions run Claude Code with permissions skipped), so
  set a password with `backend/set_password.py` first and stop the tunnel when
  you are done;
- the URL **changes every run** on the free plan, so Home Screen bookmarks
  break.

Use it as an emergency fallback only; Tailscale is the intended path.
